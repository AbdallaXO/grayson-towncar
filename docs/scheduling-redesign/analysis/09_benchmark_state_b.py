#!/usr/bin/env python
"""09 — The operated-board benchmark (state B): the yardstick every optimizer result
is measured against.

State B is what the board ACTUALLY looked like when each day ran: `reservations_leg`
read directly — driver_id, pickup, locations as they stand — never replayed from the
event stream (00 §A4/§A6: the audit log's assignment rows are 30.8% phantoms; nothing
here touches them). Any proposed schedule that cannot beat these numbers on the
founder's four criteria is not an improvement:

  1. IN-HOUSE COVERAGE   legs kept in-house vs farmed to affiliates vs unassigned
  2. CONFLICTS           consecutive same-driver pairs scored with the SHIPPED
                         feasibility constants (hard: slack < 0; tight: 0..TURN_TIGHT)
  3. HOURS               duty span from the fitted occupancy interval, judged against
                         the two SHIPPED caps (13.5h soft target / 15h hard)
  4. DISTRIBUTION        legs per driver — CV and max-min spread per date

Plus the physical layer: distinct drivers per day, vehicles via DVA, and shared
vehicle-days (two drivers on one car in one day) — the cross-check figure for the
handoff analysis (11).

Both the CURRENT REGIME and the PRIOR PLATEAU are reported, each derived at run time
from changepoints on the daily leg series — no hardcoded analysis date anywhere.

SHIPPED CONSTANTS ARE LOADED FROM SOURCE, NOT COPIED: feasibility_guards.py and
pickup_policy.py are imported by file path (they are pure); the drive-time table and
categorize_location are exec'd out of scheduler.py / analytics.py with django stubbed
in sys.modules — no django.setup(), no ORM, no writes.
"""

import datetime as dt
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

# Fitted occupancy interval, minutes around booked pickup_time (source: 00 §A3.5).
# These are FITTED PARAMETERS (P50 lead/tail per trip kind), not dates — the one
# family of literals this script is allowed to carry.
OCC = {"ARRIVAL": (20.6, 75.5), "DEPARTURE": (36.3, 34.8), "OTHER": (39.8, 53.6)}

# Every turnaround/gap figure in this package states its pairing cap (00 §A13):
# consecutive same-driver pickups more than 8h apart are separate duty blocks, not turns.
PAIR_CAP_MIN = 8 * 60

ASSUMPTIONS = (
    "State B = reservations_leg read directly (driver_id as it stands). The board's "
    "final form, never replayed from historicalleg/auditlog events (00 §A4.6).",
    "driver_type / is_active are CURRENT-STATE flags: a driver reclassified since a "
    "date worked still splits by today's label. The prior-plateau split carries this "
    "caveat hardest; the current regime is least exposed.",
    "Duty span = [min(pickup - lead), max(pickup + tail)] over a driver's day, using "
    "the A3.5 occupancy interval; trip kind for the SPAN family via _common.trip_kind "
    "(how 00 established 433/112/61).",
    "Conflict scoring keys the SHIPPED drive-time table on SHIPPED location categories "
    "(dispatching/analytics.categorize_location), so the conflict family classifies "
    "kind via those categories — the same split ab-delta used. The two classifiers "
    "agree on all but a handful of free-text edge cases.",
    f"Consecutive-pair scoring caps pairing at {PAIR_CAP_MIN // 60}h pickup-to-pickup "
    "(00 §A13).",
    "Vehicles via drivers_drivervehicleassignment (date, driver) -> vehicle; a leg "
    "whose driver has no DVA row that day contributes no vehicle-day.",
)


# --------------------------------------------------------------------------
# shipped policy constants — exec'd from dispatching source, never copied
# --------------------------------------------------------------------------

