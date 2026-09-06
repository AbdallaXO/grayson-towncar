#!/usr/bin/env python
"""28 — How much GPS history is worth keeping, and what does keeping it cost?

WHAT IS ABOUT TO BE BUILT (06_DAY_MANAGER §3.4, Phase 1.3)
----------------------------------------------------------
"The Samsara sweep already fetches every car's position every 180 s and discards
it. Bulk-insert a compact ``DispatchEtaSample`` row on the same tick." Stated
gate: "07's ETA-error table reproducible from the new table."

This is not support work. §3.4 splits day-of lateness in two: the milestone rule
(26) catches "he never got started", and GPS is the ONLY thing that can see "he
started fine and is now stuck". 07 scores that signal at 72% on "late at all" —
the strongest predictor measured anywhere in this project — and it cannot be
pushed further because the sweep keeps no history.

THE QUESTION THIS ANSWERS, WHICH THE TICKET DOES NOT
----------------------------------------------------
"A row per evaluated leg per tick" is a volume decision nobody has priced. The
sweep runs every 180 s all day, so the write rule is the whole design: too broad
and the table is the largest thing in the database and mostly records a parked
car hours from its next job; too narrow and it loses exactly the samples §3.4
needs. Both failure modes are silent.

So this script simulates the sweep's OWN target selection over 28 real days at
its real cadence and prices four candidate write rules against the two things
the samples exist for:

  KEEP EVERYTHING          the ticket read literally
  UNDER WAY                only once the driver has tapped on-the-way or later
  NEAR THE TARGET          only within N minutes of the deadline being measured
  UNDER WAY OR NEAR        the union

judged on:

  VOLUME       rows/day, rows/year, and MiB/year against per-row sizes measured
               from this database's own tables, not guessed.
  THE 07 GATE  share of SCORABLE samples kept — a sample is scorable only if it
               falls between the two taps 07 scores against (on-the-way ->
               on-location for a pickup, picked-up -> completed for a dropoff).
               A rule that halves the volume and keeps every scorable sample is
               free; one that drops scorable samples is buying storage with
               evidence.
  THE §3.4 USE The case the plan actually wants GPS for is the ~6% of legs with
               NO recorded pickup tap, where a missed milestone is ambiguous and
               only "has the car left the pickup point" resolves it. A tap-gated
               write rule would drop precisely those legs, so each rule is also
               scored on how many of 26's no-tap milestone misses still carry a
               sample. This is the check that stops the cheapest rule winning.

METHOD
------
Target SELECTION does not depend on GPS or on any paid call — it is a pure
function of each driver's leg statuses, pickup times and deadlines
(``samsara_risk.choose_active_target`` / ``evaluate_driver``). So the row stream
is exactly replayable even though the ETA values are not: leg driver/status/
pickup_time are rewound from ``reservations_historicalleg`` (23's rule), and the
sweep's own logic is re-implemented from the shipped constants — ``_ON_TRIP``,
``_DONE``, ``PAST_PICKUP_GRACE_MIN``, the mid-trip two-row branch, and the
mapped-and-Samsara-enabled vehicle gate.

  --verify-fill  the second half, runnable only AFTER the code exists: rebuilds
                 07's ETA-error table from rows in ``DispatchEtaSample``'s shape
                 (drawn from the incidental log in ``historicalleg``) using the
                 SHIPPED reader, and reports any disagreement with the committed
                 out/07_eta_prediction_errors.csv.

USAGE
  venv/bin/python docs/scheduling-redesign/analysis/28_eta_history_gate.py \
      [--days 28] [--tick-sec 180] [--near-min 60 120 180] [--verify-fill]

Outputs: out/28_write_rules.csv       volume and retention per candidate rule
         out/28_rows_per_day.csv      the simulated row stream, per date
         out/28_notap_coverage.csv    §3.4's no-tap milestone cases, per rule
         out/28_gate_parity.csv       --verify-fill only
"""
import argparse
import datetime as dt
import importlib.util
import os
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

sys.path.insert(0, C.REPO_ROOT)

# Shipped constants, mirrored by VALUE so this script can run before Django is
# up; each is re-asserted against the module after django_on_copy().
ON_TRIP = {"picked-up", "on-location"}
DONE = {"completed", "cancelled"}
STARTED = ON_TRIP | {"on-the-way"}
TICK_SEC = 180
PAST_PICKUP_GRACE_MIN = 45          # pickup_policy.OVERDUE_STALE_MIN
                                    # (asserted against the module below —
                                    #  a first draft guessed 90 and the
                                    #  assert is why that never shipped)
