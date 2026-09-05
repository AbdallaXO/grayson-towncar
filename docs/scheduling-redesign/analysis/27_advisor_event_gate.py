#!/usr/bin/env python
"""27 — Can a log of advisor cards be a faithful instrument? (Phase 1.2 gate)

WHAT IS ABOUT TO BE BUILT (06_DAY_MANAGER §3.4, Phase 1.2)
----------------------------------------------------------
``AdvisorEvent`` — "one row per card lifecycle: shown (first fingerprint), plan
applied / snoozed / task filed / expired / superseded, and the realised outcome
(filled nightly ...). This makes the precision number stay true in production
and is the D14 trust ledger."

That sentence contains four assumptions, none of them yet measured, and each one
decides a column of the table:

  1. A CARD HAS A LIFECYCLE — one birth, one death, one row. If the same card id
     dies and is reborn three times a day, "one row per lifecycle" is either a
     lie or a different key.
  2. A CARD IS ONE THING WHILE IT LIVES. If severity, basis or the impact leg
     changes under a stable id, a single row has to choose which version it
     holds, and the choice changes the precision number the row later reports.
  3. THE IMPACT LEG IS STABLE. The realised outcome is scored on ``leg_ids[-1]``
     (§3.3). If that moves mid-life, the outcome fill grades the wrong trip.
  4. THE LOG WILL SEE THE CARDS. The rail is superuser-only
     (``advisor_views.advisor_visible_to``) and nothing else computes advisor
     state, so a log fed only by the rail records what one person happened to
     have open. Any unattended feed must ride an existing loop — the Samsara
     sweep (180 s) or the GHL loop (1800 s) — and a sampler only sees cards that
     are alive when it looks.

This script measures all four BEFORE the model exists, which is the house rule
(§5: "the gate script is written and the baseline captured before the code it
judges"). §3.2, §3.3 and §3.4 all record assumptions this project asserted and
then had to withdraw; this is the same discipline applied one ticket earlier.

WHAT IT DOES
------------
Reuses 23's replay harness verbatim — ``load_pristine`` / ``rewind`` /
``restore`` / ``build_truth``, imported as a module so no rewind rule is
re-implemented here — and replays the same dates on a FINE grid (default every
3 minutes, the Samsara tick, versus 23's 15). Every card sighting is recorded
with its class, basis, impact leg and impact moment.

From that record:

  EPISODES      contiguous runs of sightings for one id on one date. Two runs
                separated by a gap are two episodes — the card left the rail and
                came back. Reported per kind, with the gap distribution.
  MUTATION      within one episode, how often severity / basis / kind / impact
                leg / impact moment change under the same id.
  LIFETIME      minutes from first to last sighting of an episode. This is what
                decides whether a 30-minute sampler is a log or a lottery.
  COVERAGE      for each candidate sampling cadence, the share of episodes a
                sampler would see AT ALL — averaged over every phase offset,
                because a real loop's tick is not aligned to anything.
  BIAS          the precision (>15 min) of the cards a coarse sampler sees,
                against the precision of all of them. A log that systematically
                misses short-lived cards reports a number that is not the
                advisor's number, and D5 would be judged on the wrong sample.

SCORING is 23's, unchanged: the impact leg is the last id in ``leg_ids``, truth
is ``pickup_policy.pickup_deadline`` against the on-location tap with 19's
batch-tap rule, and cards whose impact leg has no usable tap are counted as
UNSCORABLE, never dropped. That last number is this instrument's ceiling: it is
the share of rows the nightly fill can never resolve.

  --verify-fill  the second half of the gate, runnable only AFTER the code
                 exists: calls the shipped ``advisor_events.leg_lateness``
                 against 23's ``build_truth`` on the same legs and reports any
                 disagreement. A live precision number that does not reproduce
                 the replay's is not comparable with it, and comparing them is
                 the whole point of the log.

METHOD
------
Read-only against the snapshot; Django runs on the throwaway migrated copy
(``17_build3_gate.django_on_copy``). GPS is blanked at every tick by 23's
rewind, exactly as there, so GPS-based classes are absent from these numbers
too — a stated blind spot, and the reason Phase 1.3 exists.

USAGE
  venv/bin/python docs/scheduling-redesign/analysis/27_advisor_event_gate.py \
      [--days 28] [--tick-min 3] [--from-hour 6] [--to-hour 23] [--verify-fill]

Outputs: out/27_card_episodes.csv      one row per (date, id, episode)
         out/27_identity_stability.csv episodes and mutation rates per kind
         out/27_cadence_coverage.csv   what each sampling cadence would see
         out/27_fill_parity.csv        --verify-fill only
"""
import argparse
import datetime as dt
import os
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

