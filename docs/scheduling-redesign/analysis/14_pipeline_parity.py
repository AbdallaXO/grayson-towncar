#!/usr/bin/env python
"""14 — Build 3a prerequisite P1 gate: the pipeline extraction is byte-identical.

THE CLAIM THIS SCRIPT TESTS
---------------------------
Extracting the five-pass assignment build out of ``views.auto_assign_drivers``
into ``dispatching/assignment_pipeline.run_assignment_pipeline`` changes NOTHING
a caller can observe. The gate runs the PRODUCTION view (preview mode) over 10
replayed dates x 2 payload scenarios and captures its complete JSON response;
the pre-refactor capture and the post-refactor capture must be equal key for
key, list element for list element, float for float.

Why the whole response and not just the assignment map: the response is a
strict superset of the (assignments, warnings, moves) triple the extraction
returns — it also carries the per-driver slot ordering, span readouts, the
residual list and all four advisors, every one of which reads pipeline state.
If any of that moves, the diff shows it.

Direction: this gate is two-sided. ANY difference fails. The refactor is not
allowed to be "better".

METHOD (the 12/13 technique)
  Phase A: raw side — the read-only snapshot (GRAYSON_SNAPSHOT_DB may point at
    a frozen copy when a dev server is writing to the live one); derive the
    current regime from the data, pick 10 evenly-spaced dates. No date literals.
  Phase B: a throwaway COPY of the snapshot, migrated to the current schema;
    django.setup() with RUN_SCHEDULERS_IN_WEB=0 and
    ROUTE_DISTANCE_INLINE_RESOLVER=False — nothing here may bill a Google call
    or write anywhere but the temp copy. The view is called through a
    RequestFactory with a staff user: the real function, no URL/CSRF layer.
  Phase C: canonicalise + dump. With --baseline, deep-diff against a previous
    capture and exit non-zero on the first difference.

USAGE
  # before the refactor
  python docs/scheduling-redesign/analysis/14_pipeline_parity.py --tag before
  # after the refactor
  python docs/scheduling-redesign/analysis/14_pipeline_parity.py --tag after \
      --baseline docs/scheduling-redesign/analysis/out/14_pipeline_parity_before.json

Scenarios per date (all preview / apply=False — the gate never writes to
anything but the throwaway copy):
  bare       — no driver_hours: the view falls back to each driver's saved
               availability, against the date's REAL finished board. Realistic,
               but on a built date it leaves the passes almost nothing to do.
  modal      — driver_hours echoed back from saved availability, the two
               lowest-id drivers "build first", an explicit min_buffer.
               Exercises build-first seeding and the modal window gate.
  cold       — THE LOAD-BEARING ONE. Every driver assignment for the date is
               cleared on the throwaway copy first, so the day arrives exactly
               as Build 3 will meet it: ~108 legs, nothing assigned. All eight
               passes run at full stretch — greedy, swap recovery, evict-to-farm,
               span rescue, span trim, gap compaction, the free-insertion sweep,
               and all four advisors on a fully-populated board.
  cold_modal — cold, plus the modal payload (build-first seeding on a cold day).
Order is fixed: the two board-relative scenarios run before the clear.

The apply branch is deliberately NOT exercised: the refactor leaves its write
loop untouched, and every input it reads (final_assignments, legs_by_id,
drivers_by_id, unassigned, evict moves) is proven identical by the preview
capture. tests_assignment_pipeline.py covers the apply path directly.
"""
import argparse
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

sys.path.insert(0, C.REPO_ROOT)

N_DATES = 10

ASSUMPTIONS = (
    "Preview mode only — the refactor does not touch the apply write loop, and "
    "every value that loop reads is in the preview capture.",
    "Both captures start from a FRESH copy of the same snapshot and walk the "
    "dates in the same order, so route-distance pending rows written on a cache "
    "miss land identically in both runs.",
    "Determinism rests on int-keyed dicts/sets and sorted iteration; no "
    "wall-clock or random input reaches preview mode.",
)


def django_on_copy():
    """Boot Django against a throwaway migrated copy of the snapshot."""
    tmp = os.environ.get("PIPELINE_GATE_TMP") or tempfile.mkdtemp(prefix="pipeline_gate_")
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


def staff_user():
    from django.contrib.auth import get_user_model
    U = get_user_model()
    u = U.objects.filter(is_staff=True, is_active=True).order_by("id").first()
    if u is None:
        u = U.objects.create(username="_pipeline_gate", is_staff=True, is_active=True)
    return u


def call_view(user, payload):
    """Run the production view with a synthetic POST. Returns the parsed JSON."""
    from django.test import RequestFactory
    from dispatching.views import auto_assign_drivers
    req = RequestFactory().post(
        "/dispatching/auto-assign-drivers/",
        data=json.dumps(payload), content_type="application/json")
    req.user = user
    resp = auto_assign_drivers(req)
    return json.loads(resp.content.decode("utf-8"))


