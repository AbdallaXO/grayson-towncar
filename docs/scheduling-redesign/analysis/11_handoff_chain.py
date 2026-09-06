#!/usr/bin/env python
"""11 — Handoff empirics, the zone chain model, and the car-bound/labor-bound hour split.

THE QUESTIONS THIS SCRIPT EXISTS TO ANSWER
------------------------------------------
1. How often do two in-house drivers share one car on one day (a HANDOFF), and how much
   booked time separates the outgoing driver's last pickup from the incoming driver's
   first pickup?
2. Does the founder's chain model (drop -> wash -> fuel -> base -> next pickup,
   00 §A4.3a) explain the observed clear-to-pickup gaps, zone pair by zone pair — and
   when a gap undercuts the model, does it undercut even the skip-wash floor?
3. Hour by hour, is a farmed leg farmed because no BODY was available (labor-bound,
   a compatible fleet car sat free) or because no CAR was (car-bound)?

NO HARDCODED DATES. The current regime and the plateau before it are derived from the
daily leg series via _common.changepoints at run time.

Zone classification is the SHIPPED dispatching/analytics.categorize_location, exec'd
byte-identical from source without booting Django (the module only needs stubs for its
django imports; the function itself is pure). Drive-time estimates likewise come from
the shipped table in dispatching/scheduler.py, and the pickup buffers from
dispatching/pickup_policy.py. Nothing geographic is re-implemented here.

CSV out: 11_handoffs.csv (every measured handoff with gaps + zones),
         11_chain_matrix.csv (zone-pair clear-to-pickup lo/central/hi + skip-wash floor),
         11_hour_binder_daily.csv (per-date labor-bound vs car-bound farmed legs).
"""

import datetime as dt
import os
import re
import sys
import types
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

ASSUMPTIONS = (
    "Occupancy lead/tail minutes around booked pickup_time are the A3.5 fitted constants "
    "(00 §A3.5) — parameters, not dates. Arrival pickup_time IS the flight time (00 §A3.5).",
    "driver_type and is_active are CURRENT-STATE flags: every historical in-house/affiliate "
    "and active-fleet split below inherits that caveat.",
    "Driver->car mapping = drivers_drivervehicleassignment (DVA). In-house driver-days with "
    "no DVA row phantom-consume one otherwise-unrostered car of the driver's declared type "
    "(conservative: fewer free cars for the hour-binder).",
    "Every gap percentile states its pairing cap: pairs are same-vehicle same-day, and the "
    "capped variants drop gaps > 8 h (480 min).",
    "Founder chain components are labeled [founder-supplied] / [shipped-estimate] / "
    "[assumed]; the base->pickup composites are labeled per zone.",
    "The farm-out verdict is a GREEDY car match on whole occupancy intervals — a lower "
    "bound on labor-bound: if greedy finds a free compatible car, one existed.",
)

# --------------------------------------------------------------------------
# fitted / shipped parameters (allowed literals — each names its source)
# --------------------------------------------------------------------------

# A3.5 occupancy interval, minutes around booked pickup_time (source: 00 §A3.5, [measured])
OCC = {"ARRIVAL": (20.6, 75.5), "DEPARTURE": (36.3, 34.8), "OTHER": (39.8, 53.6)}

# Nested compatibility (dispatching/scheduler.py: car tier >= booked tier serves the leg).
TIER = {"towncar": 0, "mini_van": 1, "suv": 2, "van": 3, "Van(14 Pax)": 4}

PREMIUM = 70.99  # $/leg farm-out premium (source: 03_farmout, [measured])

PAIR_CAP_MIN = 480  # 8 h pairing cap, stated on every gap percentile (house convention)

# Founder chain components, (low, central, high) minutes (source: 00 §A4.3a).
CHAIN = {
    "mco_to_wash":  ((14.0, 15.5, 17.0), "[founder-supplied]"),
    "wash":         ((15.0, 17.5, 20.0), "[founder-supplied]"),
    "fuel":         ((5.0, 7.5, 10.0), "[assumed — founder gave no number]"),
    "wash_to_base": ((20.0, 20.0, 20.0), "[founder-supplied ~20, point estimate]"),
    "mco_to_base":  ((12.0, 12.0, 12.0), "[founder-supplied]"),
}