sys.path.insert(0, C.REPO_ROOT)

LATE_BAR = 15
# The cadences a log could actually be fed at. 3 = the Samsara sweep
# (samsara_scheduler, 180 s); 30 = the GHL loop (ghl_integration/scheduler,
# 1800 s) that Phase 1.2 names for the nightly fill; 60 = a slower ride on the
# same loop. 15 is 23's own tick, included so this table is readable against it.
CADENCES = (3, 6, 15, 30, 60)

ASSUMPTIONS = (
    "The replay harness is 23's, imported as a module — same rewind, same "
    "restore, same truth. Nothing about what the system knew at a tick is "
    "re-implemented here, so any error in the rewind is an error 23 already has "
    "and the two are directly comparable.",
    "GPS (dispatch_*) is blanked at every tick, as in 23: the Samsara sweep "
    "keeps no history. GPS-based classes therefore do not appear in these "
    "counts and their episode shape is UNMEASURED, not measured as zero.",
    "An EPISODE is a maximal run of consecutive grid ticks holding the same card "
    "id. One skipped tick ends it. On a 3-minute grid that is a strict reading — "
    "a card that flickers off for one tick counts as two episodes — and it is "
    "deliberately strict, because a log keyed on (date, id) would silently merge "
    "exactly those cases.",
    "Coverage is averaged over EVERY phase offset of the sampling grid. A real "
    "background loop's tick is not aligned to the hour and drifts with restarts, "
    "so the aligned best case would flatter every cadence.",
    "The observation window is 06:00-23:00, 23's window. Cards born outside it "
    "are outside this measurement, not absent from the day.",
)


def load_module(name, fname):
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(name, os.path.join(here, fname))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# --------------------------------------------------------------------------
# replay — 23's rewind, our collector
# --------------------------------------------------------------------------

def replay_date(con, cur, g23, g19, day, ticks, sightings, truth, kinds, timing):
    """Record every card sighting on the fine grid for one date."""
    from django.utils import timezone as djtz
    from dispatching.conflict_advisor import compute_advisor_state

    snap = g23.load_pristine(con, day)
    if not snap:
        return 0
    g23.restore(cur, snap)
    truth[day] = g23.build_truth(con, g19, day, snap["leg_ids"], kinds)
    n = 0
    for tick in sorted(ticks, reverse=True):        # descending: taps only shrink
        g23.rewind(cur, snap, tick + dt.timedelta(hours=C.utc_offset_hours(tick)),
                   False)
        t0 = time.time()
        try:
            state = compute_advisor_state(day, now=djtz.make_aware(tick))
        except Exception as exc:
            timing.append([str(day), tick.strftime("%H:%M"), -1,
                           type(exc).__name__])
            continue
        timing.append([str(day), tick.strftime("%H:%M"),
                       int((time.time() - t0) * 1000), ""])
        for c in state["disruptions"]:
            legs = c.get("leg_ids") or []
            sightings[(day, c["id"])].append({
                "tick": tick,
                "kind": c["kind"],
                "severity": c["severity"],
                "basis": c.get("basis") or "",
                "impact_leg": legs[-1] if legs else None,
                "n_legs": len(legs),
                "impact_at": c.get("impact_at") or "",
                "has_plans": bool(c.get("plans")),
                "detected_only": bool(c.get("detected_only")),
            })
            n += 1
    g23.restore(cur, snap)
    return n