def _modal_payload(day):
    """driver_hours echoed from each eligible driver's saved availability."""
    from drivers.models import Driver, DriverVehicleAssignment

    eligible = sorted(DriverVehicleAssignment.objects
                      .filter(date=day, driver__driver_type="inhouse")
                      .values_list("driver_id", flat=True))
    drivers = {d.id: d for d in Driver.objects.filter(
        id__in=eligible, driver_type="inhouse", is_active=True)}
    hours = {}
    for did in eligible:
        d = drivers.get(did)
        if d is None:
            continue
        is_avail, sh, eh, _pref, flex = d.get_availability_for_date(day)
        if not is_avail:
            continue
        hours[str(did)] = {"start": int(sh), "end": int(eh), "flexible": bool(flex)}
    return {
        "date": day.isoformat(),
        "apply": False,
        "driver_hours": hours,
        "build_first": [int(k) for k in sorted(hours, key=int)[:2]],
        "min_buffer": 5,
    }


def clear_day(day):
    """Strip every driver assignment for the date ON THE THROWAWAY COPY, so the
    day arrives at the pipeline exactly as Build 3 will meet it. A queryset
    .update() — no save(), no signals, no history rows; deterministic."""
    from reservations.models import Leg
    return (Leg.objects.filter(pickup_date=day, driver__isnull=False)
            .update(driver=None))


def scenarios(day):
    """(name, payload, clear_first) per gated date, in fixed order."""
    yield "bare", {"date": day.isoformat(), "apply": False}, False
    yield "modal", _modal_payload(day), False
    yield "cold", {"date": day.isoformat(), "apply": False}, True
    yield "cold_modal", _modal_payload(day), False


def check_invariants(day):
    """Standing invariants the JSON diff alone cannot see.

    REST-PENALTY LIVENESS. The overnight-rest scan inside the pipeline sits in
    a bare ``except Exception`` whose try block contains two imports. An import
    cycle introduced by any future refactor is an Exception, so it would be
    swallowed silently: ``prev_end_by_driver`` empties, the rest penalty drops
    out of the greedy scorer and the Rest Advisor goes quiet — with no log line
    and no test failure. A module extraction out of ``views.py`` is exactly the
    change that creates such a cycle, so the gate asserts liveness directly
    rather than trusting the silence.

    Returns a list of problem strings (empty = clean).
    """
    from datetime import timedelta

    from drivers.models import Driver, DriverVehicleAssignment
    from reservations.models import Leg
    from dispatching.assignment_pipeline import (
        PipelineLocks, PipelineWindows, run_assignment_pipeline,
    )

    probs = []
    prev_day = day - timedelta(days=1)
    eligible = set(DriverVehicleAssignment.objects
                   .filter(date=day, driver__driver_type="inhouse")
                   .values_list("driver_id", flat=True))
    drivers = list(Driver.objects.filter(
        driver_type="inhouse", is_active=True, id__in=eligible)
        .select_related("profile").prefetch_related("weekly_schedule", "date_overrides"))
    hours, flexible = {}, set()
    for d in drivers:
        is_avail, sh, eh, _p, flex = d.get_availability_for_date(day)
        if is_avail:
            hours[d.id] = (sh, eh)
            if flex:
                flexible.add(d.id)
    drivers = [d for d in drivers if d.id in hours]
    worked_yesterday = set(
        Leg.objects.filter(pickup_date=prev_day, driver_id__in=hours)
        .exclude(status="cancelled").values_list("driver_id", flat=True))
    if not worked_yesterday:
        return probs                      # nothing to assert on this date

    legs = list(Leg.objects.filter(pickup_date=day)
                .exclude(reservation__status="cancelled").exclude(status="cancelled")
                .select_related("driver", "reservation", "vehicle", "flight_information")
                .prefetch_related("legstop_set", "legflight_set"))
    res = run_assignment_pipeline(
        legs, drivers, day,
        PipelineWindows(driver_hours=hours, flexible_drivers=flexible),
        PipelineLocks())
    got = set(res.prev_end_by_driver)
    if not got:
        probs.append(
            f"{day}: prev_end_by_driver is EMPTY but {len(worked_yesterday)} "
            f"working drivers had legs on {prev_day} — the overnight-rest scan "
            f"is being swallowed by its except Exception (import cycle?)")
    elif not (got & worked_yesterday):
        probs.append(
            f"{day}: prev_end_by_driver covers {sorted(got)[:5]} but none of the "
            f"{len(worked_yesterday)} drivers who actually worked {prev_day}")
    return probs


