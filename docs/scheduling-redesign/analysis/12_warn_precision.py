#!/usr/bin/env python
"""12 — Manual-assign warning precision: the Build-1a ship gate (04 §2).

QUESTION: if the warn-only validation now wired into ``update_leg_assignment``
(dispatching/assign_warnings.py) had been live while the current-regime boards
were built, what fraction of the warnings it fired would have pointed at a REAL
problem? The bar (04 §1 rule 2, decision D5): a warning class ships visibly as
a warning only at >=70% precision against the analysis/09 conflict definitions;
below the bar it demotes to a passive info row.

METHOD
  Phase A (this snapshot, READ-ONLY — house conventions): derive the current
    regime exactly as 09 does (changepoints, min_seg=28, min_effect=0.09,
    clipped to actuals) and score every adjacent same-driver pair of the
    operated boards with 09's SHIPPED-constant conflict arithmetic at PAIR
    level: hard (slack < 0) / tight (0..TURN_TIGHT) under the 8h pairing cap
    (00 §A13). That per-pair verdict is the TRUTH SET.
  Phase B (a throwaway COPY of the snapshot, never the original): run the
    PRODUCTION warning code itself — build_driver_schedules over each date's
    in-house board, board_validation.turn_slack_minutes + pickup_policy
    .turn_band per adjacent pair (exactly what assign_warnings._turn_warnings
    computes), and assign_warnings.share_conflicts over every shared unit-day
    (the same pure core the endpoint calls). The copy is migrated to the
    current schema first; the inline route-distance resolver and the daemon
    schedulers are disabled, so nothing here can bill a Google call or write
    anywhere but the temp copy.

  PRECISION(turn class) = fired pairs whose truth is hard-or-tight / fired.
  The two formulas differ structurally (09 clears a job by the fitted
  occupancy tail; production chains the founder static model), so agreement is
  measured, not assumed.

  Share classes have no 09 pair-truth: share_overlap/interleave describe a
  physically impossible state (one car, two places / >1 hand-back) and are
  reported as fire-rates over the measured shared vehicle-days; share_pad is
  calibration-reported against the executed-handoff gap record (03 §3.4) and
  ships demoted to "info" regardless (an under-pad handoff is AMBER — feasible
  with an explicit plan — not a conflict).

Run:  python docs/scheduling-redesign/analysis/12_warn_precision.py
"""

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

PAIR_CAP_MIN = 8 * 60      # stated per 00 §A13; matches 09

ASSUMPTIONS = (
    "Truth = analysis/09's conflict definitions at PAIR level: occupancy-tail "
    "clear + shipped drive table + shipped grace/buffer constants; hard slack<0, "
    "tight 0..TURN_TIGHT_SLACK_MIN; 8h pairing cap.",
    "Fired = the production planning-clock formula: turn_slack_minutes over "
    "build_driver_schedules slots, banded by pickup_policy.turn_band — the same "
    "calls assign_warnings makes on a manual assign. Final-board pairs stand in "
    "for the pairs a dispatcher's assigns would have been warned on.",
    "driver_type is a CURRENT-STATE flag (00 A4): both phases split in-house by "
    "today's label, so the comparison is internally consistent.",
    "Share replay: DVA unit-days with >=2 in-house drivers; occupancy blocks at "
    "P75 (handoff_chain), pad from SchedulerSettings.vehicle_share_pad_min.",
)


