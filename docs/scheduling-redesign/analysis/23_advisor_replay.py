#!/usr/bin/env python
"""23 — Is the Recovery Advisor right often enough to show a dispatcher?

THE GATE THIS IS (06_DAY_MANAGER §3.2, §3.3, D5)
------------------------------------------------
``dispatching/conflict_advisor.py`` is a complete day-of repair engine — five
detectors, a full ladder, whole-remaining-day validation, a front-door apply
path. It is gated to superusers (``advisor_views.advisor_visible_to``), has
never been applied in production, and **its precision has never been measured.**

D5 says a warning class ships only at >=70% precision. No single signal in this
system clears that bar on the honest test: planning-clock negative slack scores
32-47% (analysis/19), the GPS at_risk band 4% (analysis/07). The 92.5% that let
Build 1 ship measured agreement with a DEFINITION, not with what happened.

So this script asks the only question that matters before the visibility flip:
of the cards the advisor would have put on the rail, how many were about a trip
that actually ran late?

WHAT IT DOES
------------
For each replay date and each tick (default every 15 min, 06:00-23:00), it
REWINDS a throwaway copy of the database to what the system actually knew at
that minute, then calls ``compute_advisor_state(day, now=tick)`` and scores
every card it raises.

The rewind, per tick:
  legs      driver_id / pickup_time / pickup_date / status restored from the
            latest ``historicalleg`` row at or before the tick. A leg with no
            history row yet did not exist: it is parked as 'cancelled', which is
            exactly how ``build_board_state`` excludes it.
  taps      ``reservations_legstatus`` rows after the tick are deleted (ticks run
            DESCENDING, so this only ever removes more).
  flights   an actual arrival later than the tick had not landed yet and is
            nulled. Estimates cannot be historized at all (``last_updated`` is
            touched by every later refresh sweep — measured: a median ~10 h after
            the arrival it describes), so they are handled as two BOUNDS:
              live    estimates left as the day finally knew them
                      -> OVERSTATES flight_change detection
              masked  estimates blanked entirely
                      -> UNDERSTATES it
            The truth is between the two. Default runs both.
  GPS       every ``dispatch_*`` field is blanked at every tick. The Samsara
            sweep writes them with bulk_update and keeps no history, so they
            cannot be replayed. GPS-based classes therefore do not appear here
            and stay detected-only until the live log (AdvisorEvent) scores
            them. This is a stated blind spot, not an omission.

SCORING
-------
A card's IMPACT LEG is the last id in ``leg_ids`` (§3.3). Truth for that leg is
built from the pristine snapshot BEFORE any rewind, using production's own
definition of late — ``pickup_policy.pickup_deadline`` (gate + 10 min at an
in-terminal meet, booked time everywhere else) — against the driver's
on-location tap. analysis/19's ``on_location_minutes`` supplies the tap-quality
rule, so batch taps (a driver clearing a whole day in one go) are discarded here
exactly as they are there.

A card counts as RIGHT at >15 when its impact leg's on-location tap landed more
than 15 minutes past that deadline. Cards whose impact leg has no usable tap are
counted as UNSCORABLE and reported — never quietly dropped.

Precision is computed over UNIQUE cards (a card that survives eight ticks is one
card, keyed on the advisor's own anti-flap id), while the rail-load figure —
cards per glance — is per tick, because that is what a dispatcher actually sees.

METHOD
------
Raw side: the frozen snapshot, read-only. Django side: a migrated throwaway copy
(``17_build3_gate.django_on_copy``). Each date is restored from the snapshot
before and after its ticks, so no date is ever judged against another date's
mutations. Nothing is written to the snapshot; nothing external is called.

USAGE
  python docs/scheduling-redesign/analysis/23_advisor_replay.py \
      [--days 28] [--tick-min 15] [--from-hour 6] [--to-hour 23] \
      [--estimates both|live|masked] [--dates 2026-08-14 ...]

Outputs: out/23_advisor_precision.csv
         out/23_cards_per_day.csv
         out/23_timing.csv
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

LATE_BARS = (15, 10)

ASSUMPTIONS = (
    "The rewind restores what the DATABASE knew at the tick. Anything the "
    "database never historized cannot be replayed: GPS (dispatch_*, blanked "
    "everywhere) and flight ESTIMATES (reported as two bounds, live and masked).",
    "'Late' is production's own pickup_policy.pickup_deadline — gate arrival + 10 "
    "at an in-terminal meet, booked time otherwise — measured against the "
    "on-location tap, with analysis/19's batch-tap rule discarding days a driver "
    "cleared in one go. It is NOT the engine's slack definition, which is what "
    "makes this a test of the advisor rather than a test of its own arithmetic.",
    "Precision counts UNIQUE cards (the advisor's anti-flap id, per date); rail "
    "load counts card instances per tick, which is what a dispatcher sees.",
    "pct_single_leg is the honesty column. A one-leg card has no downstream leg to "
    "be wrong about: its impact leg IS the leg it fired on, so 'this leg is overdue' "
    "gets graded against 'this leg's tap was late' — close to circular, a description "
    "rather than a forecast. Precision on a class with a high value here is NOT "
    "comparable with a class that names a break on a LATER leg.",
    "The roster (DriverVehicleAssignment), the ops task table and KEOI flags are "
    "NOT rewound: none of the three is historized, KEOI carries a paired-field CHECK "
    "constraint that makes a partial rewind illegal, and the entire snapshot holds 63 "
    "KEOI rows. Tasks affect only a card's deep-link, not detection.",
    "A leg whose pickup_date at the tick differs from the replay date leaves the "
    "board, as it should. A leg that MOVED ONTO the date later in the day is not "
    "pulled in; that case is rare and is a known under-count.",
)

# The GPS precompute. Five of these columns are NOT NULL varchars (Django
# blank='' fields), so "no GPS" is '' there and NULL everywhere else.
DISPATCH_NULL_FIELDS = (
    "dispatch_eta_minutes", "dispatch_eta_target_time", "dispatch_eta_evaluated_at",
    "dispatch_is_moving", "dispatch_stationary_minutes",
    "dispatch_eta_origin_lat", "dispatch_eta_origin_lng",
)
DISPATCH_TEXT_FIELDS = (
    "dispatch_eta_target", "dispatch_risk_status", "dispatch_risk_reason",
    "dispatch_vehicle_label", "dispatch_eta_origin_target",
)
HIST_FIELDS = ("driver_id", "pickup_time", "pickup_date", "status")
ACTUAL_FLIGHT_FIELDS = ("actual_gate_arrival_local", "actual_arrival_local")
EST_FLIGHT_FIELDS = ("estimated_gate_arrival_local", "estimated_arrival_local")


def load_module(name, fname):
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(name, os.path.join(here, fname))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# --------------------------------------------------------------------------
# date selection
# --------------------------------------------------------------------------

def pick_dates(con, horizon, n, explicit):
    if explicit:
        return sorted(dt.date.fromisoformat(d) for d in explicit)
    rows = C.q(con, C.live_legs_sql(
        "l.pickup_date d, COUNT(*) n",
        "AND l.driver_id IN (SELECT id FROM drivers_driver WHERE LOWER(driver_type)='inhouse') "
        "AND l.pickup_date <= ?", order="GROUP BY l.pickup_date ORDER BY l.pickup_date DESC"),
        (str(horizon.last_actuals_day),))
    return sorted(dt.date.fromisoformat(r["d"]) for r in rows[:n])


# --------------------------------------------------------------------------
# the pristine per-date snapshot, and putting it back
# --------------------------------------------------------------------------

def load_pristine(con, day):
    """Everything about one date that the rewind mutates, straight from the
    read-only snapshot."""
    legs = C.q(con, "SELECT id, driver_id, pickup_time, pickup_date, status, "
                    "reservation_id FROM reservations_leg WHERE pickup_date=?",
               (str(day),))
    ids = [r["id"] for r in legs]
    if not ids:
        return None
    ph = ",".join("?" * len(ids))
    taps = C.q(con, f"SELECT id, status, timestamp, notes, leg_id, updated_by_id "
                    f"FROM reservations_legstatus WHERE leg_id IN ({ph})", ids)
    hist = C.q(con, f"SELECT id, history_date, driver_id, pickup_time, pickup_date, "
                    f"status FROM reservations_historicalleg WHERE id IN ({ph}) "
                    f"ORDER BY history_date", ids)
    flight_ids = [r["flight_id"] for r in C.q(
        con, f"SELECT DISTINCT flight_id FROM reservations_legflight "
             f"WHERE leg_id IN ({ph})", ids) if r["flight_id"]]
    flights = []
    if flight_ids:
        fph = ",".join("?" * len(flight_ids))
        cols = ", ".join(ACTUAL_FLIGHT_FIELDS + EST_FLIGHT_FIELDS)
        flights = C.q(con, f"SELECT id, {cols} FROM reservations_flight "
                           f"WHERE id IN ({fph})", flight_ids)
    by_leg = defaultdict(list)
    for h in hist:
        by_leg[h["id"]].append(h)
    return {"day": day, "leg_ids": ids, "legs": legs, "taps": taps,
            "hist": by_leg, "flights": flights}


def restore(cur, snap):
    ids = snap["leg_ids"]
    ph = ",".join("?" * len(ids))
    for r in snap["legs"]:
        cur.execute("UPDATE reservations_leg SET driver_id=?, pickup_time=?, "
                    "pickup_date=?, status=? WHERE id=?",
                    (r["driver_id"], r["pickup_time"], r["pickup_date"],
                     r["status"], r["id"]))
    cur.execute(f"DELETE FROM reservations_legstatus WHERE leg_id IN ({ph})", ids)
    for t in snap["taps"]:
        cur.execute("INSERT INTO reservations_legstatus "
                    "(id, status, timestamp, notes, leg_id, updated_by_id) "
                    "VALUES (?,?,?,?,?,?)",
                    (t["id"], t["status"], t["timestamp"], t["notes"], t["leg_id"],
                     t["updated_by_id"]))
    for f in snap["flights"]:
        sets = ", ".join(f"{c}=?" for c in ACTUAL_FLIGHT_FIELDS + EST_FLIGHT_FIELDS)
        cur.execute(f"UPDATE reservations_flight SET {sets} WHERE id=?",
                    tuple(f[c] for c in ACTUAL_FLIGHT_FIELDS + EST_FLIGHT_FIELDS)
                    + (f["id"],))


def rewind(cur, snap, tick_utc, mask_estimates):
    """Put the copy back to what was known at tick_utc. Ticks run descending."""
    ts = tick_utc.strftime("%Y-%m-%d %H:%M:%S.%f")
    ids = snap["leg_ids"]
    ph = ",".join("?" * len(ids))
    blanks = ", ".join([f"{c}=NULL" for c in DISPATCH_NULL_FIELDS]
                       + [f"{c}=''" for c in DISPATCH_TEXT_FIELDS])
    cur.execute(f"UPDATE reservations_leg SET {blanks} WHERE id IN ({ph})", ids)
    for leg in snap["legs"]:
        rows = [h for h in snap["hist"].get(leg["id"], [])
                if str(h["history_date"]) <= ts]
        if not rows:
            cur.execute("UPDATE reservations_leg SET status='cancelled' WHERE id=?",
                        (leg["id"],))
            continue
        h = rows[-1]
        cur.execute("UPDATE reservations_leg SET driver_id=?, pickup_time=?, "
                    "pickup_date=?, status=? WHERE id=?",
                    (h["driver_id"], h["pickup_time"], h["pickup_date"],
                     h["status"], leg["id"]))
    cur.execute(f"DELETE FROM reservations_legstatus WHERE leg_id IN ({ph}) "
                f"AND timestamp > ?", ids + [ts])
    for f in snap["flights"]:
        sets, vals = [], []
        for c in ACTUAL_FLIGHT_FIELDS:
            if f[c] and str(f[c]) > ts:
                sets.append(f"{c}=NULL")
        for c in EST_FLIGHT_FIELDS:
            if mask_estimates and f[c]:
                sets.append(f"{c}=NULL")
        if sets:
            cur.execute(f"UPDATE reservations_flight SET {', '.join(sets)} WHERE id=?",
                        vals + [f["id"]])


# --------------------------------------------------------------------------
# truth
# --------------------------------------------------------------------------

def build_truth(con, g19, day, leg_ids, kinds):
    """{leg_id: (late_minutes_vs_deadline, quality)} from the PRISTINE snapshot,
    using production's pickup_deadline and 19's tap-quality rule."""
    from reservations.models import Leg
    from dispatching.pickup_policy import pickup_deadline
    out = {}
    legs = (Leg.objects.filter(id__in=leg_ids)
            .select_related("reservation", "flight_information")
            .prefetch_related("legflight_set__flight"))
    for leg in legs:
        booked = leg.pickup_time
        _, quality = g19.on_location_minutes(con, leg.id, day, booked)
        if quality != "ok":
            out[leg.id] = (None, quality)
            continue
        rows = C.q(con, "SELECT timestamp FROM reservations_legstatus WHERE leg_id=? "
                        "AND status='on-location' ORDER BY timestamp", (leg.id,))
        at = C.to_local(rows[-1]["timestamp"])
        try:
            deadline, _basis = pickup_deadline(leg, aware=False)
        except Exception:
            deadline = None
        if deadline is None:
            out[leg.id] = (None, "no_deadline")
            continue
        out[leg.id] = (round((at - deadline).total_seconds() / 60.0, 1), "ok")
    for leg in legs:
        try:
            kinds[leg.id] = leg.get_trip_type() or "?"
        except Exception:
            kinds[leg.id] = "?"
    return out


# --------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------

def ticks_for(day, from_hour, to_hour, step_min):
    t = dt.datetime.combine(day, dt.time(from_hour, 0))
    end = dt.datetime.combine(day, dt.time(to_hour, 0))
    out = []
    while t <= end:
        out.append(t)
        t += dt.timedelta(minutes=step_min)
    return out


def replay_date(con, cur, g19, day, tickset, mask_estimates, cards, load, timing):
    from django.utils import timezone as djtz
    from dispatching.conflict_advisor import compute_advisor_state
    snap = load_pristine(con, day)
    if not snap:
        return
    restore(cur, snap)
    kinds = {}
    truth = build_truth(con, g19, day, snap["leg_ids"], kinds)
    mode = "masked" if mask_estimates else "live"
    for tick in sorted(tickset, reverse=True):          # descending: taps only shrink
        rewind(cur, snap, tick + dt.timedelta(hours=C.utc_offset_hours(tick)),
               mask_estimates)
        now = djtz.make_aware(tick)
        t0 = time.time()
        try:
            state = compute_advisor_state(day, now=now)
        except Exception as exc:
            timing.append([str(day), tick.strftime("%H:%M"), mode, -1, 0,
                           type(exc).__name__])
            continue
        ms = int((time.time() - t0) * 1000)
        cs = state["disruptions"]
        timing.append([str(day), tick.strftime("%H:%M"), mode, ms, len(cs), ""])
        load.append([str(day), tick.strftime("%H:%M"), mode, len(cs),
                     sum(1 for c in cs if c["severity"] == "critical"),
                     sum(1 for c in cs if c.get("hygiene")),
                     sum(1 for c in cs if c.get("plans")),
                     1 if state.get("truncated") else 0])
        for c in cs:
            impact = (c.get("leg_ids") or [None])[-1]
            late, quality = truth.get(impact, (None, "unknown"))
            lead = None
            if c.get("impact_at"):
                try:
                    lead = int((dt.datetime.fromisoformat(c["impact_at"])
                                - tick).total_seconds() / 60)
                except Exception:
                    lead = None
            key = (mode, str(day), c["id"])
            prev = cards.get(key)
            if prev is None or bool(c.get("plans")) > prev["has_plans"]:
                cards[key] = {
                    "mode": mode, "date": str(day), "id": c["id"],
                    "kind": c["kind"], "severity": c["severity"],
                    "basis": c.get("basis") or "",
                    "hygiene": bool(c.get("hygiene")),
                    "abstain": bool(c.get("abstain")),
                    "has_plans": bool(c.get("plans")),
                    "impact_leg": impact, "late_min": late, "quality": quality,
                    "n_legs": len(c.get("leg_ids") or []),
                    "lead_min": lead, "impact_kind": kinds.get(impact, "?"),
                    "self_scored": len(c.get("leg_ids") or []) <= 1,
                    "instances": (prev or {}).get("instances", 0),
                }
            cards[key]["instances"] = cards[key].get("instances", 0) + 1
            if lead is not None and (cards[key].get("lead_min") is None
                                     or lead > cards[key]["lead_min"]):
                cards[key]["lead_min"] = lead
    restore(cur, snap)


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def precision_rows(cards, n_dates):
    groups = defaultdict(list)
    for c in cards.values():
        groups[(c["mode"], c["kind"], c["severity"], c["basis"] or "-")].append(c)
    rows = []
    for (mode, kind, sev, basis), g in sorted(groups.items()):
        scored = [c for c in g if c["quality"] == "ok"]
        rows.append({
            "estimates": mode, "kind": kind, "severity": sev, "basis": basis,
            "unique_cards": len(g),
            "cards_per_day": round(len(g) / n_dates, 1),
            "pct_with_plans": round(100.0 * sum(c["has_plans"] for c in g) / len(g), 1),
            "scored": len(scored),
            "pct_single_leg": round(100.0 * sum(c["self_scored"] for c in g) / len(g), 1),
            "unscorable": len(g) - len(scored),
            "pct_late_15": (round(100.0 * sum(c["late_min"] > 15 for c in scored)
                                  / len(scored), 1) if scored else None),
            "pct_late_10": (round(100.0 * sum(c["late_min"] > 10 for c in scored)
                                  / len(scored), 1) if scored else None),
            "pct_late_any": (round(100.0 * sum(c["late_min"] > 0 for c in scored)
                                   / len(scored), 1) if scored else None),
            # The D5 number. A one-leg card names no downstream victim, so it can
            # only describe a leg that is already late; judging the advisor on
            # those flatters it. This column is precision on the cards that
            # actually forecast a break somewhere else.
            "scored_multileg": len([c for c in scored if not c["self_scored"]]),
            "pct_late_15_multileg": (
                round(100.0 * sum(c["late_min"] > 15 for c in scored
                                  if not c["self_scored"])
                      / len([c for c in scored if not c["self_scored"]]), 1)
                if [c for c in scored if not c["self_scored"]] else None),
        })
    return sorted(rows, key=lambda r: (-r["unique_cards"], r["kind"]))


LEAD_BUCKETS = ((0, 30, "0-30 min"), (30, 60, "30-60 min"), (60, 120, "1-2 h"),
                (120, 240, "2-4 h"), (240, 10**6, "4 h +"))


def bucket_of(lead):
    if lead is None:
        return "unknown"
    if lead < 0:
        return "already past"
    for lo, hi, label in LEAD_BUCKETS:
        if lo <= lead < hi:
            return label
    return "unknown"


def lead_rows(cards, mode):
    """Precision by HOW FAR AHEAD the warning was, forecast-only cards (a card
    naming a later leg), scored population only. This is the cut that says
    whether the advisor can see trouble coming or only describe it arriving."""
    groups = defaultdict(list)
    for c in cards.values():
        if c["mode"] != mode or c["self_scored"] or c["quality"] != "ok":
            continue
        groups[(bucket_of(c.get("lead_min")), c["kind"])].append(c)
        groups[(bucket_of(c.get("lead_min")), "ALL")].append(c)
    order = ["already past"] + [b[2] for b in LEAD_BUCKETS] + ["unknown"]
    rows = []
    for (b, kind), g in groups.items():
        rows.append({
            "estimates": mode, "lead_bucket": b, "kind": kind, "scored": len(g),
            "pct_late_15": round(100.0 * sum(c["late_min"] > 15 for c in g) / len(g), 1),
            "pct_late_10": round(100.0 * sum(c["late_min"] > 10 for c in g) / len(g), 1),
            "pct_late_any": round(100.0 * sum(c["late_min"] > 0 for c in g) / len(g), 1),
        })
    return sorted(rows, key=lambda r: (order.index(r["lead_bucket"])
                                       if r["lead_bucket"] in order else 99,
                                       r["kind"] != "ALL", r["kind"]))


def kind_rows(cards, mode):
    """Precision by the IMPACT leg's trip type. An airport arrival is scored
    against a booked time that is really the landing slot, so deplaning noise
    lands in this column — 19 saw the same thing."""
    groups = defaultdict(list)
    for c in cards.values():
        if c["mode"] != mode or c["self_scored"] or c["quality"] != "ok":
            continue
        groups[c.get("impact_kind") or "?"].append(c)
    return [{"estimates": mode, "impact_trip_type": k, "scored": len(g),
             "pct_late_15": round(100.0 * sum(c["late_min"] > 15 for c in g) / len(g), 1),
             "pct_late_10": round(100.0 * sum(c["late_min"] > 10 for c in g) / len(g), 1)}
            for k, g in sorted(groups.items(), key=lambda kv: -len(kv[1]))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--dates", nargs="*", default=[])
    ap.add_argument("--tick-min", type=int, default=15)
    ap.add_argument("--from-hour", type=int, default=6)
    ap.add_argument("--to-hour", type=int, default=23)
    ap.add_argument("--estimates", choices=("both", "live", "masked"), default="both")
    args = ap.parse_args()
    t0 = time.time()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    con = C.connect()
    hz = C.Horizon(con)
    C.preamble("23_advisor_replay.py",
               "does the Recovery Advisor earn a place on a dispatcher's screen?",
               hz, ASSUMPTIONS)
    dates = pick_dates(con, hz, args.days, args.dates)
    print(f"\ndates ({len(dates)}): {dates[0]} .. {dates[-1]}")
    modes = (["live", "masked"] if args.estimates == "both" else [args.estimates])
    per_day = len(ticks_for(dates[0], args.from_hour, args.to_hour, args.tick_min))
    print(f"ticks       : {args.from_hour:02d}:00-{args.to_hour:02d}:00 every "
          f"{args.tick_min} min = {per_day}/day")
    print(f"estimates   : {', '.join(modes)}  "
          f"({len(dates) * per_day * len(modes)} advisor computes)")

    g17 = load_module("gate17", "17_build3_gate.py")
    g19 = load_module("clock19", "19_clock_calibration.py")
    g17.django_on_copy()
    from django.db import connection as dj
    cur = dj.cursor()

    cards, load, timing = {}, [], []
    for i, day in enumerate(dates, 1):
        for mode in modes:
            t1 = time.time()
            replay_date(con, cur, g19, day,
                        ticks_for(day, args.from_hour, args.to_hour, args.tick_min),
                        mode == "masked", cards, load, timing)
            print(f"  [{i:>2}/{len(dates)}] {day} {mode:<6} "
                  f"{time.time() - t1:6.1f}s", flush=True)

    # ── rail load ───────────────────────────────────────────────────────────
    C.sub("RAIL LOAD — what a dispatcher would see, per glance")
    for mode in modes:
        rows = [r for r in load if r[2] == mode]
        if not rows:
            continue
        n = [r[3] for r in rows]
        crit = [r[4] for r in rows]
        print(f"  estimates={mode:<7} cards/glance  mean {sum(n) / len(n):.1f}  "
              f"P50 {C.pct(n, 50):.0f}  P90 {C.pct(n, 90):.0f}  max {max(n)}   "
              f"critical/glance mean {sum(crit) / len(crit):.1f}   "
              f"truncated {100.0 * sum(r[7] for r in rows) / len(rows):.0f}% of ticks")

    # ── precision ───────────────────────────────────────────────────────────
    rows = precision_rows(cards, len(dates))
    C.sub("PRECISION BY CARD CLASS — did the impact leg actually run late?")
    print(f"{'est':<7}{'kind':<15}{'sev':<9}{'basis':<17}{'cards':>7}{'/day':>7}"
          f"{'plans%':>8}{'1leg%':>7}{'scored':>7}{'>15':>7}{'>10':>7}{'any':>7}"
          f"{'>15 fcst':>10}")
    for r in rows:
        f = lambda v: f"{v:>7.1f}" if v is not None else f"{'-':>7}"
        print(f"{r['estimates']:<7}{r['kind']:<15}{r['severity']:<9}"
              f"{r['basis'][:16]:<17}{r['unique_cards']:>7}{r['cards_per_day']:>7.1f}"
              f"{r['pct_with_plans']:>8.1f}{r['pct_single_leg']:>7.1f}{r['scored']:>7}"
              f"{f(r['pct_late_15'])}{f(r['pct_late_10'])}{f(r['pct_late_any'])}"
              f"{(f'{r[chr(39)+chr(39)]}' if False else (f"{r['pct_late_15_multileg']:>10.1f}" if r['pct_late_15_multileg'] is not None else f"{'-':>10}"))}")
    print(f"\n  D5 bar is 70% at >15 min, and '>15 fcst' is the column it applies to: "
          f"precision on the\n  cards that name a LATER leg. A one-leg card has no "
          f"downstream victim — it describes a\n  leg that is already late, so its "
          f"score is a tautology, not a forecast.")

    # ── can it see it coming? ───────────────────────────────────────────────
    ref = "live" if "live" in modes else modes[0]
    lrows = lead_rows(cards, ref)
    C.sub("HOW FAR AHEAD — precision by time from the warning to the moment "
          "(forecast-only cards)")
    print(f"{'lead time':<14}{'kind':<16}{'scored':>8}{'>15':>8}{'>10':>8}{'any':>8}")
    for r in lrows:
        mark = "  <-- D5" if r["pct_late_15"] >= 70 and r["scored"] >= 20 else ""
        print(f"{r['lead_bucket']:<14}{r['kind']:<16}{r['scored']:>8}"
              f"{r['pct_late_15']:>8.1f}{r['pct_late_10']:>8.1f}"
              f"{r['pct_late_any']:>8.1f}{mark}")
    krows = kind_rows(cards, ref)
    C.sub("WHAT KIND OF TRIP THE WARNING WAS ABOUT")
    print(f"{'impact trip':<20}{'scored':>8}{'>15':>8}{'>10':>8}")
    for r in krows:
        print(f"{str(r['impact_trip_type'])[:19]:<20}{r['scored']:>8}"
              f"{r['pct_late_15']:>8.1f}{r['pct_late_10']:>8.1f}")

    # ── timing ──────────────────────────────────────────────────────────────
    ok = [r[3] for r in timing if r[3] >= 0]
    errs = Counter(r[5] for r in timing if r[5])
    C.sub("COMPUTE TIME (budget is ADVISOR_BUDGET_MS = 4000)")
    if ok:
        print(f"  n={len(ok)}  P50 {C.pct(ok, 50):.0f} ms  P90 {C.pct(ok, 90):.0f} ms  "
              f"max {max(ok)} ms  over budget: "
              f"{100.0 * sum(1 for v in ok if v > 4000) / len(ok):.1f}% of ticks")
    if errs:
        print(f"  errors: {dict(errs)}")

    cols = list(rows[0].keys()) if rows else []
    p1 = C.write_csv("23_advisor_precision.csv", cols,
                     [[r[c] for c in cols] for r in rows])
    p2 = C.write_csv("23_cards_per_day.csv",
                     ["date", "tick", "estimates", "cards", "critical", "hygiene",
                      "with_plans", "truncated"], load)
    C.write_csv("23_lead_time.csv",
                ["estimates", "lead_bucket", "kind", "scored", "pct_late_15",
                 "pct_late_10", "pct_late_any"],
                [[r[c] for c in ("estimates", "lead_bucket", "kind", "scored",
                                 "pct_late_15", "pct_late_10", "pct_late_any")]
                 for m in modes for r in lead_rows(cards, m)])
    C.write_csv("23_impact_trip_type.csv",
                ["estimates", "impact_trip_type", "scored", "pct_late_15", "pct_late_10"],
                [[r[c] for c in ("estimates", "impact_trip_type", "scored",
                                 "pct_late_15", "pct_late_10")]
                 for m in modes for r in kind_rows(cards, m)])
    p3 = C.write_csv("23_timing.csv",
                     ["date", "tick", "estimates", "ms", "cards", "error"], timing)
    for p in (p1, p2, p3):
        print(f"\nWrote: {os.path.relpath(p, C.REPO_ROOT)}")
    print(f"runtime: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
