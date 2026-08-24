#!/usr/bin/env python
"""13 — Build 2 acceptance gate (04 §5): the Day Setup panel vs analysis/10.

THE CLAIM THIS SCRIPT TESTS
---------------------------
On 10 replayed dates, every standby second-shift proposal the panel makes is
one the analysis/10 replay would ACCEPT — pool membership, car eligibility,
leg provenance (farmed / shed by the 13.5h cap), strict co-driver geometry,
and both-side rest all verified against 10's own raw-sqlite state, NOT the
ORM state the panel computed from. Panel and script share one engine
(dispatching/standby_mints.py, extracted byte-identically from 10), so what
this gate actually hunts is DATA-PLUMBING drift: the two sides assemble their
inputs from different stacks (ORM vs raw SQL), and every verdict here is
recomputed from the raw side.

Direction matters: the panel proposing FEWER mints than 10 is fine (it
excludes demo accounts and drops chain-RED handoffs); a proposal 10 would
reject fails the gate.

Also emitted (open-decision evidence for the founder): the green/amber/red
band distribution over every measured executed handoff (out/11_handoffs.csv)
and over the shared units the panel scored on the gated dates.

METHOD (the 12_warn_precision technique)
  Phase A: raw side — module 10's own loaders over the read-only snapshot
    (GRAYSON_SNAPSHOT_DB may point at a frozen copy when a dev server is
    writing to the live one); derive the regime, pick 10 evenly-spaced dates.
  Phase B: a throwaway COPY of the snapshot, migrated to the current schema;
    django.setup(); run the production suggest_day_setup on each gated date
    (RUN_SCHEDULERS_IN_WEB=0, ROUTE_DISTANCE_INLINE_RESOLVER=False — nothing
    here may bill a Google call or write anywhere but the temp copy).
  Phase C: re-derive each proposal's feasibility from the raw state.
"""
import csv
import datetime as dt
import importlib.util
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

sys.path.insert(0, C.REPO_ROOT)
from dispatching import handoff_chain as hc  # noqa: E402  (pure, no Django)
from dispatching import standby_mints as sm  # noqa: E402

REST_MIN = 510.0     # live rest floor (SchedulerSettings default)
GAP, BUF = 120, 30   # the central mint setting; GAP = vehicle_share_pad_min default
N_DATES = 10
EPS = 1e-9

ASSUMPTIONS = (
    "Gate direction: panel proposals must be 10-acceptable; the panel offering "
    "fewer proposals than 10 is by design (demo exclusion, RED-band filter).",
    "Rest is verified against ACTUAL adjacent state-B boards (03 §1) — the "
    "panel's convention; 10's chronological replay re-validates yesterday's "
    "replayed board, which can only be stricter on these fully-built dates.",
    "Partner geometry is verified against the CAPPED partner board "
    "(best_window at 13.5h) — exactly the boards 10's engine mints against.",
)


