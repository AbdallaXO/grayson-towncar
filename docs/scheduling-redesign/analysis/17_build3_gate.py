#!/usr/bin/env python
"""17 — Build 3b Gate 4: the acceptance harness (05 §7, Ticket F).

THE CLAIM THIS SCRIPT TESTS
---------------------------
On 10 replayed dates, at ``opt_epsilon_farmouts = 0``, the day-builder's
proposed plan clears every one of the ten criteria in 05 §7:

   1  in-house coverage >= state B (the hand-finished board), per date
   2  in-house coverage >= the same-date suggest+build baseline (05 §1)
   3  hard-infeasible turn pairs         == 0
   4  driver-days over the 15.0h ceiling == 0
   5  NEW rest-floor breaches vs the real board == 0
   6  every 13.5–15.0h exception priced and visible in the payload
   7  drivers per vehicle-date           <= 2
   8  handoff bands proposed             no RED
   9  driver-days used <= the same-date baseline (the point of Pass A)
  10  wall-clock <= opt_runtime_budget_s, or budget_exhausted flagged

Criterion 1 is the founder's success test and criterion 9 is
"available != required"; a run that passes 1 but not 9 has not built what
Build 3 was for. Also emitted, as EVIDENCE rather than as a gate: the
per-date delta table (coverage, driver-days, farm cost, span pressure) so
the founder can judge D11 promotion on numbers.

TICKET-D BILLING ASSERTION (05 §5): the builder must not become a new
billing surface — it may perform no drive-time lookups on unknown routes
beyond what the shipped pipeline already does. Asserted here by counting
``RouteDistanceCache`` rows before/after the optimizer call: after the
same-date baseline pipeline run has already enqueued whatever the shipped
passes enqueue, the optimizer run must add ZERO new rows.

THE OPTIMIZER UNDER TEST
------------------------
Ticket A lands ``dispatching.day_planner.build_day_plan``. Until it exists
this harness runs everything else — the raw-side state-B scorecard, the
same-date cold baseline, the criteria machinery — and reports
"NOTHING TO GATE YET" with exit 0. That is the designed Ticket-F state:
the gate is written and the baseline captured BEFORE the code it will
judge (05 §10). Once the module imports, every criterion goes live and
any failure exits non-zero.

The expected interface (duck-typed; attribute or dict access both work):

    plan = build_day_plan(target_date, epsilon=0)
      .roster_driver_ids   [int]                 drivers proposed to work
      .dva_rows            [(driver_id, vehicle_id)]  proposed pairing
      .assignments         {leg_id: driver_id}   the proposed board
      .exceptions          [{driver_id, eff_hours, price_usd, ...}]
                           every proposed 13.5–15.0h driver-day, priced
      .shares              [{vehicle_id, band, ...}]  proposed shared cars
      .budget_exhausted    bool
      .evaluations         int
      .wall_clock_s        float (the harness also times the call itself)

Everything judgeable is RE-DERIVED here from ``assignments`` + the raw
side — coverage, spans, turn bands, rest, driver-days — never trusted
from the plan's own bookkeeping (the 12/13/14 discipline).

METHOD (the 13/14 technique)
  Phase A: raw side — the read-only snapshot (GRAYSON_SNAPSHOT_DB points a
    run at a frozen copy while the dev server writes to the live one);
    derive the current regime from the data, pick 10 evenly-spaced dates
    (no date literals). Per date: the A6-filtered leg census, the state-B
    scorecard via analysis/09's own ``day_metrics`` (imported, not
    copied), and the adjacent-day actual boards + the real board's
    pre-existing rest breaches for criterion 5's delta.
  Phase B: a throwaway COPY of the snapshot, migrated to the current
    schema; django.setup() with RUN_SCHEDULERS_IN_WEB=0 and
    ROUTE_DISTANCE_INLINE_RESOLVER=False. Per date, in order: clear the
    day's assignments on the copy (cold, exactly as the builder meets
    it), run the shipped pipeline at the dispatcher's real roster (the
    same-date suggest+build baseline — recomputed at run time, never
    hard-coded), then ask the optimizer for its plan and judge it.
  Phase C: verdicts re-derived from the raw side wherever the raw side
    can see them (coverage universe, rest arithmetic); Django-side
    formulas used exactly where 05 §2 names them (board_validation.
    turn_slack_minutes + pickup_policy.turn_band, scheduler.
    effective_span_hours, handoff_chain.handoff_band).

USAGE
  python docs/scheduling-redesign/analysis/17_build3_gate.py
  # optional: --dates N (default 10)

Conventions: no date literals; snapshot opened read-only; A6 filters via
_common; consecutive-pair arithmetic inherits analysis/09's 8h pairing cap
through its imported day_metrics.
"""
import argparse
import datetime as dt
import importlib.util
import os
import shutil
import sys
import tempfile
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