# base (6785 Narcoossee, SR-528/Narcoossee corridor ~12 min E of MCO) -> pickup zone,
# (low, central, high) minutes + provenance label per zone (source: 00 §A4.3a).
BASE_TO = {
    "MCO Terminal":        ((12, 12, 12), "[founder-supplied]"),
    "SFB Terminal":        ((55, 60, 65), "[shipped-estimate MCO<->SFB 60 +/- base offset]"),
    "Disney Resort":       ((30, 35, 40), "[founder-supplied ~40 high; shipped 30 low]"),
    "Universal Resort":    ((25, 32, 40), "[founder-supplied ~40 high; shipped 25 low]"),
    "Port Canaveral Area": ((45, 50, 55), "[assumed — base sits ON SR-528 E of MCO, < MCO's 55]"),
    "Airport Hotel":       ((12, 15, 18), "[shipped-estimate 12 + base offset]"),
    "Other Hotel":         ((25, 28, 32), "[shipped-estimate 25 + base offset]"),
    "Residential":         ((30, 33, 37), "[shipped-estimate 30 + base offset]"),
    "Other":               ((35, 38, 42), "[shipped-estimate DEFAULT 35 + base offset]"),
}

ZONES = list(BASE_TO)
AIRPORT_ZONES = {"MCO Terminal", "SFB Terminal"}


# --------------------------------------------------------------------------
# shipped code, loaded byte-identical without Django
# --------------------------------------------------------------------------

def load_shipped():
    """categorize_location + DRIVE_TIME_ESTIMATES + pickup buffers from the shipped source.

    dispatching/analytics.py imports django at module level, but categorize_location and
    everything it needs (is_airport_location, the keyword lists) are pure. Stub the django
    modules in sys.modules and exec the source up to the end of categorize_location's
    block, so the classifier used here is byte-identical to production rather than a
    re-implementation. Same technique for the scheduler drive-time table.
    """
    for n in ("django", "django.db", "django.db.models", "django.utils",
              "django.utils.timezone"):
        if n not in sys.modules:
            sys.modules[n] = types.ModuleType(n)
    m = sys.modules["django.db.models"]
    for a in ("Avg", "Count", "Q", "Sum", "F"):
        setattr(m, a, None)
    sys.modules["django.utils"].timezone = sys.modules["django.utils.timezone"]

    src = open(os.path.join(C.REPO_ROOT, "dispatching", "analytics.py"),
               encoding="utf-8").read()
    ns = {"__name__": "shipped_analytics"}
    end = src.index("\ndef ", src.index("def categorize_location"))
    exec(compile(src[:end], "dispatching/analytics.py[:categorize_location]", "exec"), ns)

    ssrc = open(os.path.join(C.REPO_ROOT, "dispatching", "scheduler.py"),
                encoding="utf-8").read()
    a, b = ssrc.index("DRIVE_TIME_ESTIMATES = {"), ssrc.index("DEFAULT_DRIVE_TIME = ")
    ns2 = {}
    exec(compile(ssrc[a:b], "dispatching/scheduler.py[DRIVE_TIME_ESTIMATES]", "exec"), ns2)
    default = int(re.search(r"^DEFAULT_DRIVE_TIME = (\d+)", ssrc, re.M).group(1))

    psrc = open(os.path.join(C.REPO_ROOT, "dispatching", "pickup_policy.py"),
                encoding="utf-8").read()
    grace = int(re.search(r"^ARRIVAL_MEET_GRACE_MIN = (\d+)", psrc, re.M).group(1))
    ready = int(re.search(r"^PAX_READY_MIN = (\d+)", psrc, re.M).group(1))
    return ns["categorize_location"], ns2["DRIVE_TIME_ESTIMATES"], default, grace, ready


CATEGORIZE, DRIVE, DRIVE_DEFAULT, ARRIVAL_MEET_GRACE_MIN, PAX_READY_MIN = load_shipped()


def shipped_drive(a, b):
    return DRIVE.get((a, b), DRIVE_DEFAULT)


# --------------------------------------------------------------------------
# the chain model (00 §A4.3a)
# --------------------------------------------------------------------------

def _t3(key):
    return CHAIN[key][0]


def drop_to_wash(z):
    """drop zone -> wash. Wash sits ~15 min from MCO; non-MCO drops route via the MCO
    corridor: shipped(Z -> MCO) + founder MCO->wash range. [composite]"""
    if z == "MCO Terminal":
        return _t3("mco_to_wash")
    base = shipped_drive(z, "MCO Terminal")
    lo, ce, hi = _t3("mco_to_wash")
    return (base + lo, base + ce, base + hi)


def car_ready(z):
    """minutes after the guest is dropped until the car is washed, fueled and at base."""
    dw = drop_to_wash(z)
    return tuple(dw[i] + _t3("wash")[i] + _t3("fuel")[i] + _t3("wash_to_base")[i]
                 for i in range(3))


def drop_to_base(z):
    """drop zone -> base DIRECT (no wash, no fuel). MCO->base is founder-supplied; other
    zones route via the MCO corridor: shipped(Z -> MCO) + 12. [composite]"""
    if z == "MCO Terminal":
        return _t3("mco_to_base")
    base = shipped_drive(z, "MCO Terminal")
    lo, ce, hi = _t3("mco_to_base")
    return (base + lo, base + ce, base + hi)


