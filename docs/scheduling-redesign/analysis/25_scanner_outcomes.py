#!/usr/bin/env python
"""25 — What the floor's alarm system files, and what it costs to triage.

THE QUESTION THIS ANSWERS (06_DAY_MANAGER §0.2, §1.1, §1.3)
-----------------------------------------------------------
The 30-minute ops scanner files DRIVER_CONFLICT and TIGHT_TURN tasks off its own
clock (``ops.tasks.classify_turn`` -> ``scheduler.estimate_job_end_time`` p75,
red past ``TIGHT_TURN_RED_AFTER_MIN`` = 10). It is one of THREE detectors pricing
the same turn — the board chips and the Recovery Advisor read
``board_validation.turn_slack_minutes``, the Samsara chip reads GPS — so a turn
can be green on the board and red in the task queue at the same minute.

06 ranks triaging those alarms as the single largest consumer of dispatcher time
on the day, and Phase 1.4 proposes collapsing the scanner onto the advisor's
clock. Before sizing that work, two things have to be true and measured:

  1. HOW BIG IS IT, TODAY. Not in June. Four commits between 2026-08-09 and
     2026-08-27 were aimed squarely at this noise:
         2c36aada  builder stops building chains that cannot be driven
         c04489f8  conflict tasks turn critical only when the driver won't make it
         083a7d0a  the tight-turn / conflict boundary moves to 10 minutes
         2419c414  flags come down when the conflict does
         076dfe8e  reassigning a trip clears its flag on the spot
     If they worked, part of the problem 06 exists to solve is already gone, and
     Phase 1.4 shrinks to "keep it that way".

  2. HOW MUCH OF IT IS WASTED. A task that closes because the clock moved on, or
     because the leg simply completed, cost a dispatcher a look and bought
     nothing. That share is the noise floor any replacement has to beat, and it
     is the number the D5 precision gate is ultimately arguing with.

WHAT THIS MEASURES
------------------
Per month, and per day over the most recent regime window:

  volume      tasks filed, active days, tasks per active day, distinct legs
              flagged, and that as a share of the month's live legs
  timing      minutes a task stays open (P50 / P75), and the share filed on the
              pickup date itself (a day-of alarm, not a build-time one)
  outcome     how each task ended, classified from ``resolution_notes`` — the
              scanner writes a distinct phrase for every close path:

                MOVED      a driver actually changed
                           ("...reassigned...", "...driver unassigned")
                RETIMED    the pickup moved to the flight ("Flight matched...")
                NO MOVE    the arithmetic changed under it or the leg ran out
                           ("turn no longer tight", "driver conflict resolved",
                            "conflicting leg completed", "date/time has passed")
                ESCALATED  superseded by a harder task, not a resolution
                HAND       closed by a person with no note at all
                CANCELLED  "Manually cancelled"

              NO MOVE + HAND is the waste line: a look that bought nothing.

METHOD
------
Read-only over the snapshot. ``created_at`` / ``resolved_at`` are UTC and are
converted with ``_common.to_local`` before any date is taken, so "the day a task
was filed" is the dispatcher's day. Leg counts use the standard demand filter
(``_common.live_legs_sql``) so the share-of-legs denominator matches every other
script in this package. Nothing here reads the advisor or runs the engine.

USAGE
  python docs/scheduling-redesign/analysis/25_scanner_outcomes.py [--recent 28]

Outputs: out/25_scanner_by_month.csv
         out/25_scanner_closures.csv
         out/25_scanner_by_day.csv
"""
import argparse
import datetime as dt
import os
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

SCANNER_TYPES = ("driver_conflict", "tight_turn")

# Repo events, not analysis dates: the five commits aimed at this noise. Used
# only to split the reporting window into before/after, never as a "present".
TUNING_COMMITS = (
    ("2026-08-09", "2c36aada", "builder stops building undriveable chains"),
    ("2026-08-25", "c04489f8", "conflict tasks critical only when he won't make it"),
    ("2026-08-25", "083a7d0a", "tight-turn / conflict boundary -> 10 minutes"),
    ("2026-08-27", "2419c414", "flags come down when the conflict does"),
    ("2026-08-27", "076dfe8e", "reassigning a trip clears its flag on the spot"),
)
TUNING_CUT = TUNING_COMMITS[-1][0]   # the last one; "after" means fully tuned

ASSUMPTIONS = (
    "A task's day is the LOCAL day of created_at, not the leg's pickup date. A "
    "task filed at 21:00 local about tomorrow's 06:00 pickup counts against the "
    "evening it was filed, because that is when someone had to look at it.",
    "Outcome is read from resolution_notes, which the scanner writes itself on "
    "every auto-close path. A blank note with a user on it is a human closing the "
    "task by hand and saying nothing — counted as HAND, not as a move, because "
    "the record does not show one.",
    "ESCALATED is not an outcome: a tight_turn that escalates into a "
    "driver_conflict is one problem counted twice. It is reported separately and "
    "excluded from the moved/no-move shares.",
    "Share-of-legs uses the same demand filter as 00-22 (live legs, sane dates), "
    "so it is comparable with every other legs/day figure in this package.",
)


