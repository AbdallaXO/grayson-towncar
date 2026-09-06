#!/usr/bin/env python
"""16 — The surrogate-noise test (Ticket C; 05 §4). Ship/no-ship for Pass A.

THE QUESTION THIS SCRIPT ANSWERS
--------------------------------
Pass A (the roster ladder) proposes removing drivers one at a time and judging
each removal by re-running the shipped pipeline. That is only a real signal if
CHANGING THE ROSTER SIZE moves the score more than swapping WHICH same-size
drivers work does — otherwise the descent is reading engine placement jitter
and would hand the dispatcher fake-precise "leave these two off" advice.

04 §4 makes this a gate, not a nice-to-have: **if between-roster-size score
differences do not exceed within-size jitter, the ladder is cut from v1** and
the builder optimizes pairing and splits at the dispatcher-chosen headcount.
The verdict lives in this script's output (and CSV), not in a person's memory.

METHOD (05 §4, exactly)
  * One P50-demand and one P90-demand date from the current regime — derived
    from the data (regime via changepoints, demand percentiles over its daily
    leg counts), no date literals.
  * R0 = the dispatcher's real roster for the date, exactly as the view's
    bare-payload path derives it (DVA-eligible in-house, active, saved
    availability) — the same derivation the Gate-4 baseline uses.
  * For each roster size k in {|R0|, |R0|-1, ..., |R0|-4}: draw M = 8 DISTINCT
    rosters of size k from R0. Deterministic, index-strided, never random:
    the j = |R0|-k removed drivers are the lexicographic j-combinations of the
    sorted R0 index list at ranks floor(m * C(n,j) / M), m = 0..M-1 — evenly
    strided through combination space, re-runnable bit for bit. (At k = |R0|
    only one roster exists; its within-spread is trivially 0 and the strict
    rule below covers that pair through the k-1 side.)
  * Run the shipped pipeline cold on each roster (dva_rows filtered to the
    subset — the Build-3a hypothetical-roster mechanism) and record the A1
    score parts: driver_days, farm_outs, farm_cost, quality.

  within(k)  = P90 - P10 of the score across the same-size rosters
  between(k) = |median(k) - median(k-1)|

  SHIP THE LADDER iff between(k) > within(k) for every adjacent pair, on BOTH
  days (05 §4's literal rule, within taken at the larger k). The stricter
  variant between(k) > max(within(k), within(k-1)) is also printed — it is
  the conservative reading and the one that actually bounds the pair's noise
  floor when the larger size is the degenerate single-roster top rung.

THE SCORE SCALARS
  A1's score is lexicographic (driver_days, farm_outs, farm_cost, quality) —
  not summable into one number without inventing weights 05 refuses to
  invent. At fixed k, driver_days is (near-)constant, so the comparable tail
  is the farm-and-quality part. The PRIMARY scalar for the verdict is
  **farm_outs** — the exact quantity Pass A's acceptance rule constrains
  (cand.farm_outs <= baseline.farm_outs + epsilon); farm_cost and the quality
  term are computed and tested identically as supporting evidence. If the
  scalars disagree, that disagreement is printed and belongs in the founder
  report at the Ticket-C stop.

  quality = 1.0*span_pressure + 1.0*fairness + 2.0*handoff_amber + 0.5*idle_gaps
  (the A1 default weights, [assumed]; defined exactly as 05 §2 A1's table).

Cost: 2 dates x (1 + 4x8) = 66 cold pipeline evaluations. Offline, never in
production.

USAGE
  python docs/scheduling-redesign/analysis/16_surrogate_noise.py
"""
import datetime as dt
import os
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from math import comb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

sys.path.insert(0, C.REPO_ROOT)

M_DRAWS = 8
LADDER_DEPTH = 4          # k runs |R0| .. |R0|-4
W_SPAN, W_FAIR, W_HANDOFF, W_GAPS = 1.0, 1.0, 2.0, 0.5   # A1 defaults [assumed]

ASSUMPTIONS = (
    "R0 is the view's bare-payload roster derivation (DVA-eligible in-house, "
    "active, saved availability) — the same R0 Pass A would start from.",
    "Subset runs thread dva_rows filtered to the subset, so vehicle caps, "
    "tier map and the co-driver partner map all see the hypothetical roster "
    "(the Build-3a mechanism, gated byte-identical by analysis/14).",
    "The primary verdict scalar is farm_outs — the quantity Pass A's "
    "acceptance rule constrains; farm_cost and quality are computed and "
    "tested identically as supporting evidence.",
    "quality's handoff term bands each shared unit held by two subset "
    "drivers: gap = incoming first booked pickup - (outgoing last booked "
    "pickup + P50 occupancy tail), the same arithmetic handoff_band's "
    "docstring calibrates against; AMBER counts, RED would too (walls are "
    "not scored here — this is the noise probe, not the gate).",
    "Each date is cleared cold ONCE and reused across its 33 runs; the "
    "pipeline is read-only over legs (proven by the 14 determinism capture), "
    "so runs cannot contaminate each other.",
)