def buffer_for(pick_z):
    """Airport pickup: driver inside the terminal by gate+grace (pickup_policy — the
    IN-TERMINAL meet point, never 'curb'). Elsewhere: passenger-ready convention."""
    if pick_z in AIRPORT_ZONES:
        return (ARRIVAL_MEET_GRACE_MIN,) * 3
    return (PAX_READY_MIN,) * 3


def clear_to_pickup(drop_z, pick_z):
    """full chain: car_ready(drop) + base->pickup + pickup buffer, (lo, central, hi)."""
    cr, bt, bf = car_ready(drop_z), BASE_TO[pick_z][0], buffer_for(pick_z)
    return tuple(cr[i] + bt[i] + bf[i] for i in range(3))


def skip_wash_floor(drop_z, pick_z):
    """chain WITHOUT wash+fuel: drop->base direct + base->pickup + buffer, central."""
    return drop_to_base(drop_z)[1] + BASE_TO[pick_z][0][1] + buffer_for(pick_z)[1]


def direct_floor(drop_z, pick_z):
    """absolute floor: B takes the car straight off A's drop (no base at all)."""
    return shipped_drive(drop_z, pick_z) + buffer_for(pick_z)[1]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def D(s):
    return dt.date.fromisoformat(str(s)[:10])


def daterange(a, b):
    x = a
    while x <= b:
        yield x
        x += dt.timedelta(days=1)


def share(a, b):
    return (100.0 * a / b) if b else 0.0


def overlap(a0, a1, b0, b1):
    return a0 < b1 and a1 > b0