# --------------------------------------------------------------------------
# episodes
# --------------------------------------------------------------------------

MUTABLE = ("kind", "severity", "basis", "impact_leg", "n_legs", "impact_at")


def episodes_for(seen, step_min):
    """Split one card's sightings into maximal runs of consecutive grid ticks."""
    seen = sorted(seen, key=lambda s: s["tick"])
    runs, cur_run = [], [seen[0]]
    for prev, s in zip(seen, seen[1:]):
        if (s["tick"] - prev["tick"]).total_seconds() > step_min * 60 + 1:
            runs.append(cur_run)
            cur_run = []
        cur_run.append(s)
    runs.append(cur_run)
    return runs


def build_episodes(sightings, truth, kinds, step_min):
    out = []
    for (day, cid), seen in sightings.items():
        runs = episodes_for(seen, step_min)
        for i, run in enumerate(runs):
            first, last = run[0], run[-1]
            mutations = {f: len({s[f] for s in run}) - 1 for f in MUTABLE}
            impact = last["impact_leg"]
            late, quality = truth.get(day, {}).get(impact, (None, "unknown"))
            out.append({
                "date": str(day), "id": cid, "episode": i + 1,
                "episodes_on_id": len(runs),
                "kind": first["kind"], "severity": first["severity"],
                "basis": first["basis"],
                "severity_end": last["severity"], "basis_end": last["basis"],
                "first_tick": first["tick"].strftime("%H:%M"),
                "last_tick": last["tick"].strftime("%H:%M"),
                "n_ticks": len(run),
                "lifetime_min": int((last["tick"] - first["tick"]).total_seconds() / 60),
                "impact_leg": impact,
                "impact_leg_first": first["impact_leg"],
                "n_legs": last["n_legs"],
                "self_scored": 1 if last["n_legs"] <= 1 else 0,
                "has_plans": 1 if any(s["has_plans"] for s in run) else 0,
                "late_min": late,
                "quality": quality,
                "scorable": 1 if late is not None else 0,
                "late_15": (1 if (late is not None and late > LATE_BAR) else
                            (0 if late is not None else None)),
                "impact_trip": kinds.get(impact, "?"),
                "ticks": [s["tick"] for s in run],
                **{f"mut_{f}": mutations[f] for f in MUTABLE},
            })
    out.sort(key=lambda e: (e["date"], e["first_tick"], e["id"], e["episode"]))
    return out


# --------------------------------------------------------------------------
# cadence coverage
# --------------------------------------------------------------------------