sys.path.insert(0, C.REPO_ROOT)

N_DATES = 10
REST_MIN = 510.0          # live rest floor default; re-read from settings in Phase B

ASSUMPTIONS = (
    "The coverage universe on BOTH sides is the A6-filtered leg census (both "
    "cancellation spellings excluded). The pipeline's own query keeps one-L "
    "'canceled' reservations; any such leg it assigns simply does not count "
    "toward coverage here, on either side of the comparison.",
    "Criterion 5's rest arithmetic runs on the RAW side with the 09 occupancy "
    "envelope (booked pickup ± A3.5 P50 lead/tail) for plan, baseline and real "
    "board alike — one formula on both sides of the delta, so the count is a "
    "fair 'new breaches' figure, not a clock-convention artifact.",
    "The same-date baseline replays the view's bare-payload roster derivation "
    "(DVA-eligible, active, saved availability) against the pipeline directly; "
    "analysis/14 proved view and pipeline byte-identical.",
    "Criteria 3/4/6/8 use the production formulas 05 §2 names "
    "(board_validation.turn_slack_minutes + pickup_policy.turn_band, "
    "scheduler.effective_span_hours, handoff_chain.handoff_band).",
    "Clearing runs per-date, ascending; gated dates are checked to be "
    "non-adjacent so clearing one never empties another's rest-scan yesterday.",
)


# --------------------------------------------------------------------------
# Phase B bootstrap — Django on a migrated throwaway copy of the snapshot
# --------------------------------------------------------------------------

def django_on_copy():
    tmp = os.environ.get("BUILD3_GATE_TMP") or tempfile.mkdtemp(prefix="build3_gate_")
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


