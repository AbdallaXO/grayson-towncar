"""
One definition of "late" for the whole dispatch board.

Before this module the board carried six independent engines that each answered
"is this pickup at risk?" with different numbers — the live GPS sweep, the clock
flags on the timeline pill, the gap chips (twice), the row tint, and the ops
tight-turn scanner — plus the real feasibility engine that auto-assign uses.
They disagreed at the threshold, so a turn auto-assign had just seated as legal
could paint red the moment it hit the board, and a dispatcher who learned that a
red can be wrong stopped believing any of them.

Everything here is a pure function over already-loaded data: no queries, no
writes, no clock reads except the `now` a caller passes in. Same contract as
feasibility_guards.py, so it is cheap to call inside render loops and trivial to
unit-test.

The four questions this module answers
--------------------------------------
1. WHEN is the driver actually due?          -> pickup_deadline()
2. Given his slack, how bad is it?           -> classify_slack() / turn_band()
3. GPS or clock — which one do I believe?    -> pickup_risk()
4. Is this signal still worth showing?       -> is_overdue_stale() /
                                                should_flag_not_moving()

Slack is always ``time_until_deadline - travel_time_still_needed``. Every band
below reads slack, never raw clock distance — that distinction is the whole
point. A driver parked five minutes from a pickup twenty minutes out is not a
problem; the old rules called him one.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from django.utils import timezone

# ════════════════════════════════════════════════════════════════════════════
# POLICY CONSTANTS — the numbers, in one place
# ════════════════════════════════════════════════════════════════════════════

# Airport ARRIVAL: how long after the plane reaches the gate the driver has to be
# standing at the meet point. Grayson does NOT pick up at the curb for airports —
# the driver parks and walks in to the commercial-lane meet point inside the
# terminal, so this covers his walk-in, not a curbside pull-up.
# Founder rule (2026-06-12, reaffirmed 2026-07-31): a 10:30 flight means the
# driver is inside and waiting by 10:40.
ARRIVAL_MEET_GRACE_MIN = 10

# When the GUEST is realistically ready to roll after the gate — deplaning, walk,
# bags. This is a different question from "is the driver late" and must never be
# used to judge lateness; it feeds chain/end-time math only. Keeping the two
# numbers apart (10 vs 15) is deliberate: they answer different questions.
PAX_READY_MIN = 15

# How long a real airport pickup takes end to end — gate to guest actually in the
# car. Tracks scheduler.STATIC_FLOOR_DWELL_MIN / get_airport_dwell_time's fallback.
#
# This is the clock the "pickup hasn't happened yet" flag must run on, and it is
# NOT the driver's meet deadline. Two different questions:
#   ARRIVAL_MEET_GRACE_MIN (10) — must the DRIVER be standing there? (lateness)
#   ARRIVAL_DWELL_MIN      (45) — should the pickup have HAPPENED by now?
# Judging the second with the first reports a driver "35 min overdue" at 2:45 on a
# 2:00 gate, when he is exactly on schedule and the guest is still at baggage claim.
ARRIVAL_DWELL_MIN = 45

# Thin-slack amber. Under this much spare time the pickup is worth a glance.
WATCH_SLACK_MIN = 10

# A stationary vehicle only matters once the driver actually needs to be rolling.
# Gate this on SLACK, never on raw time-to-pickup: the old rule ("not moving and
# pickup within 30 min") fired on every parked driver near a pickup, including
# one five minutes away with a quarter hour to spare, which is precisely the
# behaviour we want from a driver. That single rule produced most of the board's
# amber and taught dispatchers to ignore the colour.
DEPART_SOON_SLACK_MIN = 5

# Past this much overdue with nobody having touched it, a pickup is stale status
# — an unpressed button — not a live risk. The GPS engine already drew this line
# at 45 min (samsara_risk.PAST_PICKUP_GRACE_MIN) with exactly this reasoning; the
# clock flags never did, which is why the board showed "108m late" pills that no
# dispatcher could act on. Same judgement, now applied in both places.
OVERDUE_STALE_MIN = 45

# Turnaround slack (gap between two jobs, AFTER subtracting the required
# turnaround). Matches scheduler.check_feasibility, which hard-rejects a negative
# buffer and warns "Tight" under 15 — so the board and the assignment engine can
# no longer describe the same turn differently.
TURN_TIGHT_SLACK_MIN = 15

# Risk band vocabulary. These are the Leg.DISPATCH_RISK_CHOICES values; every
# surface (sweep, panel, pill, chips) maps onto this one ladder.
ON_TIME = "on_time"
WATCH = "watch"
AT_RISK = "at_risk"
LATE = "late"
UNKNOWN = "unknown"


# ════════════════════════════════════════════════════════════════════════════
# WHEN IS THE DRIVER DUE?
# ════════════════════════════════════════════════════════════════════════════

def _to_naive(dt):
    """Local naive, matching datetime.combine(pickup_date, pickup_time)."""
    if dt is None:
        return None
    return timezone.make_naive(dt, timezone.get_current_timezone()) if timezone.is_aware(dt) else dt


def _to_aware(dt):
    """Aware in the current timezone."""
    if dt is None:
        return None
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


def booked_pickup_dt(leg, *, aware=True):
    """The leg's booked pickup as a datetime, or None when either half is missing.

    pickup_date/pickup_time are a naive DateField+TimeField pair with no combined
    column, so every consumer has to do this — and half of them left it naive
    while the other half made it aware. Do it here, once.
    """
    if not getattr(leg, "pickup_date", None) or not getattr(leg, "pickup_time", None):
        return None
    naive = datetime.combine(leg.pickup_date, leg.pickup_time)
    return _to_aware(naive) if aware else naive


def controlling_flight(leg):
    """The flight that drives this leg's pickup timing — WITHOUT firing a query
    when the caller has already prefetched.

    ``Leg.controlling_flight`` resolves the same thing, but it does
    ``legflight_set.filter(is_controlling=True)``, and a ``.filter()`` on a
    related manager always bypasses the prefetch cache and hits the DB. That is
    fine for a detail page and an N+1 on a board rendering ~140 legs, so read the
    cache directly when it's there and only fall back to the model property when
    it isn't. Callers doing bulk work should
    ``prefetch_related("legflight_set__flight")``.
    """
    cache = getattr(leg, "_prefetched_objects_cache", None) or {}
    rows = cache.get("legflight_set")
    if rows is not None:
        for lf in rows:
            if lf.is_controlling:
                return lf.flight
        return getattr(leg, "flight_information", None)
    return getattr(leg, "controlling_flight", None)


def controlling_arrival_dt(leg, *, aware=True):
    """Best-available gate arrival for this leg's controlling flight, or None.

    Honours LegFlight.is_controlling and falls back to the legacy
    flight_information OneToOne, so a multi-flight leg is judged against the
    flight that actually drives its timing rather than whichever one happens to
    be attached.

    Guarded on the arrival landing on the leg's own pickup_date: a stale or
    wrong-dated flight record must not hijack the deadline. When it doesn't
    match, callers fall back to the booked time.
    """
    flight = controlling_flight(leg)
    if flight is None:
        return None
    arr = flight.best_arrival_local()
    if arr is None:
        return None
    if timezone.localdate(_to_aware(arr)) != leg.pickup_date:
        return None
    return _to_aware(arr) if aware else _to_naive(arr)


def pickup_deadline(leg, *, aware=True):
    """(deadline, basis) — when the driver must be at the pickup, and why.

    * flight-tracked arrival (a normal airport arrival OR an airport->cruise-port
      transfer, via leg.is_flight_tracked_arrival(), so the cruise case is no
      longer silently excluded): gate arrival + ARRIVAL_MEET_GRACE_MIN.
      A delayed flight therefore moves the deadline out, and the driver stops
      being "late" for a plane that hasn't landed.
    * everything else — departures/returns, point-to-point, hotel, from-cruise:
      the booked pickup time, no grace. The guest is standing there.

    `basis` is a short human string for the tooltip, so a dispatcher can see
    which rule produced the flag instead of having to trust it.
    Returns (None, "") when the leg has no usable time at all.
    """
    is_tracked = False
    try:
        is_tracked = bool(leg.is_flight_tracked_arrival())
    except Exception:
        # Bare/synthetic legs in tests and planner scratch rows may not carry the
        # location strings the predicate reads. Fall through to booked time.
        is_tracked = False

    if is_tracked:
        arr = controlling_arrival_dt(leg, aware=aware)
        if arr is not None:
            deadline = arr + timedelta(minutes=ARRIVAL_MEET_GRACE_MIN)
            gate = arr.strftime("%I:%M").lstrip("0")
            meet = deadline.strftime("%I:%M").lstrip("0")
            return deadline, f"flight gated {gate} · meet by {meet}"

    booked = booked_pickup_dt(leg, aware=aware)
    if booked is None:
        return None, ""
    return booked, "booked pickup"


def pickup_expected_dt(leg, *, aware=True, dwell_min=None):
    """(expected, basis) — when the pickup should have COMPLETED, guest in the car.

    Deliberately NOT pickup_deadline(). That one answers "must the driver be
    standing there yet" and is the right clock for a live ETA. This one answers
    "should this job have started by now", which is the right clock for the
    board's "no pickup recorded" flag.

    * flight-tracked arrival: gate arrival + ARRIVAL_DWELL_MIN. A guest who lands
      at 2:00 is realistically in the car around 2:45 — deplane, walk, bags. Using
      the driver's 10-minute meet deadline here flagged every on-schedule airport
      pickup as overdue from 2:13 onward.
    * everything else: the booked pickup time. The guest is already standing there,
      so the moment it passes with nothing recorded, something is wrong.

    `dwell_min` lets a caller substitute a real per-route dwell
    (scheduler.get_airport_dwell_time) instead of the flat fallback.
    """
    from datetime import timedelta as _td
    dwell = ARRIVAL_DWELL_MIN if dwell_min is None else dwell_min

    is_tracked = False
    try:
        is_tracked = bool(leg.is_flight_tracked_arrival())
    except Exception:
        is_tracked = False

    if is_tracked:
        arr = controlling_arrival_dt(leg, aware=aware)
        if arr is not None:
            expected = arr + _td(minutes=dwell)
            gate = arr.strftime("%I:%M").lstrip("0")
            return expected, f"flight gated {gate} · ~{dwell} min to clear the airport"

    booked = booked_pickup_dt(leg, aware=aware)
    if booked is None:
        return None, ""
    return booked, "booked pickup"


# ════════════════════════════════════════════════════════════════════════════
# HOW BAD IS IT?
# ════════════════════════════════════════════════════════════════════════════

def classify_slack(slack_min):
    """Band a live pickup slack (minutes spare after driving there).

    slack < 0                -> at_risk  (he cannot make it)
    slack < WATCH_SLACK_MIN  -> watch    (cutting it close)
    otherwise                -> on_time
    """
    if slack_min is None:
        return UNKNOWN
    if slack_min < 0:
        return AT_RISK
    if slack_min < WATCH_SLACK_MIN:
        return WATCH
    return ON_TIME


def turn_band(slack_min):
    """Band a TURNAROUND slack — gap between two jobs minus the required
    turnaround (see feasibility_guards.required_turnaround).

    Mirrors scheduler.check_feasibility exactly: negative is infeasible, under 15
    is "Tight". Returns '' | 'tight' | 'critical' for the timeline gap chips.
    """
    if slack_min is None:
        return ""
    if slack_min < 0:
        return "critical"
    if slack_min < TURN_TIGHT_SLACK_MIN:
        return "tight"
    return ""


# ════════════════════════════════════════════════════════════════════════════
# ONE CUE PER BAR — GPS vs clock precedence
# ════════════════════════════════════════════════════════════════════════════

def pickup_risk(*, pickup_overdue, pickup_stalled, overdue_mins,
                gps_status, gps_eta_mins, gps_reason):
    """Fold the live-GPS "will he make the pickup?" band together with the clock-based
    pickup flags into ONE escalating cue, so a dispatcher sees a single signal per bar.

    The GPS band (from the Samsara sweep — ``dispatch_risk_status``) is the PROACTIVE
    signal: it fires *before* the pickup time, which is the whole point — time to line
    up a backup before the guest is affected.
        * ``at_risk`` — the vehicle's live ETA exceeds the time left to the pickup
                        (he's simply too far to make it);
        * ``watch``   — thin slack, or sitting still with the pickup coming up
                        (not on the way soon enough);
        * ``late``    — GPS-confirmed past the pickup, still not there.
    The clock flags (``pickup_stalled`` / ``pickup_overdue``) are the FALLBACK for when
    there's no fresh telematics at all — affiliates, un-onboarded vehicles, stale GPS.
    They are exactly that — a fallback — so a fresh GPS ``on_time`` SUPPRESSES them:
    the clock only knows nobody pressed a button, while the telemetry knows the car is
    positioned to make the pickup. Any other GPS state (missing, ``unknown``, stale)
    leaves the clock in charge, because no signal is not a clean bill of health.

    Returns ``{tier, source, label, reason}``; tier is '', 'watch' or 'critical'.
    Pure function so the precedence can be unit-tested. (Promoted from
    ``views._pickup_risk`` so the Recovery Advisor reads the exact precedence
    ladder the board renders — never a re-derivation.)
    """
    _eta = f'{gps_eta_mins}m' if gps_eta_mins is not None else None

    # ── critical: won't make it, or already blown the pickup ──
    if gps_status == 'at_risk':
        return {'tier': 'critical', 'source': 'gps',
                'label': _eta if _eta else 'at risk',   # pin icon already implies "ETA"
                'reason': gps_reason or 'GPS ETA exceeds time to pickup'}
    if gps_status == 'late':
        return {'tier': 'critical', 'source': 'gps',
                'label': 'late', 'reason': gps_reason or 'Past pickup — still en route'}
    # A fresh GPS 'on_time' OUTRANKS the clock's stalled flag. The clock only knows
    # that no button was pressed; the vehicle telemetry knows the car is positioned to
    # make it. Believing the clock over live GPS is what put a critical red on drivers
    # who were demonstrably fine — usually just a driver who hadn't tapped "on the way".
    # Absent / stale / 'unknown' GPS still yields to the clock: no signal is not a
    # clean bill of health.
    if pickup_stalled and gps_status != 'on_time':
        return {'tier': 'critical', 'source': 'clock',
                'label': f'{overdue_mins}m late',
                'reason': f'Pickup {overdue_mins} min overdue — no driver status yet'}

    # ── watch: cutting it close / not moving / past pickup but en route ──
    if gps_status == 'watch':
        return {'tier': 'watch', 'source': 'gps',
                'label': _eta if _eta else 'watch',   # pin icon already implies "ETA"
                'reason': gps_reason or 'Little slack to pickup'}
    if pickup_overdue and gps_status != 'on_time':
        return {'tier': 'watch', 'source': 'clock',
                'label': f'{overdue_mins}m late',
                'reason': f'Pickup {overdue_mins} min overdue — driver en route'}

    return {'tier': '', 'source': '', 'label': '', 'reason': ''}


# ════════════════════════════════════════════════════════════════════════════
# IS THIS SIGNAL STILL WORTH SHOWING?
# ════════════════════════════════════════════════════════════════════════════

def should_flag_not_moving(slack_min, is_moving):
    """True when a stationary vehicle is genuinely a problem.

    Only once the driver needs to be leaving. Sitting still with real slack is
    correct behaviour and must not raise anything — this is the rule that used to
    amber most of the parked fleet.
    """
    if is_moving or slack_min is None:
        return False
    return slack_min <= DEPART_SOON_SLACK_MIN


def is_overdue_stale(minutes_past):
    """True when an overdue pickup has aged out of being a live signal.

    Past OVERDUE_STALE_MIN nobody has acted on it, so it is a data-hygiene
    problem for the status workflow, not something to keep shouting about on the
    live board.
    """
    if minutes_past is None:
        return False
    return minutes_past > OVERDUE_STALE_MIN