def coverage_rows(episodes, step_min, from_hour):
    """For each cadence, averaged over every phase offset: what share of
    episodes a sampler sees at all, how many sightings it logs, and the
    precision of the sample it ends up with."""
    rows = []
    all_scorable = [e for e in episodes if e["scorable"]]
    base_prec = (100.0 * sum(e["late_15"] for e in all_scorable) / len(all_scorable)
                 if all_scorable else None)
    for cad in CADENCES:
        if cad % step_min:
            continue
        stride = cad // step_min
        seen_shares, prec_vals, sightings_per_day, seen_scorable_n = [], [], [], []
        dates = {e["date"] for e in episodes}
        for phase in range(stride):
            seen, sight = [], 0
            for e in episodes:
                # A sampler at this phase sees the episode iff any of its ticks
                # falls on the sampling grid. Grid index is minutes-since-06:00
                # divided by the fine step.
                hit = [t for t in e["ticks"]
                       if ((t.hour * 60 + t.minute - from_hour * 60) // step_min)
                       % stride == phase]
                if hit:
                    seen.append(e)
                    sight += len(hit)
            seen_shares.append(100.0 * len(seen) / len(episodes) if episodes else 0.0)
            sightings_per_day.append(sight / max(1, len(dates)))
            sc = [e for e in seen if e["scorable"]]
            seen_scorable_n.append(len(sc))
            if sc:
                prec_vals.append(100.0 * sum(e["late_15"] for e in sc) / len(sc))
        rows.append({
            "cadence_min": cad,
            "pct_episodes_seen": round(sum(seen_shares) / len(seen_shares), 1),
            "pct_episodes_seen_worst": round(min(seen_shares), 1),
            "sightings_per_day": round(sum(sightings_per_day) / len(sightings_per_day), 1),
            "scorable_seen_mean": round(sum(seen_scorable_n) / len(seen_scorable_n), 1),
            "pct_late_15_seen": (round(sum(prec_vals) / len(prec_vals), 1)
                                 if prec_vals else None),
            "pct_late_15_all": round(base_prec, 1) if base_prec is not None else None,
            "bias_points": (round(sum(prec_vals) / len(prec_vals) - base_prec, 1)
                            if prec_vals and base_prec is not None else None),
        })
    return rows


def stability_rows(episodes):
    by_kind = defaultdict(list)
    for e in episodes:
        by_kind[e["kind"]].append(e)
    by_kind["ALL"] = episodes
    rows = []
    for kind, es in by_kind.items():
        ids = {(e["date"], e["id"]) for e in es}
        multi = len({(e["date"], e["id"]) for e in es if e["episodes_on_id"] > 1})
        n = len(es)
        life = [e["lifetime_min"] for e in es]
        rows.append({
            "kind": kind,
            "episodes": n,
            "unique_ids": len(ids),
            "pct_ids_multi_episode": round(100.0 * multi / len(ids), 1) if ids else 0.0,
            "life_p10": C.pct(life, 10), "life_p50": C.pct(life, 50),
            "life_p90": C.pct(life, 90), "life_max": max(life) if life else 0,
            "pct_life_under_30": round(
                100.0 * sum(1 for v in life if v < 30) / n, 1) if n else 0.0,
            **{f"pct_mut_{f}": round(100.0 * sum(1 for e in es if e[f"mut_{f}"]) / n, 1)
               for f in MUTABLE},
            "pct_scorable": round(100.0 * sum(e["scorable"] for e in es) / n, 1) if n else 0.0,
        })
    rows.sort(key=lambda r: (r["kind"] == "ALL", -r["episodes"]))
    return rows


# --------------------------------------------------------------------------
# --verify-fill — the shipped outcome function against 23's truth
# --------------------------------------------------------------------------

def verify_fill(con, g23, g19, dates):
    """Does the SHIPPED nightly-fill lateness function reproduce 23's truth?

    A live precision number computed a different way is not comparable with the
    replay's, and comparability is the only reason the log exists."""
    from dispatching import advisor_events
    rows, disagree, n_legs = [], 0, 0
    for day in dates:
        snap = g23.load_pristine(con, day)
        if not snap:
            continue
        kinds = {}
        truth = g23.build_truth(con, g19, day, snap["leg_ids"], kinds)
        shipped = advisor_events.leg_lateness(snap["leg_ids"])
        for leg_id in snap["leg_ids"]:
            want_min, want_q = truth.get(leg_id, (None, "unknown"))
            got = shipped.get(leg_id)
            got_min = got.late_min if got is not None else None
            got_q = got.quality if got is not None else "unknown"
            same = (want_min == got_min) and (want_q == got_q)
            n_legs += 1
            if not same:
                disagree += 1
                rows.append([str(day), leg_id, want_min, want_q, got_min, got_q])
    return rows, disagree, n_legs


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--dates", nargs="*", default=[])
    ap.add_argument("--tick-min", type=int, default=3)
    ap.add_argument("--from-hour", type=int, default=6)
    ap.add_argument("--to-hour", type=int, default=23)
    ap.add_argument("--verify-fill", action="store_true")
    args = ap.parse_args()
    t0 = time.time()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    con = C.connect()
    hz = C.Horizon(con)
    C.preamble("27_advisor_event_gate.py",
               "can a log of advisor cards be a faithful instrument?",
               hz, ASSUMPTIONS)

    g23 = load_module("replay23", "23_advisor_replay.py")
    g19 = load_module("clock19", "19_clock_calibration.py")
    g17 = load_module("gate17", "17_build3_gate.py")
    dates = g23.pick_dates(con, hz, args.days, args.dates)
    ticks = g23.ticks_for(dates[0], args.from_hour, args.to_hour, args.tick_min)
    print(f"\ndates ({len(dates)}): {dates[0]} .. {dates[-1]}")
    print(f"grid        : {args.from_hour:02d}:00-{args.to_hour:02d}:00 every "
          f"{args.tick_min} min = {len(ticks)}/day "
          f"({len(dates) * len(ticks)} advisor computes)")
    g17.django_on_copy()
    from django.db import connection as dj
    cur = dj.cursor()

    if args.verify_fill:
        C.sub("FILL PARITY — the shipped outcome function against 23's truth")
        rows, n_bad, n_legs = verify_fill(con, g23, g19, dates)
        print(f"  {n_legs} legs compared over {len(dates)} dates; "
              f"disagreements: {n_bad}")
        if rows:
            print(f"{'date':<12}{'leg':>8}{'23 min':>10}{'23 q':>10}"
                  f"{'fill min':>10}{'fill q':>10}")
            for r in rows[:40]:
                print(f"{r[0]:<12}{r[1]:>8}{str(r[2]):>10}{r[3]:>10}"
                      f"{str(r[4]):>10}{str(r[5]):>10}")
        p = C.write_csv("27_fill_parity.csv",
                        ["date", "leg_id", "replay_late_min", "replay_quality",
                         "fill_late_min", "fill_quality"], rows)
        print(f"\nWrote: {os.path.relpath(p, C.REPO_ROOT)}")
        print(f"\n  GATE: the fill reproduces the replay exactly."
              if not n_bad else
              f"\n  GATE FAILED: {n_bad} legs scored differently. The live number "
              f"would not be\n  comparable with §3.3's, which is the only reason "
              f"the log exists.")
        print(f"runtime: {time.time() - t0:.1f}s")
        return

    sightings, truth, kinds, timing = defaultdict(list), {}, {}, []
    for i, day in enumerate(dates, 1):
        t1 = time.time()
        n = replay_date(con, cur, g23, g19, day,
                        g23.ticks_for(day, args.from_hour, args.to_hour,
                                      args.tick_min),
                        sightings, truth, kinds, timing)
        print(f"  [{i:>2}/{len(dates)}] {day}  {n:>5} sightings  "
              f"{time.time() - t1:6.1f}s", flush=True)

    episodes = build_episodes(sightings, truth, kinds, args.tick_min)
    n_ids = len(sightings)

    # ── 1. identity ─────────────────────────────────────────────────────────
    C.sub("IDENTITY — is 'one row per card lifecycle' a thing that exists?")
    srows = stability_rows(episodes)
    print(f"{'kind':<16}{'eps':>7}{'ids':>7}{'>1 ep%':>8}{'life P10':>10}"
          f"{'P50':>7}{'P90':>7}{'max':>7}{'<30m%':>8}{'scor%':>8}")
    for r in srows:
        print(f"{r['kind']:<16}{r['episodes']:>7}{r['unique_ids']:>7}"
              f"{r['pct_ids_multi_episode']:>8.1f}{r['life_p10']:>10.0f}"
              f"{r['life_p50']:>7.0f}{r['life_p90']:>7.0f}{r['life_max']:>7}"
              f"{r['pct_life_under_30']:>8.1f}{r['pct_scorable']:>8.1f}")
    print(f"\n  {n_ids} unique (date, id) pairs -> {len(episodes)} episodes "
          f"({len(episodes) / max(1, len(dates)):.1f}/day). A row keyed on "
          f"(date, id) alone\n  merges every episode above the first.")

    C.sub("MUTATION — does a card stay the same card while it is alive?")
    print(f"{'kind':<16}" + "".join(f"{f:>14}" for f in MUTABLE))
    for r in srows:
        print(f"{r['kind']:<16}"
              + "".join(f"{r[f'pct_mut_{f}']:>13.1f}%" for f in MUTABLE))
    print(f"\n  Percent of EPISODES in which the field changed at least once under "
          f"a stable id.\n  impact_leg is the one that decides the outcome fill: it "
          f"is what leg_ids[-1]\n  points at, and it is the trip the nightly job "
          f"grades.")

    # ── 2. coverage ─────────────────────────────────────────────────────────
    C.sub("COVERAGE — what an unattended sampler on an existing loop would see")
    crows = coverage_rows(episodes, args.tick_min, args.from_hour)
    print(f"{'every':<10}{'episodes seen':>15}{'worst phase':>13}"
          f"{'sightings/day':>15}{'scorable':>10}{'>15 seen':>10}"
          f"{'>15 all':>9}{'bias':>8}")
    for r in crows:
        b = f"{r['bias_points']:+.1f}" if r["bias_points"] is not None else "-"
        print(f"{str(r['cadence_min']) + ' min':<10}"
              f"{r['pct_episodes_seen']:>14.1f}%{r['pct_episodes_seen_worst']:>12.1f}%"
              f"{r['sightings_per_day']:>15.1f}{r['scorable_seen_mean']:>10.0f}"
              f"{(r['pct_late_15_seen'] if r['pct_late_15_seen'] is not None else 0):>9.1f}%"
              f"{(r['pct_late_15_all'] if r['pct_late_15_all'] is not None else 0):>8.1f}%"
              f"{b:>8}")
    print(f"\n  3 min is the Samsara sweep, 30 min the GHL loop Phase 1.2 names. "
          f"'bias' is how\n  many points the sampled precision differs from the "
          f"precision of every card —\n  a log that misses short-lived cards "
          f"reports a number that is not the advisor's.")

    # ── 3. the fill's ceiling ───────────────────────────────────────────────
    C.sub("THE FILL'S CEILING — how many rows can ever be resolved")
    q = Counter(e["quality"] for e in episodes)
    tot = len(episodes)
    for k, v in q.most_common():
        print(f"  {k:<12}{v:>6}{100.0 * v / tot:>8.1f}%")
    scor = [e for e in episodes if e["scorable"]]
    print(f"\n  {len(scor)}/{tot} episodes ({100.0 * len(scor) / tot:.1f}%) have an "
          f"impact leg with a usable\n  on-location tap. The rest can never be "
          f"graded by any nightly job, and a\n  precision denominator that quietly "
          f"drops them would overstate the tool.")

    ok = [r[2] for r in timing if r[2] >= 0]
    errs = Counter(r[3] for r in timing if r[3])
    C.sub("COMPUTE TIME (budget is ADVISOR_BUDGET_MS = 4000)")
    if ok:
        print(f"  n={len(ok)}  P50 {C.pct(ok, 50):.0f} ms  P90 {C.pct(ok, 90):.0f} ms  "
              f"max {max(ok)} ms  over budget: "
              f"{100.0 * sum(1 for v in ok if v > 4000) / len(ok):.1f}%")
    if errs:
        print(f"  errors: {dict(errs)}")

    ecols = [c for c in episodes[0].keys() if c != "ticks"] if episodes else []
    p1 = C.write_csv("27_card_episodes.csv", ecols,
                     [[e[c] for c in ecols] for e in episodes])
    scols = list(srows[0].keys()) if srows else []
    p2 = C.write_csv("27_identity_stability.csv", scols,
                     [[r[c] for c in scols] for r in srows])
    ccols = list(crows[0].keys()) if crows else []
    p3 = C.write_csv("27_cadence_coverage.csv", ccols,
                     [[r[c] for c in ccols] for r in crows])
    for p in (p1, p2, p3):
        print(f"\nWrote: {os.path.relpath(p, C.REPO_ROOT)}")
    print(f"runtime: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