def load_09():
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        "bench09", os.path.join(here, "09_benchmark_state_b.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ────────────────────────────────────────────────────────────────────────────
# Phase A — the 09 pair-level truth set (read-only snapshot)
# ────────────────────────────────────────────────────────────────────────────

def truth_pairs(con, m09, fg, pp, DRIVE, DEFAULT_DRIVE, catloc, day_a, day_b):
    """{(date, did, leg_a, leg_b): {"slack09","verdict"}} over in-house boards."""
    dtype = {r["id"]: (r["driver_type"] or "").lower()
             for r in C.q(con, "SELECT id, driver_type FROM drivers_driver")}
    rows = C.q(con, C.live_legs_sql(
        "l.id, l.pickup_date d, l.pickup_time pt, l.pickup_location pl, "
        "l.dropoff_location dl, l.driver_id did",
        "AND l.pickup_date BETWEEN ? AND ?"), (str(day_a), str(day_b)))
    byd = defaultdict(list)
    for r in rows:
        if r["did"] is None or dtype.get(r["did"]) != "inhouse":
            continue
        pick = C.booked_dtm(r["d"], r["pt"])
        if pick is None:
            continue
        pcat, dcat = catloc(r["pl"] or ""), catloc(r["dl"] or "")
        kc = ("ARRIVAL" if pcat in fg.AIRPORT_TERMINALS
              else "DEPARTURE" if dcat in fg.AIRPORT_TERMINALS else "OTHER")
        byd[(r["d"], r["did"])].append(
            {"id": r["id"], "pick": pick, "kc": kc, "pcat": pcat, "dcat": dcat})

    out = {}
    for (d, did), ls in byd.items():
        ls.sort(key=lambda x: (x["pick"], x["id"]))
        for a, b in zip(ls[:-1], ls[1:]):
            gap = (b["pick"] - a["pick"]).total_seconds() / 60.0
            if gap > PAIR_CAP_MIN:
                continue
            # 09's conflict arithmetic, verbatim (09_benchmark_state_b.day_metrics)
            clear = a["pick"] + dt.timedelta(minutes=m09.OCC[a["kc"]][1])
            avail = (b["pick"] - clear).total_seconds() / 60.0
            nxt_arr = b["kc"] == "ARRIVAL"
            same_term = (a["dcat"] == b["pcat"]) and (b["pcat"] in fg.AIRPORT_TERMINALS)
            req = (-fg.DEPLANING_GRACE_MIN if (nxt_arr and same_term)
                   else DRIVE.get((a["dcat"], b["pcat"]), DEFAULT_DRIVE)) + fg.SAFETY_PAD_MIN
            buf = (0 if (fg.BUFFER_EXEMPT_SAME_TERMINAL_ARRIVAL and nxt_arr and same_term)
                   else fg.MIN_TURN_BUFFER_DEFAULT)
            slack = avail - req - buf
            verdict = ("hard" if slack < 0
                       else "tight" if slack < pp.TURN_TIGHT_SLACK_MIN else "")
            out[(d, did, a["id"], b["id"])] = {"slack09": round(slack, 1),
                                               "verdict": verdict}
    return out


# ────────────────────────────────────────────────────────────────────────────
# Phase B — the production warning logic on a migrated throwaway copy
# ────────────────────────────────────────────────────────────────────────────

def django_on_copy():
    """Copy the snapshot, migrate the copy, django.setup() against it.
    Returns the temp dir (caller may keep it for inspection)."""
    tmp = os.environ.get("WARN_PRECISION_TMP") or tempfile.mkdtemp(
        prefix="warn_precision_")
    os.makedirs(tmp, exist_ok=True)
    db_copy = os.path.join(tmp, "db_copy.sqlite3")
    print(f"\ncopying snapshot -> {db_copy} (the ORIGINAL is never opened "
          f"writable by this script)")
    shutil.copyfile(C.DB_PATH, db_copy)

    with open(os.path.join(tmp, "replay_settings.py"), "w", encoding="utf-8") as f:
        f.write(
            "from business.settings import *\n"
            f"DATABASES['default']['NAME'] = {db_copy!r}\n"
            "# no billed Google calls from a replay, ever\n"
            "ROUTE_DISTANCE_INLINE_RESOLVER = False\n"
        )

    os.environ["RUN_SCHEDULERS_IN_WEB"] = "0"     # no daemon loops
    os.environ["ENABLE_DEBUG_TOOLBAR"] = "0"
    os.environ.setdefault("USE_LIVE_DISTANCE", "0")
    os.environ["DJANGO_SETTINGS_MODULE"] = "replay_settings"
    sys.path.insert(0, tmp)
    sys.path.insert(0, C.REPO_ROOT)

    # Phase A (09's load_shipped) planted empty stub modules under the django
    # names so it could exec production files without an ORM. Purge them or the
    # real import below resolves to the stubs.
    for name in [m for m in list(sys.modules)
                 if m == "django" or m.startswith("django.")]:
        del sys.modules[name]

    import django
    django.setup()
    from django.core.management import call_command
    from django.db import connection
    print("migrating the copy to the current schema (adds Build-1 fields) ...")
    # The production snapshot carries at least one pre-existing orphaned FK
    # (e.g. a reservation whose customer row is gone). Production runs fine on
    # it; only the sqlite schema editor's post-migration whole-DB FK sweep
    # trips over it. Disable that sweep for the migrate call ONLY — the data is
    # left exactly as production sees it, which is the point of a replay.
    orig_check = connection.check_constraints
    connection.check_constraints = lambda *a, **k: None
    try:
        call_command("migrate", verbosity=0, interactive=False)
    finally:
        connection.check_constraints = orig_check
    return tmp


def replay_production_warnings(day_a, day_b):
    """Run the shipped warning primitives over every regime date.

    Returns (fired_turn, share_stats, n_slack_none)
      fired_turn: {(date, did, leg_a, leg_b): {"slack","band"}}
      share_stats: per-class fire counts + pad-gap list over shared unit-days
    """
    from dispatching.analytics import categorize_location
    from dispatching.assign_warnings import build_share_entry, share_conflicts
    from dispatching.board_validation import turn_slack_minutes
    from dispatching.models import SchedulerSettings
    from dispatching import pickup_policy
    from dispatching.scheduler import build_driver_schedules, preload_timing_cache
    from drivers.models import Driver, DriverVehicleAssignment
    from reservations.models import Leg

    preload_timing_cache()
    pad_min = SchedulerSettings.get_settings().vehicle_share_pad_min
    print(f"share pad from SchedulerSettings.vehicle_share_pad_min = {pad_min} min")

    inhouse = {d.id: d for d in Driver.objects.filter(driver_type="inhouse")}

    fired = {}
    n_none = 0
    share = {"unit_days": 0, "share_overlap": 0, "share_interleave": 0,
             "share_pad": 0, "pad_gaps": [], "unit_days_fired": 0}

    d = day_a
    while d <= day_b:
        legs = list(
            Leg.objects.filter(pickup_date=d, driver_id__in=inhouse.keys(),
                               pickup_time__isnull=False)
            .exclude(status="cancelled")
            .exclude(reservation__status__in=["cancelled", "canceled"])
            .select_related("reservation", "flight_information",
                            "cruise_information", "driver")
        )
        if legs:
            drivers = [inhouse[i] for i in {l.driver_id for l in legs}]
            schedules = build_driver_schedules(legs, drivers, d)
            for did, sched in schedules.items():
                slots = sorted(sched.slots, key=lambda s: (s.pickup_time, s.leg_id))
                for a, b in zip(slots, slots[1:]):
                    slack = turn_slack_minutes(a, b, d)
                    if slack is None:
                        n_none += 1
                        continue
                    band = pickup_policy.turn_band(slack)
                    if band:
                        fired[(d.isoformat(), did, a.leg_id, b.leg_id)] = {
                            "slack": slack, "band": band}

            # shared unit-days (>=2 in-house drivers on one physical unit)
            unit_holders = defaultdict(list)
            for r in DriverVehicleAssignment.objects.filter(
                    date=d, vehicle__isnull=False, driver_id__in=inhouse.keys()):
                unit_holders[r.vehicle_id].append(r.driver_id)
            legs_by_driver = defaultdict(list)
            for l in legs:
                legs_by_driver[l.driver_id].append(l)
            for veh, dids in unit_holders.items():
                if len(dids) < 2:
                    continue
                entries = []
                for did in dids:
                    for l in legs_by_driver.get(did, ()):
                        entries.append(build_share_entry(
                            l.id, did, dt.datetime.combine(d, l.pickup_time),
                            categorize_location(l.pickup_location),
                            categorize_location(l.dropoff_location)))
                if len({e["did"] for e in entries}) < 2:
                    continue          # a sharer ran no live legs — nothing to judge
                share["unit_days"] += 1
                conflicts = share_conflicts(entries, pad_min, focus_leg_id=None)
                if conflicts:
                    share["unit_days_fired"] += 1
                for c in conflicts:
                    share[c["code"]] += 1
                    if c["code"] == "share_pad":
                        share["pad_gaps"].append(int(
                            (c["b"]["pick"] - c["a"]["pick"]).total_seconds() / 60))
        d += dt.timedelta(days=1)
    return fired, share, n_none


# ────────────────────────────────────────────────────────────────────────────

def main():
    t0 = time.time()
    con = C.connect()
    h = C.Horizon(con)
    C.preamble("12_warn_precision.py",
               "manual-assign warning precision vs the 09 conflict truth "
               "(Build-1a ship gate)", h, ASSUMPTIONS)

    m09 = load_09()
    fg, pp, DRIVE, DEFAULT_DRIVE, catloc = m09.load_shipped()

    byday = C.legs_per_day(con, end=h.last_demand_day)
    scan_from = dt.date.fromisoformat(min(byday))
    segs = C.changepoints(byday, scan_from, h.last_demand_day,
                          min_seg=28, min_effect=0.09)
    cur_a = segs[-1][0]
    cur_b = min(segs[-1][1], h.last_actuals_day)
    print(f"\ncurrent regime (derived, clipped to actuals): {cur_a} .. {cur_b} "
          f"({(cur_b - cur_a).days + 1} days)")

    truth = truth_pairs(con, m09, fg, pp, DRIVE, DEFAULT_DRIVE, catloc, cur_a, cur_b)
    n_bad = sum(1 for v in truth.values() if v["verdict"])
    print(f"truth pairs: {len(truth)}  (hard/tight: {n_bad}; "
          f"{sum(1 for v in truth.values() if v['verdict'] == 'hard')} hard, "
          f"{sum(1 for v in truth.values() if v['verdict'] == 'tight')} tight)")
    con.close()

    django_on_copy()
    fired, share, n_none = replay_production_warnings(cur_a, cur_b)

    # ── the gate ──
    C.hdr("TURN-CLASS PRECISION vs the 09 hard/tight pair truth  [measured]")
    rows = []
    by_band = defaultdict(list)
    for key, info in fired.items():
        t = truth.get(key)
        real = bool(t and t["verdict"])
        by_band[info["band"]].append(real)
        by_band["all"].append(real)
        d, did, a_id, b_id = key
        rows.append([d, did, a_id, b_id, info["band"], info["slack"],
                     (t or {}).get("slack09", ""), (t or {}).get("verdict", "(no truth pair)"),
                     int(real)])

    print(f"{'class':16s}{'fired':>7s}{'real':>7s}{'precision':>11s}   gate")
    for band, label in (("critical", "turn_critical"), ("tight", "turn_tight"),
                        ("all", "ALL turn warnings")):
        v = by_band.get(band, [])
        if not v:
            print(f"{label:16s}{0:7d}{0:7d}{'—':>11s}")
            continue
        p = 100.0 * sum(v) / len(v)
        verdict = "PASS >=70%" if p >= 70.0 else "BELOW BAR -> demote to info"
        print(f"{label:16s}{len(v):7d}{sum(v):7d}{p:10.1f}%   {verdict}")
    print(f"\npairs skipped for missing timing (slack=None): {n_none}")

    C.hdr("SHARE-CLASS FIRE RATES over shared unit-days  [measured]")
    print(f"shared unit-days scored          : {share['unit_days']}")
    print(f"unit-days firing anything        : {share['unit_days_fired']}")
    print(f"share_overlap fires              : {share['share_overlap']}")
    print(f"share_interleave fires           : {share['share_interleave']}")
    print(f"share_pad fires                  : {share['share_pad']}")
    if share["pad_gaps"]:
        print("  " + C.fmt_describe("pad-fire pickup-to-pickup gaps (min)",
                                    share["pad_gaps"]))
    print("\nshare_overlap/interleave describe a physically impossible unit-day "
          "(one car, two places / >1 hand-back);\nshare_pad ships as passive "
          "INFO regardless — an under-pad handoff is AMBER (03 §3.2), a plan "
          "prompt, not an alarm.")

    p = C.write_csv("12_warn_pairs.csv",
                    ["date", "driver_id", "leg_a", "leg_b", "band",
                     "slack_prod", "slack_09", "verdict_09", "real"],
                    sorted(rows))
    print(f"\nWrote: {os.path.relpath(p, C.REPO_ROOT)}")
    print(f"runtime: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