NEAR_MIN_DEFAULT = (60, 120, 180)
#: How far either side of a missed milestone a sample still answers the question
#: "has he left the pickup point yet". An hour before, because 26 measures the
#: typical warning at 80-97 minutes and the impatient quartile at 55-70; half an
#: hour after, because past that the next pickup is already happening. It is a
#: CHOICE, so it is named rather than buried, and the sensitivity is reported:
#: at -60..+30 a per-tick log covers 21 of the 25 ambiguous legs, at -120..+60
#: 24 of 25, and over the whole day all 25. The four it misses at the shipped
#: window were sampled earlier and tapped `completed` before it opened.
NOTAP_WINDOW_MIN = (-60, 30)

# Bytes per row, measured from this database with dbstat rather than guessed —
# three existing narrow tables bracket what a compact sample row costs.
BYTES_PER_ROW = 233                 # reservations_legstatus, all-in

ASSUMPTIONS = (
    "Target SELECTION is replayable and the ETA VALUES are not. The sweep picks "
    "its target from leg status, pickup time and deadline alone, so the row "
    "STREAM is exact; what each row would have contained (drive minutes, risk "
    "band, position) depends on GPS and a paid Google call and is unknowable "
    "after the fact. Every number here is about how many rows, never about what "
    "they would have said.",
    "Leg driver/status/pickup_time are rewound from reservations_historicalleg "
    "exactly as 23 does. The DEADLINE is not rewound: pickup_deadline is "
    "computed once per leg from the final flight record, so a leg whose flight "
    "moved during the day is priced against where the deadline ended up. That "
    "shifts a handful of legs in and out of the grace window and cannot move a "
    "volume figure materially.",
    "The vehicle gate is applied: a driver with no DriverVehicleAssignment on "
    "the date, or one whose car carries no samsara_vehicle_id, produces NO rows "
    "— evaluate_driver returns {} for him. That is the same gate production "
    "applies, and it is why row volume tracks DRIVERS rather than legs.",
    "A sample is SCORABLE by 07's definition only if it falls strictly inside "
    "the tap pair its target type is scored against — on-the-way -> on-location "
    "for a pickup or next_pickup, picked-up -> completed for a dropoff. Samples "
    "outside that window are real rows that no analysis in this project can "
    "grade, which is what makes the volume question a real one.",
    "The no-tap coverage figure reads 26's committed out/26_missed_milestones.csv "
    "rather than re-deriving the milestone here — the derivation lives in one "
    "place and this script must not grow a second copy of it. Re-run 26 first if "
    "the milestone rule changes.",
)


def load_module(name, fname):
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(name, os.path.join(here, fname))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# --------------------------------------------------------------------------
# the day's board, rewound in memory
# --------------------------------------------------------------------------

def load_day(con, day):
    """Everything one date's simulation needs, straight from the snapshot."""
    legs = C.q(con, "SELECT id, driver_id, pickup_time, pickup_date, status, "
                    "pickup_location, dropoff_location FROM reservations_leg "
                    "WHERE pickup_date=?", (str(day),))
    if not legs:
        return None
    ids = [r["id"] for r in legs]
    ph = ",".join("?" * len(ids))
    hist = defaultdict(list)
    for h in C.q(con, f"SELECT id, history_date, driver_id, pickup_time, pickup_date, "
                      f"status FROM reservations_historicalleg WHERE id IN ({ph}) "
                      f"ORDER BY history_date", ids):
        hist[h["id"]].append(h)
    taps = defaultdict(dict)
    for t in C.q(con, f"SELECT leg_id, status, MIN(timestamp) ts FROM "
                      f"reservations_legstatus WHERE leg_id IN ({ph}) "
                      f"GROUP BY leg_id, status", ids):
        taps[t["leg_id"]][t["status"]] = C.to_local(t["ts"])
    inhouse = {r["id"] for r in C.q(
        con, "SELECT id FROM drivers_driver WHERE LOWER(driver_type)='inhouse'")}
    # The vehicle gate, exactly as resolve_assigned_fleet_vehicle applies it:
    # the driver's assignment on the leg's own date, and the car must be mapped.
    mapped = {r["driver_id"] for r in C.q(
        con, "SELECT a.driver_id FROM drivers_drivervehicleassignment a "
             "JOIN drivers_fleetvehicle v ON v.id = a.vehicle_id "
             "WHERE a.date=? AND v.samsara_vehicle_id IS NOT NULL "
             "AND v.samsara_vehicle_id <> ''", (str(day),))}
    return {"day": day, "legs": legs, "hist": hist, "taps": taps,
            "inhouse": inhouse, "mapped": mapped}