def classify(note, resolved_by):
    """Outcome class from the scanner's own closing phrase. Order matters:
    'driver reassigned, original conflict resolved' is a MOVE, not a NO MOVE."""
    n = (note or "").strip().lower()
    if not n:
        return "HAND" if resolved_by is not None else "NO_NOTE_NO_USER"
    if "escalat" in n:
        return "ESCALATED"
    if "reassigned" in n or "unassigned" in n:
        return "MOVED"
    if "flight matched" in n:
        return "RETIMED"
    if "cancel" in n:
        return "CANCELLED"
    if ("no longer tight" in n or "conflict resolved" in n or "completed" in n
            or "has passed" in n or "resolved" in n):
        return "NO_MOVE"
    return "OTHER"


ORDER = ["MOVED", "RETIMED", "NO_MOVE", "HAND", "CANCELLED", "NO_NOTE_NO_USER",
         "OTHER", "ESCALATED"]


def load_tasks(con):
    rows = C.q(con, """
        SELECT t.id, t.task_type, t.created_at, t.resolved_at, t.status,
               t.resolution_notes, t.resolved_by_id, t.leg_id, l.pickup_date
          FROM ops_operationaltask t
          LEFT JOIN reservations_leg l ON l.id = t.leg_id
         WHERE t.task_type IN (?, ?)
    """, SCANNER_TYPES)
    out = []
    for r in rows:
        created = C.to_local(r["created_at"])
        if created is None:
            continue
        resolved = C.to_local(r["resolved_at"])
        out.append({
            "id": r["id"],
            "kind": r["task_type"],
            "day": created.date(),
            "open_min": (round((resolved - created).total_seconds() / 60.0, 1)
                         if resolved else None),
            "outcome": classify(r["resolution_notes"], r["resolved_by_id"]),
            "leg_id": r["leg_id"],
            "same_day": (str(r["pickup_date"]) == str(created.date())
                         if r["pickup_date"] else None),
        })
    return out


def shares(counter):
    """Percentages over the RESOLVED, non-escalation population."""
    base = sum(counter[k] for k in ORDER if k != "ESCALATED")
    if not base:
        return {k: 0.0 for k in ORDER}, 0
    return {k: 100.0 * counter[k] / base for k in ORDER}, base