def canon(obj):
    """Canonical form: floats rounded to 6 dp so a formatting-neutral refactor
    is not failed by the last bit of a float repr, everything else verbatim."""
    if isinstance(obj, float):
        return round(obj, 6)
    if isinstance(obj, dict):
        return {k: canon(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [canon(v) for v in obj]
    return obj


def diff(a, b, path="$"):
    """First differences between two canonical captures (bounded)."""
    out = []
    if type(a) is not type(b):
        return [f"{path}: type {type(a).__name__} -> {type(b).__name__}"]
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"{path}.{k}: ADDED = {b[k]!r}")
            elif k not in b:
                out.append(f"{path}.{k}: REMOVED (was {a[k]!r})")
            else:
                out.extend(diff(a[k], b[k], f"{path}.{k}"))
            if len(out) > 40:
                return out
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f"{path}: length {len(a)} -> {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            out.extend(diff(x, y, f"{path}[{i}]"))
            if len(out) > 40:
                return out
    elif a != b:
        out.append(f"{path}: {a!r} -> {b!r}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="before", help="names the capture file")
    ap.add_argument("--baseline", default=None, help="capture to diff against")
    ap.add_argument("--dates", type=int, default=N_DATES)
    args = ap.parse_args()

    t0 = time.time()
    con = C.connect()
    h = C.Horizon(con)
    C.preamble("14_pipeline_parity.py",
               "Build 3a P1: run_assignment_pipeline extraction is byte-identical",
               h, ASSUMPTIONS)

    byday = C.legs_per_day(con)
    scan_from = dt.date.fromisoformat(min(byday))
    segs = C.changepoints(byday, scan_from, h.today, min_seg=28, min_effect=0.08)
    cur_a = segs[-1][0]
    cur_b = min(segs[-1][1], h.last_actuals_day)
    all_days = [cur_a + dt.timedelta(days=i) for i in range((cur_b - cur_a).days + 1)]
    n = args.dates
    picks = sorted({all_days[round(i * (len(all_days) - 1) / (n - 1))] for i in range(n)})
    print(f"\nregime {cur_a}..{cur_b}; gated dates ({len(picks)}, evenly spaced): "
          + ", ".join(str(d) for d in picks))
    con.close()

    django_on_copy()
    user = staff_user()
    print(f"acting as staff user id={user.id}")

    capture = {}
    for day in picks:
        for name, payload, clear_first in scenarios(day):
            if clear_first:
                n_cleared = clear_day(day)
                print(f"  {day} -- cleared {n_cleared} driver assignments "
                      f"on the throwaway copy (cold start)")
            t1 = time.time()
            try:
                body = call_view(user, payload)
            except Exception as exc:                      # noqa: BLE001
                body = {"__exception__": f"{type(exc).__name__}: {exc}"}
            capture[f"{day.isoformat()}/{name}"] = canon(body)
            n_drv = len(body.get("driver_schedules") or [])
            print(f"  {day} {name:10s} assigned={body.get('assigned')!s:>4} "
                  f"remaining={body.get('remaining')!s:>4} drivers={n_drv:>3} "
                  f"adv={len(body.get('advisor') or []):>2} "
                  f"({time.time() - t1:.1f}s)")

    C.ensure_out()
    path = os.path.join(C.OUT_DIR, f"14_pipeline_parity_{args.tag}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(capture, f, indent=1, sort_keys=True, default=str)
    print(f"\nWrote: {os.path.relpath(path, C.REPO_ROOT)}")

    C.hdr("GATE — pipeline extraction parity  [measured]")
    print(f"dates gated     : {len(picks)}")
    print(f"captures        : {len(capture)} (4 scenarios/date)")
    total_legs = sum(v.get("total") or 0 for v in capture.values())
    total_assigned = sum(v.get("assigned") or 0 for v in capture.values())
    print(f"legs seen       : {total_legs}")
    print(f"legs assigned   : {total_assigned}")
    exc = [k for k, v in capture.items() if "__exception__" in v]
    if exc:
        print(f"VIEW RAISED on  : {exc}")

    inv = []
    for day in picks:
        try:
            inv.extend(check_invariants(day))
        except Exception as e:                            # noqa: BLE001
            inv.append(f"{day}: invariant check itself raised {type(e).__name__}: {e}")
    print(f"invariants      : {'OK on all dates' if not inv else f'{len(inv)} PROBLEM(S)'}")
    for p in inv:
        print(f"  {p}")

    if not args.baseline:
        print("\nVERDICT: capture only (no --baseline given). Re-run after the "
              "refactor with --baseline pointing at this file.")
        print(f"runtime: {time.time() - t0:.1f}s")
        if inv:
            raise SystemExit(1)
        return

    with open(args.baseline, encoding="utf-8") as f:
        base = json.load(f)
    base = canon(base)
    diffs = diff(base, capture)
    print(f"baseline        : {os.path.relpath(args.baseline, C.REPO_ROOT)}")
    print(f"differences     : {len(diffs)}")
    for d in diffs[:40]:
        print(f"  {d}")
    ok = not diffs and not inv
    print("\nVERDICT:",
          "PASS — byte-identical across every gated date/scenario, invariants hold"
          if ok else
          "FAIL — the extraction changed observable output or broke an invariant")
    print(f"runtime: {time.time() - t0:.1f}s")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