def state_at(snap, tick_local):
    """{leg_id: (driver_id, status, pickup_time, pickup_date)} at a tick."""
    ts = (tick_local - dt.timedelta(hours=-C.utc_offset_hours(tick_local))
          ).strftime("%Y-%m-%d %H:%M:%S.%f")
    out = {}
    for leg in snap["legs"]:
        rows = [h for h in snap["hist"].get(leg["id"], [])
                if str(h["history_date"]) <= ts]
        if not rows:
            continue                        # the leg did not exist yet
        h = rows[-1]
        out[leg["id"]] = (h["driver_id"], h["status"], h["pickup_time"],
                          h["pickup_date"])
    return out


def targets_at(snap, tick_local, state, deadlines):
    """The rows sweep_eta would write this tick: [(leg_id, kind, target_dt)].

    Re-implements evaluate_driver's selection: mid-trip driver -> his dropoff
    (ETA only, no deadline) PLUS his next pickup inside the grace window;
    otherwise choose_active_target's single next pickup."""
    by_driver = defaultdict(list)
    for leg in snap["legs"]:
        st = state.get(leg["id"])
        if st is None:
            continue
        did, status, ptime, pdate = st
        if did is None or did not in snap["inhouse"] or did not in snap["mapped"]:
            continue
        if str(pdate) != str(snap["day"]) or (status or "") in DONE:
            continue
        by_driver[did].append((ptime or "23:59:59", leg, status))

    rows = []
    for did, entries in by_driver.items():
        entries.sort(key=lambda e: e[0])
        mid = next((e for e in entries if (e[2] or "") in ON_TRIP), None)
        if mid is not None:
            _, leg, _ = mid
            if leg["dropoff_location"]:
                rows.append((leg["id"], "dropoff", None, did))
            nxt = next((e for e in entries
                        if e[1]["id"] != leg["id"]
                        and _within_grace(deadlines.get(e[1]["id"]), tick_local)),
                       None)
            if nxt is not None:
                d = deadlines.get(nxt[1]["id"])
                rows.append((nxt[1]["id"], "next_pickup", d, did))
            continue
        for _, leg, _status in entries:
            if not leg["pickup_location"]:
                continue
            d = deadlines.get(leg["id"])
            if d is None:
                continue
            if (tick_local - d).total_seconds() / 60 <= PAST_PICKUP_GRACE_MIN:
                rows.append((leg["id"], "pickup", d, did))
                break
    return rows


def notap_cases(dates):
    """26's missed milestones whose first leg carries NO recorded pickup tap —
    §3.4's ambiguous case, and the one GPS is actually for: "he may have picked
    up and not tapped", answered by whether the car has left the pickup point.

    Read from 26's committed output rather than re-derived: the milestone
    formula lives in one script and must not be copied into a second."""
    import csv
    path = os.path.join(C.OUT_DIR, "26_missed_milestones.csv")
    if not os.path.exists(path):
        return []
    keep = {str(d) for d in dates}
    out = []
    for r in csv.DictReader(open(path, encoding="utf-8")):
        if r["date"] not in keep or r["actual_pickup_tap"]:
            continue
        try:
            ms = dt.datetime.strptime(f"{r['date']} {r['milestone']}",
                                      "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        out.append({"date": r["date"], "leg_a": int(r["leg_a"]),
                    "leg_b": int(r["leg_b"]), "milestone": ms,
                    "b_trip": r["b_trip"]})
    return out


def _within_grace(deadline, now):
    if deadline is None:
        return False
    return (now - deadline).total_seconds() / 60 <= PAST_PICKUP_GRACE_MIN


# --------------------------------------------------------------------------
# the candidate write rules
# --------------------------------------------------------------------------

def rule_names(near_mins):
    return ["everything", "under_way"] + [f"near_{n}" for n in near_mins] \
        + [f"under_way_or_near_{n}" for n in near_mins]