def main():
    con = C.connect()
    h = C.Horizon(con)
    C.preamble("11_handoff_chain.py",
               "handoff empirics, the zone chain model, and the car/labor hour split",
               h, ASSUMPTIONS)

    # regime + plateau derived, never assumed (same knobs the sibling scripts use)
    byday = C.legs_per_day(con, end=h.last_demand_day)
    segs = C.changepoints(byday, D(min(byday)), h.last_demand_day,
                          min_seg=28, min_effect=0.09)
    cur = segs[-1]
    plat = segs[-2] if len(segs) > 1 else segs[-1]
    A, B = cur[0], min(cur[1], h.last_actuals_day)
    PA, PB = plat[0], min(plat[1], h.last_actuals_day)
    ndays = (B - A).days + 1
    print(f"\ncurrent regime (derived): {A} .. {B}  ({ndays}d, {cur[3]:.1f} legs/day)")
    print(f"plateau  before (derived): {PA} .. {PB}  ({(PB-PA).days+1}d, {plat[3]:.1f} legs/day)")

    # ------------------------------------------------------------- shared data
    drv = {r["id"]: (r["driver_type"], (r["vehicle"] or "").strip().lower())
           for r in C.q(con, "SELECT id, driver_type, vehicle FROM drivers_driver")}
    vtype = {r["id"]: r["t"] for r in C.q(con, """
        SELECT fv.id, rv.vehicle_type t FROM drivers_fleetvehicle fv
        LEFT JOIN rates_vehicle rv ON rv.id = fv.vehicle_type_id""")}

    # DVA: one row per (date, driver) in practice; a duplicate would keep the last row,
    # matching every sibling prototype.
    dva_rows = C.q(con, "SELECT date, driver_id, vehicle_id "
                        "FROM drivers_drivervehicleassignment WHERE vehicle_id IS NOT NULL")
    DV = {(D(r["date"]), r["driver_id"]): r["vehicle_id"] for r in dva_rows}
    first_dva = min(D(r["date"]) for r in dva_rows)
    print(f"DVA coverage starts {first_dva} (plateau window is fully covered: "
          f"{first_dva <= PA})")

    # ============================================================ 1. shared vehicle-days
    C.hdr("1. SHARED VEHICLE-DAYS via DVA — 2+ in-house drivers on one car/date [measured]")

    def shared_days(a, b):
        per = defaultdict(set)  # (date, vehicle) -> {driver}
        for r in dva_rows:
            day = D(r["date"])
            if a <= day <= b:
                per[(day, r["vehicle_id"])].add(r["driver_id"])
        sh = {k: v for k, v in per.items() if len(v) >= 2}
        return per, sh

    for label, a, b in (("current regime", A, B), ("plateau", PA, PB)):
        per, sh = shared_days(a, b)
        nd = (b - a).days + 1
        hdates = {k[0] for k in sh}
        mx = max((len(v) for v in per.values()), default=0)
        print(f"  {label:15s} DVA vehicle-days {len(per):5d}   shared {len(sh):4d} "
              f"({share(len(sh), len(per)):.1f}%)   dates w/ handoff {len(hdates)}/{nd}   "
              f"max drivers on one car {mx}")
    _, SH = shared_days(A, B)
    mix = Counter(vtype.get(k[1]) for k in SH)
    print(f"  current-regime shared units by type: {dict(mix)}")

    # ============================================================ 2. handoff pairs + zones
    C.hdr("2. HANDOFF PAIRS — A's last pickup to B's first pickup, with zones [measured]")

    SQL_LEGS = C.live_legs_sql(
        "l.id, l.pickup_date d, l.pickup_time pt, l.pickup_location pl, "
        "l.dropoff_location dl",
        extra="AND l.driver_id = ? AND l.pickup_date = ? AND l.pickup_time IS NOT NULL")
    taps = C.first_taps(con)

    def build_pairs(sh):
        out, skipped = [], 0
        for (day, veh), drivers in sorted(sh.items()):
            legs = {}
            for did in drivers:
                rows = [r for r in C.q(con, SQL_LEGS, (did, str(day)))
                        if C.booked_dtm(r["d"], r["pt"])]
                rows.sort(key=lambda r: C.booked_dtm(r["d"], r["pt"]))
                legs[did] = rows
            with_legs = [d_ for d_ in drivers if legs[d_]]
            if len(with_legs) < 2:
                skipped += 1
                continue
            # outgoing = driver whose FIRST pickup is earlier
            a, b = sorted(with_legs,
                          key=lambda d_: C.booked_dtm(legs[d_][0]["d"], legs[d_][0]["pt"]))[:2]
            la, fb = legs[a][-1], legs[b][0]
            ta = C.booked_dtm(la["d"], la["pt"])
            tb = C.booked_dtm(fb["d"], fb["pt"])
            gap = (tb - ta).total_seconds() / 60.0
            if gap <= 0:
                skipped += 1
                continue
            k_out = C.trip_kind(la["pl"], la["dl"])
            tail = OCC[k_out][1]
            obs_clear = gap - tail
            drop_z = CATEGORIZE(la["dl"])
            pick_z = CATEGORIZE(fb["pl"])
            lo, ce, hi = clear_to_pickup(drop_z, pick_z)
            # [measured] taps variant: A completed -> B picked-up, when both taps exist
            tclr = None
            ca = taps.get(la["id"], {}).get("completed")
            pb = taps.get(fb["id"], {}).get("picked-up")
            if ca and pb:
                tclr = round((pb - ca).total_seconds() / 60.0, 1)
            out.append(dict(day=day, veh=veh, a=a, b=b, gap=gap, kind=k_out, tail=tail,
                            clear=obs_clear, dz=drop_z, pz=pick_z, lo=lo, ce=ce, hi=hi,
                            delta=obs_clear - ce, tclr=tclr))
        return out, skipped

    pairs, skipped = build_pairs(SH)
    print(f"shared vehicle-days {len(SH)}; with legs both sides and a positive gap: "
          f"{len(pairs)} pairs (skipped {skipped})")

    print(f"\n  date        veh  A->B    p2p_gap  A_kind     obs_clear  "
          f"drop_zone -> pickup_zone           model lo/ce/hi   delta")
    for p in pairs:
        print(f"  {p['day']}  {p['veh']:3d}  {p['a']:3d}>{p['b']:<3d} {p['gap']:7.0f}  "
              f"{p['kind']:9s} {p['clear']:9.1f}  {p['dz'][:14]:14s} -> {p['pz'][:14]:14s}  "
              f"{p['lo']:5.0f}/{p['ce']:5.0f}/{p['hi']:5.0f}  {p['delta']:+7.1f}")

    gaps_all = [p["gap"] for p in pairs]
    gaps_cap = [g for g in gaps_all if g <= PAIR_CAP_MIN]
    clr_all = [p["clear"] for p in pairs]
    clr_cap = [p["clear"] for p in pairs if p["gap"] <= PAIR_CAP_MIN]
    tclrs = [p["tclr"] for p in pairs if p["tclr"] is not None
             and 0 < p["tclr"] <= PAIR_CAP_MIN]
    print(f"\n  pairing: same vehicle, same date; cap = {PAIR_CAP_MIN} min (8 h) — "
          f"{len(gaps_all) - len(gaps_cap)} of {len(gaps_all)} pairs exceed it")
    print(C.fmt_describe("  p2p gap, ALL positive pairs", gaps_all))
    print(f"    min {min(gaps_all):.0f}  max {max(gaps_all):.0f}")
    print(C.fmt_describe("  p2p gap, 8h-capped", gaps_cap))
    print(C.fmt_describe("  obs clear (gap - A3.5 tail), all", clr_all))
    print(C.fmt_describe("  obs clear, 8h-capped", clr_cap))
    print(C.fmt_describe("  taps clear (completed->picked-up)", tclrs))

    # plateau, for context (same construction)
    _, SHP = shared_days(PA, PB)
    ppairs, _ = build_pairs(SHP)
    pg = [p["gap"] for p in ppairs if p["gap"] <= PAIR_CAP_MIN]
    print("\n  plateau context (same construction, 8h cap):")
    print(C.fmt_describe("  plateau p2p gap", pg))

    # ============================================================ 3. the chain model
    C.hdr("3. FOUNDER CHAIN MODEL (00 §A4.3a) — config, matrix, observed deltas")
    print("chain components (lo/central/hi min):")
    for k, (t3, label) in CHAIN.items():
        print(f"  {k:14s} {t3[0]:5.1f}/{t3[1]:5.1f}/{t3[2]:5.1f}  {label}")
    print("\nbase -> pickup zone (lo/central/hi min):")
    for z, (t3, label) in BASE_TO.items():
        print(f"  {z:20s} {t3[0]:3d}/{t3[1]:3d}/{t3[2]:3d}  {label}")
    print(f"\npickup buffer: airport {ARRIVAL_MEET_GRACE_MIN} min (gate+grace, in-terminal "
          f"meet point), elsewhere {PAX_READY_MIN} min  [shipped-estimate: pickup_policy]")

    print("\ncar-ready-at-base by drop zone (min after guest dropped):")
    for z in ZONES:
        dw, cr = drop_to_wash(z), car_ready(z)
        print(f"  {z:20s} to-wash {dw[0]:5.1f}/{dw[1]:5.1f}/{dw[2]:5.1f}   "
              f"car-ready {cr[0]:6.1f}/{cr[1]:6.1f}/{cr[2]:6.1f}")

    common = ["MCO Terminal", "SFB Terminal", "Disney Resort", "Universal Resort",
              "Port Canaveral Area", "Other Hotel", "Other"]
    print("\nclear-to-pickup CENTRAL matrix (full 9x9 in the CSV):")
    print("  drop \\ pickup       " + "  ".join(f"{p[:9]:>9s}" for p in common))
    for dz in common:
        row = [clear_to_pickup(dz, pz)[1] for pz in common]
        print(f"  {dz:19s} " + "  ".join(f"{v:9.1f}" for v in row))

    print("\nobserved vs central, per zone pair (current-regime pairs):")
    agg = defaultdict(list)
    for p in pairs:
        agg[(p["dz"], p["pz"])].append(p)
    print(f"  {'drop_zone':17s} {'pickup_zone':17s} {'n':>3s} {'mean_obs':>9s} "
          f"{'central':>8s} {'mean_d':>8s} {'<lo':>4s} {'<skipwash':>9s} {'<direct':>8s}")
    for k in sorted(agg):
        v = agg[k]
        mo = sum(x["clear"] for x in v) / len(v)
        md = sum(x["delta"] for x in v) / len(v)
        ce = v[0]["ce"]
        swf = skip_wash_floor(*k)
        dfl = direct_floor(*k)
        print(f"  {k[0][:17]:17s} {k[1][:17]:17s} {len(v):3d} {mo:9.1f} {ce:8.1f} "
              f"{md:+8.1f} {sum(1 for x in v if x['clear'] < x['lo']):4d} "
              f"{sum(1 for x in v if x['clear'] < swf):9d} "
              f"{sum(1 for x in v if x['clear'] < dfl):8d}")
    deltas = [p["delta"] for p in pairs]
    below_lo = sum(1 for p in pairs if p["clear"] < p["lo"])
    below_sw = sum(1 for p in pairs if p["clear"] < skip_wash_floor(p["dz"], p["pz"]))
    below_di = sum(1 for p in pairs if p["clear"] < direct_floor(p["dz"], p["pz"]))
    print(C.fmt_describe("\n  delta obs_clear - central", deltas))
    print(f"  below central {sum(1 for d_ in deltas if d_ < 0)}/{len(deltas)}   "
          f"below LOW {below_lo}/{len(deltas)}   below SKIP-WASH floor {below_sw}/"
          f"{len(deltas)}   below DIRECT floor {below_di}/{len(deltas)}")
    print("""
READ: a handoff below the CENTRAL chain but above the SKIP-WASH floor is consistent with
handing the car over unwashed; below the DIRECT floor the car cannot even have driven
drop -> pickup in the booked gap, i.e. the meet happened elsewhere or the schedule ate
the buffer. [modeled on founder-supplied + shipped components]""")

    C.write_csv("11_handoffs.csv",
                ["date", "vehicle_id", "vehicle_type", "driver_out", "driver_in",
                 "p2p_gap_min", "out_kind", "tail_model_min", "obs_clear_min",
                 "drop_zone", "pickup_zone", "model_lo", "model_central", "model_hi",
                 "delta_obs_minus_central", "skip_wash_floor", "direct_floor",
                 "taps_clear_min"],
                [[p["day"], p["veh"], vtype.get(p["veh"]), p["a"], p["b"],
                  round(p["gap"], 1), p["kind"], p["tail"], round(p["clear"], 1),
                  p["dz"], p["pz"], round(p["lo"], 1), round(p["ce"], 1),
                  round(p["hi"], 1), round(p["delta"], 1),
                  round(skip_wash_floor(p["dz"], p["pz"]), 1),
                  round(direct_floor(p["dz"], p["pz"]), 1), p["tclr"]] for p in pairs])
    C.write_csv("11_chain_matrix.csv",
                ["drop_zone", "pickup_zone", "clear_lo", "clear_central", "clear_hi",
                 "skip_wash_floor_central", "direct_floor_central"],
                [[dz, pz, *(round(x, 1) for x in clear_to_pickup(dz, pz)),
                  round(skip_wash_floor(dz, pz), 1), round(direct_floor(dz, pz), 1)]
                 for dz in ZONES for pz in ZONES])

    # ============================================================ 4. the hour-binder
    C.hdr("4. HOUR-BINDER — farmed legs vs free compatible cars, labor- vs car-bound")

    LOAD_A = A - dt.timedelta(days=1)  # previous day feeds occupancy spillover into hour 0

    FLEET_SQL = """SELECT fv.id, rv.vehicle_type, fv.is_active,
                          fv.out_of_service_from, fv.out_of_service_until
                   FROM drivers_fleetvehicle fv
                   LEFT JOIN rates_vehicle rv ON rv.id = fv.vehicle_type_id"""
    fleet = {}
    for r in C.q(con, FLEET_SQL):
        fleet[r["id"]] = {
            "tier": TIER.get(r["vehicle_type"], 2), "type": r["vehicle_type"],
            "active": r["is_active"],
            "oos": (D(r["out_of_service_from"]) if r["out_of_service_from"] else None,
                    D(r["out_of_service_until"]) if r["out_of_service_until"] else None)}

    def available(day):
        out = set()
        for vid, v in fleet.items():
            if not v["active"]:            # is_active is CURRENT-STATE (header caveat)
                continue
            f, u = v["oos"]
            if f and day >= f and (u is None or day <= u):
                continue
            out.add(vid)
        return out

    avail_by_day = {day: available(day) for day in daterange(A, B)}
    print(f"active fleet by date: " +
          (", ".join(f"{day}={len(s)}" for day, s in sorted(avail_by_day.items())
                     if len(s) != len(available(A))) or f"{len(available(A))} cars every day"))

    rostered = defaultdict(set)
    for (day, _did), veh in DV.items():
        rostered[day].add(veh)

    LEG_SQL = f"""SELECT l.id, l.pickup_date, l.pickup_time, l.pickup_location,
                         l.dropoff_location, l.driver_id, rv.vehicle_type AS booked_class
                  FROM reservations_leg l
                  JOIN reservations_reservation r ON r.id = l.reservation_id
                  LEFT JOIN rates_vehicle rv ON rv.id = r.vehicle_id
                  WHERE {C.LIVE_LEG} AND {C.SANE_DATES}
                    AND l.pickup_date BETWEEN ? AND ?"""
    inhouse = []   # (driver, day, occ0, occ1)
    farmed = []    # farmed leg dicts, current regime only
    n_null_class = 0
    for r in C.q(con, LEG_SQL, (str(LOAD_A), str(B))):
        if r["driver_id"] is None:
            continue
        day = D(r["pickup_date"])
        bp = C.booked_dtm(r["pickup_date"], r["pickup_time"])
        if bp is None:
            continue
        kind = C.trip_kind(r["pickup_location"], r["dropoff_location"])
        lead, tail = OCC[kind]
        o0, o1 = bp - dt.timedelta(minutes=lead), bp + dt.timedelta(minutes=tail)
        if drv.get(r["driver_id"], ("unknown", ""))[0] == "affiliate":
            if day < A:
                continue   # spillover day only feeds in-house car usage
            bc = r["booked_class"]
            if bc is None:
                n_null_class += 1
                bc = "towncar"   # conservative: lowest tier, easiest to serve
            farmed.append({"id": r["id"], "day": day, "pick": bp, "kind": kind,
                           "o0": o0, "o1": o1, "tier": TIER.get(bc, 2), "cls": bc})
        else:
            inhouse.append((r["driver_id"], day, o0, o1))
    ih_dd = {(day, did) for did, day, _, _ in inhouse if day >= A}
    print(f"loaded: {len(inhouse)} in-house leg-occupancies ({LOAD_A}..{B}), "
          f"{len(farmed)} farmed legs ({A}..{B}), null booked class={n_null_class}")
    print(f"in-house driver-days in regime: {len(ih_dd)}   "
          f"farmed/day: {len(farmed)/ndays:.2f}")

    # phantom cars for in-house driver-days with no DVA row (conservative: they consume
    # an otherwise-unrostered car of the driver's declared type)
    phantom, n_fail = {}, 0
    for day, did in sorted(ih_dd | {(d_, i) for i, d_, _, _ in inhouse if d_ < A}):
        if (day, did) in DV:
            continue
        want = drv.get(did, ("", ""))[1]
        want_tier = TIER.get({"suv": "suv", "van": "van", "towncar": "towncar",
                              "mini van": "mini_van", "minivan": "mini_van"}.get(want, "suv"), 2)
        pool = (available(day) - rostered[day]
                - {c for (dd, _), c in phantom.items() if dd == day})
        pick = None
        for pref in [want_tier] + [t for t in (2, 3, 4, 1, 0) if t != want_tier]:
            cand = sorted(c for c in pool if fleet[c]["tier"] == pref)
            if cand:
                pick = cand[0]
                break
        if pick is None:
            n_fail += 1
        else:
            phantom[(day, did)] = pick
    print(f"no-DVA in-house driver-days phantom-mapped to an unrostered car: "
          f"{len(phantom)} (unmappable: {n_fail})")

    def car_of(day, did):
        return DV.get((day, did)) or phantom.get((day, did))

    # hour grid of in-house car usage
    cell_car = defaultdict(set)   # (day, h) -> {car}
    day_cars = defaultdict(set)   # day -> {car used that day}
    car_iv = defaultdict(list)    # car -> [(o0, o1)] in-house busy intervals
    for did, day, o0, o1 in inhouse:
        car = car_of(day, did)
        if car is None:
            continue
        car_iv[car].append((o0, o1))
        t = o0.replace(minute=0, second=0, microsecond=0)
        while t < o1:
            if A <= t.date() <= B:
                cell_car[(t.date(), t.hour)].add(car)
                day_cars[t.date()].add(car)
            t += dt.timedelta(hours=1)
    for car in car_iv:
        car_iv[car].sort()

    # per-hour leg-hour verdicts: within each (date, hour), greedily match the farmed
    # legs touching the hour against cars free that hour (idle-now or parked, tier-ok)
    lh = defaultdict(lambda: [0, 0])   # (day, h) -> [car_bound, labor_bound] leg-hours
    hour_prof = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])  # h -> [cars, idle, park, farm]
    for day in daterange(A, B):
        av = avail_by_day[day]
        for hh in range(24):
            used = cell_car.get((day, hh), set())
            idle_now = (day_cars[day] - used) & av
            parked = av - day_cars[day]
            free_t = sorted(fleet[c]["tier"] for c in (idle_now | parked))
            h0 = dt.datetime.combine(day, dt.time(hh))
            legs_h = [f for f in farmed if f["day"] >= A
                      and overlap(f["o0"], f["o1"], h0, h0 + dt.timedelta(hours=1))]
            taken = set()
            for f in sorted(legs_h, key=lambda x: -x["tier"]):
                pick = next((i for i, t in enumerate(free_t)
                             if t >= f["tier"] and i not in taken), None)
                if pick is None:
                    lh[(day, hh)][0] += 1
                else:
                    taken.add(pick)
                    lh[(day, hh)][1] += 1
            hp = hour_prof[hh]
            hp[0] += len(used)
            hp[1] += len(idle_now)
            hp[2] += len(parked)
            hp[3] += len(legs_h)

    print(f"\nhour-of-day profile, {ndays}-day means [measured]:")
    print(f"  {'h':>2} {'cars':>6} {'idle':>6} {'park':>6} {'farm/d':>7} "
          f"{'CB-lh':>6} {'LB-lh':>6} {'CB%':>6}")
    for hh in range(24):
        cb = sum(lh[(day, hh)][0] for day in daterange(A, B))
        lb = sum(lh[(day, hh)][1] for day in daterange(A, B))
        hp = hour_prof[hh]
        print(f"  {hh:2d} {hp[0]/ndays:6.2f} {hp[1]/ndays:6.2f} {hp[2]/ndays:6.2f} "
              f"{hp[3]/ndays:7.2f} {cb:6d} {lb:6d} {share(cb, cb+lb):5.1f}%")

    # strict per-leg verdict: greedy car match on the WHOLE occupancy interval
    # [modeled: greedy = lower bound on labor-bound]
    placed = defaultdict(list)
    strict = {}
    for f in sorted(farmed, key=lambda x: x["pick"]):
        cand = []
        for c in avail_by_day[f["day"]]:
            if fleet[c]["tier"] < f["tier"]:
                continue
            if any(overlap(f["o0"], f["o1"], a0, a1) for a0, a1 in car_iv.get(c, ())):
                continue
            if any(overlap(f["o0"], f["o1"], a0, a1) for a0, a1 in placed[c]):
                continue
            cand.append(c)
        if cand:
            c = min(cand, key=lambda c_: (fleet[c_]["tier"], c_))
            placed[c].append((f["o0"], f["o1"]))
            strict[f["id"]] = ("L", c)
        else:
            strict[f["id"]] = ("C", None)

    sL = [f for f in farmed if strict[f["id"]][0] == "L"]
    sC = [f for f in farmed if strict[f["id"]][0] == "C"]
    print(f"\nSTRICT per-leg verdict (whole interval, greedy) "
          f"[modeled: lower bound on labor-bound]:")
    print(f"  farmed legs {len(farmed)} = LABOR-bound {len(sL)} "
          f"({share(len(sL), len(farmed)):.1f}%) + CAR-bound {len(sC)} "
          f"({share(len(sC), len(farmed)):.1f}%)")
    print(f"  per day: LABOR {len(sL)/ndays:.2f}  CAR {len(sC)/ndays:.2f}   "
          f"$ at ${PREMIUM}/leg premium: labor pool ${len(sL)/ndays*PREMIUM*365:,.0f}/yr, "
          f"car pool ${len(sC)/ndays*PREMIUM*365:,.0f}/yr")
    for label, keyf in (("required class", lambda f: f["cls"]),
                        ("trip kind", lambda f: f["kind"])):
        agg2 = defaultdict(lambda: [0, 0])
        for f in farmed:
            agg2[keyf(f)][0 if strict[f["id"]][0] == "C" else 1] += 1
        print(f"  by {label}: " + "  ".join(
            f"{k}: CAR {c}/LAB {l} ({share(c, c+l):.0f}% car)"
            for k, (c, l) in sorted(agg2.items())))

    # the 08:00-12:59 morning block
    morn = [f for f in farmed if 8 <= f["pick"].hour <= 12]
    mornL = sum(1 for f in morn if strict[f["id"]][0] == "L")
    print(f"\n08:00-12:59 pickup share of ALL farmed legs: {len(morn)}/{len(farmed)} "
          f"({share(len(morn), len(farmed)):.1f}%)   of LABOR-bound: {mornL}/{len(sL)} "
          f"({share(mornL, len(sL)):.1f}%)   of CAR-bound: {len(morn)-mornL}/{len(sC)} "
          f"({share(len(morn)-mornL, len(sC)):.1f}%)")
    print("  (the ~88% figure in the redesign notes is the 08:00-12:59 share of the "
          "CAP+MINT REPLAY'S residual farm-out — a different, post-replay population "
          "measured by the replay script, not reproducible from raw farm-outs here)")

    daily = []
    for day in daterange(A, B):
        fl = [f for f in farmed if f["day"] == day]
        l = sum(1 for f in fl if strict[f["id"]][0] == "L")
        cb_lh = sum(lh[(day, hh)][0] for hh in range(24))
        lb_lh = sum(lh[(day, hh)][1] for hh in range(24))
        daily.append([day, day.strftime("%a"), len(fl), l, len(fl) - l, cb_lh, lb_lh,
                      round(share(cb_lh, cb_lh + lb_lh), 1)])
    C.write_csv("11_hour_binder_daily.csv",
                ["date", "dow", "farmed_legs", "labor_bound", "car_bound",
                 "car_bound_leghours", "labor_bound_leghours", "cb_leghour_pct"], daily)

    # ------------------------------------------------------------- sanity gates
    C.hdr("5. SANITY GATES")
    print(f"  farmed legs {ndays}d: {len(farmed)} ({len(farmed)/ndays:.2f}/day)")
    print(f"  in-house driver-days: {len(ih_dd)}")
    print(f"  max distinct in-house cars in any hour: "
          f"{max(len(v) for v in cell_car.values())} "
          f"(fleet table {len(fleet)} rows, active on {A}: {len(available(A))})")
    print(f"  handoff pairs vs shared vehicle-days: {len(pairs)} of {len(SH)}")

    print("\nWrote: out/11_handoffs.csv, out/11_chain_matrix.csv, "
          "out/11_hour_binder_daily.csv")


if __name__ == "__main__":
    main()