def django_on_copy():
    tmp = os.environ.get("BUILD3_NOISE_TMP") or tempfile.mkdtemp(prefix="build3_noise_")
    os.makedirs(tmp, exist_ok=True)
    db_copy = os.path.join(tmp, "db_copy.sqlite3")
    print(f"\ncopying snapshot -> {db_copy} (the snapshot itself stays read-only)")
    shutil.copyfile(C.DB_PATH, db_copy)
    with open(os.path.join(tmp, "gate_settings.py"), "w", encoding="utf-8") as f:
        f.write(
            "from business.settings import *\n"
            f"DATABASES['default']['NAME'] = {db_copy!r}\n"
            "ROUTE_DISTANCE_INLINE_RESOLVER = False\n"
        )
    os.environ["RUN_SCHEDULERS_IN_WEB"] = "0"
    os.environ["ENABLE_DEBUG_TOOLBAR"] = "0"
    os.environ.setdefault("USE_LIVE_DISTANCE", "0")
    os.environ["DJANGO_SETTINGS_MODULE"] = "gate_settings"
    sys.path.insert(0, tmp)
    import django
    django.setup()
    from django.core.management import call_command
    from django.db import connection
    print("migrating the copy to the current schema ...")
    orig_check = connection.check_constraints
    connection.check_constraints = lambda *a, **k: None
    try:
        call_command("migrate", verbosity=0, interactive=False)
    finally:
        connection.check_constraints = orig_check
    return tmp


# --------------------------------------------------------------------------
# deterministic roster draws — index-strided combination unranking
# --------------------------------------------------------------------------

def unrank_comb(n, j, rank):
    """The lexicographic j-combination of range(n) at ``rank``."""
    out, x = [], 0
    for pos in range(j):
        remaining = j - pos - 1
        while True:
            c = comb(n - x - 1, remaining)
            if rank < c:
                out.append(x)
                x += 1
                break
            rank -= c
            x += 1
    return out