def load_shipped():
    """Load production policy values byte-identical from the shipped modules.

    feasibility_guards.py imports only datetime — loaded by path directly.
    pickup_policy.py / analytics.py import django at module level but the pieces we
    need are pure, so django is stubbed in sys.modules first (the sanctioned
    stub-exec technique; no django.setup(), nothing can touch a database).
    scheduler.py is exec'd only across its DRIVE_TIME_ESTIMATES/DEFAULT_DRIVE_TIME
    block (module-level constants, no imports needed).
    """
    import importlib.util
    import types

    def stub(name):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
        return sys.modules[name]

    stub("django")
    dj_db = stub("django.db")
    dj_models = stub("django.db.models")
    for a in ("Avg", "Count", "Q", "Sum", "F"):
        setattr(dj_models, a, None)
    dj_db.models = dj_models
    dj_utils = stub("django.utils")
    dj_tz = stub("django.utils.timezone")
    dj_utils.timezone = dj_tz

    def by_path(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    disp = os.path.join(C.REPO_ROOT, "dispatching")
    fg = by_path("fg_shipped", os.path.join(disp, "feasibility_guards.py"))
    pp = by_path("pp_shipped", os.path.join(disp, "pickup_policy.py"))

    src = open(os.path.join(disp, "scheduler.py"), encoding="utf-8").read()
    a = src.index("DRIVE_TIME_ESTIMATES = {")
    b = src.index("\n", src.index("DEFAULT_DRIVE_TIME ="))
    ns = {}
    exec(compile(src[a:b], "scheduler.py[drive-table]", "exec"), ns)  # noqa: S102

    src = open(os.path.join(disp, "analytics.py"), encoding="utf-8").read()
    end = src.index("\ndef ", src.index("def categorize_location"))
    ns2 = {"__name__": "analytics_slice"}
    exec(compile(src[:end], "analytics.py[categorize_location]", "exec"), ns2)  # noqa: S102

    return fg, pp, ns["DRIVE_TIME_ESTIMATES"], ns["DEFAULT_DRIVE_TIME"], ns2["categorize_location"]


# --------------------------------------------------------------------------
# per-date metrics — the founder's four criteria on one operated day
# --------------------------------------------------------------------------

def day_metrics(rows, dtype, dva_date, fg, pp, DRIVE, DEFAULT_DRIVE):
    """rows: leg dicts for ONE date. Returns the scorecard row for that date."""
    m = {"legs": len(rows)}
    m["inhouse"] = sum(1 for r in rows if dtype.get(r["did"]) == "inhouse")
    m["farm"] = sum(1 for r in rows if r["did"] is not None
                    and dtype.get(r["did"]) == "affiliate")
    m["other_typed"] = sum(1 for r in rows if r["did"] is not None
                           and dtype.get(r["did"]) not in ("inhouse", "affiliate"))
    m["unassigned"] = sum(1 for r in rows if r["did"] is None)
    m["coverage"] = 100.0 * m["inhouse"] / m["legs"] if m["legs"] else None

    # in-house boards (legs carrying a parseable booked pickup instant)
    byd = defaultdict(list)
    for r in rows:
        if r["pick"] is not None and dtype.get(r["did"]) == "inhouse":
            byd[r["did"]].append(r)
    for v in byd.values():
        v.sort(key=lambda r: (r["pick"], r["id"]))

    # criterion 3 — hours: full occupancy envelope per driver-day (span family
    # classifies kind via _common.trip_kind, matching how 00 established the caps)
    spans = []
    for ls in byd.values():
        s = min(l["pick"] - dt.timedelta(minutes=OCC[l["ks"]][0]) for l in ls)
        e = max(l["pick"] + dt.timedelta(minutes=OCC[l["ks"]][1]) for l in ls)
        spans.append((e - s).total_seconds() / 3600.0)
    m["driver_days"] = len(byd)
    m["over_soft"] = sum(1 for s in spans if s > fg.SPAN_SOFT_EFFECTIVE_HOURS)
    m["over_hard"] = sum(1 for s in spans if s > fg.SPAN_HARD_HOURS_DEFAULT)
    m["max_span"] = max(spans) if spans else None
    m["spans"] = spans

    # criterion 2 — conflicts on consecutive same-driver pairs, SHIPPED constants.
    # slack = available - required_turnaround - min_turn_buffer, with the shipped
    # same-terminal-arrival exemption (deplaning grace instead of a drive, no buffer).
    # NOTE: the ab-delta prototype scored the driver IN FORCE AT EACH PICKUP INSTANT
    # (event-stream reconstruction); a handful of legs per regime are reassigned
    # after pickup, so its hard/tight run ~0.2/day apart from this direct read of
    # the final board. This script reads the final board by design (assumption A1).
    pairs = hard = tight = overlap = 0
    for ls in byd.values():
        for a, b in zip(ls[:-1], ls[1:]):
            gap = (b["pick"] - a["pick"]).total_seconds() / 60.0
            if gap > PAIR_CAP_MIN:            # 8h cap — separate duty blocks
                continue
            pairs += 1
            clear = a["pick"] + dt.timedelta(minutes=OCC[a["kc"]][1])
            avail = (b["pick"] - clear).total_seconds() / 60.0
            nxt_arr = b["kc"] == "ARRIVAL"
            same_term = (a["dcat"] == b["pcat"]) and (b["pcat"] in fg.AIRPORT_TERMINALS)
            req = (-fg.DEPLANING_GRACE_MIN if (nxt_arr and same_term)
                   else DRIVE.get((a["dcat"], b["pcat"]), DEFAULT_DRIVE)) + fg.SAFETY_PAD_MIN
            buf = (0 if (fg.BUFFER_EXEMPT_SAME_TERMINAL_ARRIVAL and nxt_arr and same_term)
                   else fg.MIN_TURN_BUFFER_DEFAULT)
            slack = avail - req - buf
            if avail < 0:
                overlap += 1
            if slack < 0:
                hard += 1
            elif slack < pp.TURN_TIGHT_SLACK_MIN:
                tight += 1
    m["pairs"], m["hard"], m["tight"], m["overlap"] = pairs, hard, tight, overlap

    # criterion 4 — distribution/fairness of legs across the drivers who worked
    counts = [len(v) for v in byd.values()]
    if counts:
        mean = sum(counts) / len(counts)
        m["legs_per_driver"] = mean
        m["maxmin"] = max(counts) - min(counts)
        var = sum((c - mean) ** 2 for c in counts) / len(counts)
        m["cv"] = (var ** 0.5) / mean if mean else None
    else:
        m["legs_per_driver"] = m["maxmin"] = m["cv"] = None

    # physical layer — vehicles via DVA; shared = >=2 distinct drivers on one car.
    # NOTE: this count reaches vehicles only through LEG-CARRYING drivers, so it sits
    # ~2 below 11_handoff_chain.py's DVA-based count in the same window: a shared
    # roster day where one driver ran zero live legs (verified: 2026-07-27 veh 7,
    # 2026-08-08 veh 6) is invisible here. 11's DVA figure owns the handoff family;
    # this one is the legs-side cross-check.
    veh_drivers = defaultdict(set)
    for r in rows:
        if r["did"] is not None:
            v = dva_date.get(r["did"])
            if v is not None:
                veh_drivers[v].add(r["did"])
    m["veh_used"] = len(veh_drivers)
    m["veh_shared"] = sum(1 for s in veh_drivers.values() if len(s) >= 2)
    m["veh_max_drv"] = max((len(s) for s in veh_drivers.values()), default=0)
    return m


# --------------------------------------------------------------------------
# window aggregation + report
# --------------------------------------------------------------------------

def report_window(label, days, fg, pp, caveat=""):
    """days: [(date, metrics)] for one window. Prints the benchmark block, returns
    the summary dict."""
    nd = len(days)
    tot = lambda k: sum(m[k] for _, m in days)                     # noqa: E731
    per = lambda k: tot(k) / float(nd)                             # noqa: E731
    mean_of = lambda k: (lambda v: sum(v) / len(v) if v else None)(
        [m[k] for _, m in days if m[k] is not None])               # noqa: E731

    C.hdr(f"{label}  ({days[0][0]} .. {days[-1][0]}, {nd} days) [measured]")
    if caveat:
        print(f"CAVEAT: {caveat}")

    legs, ih, fm, un = tot("legs"), tot("inhouse"), tot("farm"), tot("unassigned")
    print(f"\n1. IN-HOUSE COVERAGE")
    print(f"   legs                 {legs:6d}   ({legs / nd:7.2f}/day)")
    print(f"   in-house             {ih:6d}   ({ih / nd:7.2f}/day)  share {100.0 * ih / legs:.1f}%")
    print(f"   farmed (affiliate)   {fm:6d}   ({fm / nd:7.2f}/day)  share {100.0 * fm / legs:.1f}%")
    print(f"   unassigned           {un:6d}   ({un / nd:7.2f}/day)")
    if tot("other_typed"):
        print(f"   assigned, driver_type neither inhouse nor affiliate: {tot('other_typed')}")

    pairs, hard, tight, ovl = tot("pairs"), tot("hard"), tot("tight"), tot("overlap")
    print(f"\n2. CONFLICTS on consecutive same-driver pairs "
          f"(pairing cap {PAIR_CAP_MIN // 60}h pickup-to-pickup)")
    print(f"   pairs scored         {pairs:6d}   ({pairs / nd:7.2f}/day)")
    print(f"   hard  (slack < 0)    {hard:6d}   ({hard / nd:7.2f}/day)  "
          f"{100.0 * hard / pairs if pairs else 0:.1f}% of pairs")
    print(f"   tight (0..{pp.TURN_TIGHT_SLACK_MIN}min)     {tight:6d}   ({tight / nd:7.2f}/day)  "
          f"{100.0 * tight / pairs if pairs else 0:.1f}% of pairs")
    print(f"   occupancy overlaps   {ovl:6d}   ({ovl / nd:7.2f}/day)")

    dd = tot("driver_days")
    spans = [s for _, m in days for s in m["spans"]]
    print(f"\n3. HOURS (duty span from the A3.5 occupancy envelope)")
    print(f"   in-house driver-days {dd:6d}   ({dd / nd:7.2f} distinct drivers/day)")
    print(f"   > {fg.SPAN_SOFT_EFFECTIVE_HOURS:.1f}h (soft target) {tot('over_soft'):6d}   "
          f"({per('over_soft'):7.2f}/day)  {100.0 * tot('over_soft') / dd:.1f}% of driver-days")
    print(f"   > {fg.SPAN_HARD_HOURS_DEFAULT:.1f}h (hard cap)    {tot('over_hard'):6d}   "
          f"({per('over_hard'):7.2f}/day)  {100.0 * tot('over_hard') / dd:.1f}% of driver-days")
    print(f"   max span             {max(spans):6.2f} h")
    print("   " + C.fmt_describe("span hours (driver-days)", spans))

    print(f"\n4. DISTRIBUTION / FAIRNESS (per-date, across drivers who worked)")
    print(f"   legs per driver      mean of daily means {mean_of('legs_per_driver'):6.2f}   "
          f"(overall {ih / dd if dd else 0:.2f})")
    print(f"   CV of legs/driver    mean {mean_of('cv'):6.3f}")
    print(f"   max-min spread       mean {mean_of('maxmin'):6.2f} legs")

    vd, vs = tot("veh_used"), tot("veh_shared")
    dates_shared = sum(1 for _, m in days if m["veh_shared"] > 0)
    print(f"\n5. VEHICLES via DVA (cross-check figure for the handoff analysis, 11)")
    print(f"   vehicle-days         {vd:6d}   ({vd / nd:7.2f} distinct cars/day)")
    print(f"   SHARED vehicle-days  {vs:6d}   ({100.0 * vs / vd if vd else 0:.1f}% of "
          f"vehicle-days; {dates_shared} of {nd} dates)")
    print(f"   max drivers on one car in one day: {max(m['veh_max_drv'] for _, m in days)}")

    return {"window": label.split(" ")[0], "start": days[0][0], "end": days[-1][0],
            "days": nd, "legs": legs, "legs_day": round(legs / nd, 2),
            "inhouse": ih, "inhouse_day": round(ih / nd, 2),
            "inhouse_share_pct": round(100.0 * ih / legs, 1),
            "farmed": fm, "farmed_day": round(fm / nd, 2), "unassigned": un,
            "pairs_day": round(pairs / nd, 2), "hard_day": round(hard / nd, 2),
            "tight_day": round(tight / nd, 2), "overlap_day": round(ovl / nd, 2),
            "driver_days": dd, "drivers_day": round(dd / nd, 2),
            "over_soft": tot("over_soft"), "over_soft_day": round(per("over_soft"), 2),
            "over_hard": tot("over_hard"), "over_hard_day": round(per("over_hard"), 2),
            "max_span_h": round(max(spans), 2),
            "legs_per_driver": round(mean_of("legs_per_driver"), 2),
            "cv_mean": round(mean_of("cv"), 3), "maxmin_mean": round(mean_of("maxmin"), 2),
            "vehicle_days": vd, "shared_vehicle_days": vs,
            "dates_with_share": dates_shared}


def main():
    t0 = time.time()
    con = C.connect()
    h = C.Horizon(con)
    C.preamble("09_benchmark_state_b.py",
               "the operated-board benchmark (state B) — the founder's four criteria",
               h, ASSUMPTIONS)

    fg, pp, DRIVE, DEFAULT_DRIVE, catloc = load_shipped()
    print("\nshipped constants (loaded from dispatching source, not copied):")
    print(f"  feasibility_guards: MIN_TURN_BUFFER_DEFAULT={fg.MIN_TURN_BUFFER_DEFAULT}  "
          f"SAFETY_PAD_MIN={fg.SAFETY_PAD_MIN}  DEPLANING_GRACE_MIN={fg.DEPLANING_GRACE_MIN}")
    print(f"                      BUFFER_EXEMPT_SAME_TERMINAL_ARRIVAL="
          f"{fg.BUFFER_EXEMPT_SAME_TERMINAL_ARRIVAL}  "
          f"SPAN_SOFT_EFFECTIVE_HOURS={fg.SPAN_SOFT_EFFECTIVE_HOURS}  "
          f"SPAN_HARD_HOURS_DEFAULT={fg.SPAN_HARD_HOURS_DEFAULT}")
    print(f"  pickup_policy     : TURN_TIGHT_SLACK_MIN={pp.TURN_TIGHT_SLACK_MIN}")
    print(f"  scheduler         : drive-time table {len(DRIVE)} routes, "
          f"default {DEFAULT_DRIVE} min")

    # ---------------------------------------------------------------- windows
    byday = C.legs_per_day(con, end=h.last_demand_day)
    scan_from = dt.date.fromisoformat(min(byday))
    segs = C.changepoints(byday, scan_from, h.last_demand_day, min_seg=28, min_effect=0.09)
    cur_a = segs[-1][0]
    cur_b = min(segs[-1][1], h.last_actuals_day)
    pri = segs[-2] if len(segs) > 1 else None
    print(f"\nderived regimes (changepoints on the daily leg series, min_seg=28):")
    for s in segs:
        print(f"  {s[0]} .. {s[1]}  ({s[2]:3d}d, {s[3]:6.1f} legs/day)"
              + ("   <- CURRENT" if s is segs[-1] else ""))
    print(f"current regime clipped to actuals: {cur_a} .. {cur_b} "
          f"({(cur_b - cur_a).days + 1} days)")
    if pri is None:
        raise SystemExit("no prior plateau found — nothing to compare against")
    pri_a, pri_b = pri[0], min(pri[1], h.last_actuals_day)
    print(f"prior plateau                    : {pri_a} .. {pri_b} "
          f"({(pri_b - pri_a).days + 1} days)")

    # ---------------------------------------------------------------- load
    dtype = {r["id"]: (r["driver_type"] or "").lower()
             for r in C.q(con, "SELECT id, driver_type FROM drivers_driver")}

    rows = C.q(con, C.live_legs_sql(
        "l.id, l.pickup_date d, l.pickup_time pt, l.pickup_location pl, "
        "l.dropoff_location dl, l.driver_id did",
        "AND l.pickup_date BETWEEN ? AND ?"), (str(pri_a), str(cur_b)))
    by_date = defaultdict(list)
    n_null_pick = 0
    for r in rows:
        pick = C.booked_dtm(r["d"], r["pt"])
        if pick is None:
            n_null_pick += 1
        # SPAN family kind via _common.trip_kind (00-established); CONFLICT family
        # kind + categories via the SHIPPED categorize_location (drive-table keys).
        pcat, dcat = catloc(r["pl"] or ""), catloc(r["dl"] or "")
        kc = ("ARRIVAL" if pcat in fg.AIRPORT_TERMINALS
              else "DEPARTURE" if dcat in fg.AIRPORT_TERMINALS else "OTHER")
        by_date[r["d"]].append({"id": r["id"], "did": r["did"], "pick": pick,
                                "ks": C.trip_kind(r["pl"], r["dl"]), "kc": kc,
                                "pcat": pcat, "dcat": dcat})
    print(f"\nlegs loaded {pri_a}..{cur_b}: {len(rows)} "
          f"(A6-filtered; {n_null_pick} without a parseable pickup instant — counted "
          f"in coverage, absent from spans/conflicts)")

    dva = defaultdict(dict)          # date iso -> driver -> vehicle
    for r in C.q(con, "SELECT date, driver_id, vehicle_id "
                      "FROM drivers_drivervehicleassignment "
                      "WHERE date BETWEEN ? AND ? AND vehicle_id IS NOT NULL",
                 (str(pri_a), str(cur_b))):
        dva[str(r["date"])[:10]][r["driver_id"]] = r["vehicle_id"]

    # ---------------------------------------------------------------- score
    def window_days(a, b):
        out = []
        d = a
        while d <= b:
            iso = d.isoformat()
            if by_date.get(iso):
                out.append((iso, day_metrics(by_date[iso], dtype, dva.get(iso, {}),
                                             fg, pp, DRIVE, DEFAULT_DRIVE)))
            d += dt.timedelta(days=1)
        return out

    cur_days = window_days(cur_a, cur_b)
    pri_days = window_days(pri_a, pri_b)

    sum_cur = report_window("CURRENT REGIME — the benchmark", cur_days, fg, pp)
    sum_pri = report_window("PRIOR PLATEAU — for scale, not for optimizing against",
                            pri_days, fg, pp,
                            caveat="driver_type is a CURRENT-STATE flag; the further "
                                   "back the split runs, the softer the in-house/"
                                   "farmed boundary (assumption A2).")

    # ---------------------------------------------------------------- per-date tail
    C.hdr("PER-DATE SCORECARD — current regime (full table in the CSV)")
    print(f"{'date':11s}{'dow':4s}{'legs':>5s}{'inh':>5s}{'farm':>5s}{'un':>3s}"
          f"{'cov%':>6s}{'drv':>4s}{'pairs':>6s}{'hard':>5s}{'tight':>6s}"
          f"{'>soft':>6s}{'>hard':>6s}{'maxspan':>8s}{'veh':>4s}{'shared':>7s}")
    for iso, m in cur_days:
        d = dt.date.fromisoformat(iso)
        print(f"{iso} {d.strftime('%a'):4s}{m['legs']:5d}{m['inhouse']:5d}{m['farm']:5d}"
              f"{m['unassigned']:3d}{m['coverage']:6.1f}{m['driver_days']:4d}"
              f"{m['pairs']:6d}{m['hard']:5d}{m['tight']:6d}{m['over_soft']:6d}"
              f"{m['over_hard']:6d}{m['max_span']:8.2f}{m['veh_used']:4d}"
              f"{m['veh_shared']:7d}")

    # ---------------------------------------------------------------- CSVs
    cols = ["window", "date", "dow", "legs", "inhouse", "farmed", "unassigned",
            "coverage_pct", "drivers", "legs_per_driver", "cv", "maxmin",
            "pairs", "hard", "tight", "overlap", "dd_over_soft", "dd_over_hard",
            "max_span_h", "vehicles_used", "shared_vehicle_days"]
    out_rows = []
    for wlabel, days in (("current", cur_days), ("plateau", pri_days)):
        for iso, m in days:
            d = dt.date.fromisoformat(iso)
            out_rows.append([
                wlabel, iso, d.strftime("%a"), m["legs"], m["inhouse"], m["farm"],
                m["unassigned"], round(m["coverage"], 1) if m["coverage"] is not None else "",
                m["driver_days"],
                round(m["legs_per_driver"], 2) if m["legs_per_driver"] is not None else "",
                round(m["cv"], 3) if m["cv"] is not None else "",
                m["maxmin"] if m["maxmin"] is not None else "",
                m["pairs"], m["hard"], m["tight"], m["overlap"],
                m["over_soft"], m["over_hard"],
                round(m["max_span"], 2) if m["max_span"] is not None else "",
                m["veh_used"], m["veh_shared"]])
    p1 = C.write_csv("09_state_b_scorecard.csv", cols, out_rows)

    scols = list(sum_cur.keys())
    p2 = C.write_csv("09_state_b_summary.csv", scols,
                     [[sum_cur[k] for k in scols], [sum_pri[k] for k in scols]])

    print(f"\nWrote: {os.path.relpath(p1, C.REPO_ROOT)}, {os.path.relpath(p2, C.REPO_ROOT)}")
    print(f"runtime: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