def keeps(rule, *, status, minutes_to_target, near_mins):
    started = (status or "") in STARTED
    if rule == "everything":
        return True
    if rule == "under_way":
        return started
    if rule.startswith("under_way_or_near_"):
        n = int(rule.rsplit("_", 1)[1])
        return started or (minutes_to_target is not None
                           and minutes_to_target <= n)
    if rule.startswith("near_"):
        n = int(rule.rsplit("_", 1)[1])
        return minutes_to_target is not None and minutes_to_target <= n
    raise ValueError(rule)


def scorable(kind, tick, tap):
    """07's window: a sample counts only between the two taps its target type is
    scored against. Dropoff -> picked-up..completed; pickup -> on-the-way..
    on-location."""
    if kind == "dropoff":
        s, e = tap.get("picked-up"), tap.get("completed")
    else:
        s, e = tap.get("on-the-way"), tap.get("on-location")
    if not s or not e or e <= s:
        return False
    return s <= tick < e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--dates", nargs="*", default=[])
    ap.add_argument("--tick-sec", type=int, default=TICK_SEC)
    ap.add_argument("--near-min", type=int, nargs="*", default=list(NEAR_MIN_DEFAULT))
    ap.add_argument("--verify-fill", action="store_true")
    args = ap.parse_args()
    t0 = time.time()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    con = C.connect()
    hz = C.Horizon(con)
    C.preamble("28_eta_history_gate.py",
               "how much GPS history is worth keeping, and what does keeping it cost?",
               hz, ASSUMPTIONS)

    g23 = load_module("replay23", "23_advisor_replay.py")
    g17 = load_module("gate17", "17_build3_gate.py")
    dates = g23.pick_dates(con, hz, args.days, args.dates)
    per_day = int(24 * 3600 / args.tick_sec)
    print(f"\ndates ({len(dates)}): {dates[0]} .. {dates[-1]}")
    print(f"cadence     : every {args.tick_sec} s = {per_day} ticks/day")
    g17.django_on_copy()

    # Re-assert the mirrored constants against the shipped modules.
    from dispatching import samsara_risk as sr
    from dispatching.pickup_policy import pickup_deadline
    assert set(sr._ON_TRIP) == ON_TRIP, sr._ON_TRIP
    assert set(sr._DONE) == DONE, sr._DONE
    assert sr.PAST_PICKUP_GRACE_MIN == PAST_PICKUP_GRACE_MIN, sr.PAST_PICKUP_GRACE_MIN
    print(f"shipped     : _ON_TRIP={sorted(ON_TRIP)}  grace="
          f"{PAST_PICKUP_GRACE_MIN} min  DROPOFF_SERVICE_MIN="
          f"{sr.DROPOFF_SERVICE_MIN}")

    if args.verify_fill:
        return verify_fill(con, dates, t0)

    from reservations.models import Leg
    rules = rule_names(args.near_min)
    kept = {r: 0 for r in rules}
    kept_scorable = {r: 0 for r in rules}
    kept_legs = {r: set() for r in rules}
    total_scorable = 0
    per_date_rows, kind_counts = [], Counter()
    cases = notap_cases(dates)
    notap = defaultdict(list)
    for c in cases:
        notap[(c["date"], c["leg_a"])].append(c)
    notap_seen = {r: set() for r in rules}

    for i, day in enumerate(dates, 1):
        snap = load_day(con, day)
        if not snap:
            continue
        legs = {l.id: l for l in Leg.objects.filter(
            id__in=[r["id"] for r in snap["legs"]])
            .select_related("reservation", "flight_information")
            .prefetch_related("legflight_set__flight")}
        deadlines = {}
        for lid, leg in legs.items():
            try:
                d, _ = pickup_deadline(leg, aware=False)
            except Exception:
                d = None
            deadlines[lid] = d

        day_rows = {r: 0 for r in rules}
        t = dt.datetime.combine(day, dt.time(0, 0))
        end = t + dt.timedelta(days=1)
        while t < end:
            state = state_at(snap, t)
            for leg_id, kind, target_dt, _did in targets_at(snap, t, state, deadlines):
                kind_counts[kind] += 1
                status = (state.get(leg_id) or (None, "", None, None))[1]
                mt = (None if target_dt is None
                      else (target_dt - t).total_seconds() / 60)
                sc = scorable(kind, t, snap["taps"].get(leg_id, {}))
                if sc:
                    total_scorable += 1
                for r in rules:
                    if keeps(r, status=status, minutes_to_target=mt,
                             near_mins=args.near_min):
                        kept[r] += 1
                        day_rows[r] += 1
                        kept_legs[r].add(leg_id)
                        if sc:
                            kept_scorable[r] += 1
                        # §3.4: does this rule leave any sample on an ambiguous
                        # leg in the hour BEFORE its milestone, when the question
                        # "has he left the pickup point yet" is still worth
                        # asking?
                        for case in notap.get((str(day), leg_id), ()):
                            gap = (t - case["milestone"]).total_seconds() / 60
                            if NOTAP_WINDOW_MIN[0] <= gap <= NOTAP_WINDOW_MIN[1]:
                                notap_seen[r].add((str(day), leg_id))
            t += dt.timedelta(seconds=args.tick_sec)
        per_date_rows.append([str(day)] + [day_rows[r] for r in rules])
        print(f"  [{i:>2}/{len(dates)}] {day}  "
              f"{day_rows['everything']:>5} rows  {time.time() - t0:6.1f}s",
              flush=True)

    n = len(per_date_rows)
    C.sub("WRITE RULES — what each costs, and what it throws away")
    print(f"{'rule':<24}{'rows/day':>10}{'rows/yr':>12}{'MiB/yr':>9}"
          f"{'legs/day':>10}{'scorable kept':>15}")
    out_rows = []
    for r in rules:
        rpd = kept[r] / n
        keep_pct = (100.0 * kept_scorable[r] / total_scorable
                    if total_scorable else 0.0)
        print(f"{r:<24}{rpd:>10.0f}{rpd * 365:>12,.0f}"
              f"{rpd * 365 * BYTES_PER_ROW / 1048576:>9.0f}"
              f"{len(kept_legs[r]) / n:>10.1f}{keep_pct:>14.1f}%")
        out_rows.append([r, round(rpd), round(rpd * 365),
                         round(rpd * 365 * BYTES_PER_ROW / 1048576, 1),
                         round(len(kept_legs[r]) / n, 1), round(keep_pct, 1),
                         kept_scorable[r], total_scorable])
    print(f"\n  targets written: " + ", ".join(
        f"{k}={v / n:.0f}/day" for k, v in kind_counts.most_common()))
    print(f"  'scorable' is 07's own window — a sample between the two taps it "
          f"would be graded\n  against. {total_scorable / n:.0f} of the "
          f"{kept['everything'] / n:.0f} rows a day a literal per-tick insert "
          f"writes are\n  scorable; the rest are real rows no analysis in this "
          f"project can grade.")

    C.sub("THE CASE §3.4 ACTUALLY WANTS GPS FOR — the ambiguous, untapped leg")
    print(f"  26 fires {len(cases)} missed milestones on these dates whose first leg "
          f"carries NO pickup tap.\n  For those, a missed milestone is ambiguous "
          f"— he may have picked up and not tapped — and the\n  question GPS "
          f"answers is whether the car has left the pickup point at all.\n")
    print(f"  window: {NOTAP_WINDOW_MIN[0]:+d}..{NOTAP_WINDOW_MIN[1]:+d} min "
          f"around the milestone (a named choice — see NOTAP_WINDOW_MIN)\n")
    print(f"{'rule':<24}{'ambiguous legs sampled':>26}")
    notap_rows = []
    for r in rules:
        pct = (100.0 * len(notap_seen[r]) / len(cases)) if cases else 0.0
        print(f"{r:<24}{len(notap_seen[r]):>10} / {len(cases):<4} {pct:>7.1f}%")
        notap_rows.append([r, len(notap_seen[r]), len(cases), round(pct, 1)])
    print(f"\n  A rule that keeps the 07 gate but blinds the detector §3.4 is "
          f"building would be\n  a false economy: this column is the one that "
          f"stops the cheapest rule winning.")

    p3 = C.write_csv("28_notap_coverage.csv",
                     ["rule", "legs_sampled", "legs_total", "pct"], notap_rows)
    p1 = C.write_csv("28_write_rules.csv",
                     ["rule", "rows_per_day", "rows_per_year", "mib_per_year",
                      "legs_per_day", "pct_scorable_kept", "scorable_kept",
                      "scorable_total"], out_rows)
    p2 = C.write_csv("28_rows_per_day.csv", ["date"] + rules, per_date_rows)
    for p in (p1, p2, p3):
        print(f"\nWrote: {os.path.relpath(p, C.REPO_ROOT)}")
    print(f"runtime: {time.time() - t0:.1f}s")