def load_10():
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        "mints10", os.path.join(here, "10_standby_and_mints.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def django_on_copy():
    tmp = os.environ.get("BUILD2_GATE_TMP") or tempfile.mkdtemp(prefix="build2_gate_")
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
    print("migrating the copy to the current schema (adds Build-2 fields) ...")
    orig_check = connection.check_constraints
    connection.check_constraints = lambda *a, **k: None
    try:
        call_command("migrate", verbosity=0, interactive=False)
    finally:
        connection.check_constraints = orig_check
    return tmp


def main():
    t0 = time.time()
    con = C.connect()
    h = C.Horizon(con)
    C.preamble("13_build2_gate.py",
               "Build 2 acceptance: panel proposals vs the analysis/10 replay",
               h, ASSUMPTIONS)

    m10 = load_10()
    byday = C.legs_per_day(con)
    scan_from = dt.date.fromisoformat(min(byday))
    segs = C.changepoints(byday, scan_from, h.today, min_seg=28, min_effect=0.08)
    cur_a = segs[-1][0]
    cur_b = min(segs[-1][1], h.last_actuals_day)
    all_days = [cur_a + dt.timedelta(days=i) for i in range((cur_b - cur_a).days + 1)]
    picks = sorted({all_days[round(i * (len(all_days) - 1) / (N_DATES - 1))]
                    for i in range(N_DATES)})
    print(f"\nregime {cur_a}..{cur_b}; gated dates ({len(picks)}, evenly spaced): "
          + ", ".join(str(d) for d in picks))

    D = m10.load(con, cur_a, cur_b)
    by_drv_day = m10.build_state(D["legs_all"])
    raw_leg = {l.id: l for l in D["legs_all"]}
    con.close()

    # ---- raw-side per-date state ----
    def raw_state(day):
        boards, farmed = {}, []
        for (did, dy), ls in by_drv_day.items():
            if dy != day:
                continue
            if D["dtyp"].get(did) == "inhouse":
                boards[did] = list(ls)
            elif D["dtyp"].get(did) == "affiliate":
                farmed.extend(ls)
        dva_day = D["dva"].get(day, {})
        cand = [i for i, t in D["dtyp"].items()
                if t == "inhouse" and D["active"].get(i)]
        works = {i for i in cand if by_drv_day.get((i, day))}
        off = {i for i in cand if (day, i) in D["off"]}
        pool = sm.standby_pool_ids(cand, works, dva_day, off)
        capped = {did: (sm.best_window(ls)[0] if sm.span_h(ls) > sm.SPAN_CAP_H else ls)
                  for did, ls in boards.items()}
        return boards, capped, farmed, dva_day, pool

    def rest_ok(did, day, first_start, last_end):
        prev = by_drv_day.get((did, day - dt.timedelta(days=1)))
        nxt = by_drv_day.get((did, day + dt.timedelta(days=1)))
        if prev and (first_start - max(l.end for l in prev)).total_seconds() / 60.0 < REST_MIN:
            return False
        if nxt and (min(l.start for l in nxt) - last_end).total_seconds() / 60.0 < REST_MIN:
            return False
        return True

    # ---- product side ----
    django_on_copy()
    from dispatching.day_setup import suggest_day_setup

    rows_csv, failures, n_props, n_exc = [], [], 0, 0
    shared_bands = {"green": 0, "amber": 0, "red": 0, None: 0}
    for day in picks:
        payload = suggest_day_setup(day)
        boards, capped, farmed, dva_day, pool = raw_state(day)
        farmed_ids = {l.id for l in farmed}
        veh_roster = {}
        for did, v in dva_day.items():
            veh_roster.setdefault(v, []).append(did)
        for su in payload.get("shared_units", []):
            shared_bands[su.get("handoff_band")] = \
                shared_bands.get(su.get("handoff_band"), 0) + 1

        for mp in payload.get("mint_proposals", []):
            n_props += 1
            probs = []
            did, veh = mp["driver_id"], mp["vehicle_id"]
            legs = []
            for lg in mp["legs"]:
                rl = raw_leg.get(lg["id"])
                if rl is None:
                    probs.append(f"leg {lg['id']} unknown to the raw stream")
                else:
                    legs.append(rl)
            legs.sort(key=lambda l: l.pick)

            if did not in pool:
                probs.append("driver not in 10's standby pool")
            if veh not in veh_roster:
                probs.append("vehicle has no roster row that date")
            fl = D["fleet"].get(veh)
            if not fl or not fl["active"]:
                probs.append("vehicle inactive/unknown in fleet")
            if m10.car_is_oos(D, veh, day):
                probs.append("vehicle out of service")
            if len(veh_roster.get(veh, [])) >= 2:
                probs.append(">2 drivers would share the vehicle-date")
            if fl and legs and max(l.tier for l in legs) > fl["tier"]:
                probs.append("leg tier above the vehicle's")

            for l in legs:
                if l.id in farmed_ids:
                    continue
                src_did = l.did
                if src_did is None:
                    continue   # unassigned — none exist on replayed dates
                kept = capped.get(src_did)
                if kept is None or any(k.id == l.id for k in kept):
                    probs.append(f"leg {l.id} is neither farmed nor shed by the cap")

            if legs:
                if sm.span_h(legs) > sm.SPAN_CAP_H + EPS:
                    probs.append(f"mint span {sm.span_h(legs):.2f}h over the cap")
                for a, b in zip(legs, legs[1:]):
                    if (b.start - a.end).total_seconds() / 60.0 < BUF:
                        probs.append(f"buffer under {BUF} min inside the mint")
                        break
                others = [ol for od in veh_roster.get(veh, [])
                          for ol in (capped.get(od) or [])]
                for l in legs:
                    if any(l.start < ol.end and ol.start < l.end for ol in others):
                        probs.append(f"leg {l.id} overlaps the co-driver's board")
                if others:
                    pmin = min(l.pick for l in others)
                    pmax = max(l.pick for l in others)
                    lo = (pmin - max(l.pick for l in legs)).total_seconds() / 60.0
                    hi = (min(l.pick for l in legs) - pmax).total_seconds() / 60.0
                    if lo < GAP and hi < GAP:
                        probs.append("interleaves the co-driver (gap rule)")
                if not rest_ok(did, day, min(l.start for l in legs),
                               max(l.end for l in legs)):
                    probs.append("rest floor broken vs actual adjacent boards")

            ok = not probs
            rows_csv.append([day, did, mp["driver_name"], veh, mp["side"],
                             mp["n_jobs"], mp["handoff_band"], int(ok),
                             "; ".join(probs)])
            if not ok:
                failures.append((day, mp, probs))

        for ex in payload.get("span_exceptions", []):
            n_exc += 1
            if ex["new_span"] > 15.0 + EPS:
                failures.append((day, ex, ["crunch exception over the 15.0h ceiling"]))

    C.hdr("GATE — panel proposals vs analysis/10 feasibility  [measured]")
    print(f"dates gated                : {len(picks)}")
    print(f"mint proposals checked     : {n_props}")
    print(f"crunch exceptions checked  : {n_exc}")
    print(f"proposals 10 would reject  : {len(failures)}")
    for day, mp, probs in failures[:20]:
        print(f"  FAIL {day}  {mp}")
        for p in probs:
            print(f"       - {p}")
    print("\nVERDICT:", "PASS — no proposal 10 would reject"
          if not failures else "FAIL — see above")

    C.hdr("OPEN-DECISION EVIDENCE — handoff bands  [measured/modeled]")
    print("shared units scored on the gated dates, by band:",
          {str(k): v for k, v in shared_bands.items() if v})
    hand_csv = os.path.join(C.OUT_DIR, "11_handoffs.csv")
    if os.path.exists(hand_csv):
        bands = {"green": 0, "amber": 0, "red": 0}
        with open(hand_csv, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                b = hc.handoff_band(
                    r["drop_zone"], r["pickup_zone"], float(r["obs_clear_min"]),
                    incoming_is_arrival=r["pickup_zone"] in ("MCO Terminal",
                                                             "SFB Terminal"))
                bands[b["band"]] += 1
        n = sum(bands.values())
        print(f"every measured executed handoff (n={n}) re-banded under the "
              f"shipped rule: {bands}")
        print("(a RED here is a handoff that DID run — via a fast path or a "
              "hand arrangement the base-chain model deliberately prices as "
              "unplannable)")

    p = C.write_csv("13_build2_gate.csv",
                    ["date", "driver_id", "driver", "vehicle_id", "side",
                     "n_jobs", "band", "ok", "problems"], rows_csv)
    print(f"\nWrote: {os.path.relpath(p, C.REPO_ROOT)}")
    print(f"runtime: {time.time() - t0:.1f}s")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