def load_09():
    """Import analysis/09 as a module so state B comes from the committed
    benchmark's own day_metrics — never a re-implementation that could drift."""
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        "bench09", os.path.join(here, "09_benchmark_state_b.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# --------------------------------------------------------------------------
# Phase A — raw-side state
# --------------------------------------------------------------------------

def pick_dates(con, horizon, n):
    byday = C.legs_per_day(con)
    scan_from = dt.date.fromisoformat(min(byday))
    segs = C.changepoints(byday, scan_from, horizon.today, min_seg=28, min_effect=0.08)
    cur_a = segs[-1][0]
    cur_b = min(segs[-1][1], horizon.last_actuals_day)
    all_days = [cur_a + dt.timedelta(days=i) for i in range((cur_b - cur_a).days + 1)]
    picks = sorted({all_days[round(i * (len(all_days) - 1) / (n - 1))] for i in range(n)})
    return cur_a, cur_b, picks


def load_raw(con, a, b):
    """A6-filtered legs for [a-1, b+1] in 09's row shape, plus driver types and
    the per-date DVA vehicle map. The ±1 day margin feeds the rest arithmetic."""
    dtype = {r["id"]: (r["driver_type"] or "").lower()
             for r in C.q(con, "SELECT id, driver_type FROM drivers_driver")}
    rows = C.q(con, C.live_legs_sql(
        "l.id, l.pickup_date d, l.pickup_time pt, l.pickup_location pl, "
        "l.dropoff_location dl, l.driver_id did",
        "AND l.pickup_date BETWEEN ? AND ?"),
        (str(a - dt.timedelta(days=1)), str(b + dt.timedelta(days=1))))
    dva = defaultdict(dict)
    for r in C.q(con, "SELECT date, driver_id, vehicle_id "
                      "FROM drivers_drivervehicleassignment "
                      "WHERE date BETWEEN ? AND ? AND vehicle_id IS NOT NULL",
                 (str(a - dt.timedelta(days=1)), str(b + dt.timedelta(days=1)))):
        dva[str(r["date"])[:10]][r["driver_id"]] = r["vehicle_id"]
    return dtype, rows, dva


def raw_rows_by_date(rows, m09, fg, catloc):
    """09's exact row construction: per-date dicts carrying pick instant, span-
    family kind (ks), conflict-family kind (kc) and shipped categories."""
    by_date = defaultdict(list)
    for r in rows:
        pick = C.booked_dtm(r["d"], r["pt"])
        pcat, dcat = catloc(r["pl"] or ""), catloc(r["dl"] or "")
        kc = ("ARRIVAL" if pcat in fg.AIRPORT_TERMINALS
              else "DEPARTURE" if dcat in fg.AIRPORT_TERMINALS else "OTHER")
        by_date[r["d"]].append({"id": r["id"], "did": r["did"], "pick": pick,
                                "ks": C.trip_kind(r["pl"], r["dl"]), "kc": kc,
                                "pcat": pcat, "dcat": dcat})
    return by_date


def day_envelope(legs_rows, occ):
    """(first_start, last_end) of a driver-day under the 09 occupancy envelope.
    None when no row carries a parseable pickup instant."""
    pts = [(r["pick"] - dt.timedelta(minutes=occ[r["ks"]][0]),
            r["pick"] + dt.timedelta(minutes=occ[r["ks"]][1]))
           for r in legs_rows if r["pick"] is not None]
    if not pts:
        return None
    return min(p[0] for p in pts), max(p[1] for p in pts)


def rest_breaches(day_iso, boards_by_driver, adjacent, occ):
    """{(driver_id, side)} rest-floor breaches of the given per-driver boards
    against the ACTUAL adjacent-day state-B boards. side in {'prev','next'}."""
    day = dt.date.fromisoformat(day_iso)
    prev_iso = (day - dt.timedelta(days=1)).isoformat()
    next_iso = (day + dt.timedelta(days=1)).isoformat()
    out = set()
    for did, rows_ in boards_by_driver.items():
        env = day_envelope(rows_, occ)
        if env is None:
            continue
        first_start, last_end = env
        prev_rows = adjacent.get((prev_iso, did))
        if prev_rows:
            penv = day_envelope(prev_rows, occ)
            if penv and (first_start - penv[1]).total_seconds() / 60.0 < REST_MIN:
                out.add((did, "prev"))
        next_rows = adjacent.get((next_iso, did))
        if next_rows:
            nenv = day_envelope(next_rows, occ)
            if nenv and (nenv[0] - last_end).total_seconds() / 60.0 < REST_MIN:
                out.add((did, "next"))
    return out


# --------------------------------------------------------------------------
# Phase B — the same-date suggest+build baseline (the view's bare path,
# against the pipeline directly; 14 proved the two byte-identical)
# --------------------------------------------------------------------------

def clear_day(day):
    from reservations.models import Leg
    return (Leg.objects.filter(pickup_date=day, driver__isnull=False)
            .update(driver=None))


def day_roster(target_date):
    """The dispatcher's real roster exactly as views.auto_assign_drivers derives
    it with no modal payload: DVA-eligible in-house, active, saved availability."""
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


def run_baseline(target_date):
    """One cold shipped-pipeline run at the real roster. Returns
    (assignments, legs, drivers, wall_s)."""
    from dispatching.assignment_pipeline import (
        PipelineLocks, PipelineWindows, run_assignment_pipeline)
    from dispatching.scheduler import resolve_run_min_buffer, load_driver_min_buffers
    drivers, driver_hours, flexible, driver_max_hours = day_roster(target_date)
    legs = load_day_legs(target_date)
    t0 = time.time()
    res = run_assignment_pipeline(
        legs, drivers, target_date,
        PipelineWindows(driver_hours=driver_hours, flexible_drivers=flexible,
                        driver_max_hours=driver_max_hours,
                        run_min_buffer=resolve_run_min_buffer(None),
                        driver_min_buffers=load_driver_min_buffers(
                            [d.id for d in drivers])),
        PipelineLocks())
    return res.assignments, legs, drivers, time.time() - t0


# --------------------------------------------------------------------------
# metrics over an assignment map — shared by baseline and plan
# --------------------------------------------------------------------------

def board_metrics(assignments, legs, drivers, target_date, a6_ids, hard_cap):
    """Re-derive every judgeable figure from {leg_id: driver_id}:
    coverage on the A6 universe, driver-days, farm set, spans (raw/effective),
    span pressure, critical/tight turn pairs. Uses the production formulas
    05 §2 names. Restores leg.driver to None before returning (cold state)."""
    from dispatching.scheduler import build_driver_schedules, effective_span_hours
    from dispatching.board_validation import board_turn_bands
    from dispatching import feasibility_guards as fg

    legs_by_id = {l.id: l for l in legs}
    drivers_by_id = {d.id: d for d in drivers}
    stamped = []
    for lid, did in assignments.items():
        lg = legs_by_id.get(lid)
        if lg is not None and did in drivers_by_id:
            lg.driver = drivers_by_id[did]
            lg.driver_id = did
            stamped.append(lg)
    board = build_driver_schedules(legs, drivers, target_date)
    bands = board_turn_bands(board, target_date)
    for lg in stamped:                       # restore the cold state
        lg.driver = None
        lg.driver_id = None

    assigned_a6 = [lid for lid in assignments if lid in a6_ids]
    m = {
        "assigned": len(assignments),
        "assigned_a6": len(assigned_a6),
        "coverage_pct": 100.0 * len(assigned_a6) / len(a6_ids) if a6_ids else None,
        "driver_days": len(set(assignments.values())),
        "farm_a6": len(a6_ids) - len(assigned_a6),
        "farmed_leg_ids": sorted(a6_ids - set(assignments.keys())),
        "critical_pairs": sum(1 for i in bands.values() if i["band"] == "critical"),
        "tight_pairs": sum(1 for i in bands.values() if i["band"] == "tight"),
    }
    spans = {}
    for did, sched in board.items():
        if sched.slots:
            raw, eff = effective_span_hours(sched.slots, target_date)
            spans[did] = (raw, eff)
    m["spans"] = spans
    m["over_soft"] = sorted(d for d, (_r, e) in spans.items()
                            if e > fg.SPAN_SOFT_EFFECTIVE_HOURS)
    m["over_hard"] = sorted(d for d, (_r, e) in spans.items() if e > hard_cap)
    m["span_pressure_h"] = round(sum(
        max(0.0, e - fg.SPAN_SOFT_EFFECTIVE_HOURS) for _r, e in spans.values()), 2)
    return m


def farm_cost_usd(farmed_leg_ids, legs_by_id):
    """Sum fleet_intel.affiliate_base_cost over the farmed set; the shipped
    FARMOUT_PREMIUM_PER_LEG stands in per leg where the rate was never
    captured (04 §1 rule 3 — the stand-in count is reported)."""
    from dispatching.fleet_intel import affiliate_base_cost
    from dispatching.standby_mints import FARMOUT_PREMIUM_PER_LEG
    total, fallback = 0.0, 0
    for lid in farmed_leg_ids:
        lg = legs_by_id.get(lid)
        cost = affiliate_base_cost(lg) if lg is not None else None
        if cost is None:
            total += FARMOUT_PREMIUM_PER_LEG
            fallback += 1
        else:
            total += float(cost)
    return round(total, 2), fallback


# --------------------------------------------------------------------------
# the optimizer under test
# --------------------------------------------------------------------------

def load_optimizer():
    try:
        from dispatching.day_planner import build_day_plan
        return build_day_plan
    except Exception:
        return None


def plan_get(plan, name, default=None):
    if isinstance(plan, dict):
        return plan.get(name, default)
    return getattr(plan, name, default)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", type=int, default=N_DATES)
    args = ap.parse_args()

    t0 = time.time()
    con = C.connect()
    h = C.Horizon(con)
    C.preamble("17_build3_gate.py",
               "Build 3b Gate 4: the ten acceptance criteria at epsilon=0",
               h, ASSUMPTIONS)

    cur_a, cur_b, picks = pick_dates(con, h, args.dates)
    print(f"\nregime {cur_a}..{cur_b}; gated dates ({len(picks)}, evenly spaced): "
          + ", ".join(str(d) for d in picks))
    for a, b in zip(picks, picks[1:]):
        if (b - a).days < 2:
            print(f"  WARNING: {a} and {b} are adjacent — clearing one empties "
                  f"the other's rest-scan yesterday on the copy")

    dtype, raw_rows, dva = load_raw(con, min(picks), max(picks))
    con.close()

    # ---- Django on the throwaway copy; then 09's machinery on top of it ----
    django_on_copy()
    m09 = load_09()
    # 09's load_shipped() stubs django for STANDALONE runs; under a booted
    # Django its `setattr(django.db.models, name, None)` lines would corrupt
    # the real ORM (isinstance() crashes deep in the sqlite backend). Save the
    # real attributes and restore them right after.
    import django.db.models as _dj_models
    _saved_attrs = {a: getattr(_dj_models, a) for a in ("Avg", "Count", "Q", "Sum", "F")}
    try:
        fg09, pp09, DRIVE, DEFAULT_DRIVE, catloc = m09.load_shipped()
    finally:
        for _a, _v in _saved_attrs.items():
            setattr(_dj_models, _a, _v)
    by_date = raw_rows_by_date(raw_rows, m09, fg09, catloc)

    from dispatching.models import SchedulerSettings
    from dispatching.scheduler import preload_timing_cache
    from reservations.models import RouteDistanceCache
    preload_timing_cache()
    cfg = SchedulerSettings.get_settings()
    global REST_MIN
    REST_MIN = float(cfg.rest_min_gap_minutes or 510)
    hard_cap = float(cfg.span_exception_max_hours)
    runtime_budget_s = float(getattr(cfg, "opt_runtime_budget_s", 240) or 240)
    print(f"\nlive settings: rest floor {REST_MIN:.0f} min, hard span cap "
          f"{hard_cap}h, runtime budget {runtime_budget_s:.0f}s")

    build_day_plan = load_optimizer()
    if build_day_plan is None:
        print("\noptimizer: dispatching.day_planner.build_day_plan NOT BUILT YET "
              "— baseline + criteria machinery run; nothing to gate (expected "
              "in the Ticket-F state).")

    # adjacent-day actual boards, raw side: {(date_iso, driver_id): rows}
    adjacent = defaultdict(list)
    for iso, rows_ in by_date.items():
        for r in rows_:
            if r["did"] is not None and dtype.get(r["did"]) == "inhouse":
                adjacent[(iso, r["did"])].append(r)

    CRITERIA = ["1 coverage>=stateB", "2 coverage>=baseline", "3 no critical",
                "4 none>15h", "5 no new rest", "6 exceptions priced",
                "7 <=2/vehicle", "8 no RED handoff", "9 dd<=baseline",
                "10 runtime"]
    results, evidence, failures = [], [], []

    for day in picks:
        iso = day.isoformat()
        rows_ = by_date.get(iso, [])
        a6_ids = {r["id"] for r in rows_}
        stateb = m09.day_metrics(rows_, dtype, dva.get(iso, {}),
                                 fg09, pp09, DRIVE, DEFAULT_DRIVE)

        # the real board's own rest breaches (pre-existing; not the plan's fault)
        real_boards = defaultdict(list)
        for r in rows_:
            if r["did"] is not None and dtype.get(r["did"]) == "inhouse":
                real_boards[r["did"]].append(r)
        pre_existing = rest_breaches(iso, real_boards, adjacent, m09.OCC)

        n_cleared = clear_day(day)
        print(f"\n{iso}: cleared {n_cleared} assignments on the copy (cold)")
        base_assign, legs, drivers, base_s = run_baseline(day)
        legs_by_id = {l.id: l for l in legs}
        base = board_metrics(base_assign, legs, drivers, day, a6_ids, hard_cap)
        base_cost, base_fb = farm_cost_usd(base["farmed_leg_ids"], legs_by_id)
        print(f"  baseline : {base['coverage_pct']:.1f}% coverage, "
              f"{base['driver_days']} driver-days, {base['farm_a6']} farmed "
              f"(${base_cost:,.0f}), {base_s:.1f}s")

        row = {"date": iso, "legs": len(a6_ids),
               "stateb_cov": round(stateb["coverage"], 1),
               "stateb_dd": stateb["driver_days"],
               "base_cov": round(base["coverage_pct"], 1),
               "base_dd": base["driver_days"], "base_farm": base["farm_a6"],
               "base_cost": base_cost, "base_span_pressure": base["span_pressure_h"]}

        if build_day_plan is None:
            row.update({"plan_cov": "", "plan_dd": "", "plan_farm": "",
                        "plan_cost": "", "plan_span_pressure": "",
                        "criteria": "not built"})
            results.append((iso, None, row))
            evidence.append(row)
            continue

        # ---- the plan, judged ----
        rdc_before = RouteDistanceCache.objects.count()
        t1 = time.time()
        plan = build_day_plan(day, epsilon=0)
        wall_s = time.time() - t1
        rdc_after = RouteDistanceCache.objects.count()

        assignments = dict(plan_get(plan, "assignments") or {})
        p = board_metrics(assignments, legs, drivers, day, a6_ids, hard_cap)
        plan_cost, plan_fb = farm_cost_usd(p["farmed_leg_ids"], legs_by_id)

        plan_boards = defaultdict(list)
        raw_by_id = {r["id"]: r for r in rows_}
        for lid, did in assignments.items():
            r = raw_by_id.get(lid)
            if r is not None:
                plan_boards[did].append(r)
        new_rest = rest_breaches(iso, plan_boards, adjacent, m09.OCC) - pre_existing

        exceptions = list(plan_get(plan, "exceptions") or [])
        priced = {plan_get(e, "driver_id") for e in exceptions
                  if (plan_get(e, "price_usd") or 0) > 0}
        unpriced = [d for d in p["over_soft"] if d not in priced]

        dva_rows = list(plan_get(plan, "dva_rows") or [])
        per_vehicle = defaultdict(set)
        for entry in dva_rows:
            did_, vid_ = (entry if isinstance(entry, (tuple, list))
                          else (plan_get(entry, "driver_id"),
                                plan_get(entry, "vehicle_id")))
            if vid_ is not None:
                per_vehicle[vid_].add(did_)
        over_two = {v: sorted(ds) for v, ds in per_vehicle.items() if len(ds) > 2}

        shares = list(plan_get(plan, "shares") or [])
        reds = [s for s in shares if str(plan_get(s, "band", "")).lower() == "red"]

        budget_exhausted = bool(plan_get(plan, "budget_exhausted", False))

        verdicts = {
            "1 coverage>=stateB": p["coverage_pct"] >= stateb["coverage"] - 1e-9,
            "2 coverage>=baseline": p["coverage_pct"] >= base["coverage_pct"] - 1e-9,
            "3 no critical": p["critical_pairs"] == 0,
            "4 none>15h": len(p["over_hard"]) == 0,
            "5 no new rest": len(new_rest) == 0,
            "6 exceptions priced": len(unpriced) == 0,
            "7 <=2/vehicle": len(over_two) == 0,
            "8 no RED handoff": len(reds) == 0,
            "9 dd<=baseline": p["driver_days"] <= base["driver_days"],
            "10 runtime": (wall_s <= runtime_budget_s) or budget_exhausted,
        }
        billing_ok = (rdc_after == rdc_before)
        if not billing_ok:
            failures.append((iso, "billing",
                             f"optimizer added {rdc_after - rdc_before} "
                             f"RouteDistanceCache rows beyond the baseline's"))
        for name, ok in verdicts.items():
            if not ok:
                detail = {
                    "1 coverage>=stateB": f"{p['coverage_pct']:.1f}% < {stateb['coverage']:.1f}%",
                    "2 coverage>=baseline": f"{p['coverage_pct']:.1f}% < {base['coverage_pct']:.1f}%",
                    "3 no critical": f"{p['critical_pairs']} critical pair(s)",
                    "4 none>15h": f"drivers {p['over_hard']}",
                    "5 no new rest": f"{sorted(new_rest)}",
                    "6 exceptions priced": f"unpriced over-13.5h drivers {unpriced}",
                    "7 <=2/vehicle": f"{over_two}",
                    "8 no RED handoff": f"{len(reds)} RED share(s) proposed",
                    "9 dd<=baseline": f"{p['driver_days']} > {base['driver_days']}",
                    "10 runtime": f"{wall_s:.1f}s > {runtime_budget_s:.0f}s, not flagged",
                }[name]
                failures.append((iso, name, detail))

        row.update({"plan_cov": round(p["coverage_pct"], 1),
                    "plan_dd": p["driver_days"], "plan_farm": p["farm_a6"],
                    "plan_cost": plan_cost,
                    "plan_span_pressure": p["span_pressure_h"],
                    "criteria": " ".join(
                        ("Y" if verdicts[c] else "N") for c in CRITERIA)})
        results.append((iso, verdicts, row))
        evidence.append(row)
        print(f"  plan     : {p['coverage_pct']:.1f}% coverage, "
              f"{p['driver_days']} driver-days, {p['farm_a6']} farmed "
              f"(${plan_cost:,.0f}), {wall_s:.1f}s, "
              f"evals={plan_get(plan, 'evaluations')}, "
              f"criteria {' '.join(('Y' if verdicts[c] else 'N') for c in CRITERIA)}")

    # ---- report ----
    C.hdr("EVIDENCE — per-date deltas (for D11 judgment, not a gate)  [measured]")
    print(f"{'date':11s}{'legs':>5s}{'stB%':>7s}{'base%':>7s}{'plan%':>7s}"
          f"{'stB dd':>7s}{'base dd':>8s}{'plan dd':>8s}{'base $':>9s}{'plan $':>9s}"
          f"{'base sp':>8s}{'plan sp':>8s}")
    for r in evidence:
        print(f"{r['date']:11s}{r['legs']:5d}{r['stateb_cov']:7.1f}"
              f"{r['base_cov']:7.1f}{str(r['plan_cov']):>7s}"
              f"{r['stateb_dd']:7d}{r['base_dd']:8d}{str(r['plan_dd']):>8s}"
              f"{r['base_cost']:9,.0f}{str(r['plan_cost']):>9s}"
              f"{r['base_span_pressure']:8.1f}{str(r['plan_span_pressure']):>8s}")

    cols = list(evidence[0].keys())
    pth = C.write_csv("17_build3_gate.csv", cols,
                      [[r.get(c, "") for c in cols] for r in evidence])
    print(f"\nWrote: {os.path.relpath(pth, C.REPO_ROOT)}")

    C.hdr("GATE — Build 3b acceptance (05 §7, epsilon=0)  [measured]")
    if build_day_plan is None:
        print("VERDICT: HARNESS RUNNING — NOTHING TO GATE YET.")
        print("The optimizer (dispatching.day_planner.build_day_plan) is not "
              "built; the state-B scorecard, the same-date baselines and every "
              "criterion's machinery ran end to end. Re-run unchanged once "
              "Ticket A lands.")
        print(f"runtime: {time.time() - t0:.1f}s")
        return

    print(f"dates gated : {len(picks)}")
    print(f"failures    : {len(failures)}")
    for iso, name, detail in failures:
        print(f"  FAIL {iso}  [{name}]  {detail}")
    ok = not failures
    print("\nVERDICT:", "PASS — all ten criteria hold on every gated date"
          if ok else "FAIL — see above")
    print(f"runtime: {time.time() - t0:.1f}s")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