def strided_removals(n, j, m_draws):
    """Up to ``m_draws`` DISTINCT j-element removal index sets, evenly strided
    through the C(n,j) lexicographic combination space. j=0 -> one empty set."""
    if j == 0:
        return [[]]
    total = comb(n, j)
    ranks = sorted({(m * total) // m_draws for m in range(min(m_draws, total))})
    return [unrank_comb(n, j, r) for r in ranks]


# --------------------------------------------------------------------------
# roster + one evaluation
# --------------------------------------------------------------------------

def day_roster(target_date):
    """The view's bare-payload roster derivation (same as 17's baseline)."""
    from drivers.models import Driver, DriverVehicleAssignment
    eligible = set(DriverVehicleAssignment.objects.filter(
        date=target_date, driver__driver_type="inhouse")
        .values_list("driver_id", flat=True))
    drivers = list(Driver.objects.filter(
        driver_type="inhouse", is_active=True, id__in=eligible)
        .select_related("profile")
        .prefetch_related("weekly_schedule", "date_overrides"))
    driver_hours, flexible = {}, set()
    for d in drivers:
        is_avail, sh, eh, _pref, flex = d.get_availability_for_date(target_date)
        if is_avail:
            driver_hours[d.id] = (sh, eh)
            if flex:
                flexible.add(d.id)
    drivers = [d for d in drivers if d.id in driver_hours]
    driver_max_hours = {}
    for d in drivers:
        fa = d.get_full_availability(target_date)
        if fa.get("max_hours"):
            driver_max_hours.setdefault(d.id, float(fa["max_hours"]))
    return drivers, driver_hours, flexible, driver_max_hours


def load_day_legs(target_date):
    from reservations.models import Leg
    return list(
        Leg.objects.filter(pickup_date=target_date)
        .exclude(reservation__status="cancelled")
        .exclude(status="cancelled")
        .select_related("driver", "driver__profile", "reservation",
                        "reservation__vehicle", "vehicle", "flight_information")
        .prefetch_related("legstop_set", "legflight_set")
    )


def evaluate(target_date, legs, roster, driver_hours, flexible, driver_max_hours,
             dva_rows, run_min_buffer, driver_min_buffers, cfg):
    """One cold pipeline run on a (possibly hypothetical) roster. Returns the
    A1 score parts re-derived from the result."""
    from dispatching.assignment_pipeline import (
        PipelineLocks, PipelineWindows, run_assignment_pipeline)
    from dispatching.scheduler import build_driver_schedules, effective_span_hours
    from dispatching import feasibility_guards as fg
    from dispatching.fleet_intel import affiliate_base_cost
    from dispatching.standby_mints import FARMOUT_PREMIUM_PER_LEG
    from dispatching.analytics import categorize_location
    from dispatching.handoff_chain import (
        handoff_band, occupancy_kind, OCCUPANCY_LEAD_TAIL_P50)
    from dispatching.car_share import holders_by_unit
    from datetime import datetime, timedelta

    ids = {d.id for d in roster}
    sub_hours = {i: h for i, h in driver_hours.items() if i in ids}
    sub_flex = {i for i in flexible if i in ids}
    sub_max = {i: v for i, v in driver_max_hours.items() if i in ids}
    sub_dva = [r for r in dva_rows if r.driver_id in ids]
    sub_buf = {i: v for i, v in driver_min_buffers.items() if i in ids}

    t0 = time.time()
    res = run_assignment_pipeline(
        legs, roster, target_date,
        PipelineWindows(driver_hours=sub_hours, flexible_drivers=sub_flex,
                        driver_max_hours=sub_max,
                        run_min_buffer=run_min_buffer,
                        driver_min_buffers=sub_buf),
        PipelineLocks(), dva_rows=sub_dva)
    wall = time.time() - t0

    assignments = res.assignments
    farm_outs = len(res.unassigned) - len(assignments)
    farmed = {l.id for l in res.unassigned} - set(assignments.keys())
    legs_by_id = res.legs_by_id
    cost, fallback = 0.0, 0
    for lid in farmed:
        c = affiliate_base_cost(legs_by_id[lid])
        if c is None:
            cost += FARMOUT_PREMIUM_PER_LEG
            fallback += 1
        else:
            cost += float(c)

    # board for the quality terms — stamp, build, restore
    drivers_by_id = res.drivers_by_id
    stamped = []
    for lid, did in assignments.items():
        lg = legs_by_id.get(lid)
        if lg is not None and did in drivers_by_id:
            lg.driver = drivers_by_id[did]
            lg.driver_id = did
            stamped.append(lg)
    board = build_driver_schedules(legs, roster, target_date, dva_rows=sub_dva)
    for lg in stamped:
        lg.driver = None
        lg.driver_id = None

    span_pressure = 0.0
    idle_gap_h = 0.0
    counts = []
    first_last = {}    # driver -> (first booked pickup dt, last booked pickup dt,
                       #            last leg kind) for the handoff term
    gap_thresh = float(cfg.idle_gap_threshold or 120)
    for did, sched in board.items():
        slots = sorted(sched.slots, key=lambda s: (s.pickup_time, s.leg_id))
        if not slots:
            continue
        counts.append(len(slots))
        _raw, eff = effective_span_hours(slots, target_date)
        span_pressure += max(0.0, eff - fg.SPAN_SOFT_EFFECTIVE_HOURS)
        for a, b in zip(slots, slots[1:]):
            gap_min = (datetime.combine(target_date, b.pickup_time)
                       - a.estimated_end_time).total_seconds() / 60.0
            if gap_min > gap_thresh:
                idle_gap_h += gap_min / 60.0
        first_last[did] = (slots[0], slots[-1])

    if counts:
        mean = sum(counts) / len(counts)
        fairness = (sum((c - mean) ** 2 for c in counts) / len(counts)) ** 0.5
    else:
        fairness = 0.0

    # handoff term: shared units among the subset roster, banded
    amber = red = 0
    units = holders_by_unit((r.driver_id, r.vehicle_id) for r in sub_dva)
    for vid, holders in units.items():
        active = [h for h in holders if h in first_last]
        if len(active) < 2:
            continue
        a, b = sorted(active, key=lambda h: first_last[h][0].pickup_time)[:2]
        out_slot = first_last[a][1]
        in_slot = first_last[b][0]
        drop_zone = categorize_location(out_slot.dropoff_location)
        pick_zone = categorize_location(in_slot.pickup_location)
        kind = occupancy_kind(categorize_location(out_slot.pickup_location), drop_zone)
        tail = OCCUPANCY_LEAD_TAIL_P50[kind][1]
        clear = (datetime.combine(target_date, out_slot.pickup_time)
                 + timedelta(minutes=tail))
        gap_min = (datetime.combine(target_date, in_slot.pickup_time)
                   - clear).total_seconds() / 60.0
        band = handoff_band(
            drop_zone, pick_zone, gap_min,
            incoming_is_arrival=(pick_zone in fg.AIRPORT_TERMINALS),
            green_pct=cfg.handoff_gap_green_pct,
            amber_floor_pct=cfg.handoff_gap_amber_floor_pct)["band"]
        if band == "amber":
            amber += 1
        elif band == "red":
            red += 1

    quality = (W_SPAN * span_pressure + W_FAIR * fairness
               + W_HANDOFF * amber + W_GAPS * idle_gap_h)
    return {
        "driver_days": len(set(assignments.values())),
        "farm_outs": farm_outs,
        "farm_cost": round(cost, 2),
        "cost_fallback_legs": fallback,
        "quality": round(quality, 3),
        "span_pressure": round(span_pressure, 2),
        "fairness": round(fairness, 3),
        "handoff_amber": amber,
        "handoff_red": red,
        "idle_gap_h": round(idle_gap_h, 2),
        "wall_s": round(wall, 1),
    }


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    t0 = time.time()
    con = C.connect()
    h = C.Horizon(con)
    C.preamble("16_surrogate_noise.py",
               "Ticket C: is the roster ladder a real signal or engine jitter?",
               h, ASSUMPTIONS)

    byday = C.legs_per_day(con)
    scan_from = dt.date.fromisoformat(min(byday))
    segs = C.changepoints(byday, scan_from, h.today, min_seg=28, min_effect=0.08)
    cur_a = segs[-1][0]
    cur_b = min(segs[-1][1], h.last_actuals_day)
    days = []
    d = cur_a
    while d <= cur_b:
        days.append((d, byday.get(d.isoformat(), 0)))
        d += dt.timedelta(days=1)
    counts = [n for _, n in days]
    p50v, p90v = C.pct(counts, 50), C.pct(counts, 90)

    def closest(target, exclude=()):
        return min((abs(n - target), dd) for dd, n in days if dd not in exclude)[1]

    d50 = closest(p50v)
    d90 = closest(p90v, exclude={d50})
    print(f"\nregime {cur_a}..{cur_b}; demand P50={p50v:.0f} legs -> {d50} "
          f"({byday.get(d50.isoformat())} legs), P90={p90v:.0f} legs -> {d90} "
          f"({byday.get(d90.isoformat())} legs)")
    con.close()

    django_on_copy()
    from drivers.models import DriverVehicleAssignment
    from reservations.models import Leg
    from dispatching.models import SchedulerSettings
    from dispatching.scheduler import (
        preload_timing_cache, resolve_run_min_buffer, load_driver_min_buffers)
    preload_timing_cache()
    cfg = SchedulerSettings.get_settings()
    run_min_buffer = resolve_run_min_buffer(None)

    rows_csv = []
    stats = {}      # (date_iso, k) -> {scalar: [values]}

    for day in (d50, d90):
        iso = day.isoformat()
        n_cleared = (Leg.objects.filter(pickup_date=day, driver__isnull=False)
                     .update(driver=None))
        drivers, driver_hours, flexible, driver_max_hours = day_roster(day)
        dva_rows = list(DriverVehicleAssignment.objects.filter(date=day)
                        .select_related("vehicle", "vehicle__vehicle_type"))
        legs = load_day_legs(day)
        driver_min_buffers = load_driver_min_buffers([d_.id for d_ in drivers])
        r0_ids = sorted(d_.id for d_ in drivers)
        by_id = {d_.id: d_ for d_ in drivers}
        n = len(r0_ids)
        print(f"\n{iso}: {len(legs)} legs cleared cold ({n_cleared} were assigned); "
              f"|R0| = {n} ({', '.join(str(i) for i in r0_ids)})")
        if n - LADDER_DEPTH < 2:
            raise SystemExit(f"roster too small on {iso} for a {LADDER_DEPTH}-rung ladder")

        for j in range(0, LADDER_DEPTH + 1):
            k = n - j
            for m, removal in enumerate(strided_removals(n, j, M_DRAWS)):
                removed = [r0_ids[i] for i in removal]
                roster = [by_id[i] for i in r0_ids if i not in removed]
                sc = evaluate(day, legs, roster, driver_hours, flexible,
                              driver_max_hours, dva_rows, run_min_buffer,
                              driver_min_buffers, cfg)
                key = (iso, k)
                stats.setdefault(key, defaultdict(list))
                for s in ("farm_outs", "farm_cost", "quality", "driver_days"):
                    stats[key][s].append(sc[s])
                rows_csv.append([iso, k, m, ";".join(map(str, removed)),
                                 sc["driver_days"], sc["farm_outs"],
                                 sc["farm_cost"], sc["quality"],
                                 sc["span_pressure"], sc["fairness"],
                                 sc["handoff_amber"], sc["handoff_red"],
                                 sc["idle_gap_h"], sc["cost_fallback_legs"],
                                 sc["wall_s"]])
                print(f"  k={k:2d} draw {m}: -{removed if removed else '[]'} "
                      f"farm={sc['farm_outs']:3d} cost=${sc['farm_cost']:8,.0f} "
                      f"qual={sc['quality']:7.2f} dd={sc['driver_days']:2d} "
                      f"({sc['wall_s']}s)")

    p = C.write_csv("16_surrogate_noise.csv",
                    ["date", "k", "draw", "removed_driver_ids", "driver_days",
                     "farm_outs", "farm_cost", "quality", "span_pressure",
                     "fairness", "handoff_amber", "handoff_red", "idle_gap_h",
                     "cost_fallback_legs", "wall_s"], rows_csv)
    print(f"\nWrote: {os.path.relpath(p, C.REPO_ROOT)}")

    # ---- the test ----
    SCALARS = ("farm_outs", "farm_cost", "quality")
    verdict_literal, verdict_strict = {}, {}
    for scalar in SCALARS:
        C.hdr(f"WITHIN vs BETWEEN — {scalar}  [measured]")
        all_ok_lit = all_ok_str = True
        for day in (d50, d90):
            iso = day.isoformat()
            ks = sorted({k for (i, k) in stats if i == iso}, reverse=True)
            print(f"\n{iso}:")
            print(f"  {'k':>3s} {'n':>2s} {'median':>10s} {'P10':>10s} {'P90':>10s} "
                  f"{'within':>10s} {'between':>10s} {'lit':>4s} {'strict':>7s}")
            within = {}
            med = {}
            for k in ks:
                v = stats[(iso, k)][scalar]
                med[k] = C.pct(v, 50)
                within[k] = (C.pct(v, 90) - C.pct(v, 10)) if len(v) > 1 else 0.0
            for idx, k in enumerate(ks):
                if idx == 0:
                    print(f"  {k:3d} {len(stats[(iso, k)][scalar]):2d} "
                          f"{med[k]:10.2f} {C.pct(stats[(iso, k)][scalar], 10):10.2f} "
                          f"{C.pct(stats[(iso, k)][scalar], 90):10.2f} "
                          f"{within[k]:10.2f} {'—':>10s} {'—':>4s} {'—':>7s}")
                    continue
                kp = ks[idx - 1]          # the larger size (k+1 rung)
                between = abs(med[kp] - med[k])
                lit = between > within[kp]
                strict = between > max(within[kp], within[k])
                all_ok_lit &= lit
                all_ok_str &= strict
                print(f"  {k:3d} {len(stats[(iso, k)][scalar]):2d} "
                      f"{med[k]:10.2f} {C.pct(stats[(iso, k)][scalar], 10):10.2f} "
                      f"{C.pct(stats[(iso, k)][scalar], 90):10.2f} "
                      f"{within[k]:10.2f} {between:10.2f} "
                      f"{'Y' if lit else 'N':>4s} {'Y' if strict else 'N':>7s}")
        verdict_literal[scalar] = all_ok_lit
        verdict_strict[scalar] = all_ok_str

    C.hdr("VERDICT — Ticket C (ship/no-ship for the Pass-A roster ladder)")
    print("rule: SHIP iff between(k) > within(k) for every adjacent pair on "
          "BOTH days (05 §4).\n")
    for scalar in SCALARS:
        print(f"  {scalar:10s}: literal {'SHIP' if verdict_literal[scalar] else 'CUT'}"
              f"   strict {'SHIP' if verdict_strict[scalar] else 'CUT'}")
    primary = "farm_outs"
    ship = verdict_literal[primary]
    print(f"\nPRIMARY (farm_outs, the Pass-A constraint variable), literal rule:"
          f" {'SHIP THE LADDER' if ship else 'CUT THE LADDER'}")
    if verdict_literal[primary] != verdict_strict[primary]:
        print("NOTE: literal and strict readings DISAGREE on the primary scalar "
              "— founder decides which reading governs (Ticket-C stop).")
    if any(verdict_literal[s] != verdict_literal[primary] for s in SCALARS):
        print("NOTE: the scalars disagree under the literal rule — see tables; "
              "founder report required at the Ticket-C stop either way.")
    print(f"\nruntime: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