def summarise(tasks, legs_by_day, label):
    days = sorted({t["day"] for t in tasks})
    n = len(tasks)
    active = len(days)
    legs = sum(v for d, v in legs_by_day.items() if days and days[0] <= d <= days[-1])
    flagged = len({t["leg_id"] for t in tasks if t["leg_id"]})
    opens = [t["open_min"] for t in tasks if t["open_min"] is not None]
    sd = [t["same_day"] for t in tasks if t["same_day"] is not None]
    cnt = Counter(t["outcome"] for t in tasks)
    sh, base = shares(cnt)
    return {
        "window": label,
        "first_day": str(days[0]) if days else "",
        "last_day": str(days[-1]) if days else "",
        "active_days": active,
        "tasks": n,
        "tasks_per_active_day": round(n / active, 1) if active else 0.0,
        "legs_flagged": flagged,
        "legs_in_span": legs,
        "pct_legs_flagged": round(100.0 * flagged / legs, 1) if legs else 0.0,
        "open_min_p50": round(C.pct(opens, 50), 1) if opens else None,
        "open_min_p75": round(C.pct(opens, 75), 1) if opens else None,
        "pct_filed_on_pickup_day": round(100.0 * sum(sd) / len(sd), 1) if sd else None,
        "resolved_base": base,
        "pct_moved": round(sh["MOVED"], 1),
        "pct_retimed": round(sh["RETIMED"], 1),
        "pct_no_move": round(sh["NO_MOVE"], 1),
        "pct_hand": round(sh["HAND"], 1),
        "pct_cancelled": round(sh["CANCELLED"], 1),
        "pct_wasted_look": round(sh["NO_MOVE"] + sh["HAND"], 1),
        "escalations": cnt["ESCALATED"],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recent", type=int, default=28,
                    help="days in the per-day tail table (default 28)")
    args = ap.parse_args()
    t0 = time.time()

    con = C.connect()
    hz = C.Horizon(con)
    C.preamble("25_scanner_outcomes",
               "what the 30-minute conflict scanner files, and what it buys",
               hz, ASSUMPTIONS)

    tasks = load_tasks(con)
    if not tasks:
        raise SystemExit("no driver_conflict / tight_turn tasks in this snapshot")
    legs_by_day = {dt.date.fromisoformat(k): v
                   for k, v in C.legs_per_day(con).items()}

    # ── by month ────────────────────────────────────────────────────────────
    by_month = defaultdict(list)
    for t in tasks:
        by_month[t["day"].strftime("%Y-%m")].append(t)

    C.sub("VOLUME AND OUTCOME BY MONTH  (tasks/day is per ACTIVE day)")
    print(f"{'month':9s}{'days':>6}{'tasks':>7}{'/day':>7}{'legs':>7}{'%legs':>7}"
          f"{'openP50':>9}{'openP75':>9}{'%moved':>8}{'%retime':>8}"
          f"{'%nomove':>9}{'%hand':>7}{'%waste':>8}")
    month_rows = []
    for m in sorted(by_month):
        r = summarise(by_month[m], legs_by_day, m)
        month_rows.append(r)
        print(f"{m:9s}{r['active_days']:>6}{r['tasks']:>7}"
              f"{r['tasks_per_active_day']:>7.1f}{r['legs_flagged']:>7}"
              f"{r['pct_legs_flagged']:>7.1f}"
              f"{(r['open_min_p50'] or 0):>9.0f}{(r['open_min_p75'] or 0):>9.0f}"
              f"{r['pct_moved']:>8.1f}{r['pct_retimed']:>8.1f}"
              f"{r['pct_no_move']:>9.1f}{r['pct_hand']:>7.1f}{r['pct_wasted_look']:>8.1f}")

    # ── before / after the tuning commits ───────────────────────────────────
    C.sub(f"BEFORE AND AFTER THE TUNING COMMITS  (cut at {TUNING_CUT})")
    for d, sha, msg in TUNING_COMMITS:
        print(f"  {d}  {sha}  {msg}")
    cut = dt.date.fromisoformat(TUNING_CUT)
    before = [t for t in tasks if t["day"] < cut]
    after = [t for t in tasks if t["day"] >= cut]
    ba = []
    for label, group in (("before", before), ("after", after)):
        if not group:
            print(f"\n  {label}: no tasks")
            continue
        r = summarise(group, legs_by_day, label)
        ba.append(r)
        print(f"\n  {label:6s} {r['first_day']} .. {r['last_day']}   "
              f"{r['tasks']} tasks over {r['active_days']} active days "
              f"= {r['tasks_per_active_day']}/day")
        print(f"         {r['pct_legs_flagged']}% of legs flagged, "
              f"P50 open {r['open_min_p50']} min, "
              f"{r['pct_wasted_look']}% of closes bought nothing")
    if len(ba) == 2:
        b, a = ba
        dd = a["tasks_per_active_day"] - b["tasks_per_active_day"]
        print(f"\n  => filing rate {dd:+.1f} tasks/active day "
              f"({100.0 * dd / b['tasks_per_active_day']:+.0f}%), "
              f"wasted-look share {a['pct_wasted_look'] - b['pct_wasted_look']:+.1f} pts")

    # ── closure taxonomy, full detail ───────────────────────────────────────
    C.sub("EVERY CLOSING PHRASE, MOST RECENT MONTH FIRST")
    closure_rows = []
    for m in sorted(by_month, reverse=True)[:3]:
        cnt = Counter(t["outcome"] for t in by_month[m])
        tot = sum(cnt.values())
        print(f"\n  {m}  ({tot} tasks)")
        for k in ORDER:
            if cnt[k]:
                print(f"     {k:16s}{cnt[k]:>6}{100.0 * cnt[k] / tot:>7.1f}%")
        for k in ORDER:
            closure_rows.append([m, k, cnt[k],
                                 round(100.0 * cnt[k] / tot, 1) if tot else 0.0])

    # ── per-day tail ────────────────────────────────────────────────────────
    days = sorted({t["day"] for t in tasks})
    tail = days[-args.recent:]
    C.sub(f"LAST {len(tail)} ACTIVE DAYS")
    print(f"{'date':12s}{'tasks':>7}{'legs':>7}{'legs/day':>10}{'%flagged':>10}"
          f"{'%moved':>8}{'%waste':>8}")
    day_rows = []
    for d in tail:
        grp = [t for t in tasks if t["day"] == d]
        cnt = Counter(t["outcome"] for t in grp)
        sh, base = shares(cnt)
        flagged = len({t["leg_id"] for t in grp if t["leg_id"]})
        nlegs = legs_by_day.get(d, 0)
        row = [str(d), len(grp), flagged, nlegs,
               round(100.0 * flagged / nlegs, 1) if nlegs else 0.0,
               round(sh["MOVED"], 1), round(sh["NO_MOVE"] + sh["HAND"], 1)]
        day_rows.append(row)
        print(f"{row[0]:12s}{row[1]:>7}{row[2]:>7}{row[3]:>10}{row[4]:>10.1f}"
              f"{row[5]:>8.1f}{row[6]:>8.1f}")

    cols = list(month_rows[0].keys())
    p1 = C.write_csv("25_scanner_by_month.csv", cols,
                     [[r[c] for c in cols] for r in month_rows])
    p2 = C.write_csv("25_scanner_closures.csv",
                     ["month", "outcome", "tasks", "pct"], closure_rows)
    p3 = C.write_csv("25_scanner_by_day.csv",
                     ["date", "tasks", "legs_flagged", "legs", "pct_legs_flagged",
                      "pct_moved", "pct_wasted_look"], day_rows)
    for p in (p1, p2, p3):
        print(f"\nWrote: {os.path.relpath(p, C.REPO_ROOT)}")
    print(f"runtime: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