def verify_fill(con, dates, t0):
    """THE TICKET'S STATED GATE: rebuild 07's ETA-error table through the
    SHIPPED reader (``eta_samples.prediction_errors``) and diff it against the
    committed CSV.

    The rows are drawn from the OLD incidental log — the dispatch_* columns
    unrelated .save() calls copied into reservations_historicalleg — reshaped
    into what a DispatchEtaSample carries. That is the only corpus available
    until the new table has run, and it is enough to answer the question the
    gate asks: does a sample-shaped row, read by production code, reproduce the
    number 06 §3.4 rests on? If it cannot, the live figure and the replayed one
    are not the same measurement and cannot be compared."""
    import csv
    from types import SimpleNamespace
    from dispatching import eta_samples

    def putc(v):
        """'YYYY-MM-DD HH:MM:SS[.ffffff][+00:00]' -> naive UTC, 07's own parse."""
        if not v:
            return None
        t = str(v).replace("T", " ").strip()
        if "+" in t:
            t = t.split("+")[0]
        if t.endswith("Z"):
            t = t[:-1]
        try:
            return dt.datetime.fromisoformat(t)
        except ValueError:
            return None

    samples = [
        SimpleNamespace(leg_id_ref=r["id"], sampled_at=putc(r["ev"]),
                        eta_minutes=r["em"], eta_target=r["tgt"],
                        risk_status=r["rs"] or "")
        for r in C.q(con, """SELECT DISTINCT id, dispatch_eta_evaluated_at ev,
                               dispatch_eta_minutes em, dispatch_eta_target tgt,
                               dispatch_eta_target_time tt, dispatch_risk_status rs
                             FROM reservations_historicalleg
                             WHERE dispatch_eta_minutes IS NOT NULL
                               AND dispatch_eta_evaluated_at IS NOT NULL""")]
    taps = defaultdict(dict)
    for r in C.q(con, "SELECT leg_id, status, MIN(timestamp) t "
                      "FROM reservations_legstatus GROUP BY 1,2"):
        taps[r["leg_id"]][r["status"]] = putc(r["t"])
    # 07 joins pickup_date LIVE from the leg, not from the snapshotted row —
    # 21 legs already disagree with their own history. Matching that is part of
    # reproducing the table.
    leg_dates = {r["id"]: r["pickup_date"] for r in
                 C.q(con, "SELECT id, pickup_date FROM reservations_leg")}
    today = C.Horizon(con).today

    rebuilt = {(case, str(leg_id), str(ev)[:19]): err
               for case, leg_id, _pd, ev, _em, _tg, _rs, err
               in eta_samples.prediction_errors(samples, taps, leg_dates, today)}

    want = {}
    path = os.path.join(C.OUT_DIR, "07_eta_prediction_errors.csv")
    for r in csv.DictReader(open(path, encoding="utf-8")):
        want[(r["case"], r["leg_id"], r["evaluated_at_utc"])] = float(
            r["error_minutes"])

    rows, bad = [], 0
    for k, v in sorted(want.items()):
        g = rebuilt.get(k)
        if g is None or abs(g - v) > 0.005:
            bad += 1
            rows.append([k[0], k[1], k[2], v, g])
    extra = sorted(k for k in rebuilt if k not in want)
    for k in extra[:200]:
        rows.append([k[0], k[1], k[2], "", rebuilt[k]])

    C.sub("GATE PARITY — 07's ETA-error table, rebuilt through the shipped reader")
    print(f"  committed rows {len(want):,}; rebuilt {len(rebuilt):,}; "
          f"disagreements {bad}; rows the reader invents {len(extra)}")
    if rows:
        print(f"\n{'case':<22}{'leg':>9}{'evaluated_at':>22}"
              f"{'committed':>12}{'rebuilt':>10}")
        for r in rows[:30]:
            print(f"{r[0]:<22}{r[1]:>9}{r[2]:>22}{str(r[3]):>12}{str(r[4]):>10}")
    p = C.write_csv("28_gate_parity.csv",
                    ["case", "leg_id", "evaluated_at_utc", "committed", "rebuilt"],
                    rows)
    print(f"\nWrote: {os.path.relpath(p, C.REPO_ROOT)}")
    print("\n  GATE: 07's table is reproducible from the sample shape, by "
          "production code."
          if not bad and not extra else
          f"\n  GATE FAILED: {bad} disagreements and {len(extra)} invented rows. "
          f"The live number would\n  not be comparable with §3.4's 72%, which is "
          f"the only reason to keep the series.")
    print(f"runtime: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
