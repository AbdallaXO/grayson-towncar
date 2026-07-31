"""
Samsara Phase 2 — schedule-aware ETA + late-risk logic.

Pure-ish functions (only external dependency is the cached, traffic-aware
get_drive_time). The background sweep in samsara_scheduler.py calls these and
persists the result onto the Leg; nothing here is called in a request path.

The question we answer per driver: "where is the vehicle relative to the
driver's NEXT relevant stop, and will he make it?"
"""
import logging
import math
from datetime import datetime, timedelta

from django.utils import timezone

from drivers.utils import get_drive_time
from dispatching import pickup_policy

logger = logging.getLogger(__name__)

# Tunables. The band/slack thresholds live in dispatching/pickup_policy.py so the
# sweep, the panel, the timeline pill and the gap chips can't drift apart again.
WATCH_SLACK_MIN = pickup_policy.WATCH_SLACK_MIN
# How long after a scheduled pickup we still surface a live "late" badge. Beyond
# this the leg is treated as stale/handled by the normal status workflow (a pickup
# overdue by hours that nobody marked done is noise, not a live-ETA signal). The
# board's clock flags now draw the same line via pickup_policy.OVERDUE_STALE_MIN.
PAST_PICKUP_GRACE_MIN = pickup_policy.OVERDUE_STALE_MIN

# A stale GPS fix from a PARKED car is still usable this long (ignition-off
# gateways report sparsely; the car hasn't moved, so the old fix is still where
# it sits). Bounded in case the gateway itself died mid-day.
PARKED_POSITION_MAX_AGE_HOURS = 8

# Live-tracking PANEL display tunables.
PANEL_TIGHT_BUFFER_MIN = pickup_policy.WATCH_SLACK_MIN   # spare time under this => "tight"
# Above this much slack the "arrives N min early" projection is noise (the driver
# isn't en route yet) -> show a calm waiting card instead: pickup countdown +
# how far the vehicle sits from it. Risk states ignore this (a far-future pickup
# the car can't reach in time must still warn).
PANEL_WAIT_SLACK_MIN = 60
PANEL_DWELL_MIN = 8              # stationary at least this long => stalled candidate
# NOTE: the old PANEL_DEPARTURE_WINDOW_MIN (45) is gone. "Not moving" is now gated
# on slack via pickup_policy.should_flag_not_moving — how far the car still has to
# drive is what decides whether sitting still is a problem, not the raw countdown.
# For an ALREADY-OVERDUE pickup where the driver isn't on the way (usually just stale
# status), we stay quiet — except an arrival whose flight is already at the gate while
# the vehicle is still more than this many minutes out (amber warning, he should be moving).
PANEL_STAGE_WARN_MIN = 10
# Minutes a driver spends at a drop-off before he's free for the next pickup. Used by the
# chain: time-to-current-dropoff + this + drive(dropoff -> next pickup).
DROPOFF_SERVICE_MIN = 5

# --- Cost control: gate the PAID Google drive-time call --------------------------
# The drive-time minutes are stable while a car is parked, but slack shrinks every
# cycle as the clock advances. So we gate ONLY the paid Google lookup; the free
# slack/band math always re-runs against the current clock (see evaluate()).
# Vehicle drift (meters) below which we treat the car as "hasn't moved" and reuse the
# stored ETA. Parked GPS jitters a few meters per poll — that must NOT count as moved.
ETA_MOVE_REUSE_M = 150
# A pickup further out than this renders as the calm "waiting" card (see
# PANEL_WAIT_SLACK_MIN); no point paying to refresh its ETA every cycle. Reuse the
# stored value until the leg enters the window.
ETA_FAR_FUTURE_MIN = 180

# Leg statuses meaning the driver has the guest / is at pickup -> next stop is dropoff.
_ON_TRIP = {"picked-up", "on-location"}
_DONE = {"completed", "cancelled"}


def effective_pickup_dt(leg):
    """
    When the driver is DUE at this pickup — the deadline the live ETA is judged
    against. Delegates to pickup_policy.pickup_deadline, so a flight-tracked arrival
    resolves to gate arrival + the 10-minute in-terminal meet grace (a delay widens
    the window correctly) and everything else to the booked pickup.

    The grace matters: without it this measured against the raw gate time, so a
    driver who would arrive at 2:05 for a 2:00 landing read "at risk" even though
    the founder rule gives him until 2:10 and auto-assign had already seated the job
    on that basis. Returns an aware datetime or None.
    """
    deadline, _basis = pickup_policy.pickup_deadline(leg, aware=True)
    return deadline


def choose_active_target(legs, now=None):
    """
    Given a driver's legs for the day (any order), pick the single relevant "next
    stop" and what we measure to. Returns a dict or None.

    Priority:
      1. If the driver is mid-trip (picked-up / on-location) -> the dropoff he's
         currently running (always relevant, no time filter).
      2. Otherwise the next UPCOMING pickup -- the earliest one whose (flight-aware)
         time is in the future or only recently overdue (within PAST_PICKUP_GRACE_MIN).
         Long-past pickups are skipped: a leg overdue by hours that was never marked
         done is stale data, not a live ETA the dispatcher needs a badge for.
      3. Nothing relevant -> None (no badge).
    """
    now = now or timezone.now()
    open_legs = sorted(
        (l for l in legs if (l.status or "") not in _DONE),
        key=lambda l: (l.pickup_time or datetime.max.time()),
    )

    # 1. Driver mid-trip -> dropoff.
    for leg in open_legs:
        if (leg.status or "") in _ON_TRIP:
            if not leg.dropoff_location:
                return None
            return {"leg": leg, "kind": "dropoff",
                    "location": leg.dropoff_location, "target_time": None}

    # 2. Next upcoming (or recently overdue) pickup.
    for leg in open_legs:
        if not leg.pickup_location:
            continue
        tt = effective_pickup_dt(leg)
        if tt is None:
            continue
        minutes_past = (now - tt).total_seconds() / 60
        if minutes_past <= PAST_PICKUP_GRACE_MIN:
            return {"leg": leg, "kind": "pickup",
                    "location": leg.pickup_location, "target_time": tt}

    return None


def _position_usable(vehicle, now):
    """
    True when the vehicle's last GPS fix can back a drive-time estimate. Fresh is
    always usable. A STALE fix is still usable when the last sample said the car
    was NOT moving — a parked car's gateway reports sparsely, but any drive
    produces a new sample, so the old fix is still where the car sits (capped at
    PARKED_POSITION_MAX_AGE_HOURS). Stale-while-driving stays unusable.
    """
    if vehicle.samsara_last_latitude is None or vehicle.samsara_last_longitude is None:
        return False
    if vehicle.samsara_is_fresh:
        return True
    last_seen = getattr(vehicle, "samsara_last_seen_at", None)
    if vehicle.samsara_movement_status in ("idle", "off") and last_seen:
        return (now - last_seen).total_seconds() / 3600 <= PARKED_POSITION_MAX_AGE_HOURS
    return False


def _blank_fields():
    return {
        "dispatch_eta_minutes": None,
        "dispatch_eta_target": "",
        "dispatch_eta_target_time": None,
        "dispatch_risk_status": "",
        "dispatch_risk_reason": "",
        "dispatch_is_moving": None,
        "dispatch_stationary_minutes": None,
        "dispatch_vehicle_label": "",
    }


def _haversine_m(lat1, lng1, lat2, lng2):
    """Great-circle distance in meters between two lat/lng points (float or Decimal).
    Returns +inf if any coordinate is missing, so a missing anchor never reads as
    'hasn't moved'."""
    if lat1 is None or lng1 is None or lat2 is None or lng2 is None:
        return float("inf")
    r = 6_371_000.0  # Earth radius, meters
    lat1, lng1, lat2, lng2 = float(lat1), float(lng1), float(lat2), float(lng2)
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _can_reuse_eta(vehicle, leg, target_location, target_time, now, refresh_allowed=True):
    """
    True when we may REUSE leg.dispatch_eta_minutes instead of paying for a fresh
    Google drive-time lookup. Reuse is only safe when the *inputs to the drive time*
    are effectively unchanged:
      - a stored ETA exists AND we know the origin GPS it was computed against,
      - the target location is identical, and one of:
          * this is a cost-gated "no-refresh" tick (Lever 4 cadence), or
          * the pickup is beyond ETA_FAR_FUTURE_MIN (Lever 2 window gate), or
          * the vehicle hasn't moved >= ETA_MOVE_REUSE_M since (Lever 1 drift gate).
    The caller ALWAYS recomputes the risk band from the stored minutes against the
    current clock — only the paid call is gated here.
    """
    if getattr(leg, "dispatch_eta_minutes", None) is None:
        return False  # nothing to reuse -> must compute at least once
    o_lat = getattr(leg, "dispatch_eta_origin_lat", None)
    o_lng = getattr(leg, "dispatch_eta_origin_lng", None)
    if o_lat is None or o_lng is None:
        return False  # no anchor -> can't judge drift -> recompute
    if (getattr(leg, "dispatch_eta_origin_target", "") or "") != (target_location or ""):
        return False  # target moved -> must refresh
    if not refresh_allowed:
        return True   # Lever 4: cost-gated cycle; reuse (band still recomputes)
    if target_time is not None:
        minutes_to_target = (target_time - now).total_seconds() / 60
        if minutes_to_target > ETA_FAR_FUTURE_MIN:
            return True  # Lever 2: far-future pickup, don't refresh
    moved = _haversine_m(o_lat, o_lng,
                         vehicle.samsara_last_latitude, vehicle.samsara_last_longitude)
    return moved < ETA_MOVE_REUSE_M  # Lever 1: parked / negligible drift


def evaluate(vehicle, target, now=None, eta_override=None, eta_origin=None,
             refresh_allowed=True):
    """
    Compute the Leg.dispatch_* field values for one driver's chosen target.

    `eta_override` (minutes) lets the caller supply a precomputed drive time — used
    by the chain so the "next pickup" ETA includes finishing the current job rather
    than a naive straight-line-from-GPS estimate.

    Returns a dict of field values, or None when nothing should render
    (no vehicle / not onboarded / affiliate). A mapped-but-stale vehicle returns
    an "unknown" (grey) result — "we should know but don't".
    """
    now = now or timezone.now()
    if target is None:
        return None
    if vehicle is None or not getattr(vehicle, "samsara_enabled", False):
        return None  # un-onboarded in-house or affiliate -> render nothing

    fields = _blank_fields()
    fields["dispatch_eta_target"] = target["kind"]
    fields["dispatch_eta_target_time"] = target["target_time"]
    fields["dispatch_vehicle_label"] = getattr(vehicle, "vehicle_number", "") or ""

    if not _position_usable(vehicle, now):
        fields["dispatch_risk_status"] = "unknown"
        fields["dispatch_risk_reason"] = "Vehicle telematics stale (>15 min)"
        return fields

    # Movement snapshot (for the panel's "vehicle not moving" dwell detection).
    fields["dispatch_is_moving"] = (vehicle.samsara_movement_status == "driving")
    if not fields["dispatch_is_moving"] and vehicle.samsara_stationary_since:
        fields["dispatch_stationary_minutes"] = max(
            0, int((now - vehicle.samsara_stationary_since).total_seconds() / 60))
    else:
        fields["dispatch_stationary_minutes"] = 0

    leg = target["leg"]
    if eta_override is not None:
        # Caller supplied the minutes (the chain). It also tells us which origin the
        # value is anchored to: carried-forward GPS on reuse, current GPS on a fresh
        # compute. Falling back to current GPS keeps old call sites working.
        drive_min = eta_override
        o_lat, o_lng = eta_origin if eta_origin else (
            vehicle.samsara_last_latitude, vehicle.samsara_last_longitude)
    elif _can_reuse_eta(vehicle, leg, target["location"], target["target_time"],
                        now, refresh_allowed):
        # No paid Google call: reuse the stored minutes. The band math below still
        # runs against the current clock, so a parked car's slack keeps shrinking and
        # it flips to at_risk on schedule. Keep the ORIGINAL anchor so drift
        # accumulates across cycles (a car creeping under the threshold each poll
        # still recomputes once cumulative drift crosses it).
        drive_min = leg.dispatch_eta_minutes
        o_lat = getattr(leg, "dispatch_eta_origin_lat", None)
        o_lng = getattr(leg, "dispatch_eta_origin_lng", None)
    else:
        origin = f"{vehicle.samsara_last_latitude},{vehicle.samsara_last_longitude}"
        dt = get_drive_time(origin, target["location"], snap_origin=True)
        if not dt or dt.get("duration_seconds") is None:
            fields["dispatch_risk_status"] = "unknown"
            fields["dispatch_risk_reason"] = "Could not compute drive time"
            return fields
        drive_min = round(dt["duration_seconds"] / 60)
        o_lat, o_lng = vehicle.samsara_last_latitude, vehicle.samsara_last_longitude
    fields["dispatch_eta_minutes"] = drive_min
    fields["dispatch_eta_origin_lat"] = o_lat
    fields["dispatch_eta_origin_lng"] = o_lng
    fields["dispatch_eta_origin_target"] = target["location"]

    # Dropoff target: no hard deadline -> ETA only, no risk band.
    if target["kind"] == "dropoff":
        fields["dispatch_risk_reason"] = f"~{drive_min} min to dropoff"
        return fields

    # Pickup target: band the slack.
    target_time = target["target_time"]
    if target_time is None:
        fields["dispatch_risk_status"] = "on_time"
        fields["dispatch_risk_reason"] = f"~{drive_min} min to pickup"
        return fields

    minutes_to_target = (target_time - now).total_seconds() / 60
    not_picked_up = (leg.status or "") not in _ON_TRIP

    if minutes_to_target < 0 and not_picked_up:
        fields["dispatch_risk_status"] = "late"
        fields["dispatch_risk_reason"] = f"Past pickup by {abs(round(minutes_to_target))} min"
        return fields

    slack = minutes_to_target - drive_min
    is_not_moving = vehicle.samsara_movement_status in ("idle", "off")
    # "Not moving" is judged against SLACK, never raw time-to-pickup. The old rule
    # (pickup within 30 min + idle => watch) fired on a driver parked 5 min from a
    # pickup 21 min out — 16 minutes of slack, and exactly what a good driver does
    # while he waits. It ambered most of the parked fleet and taught dispatchers
    # that amber means nothing. It is no longer its own band: thin slack is already
    # caught below, and being stopped only sharpens the REASON we show for it.
    stalled = pickup_policy.should_flag_not_moving(slack, not is_not_moving)

    band = pickup_policy.classify_slack(slack)
    fields["dispatch_risk_status"] = band
    if band == pickup_policy.AT_RISK:
        fields["dispatch_risk_reason"] = (
            f"ETA {drive_min} min vs pickup in {round(minutes_to_target)} min"
        )
    elif band == pickup_policy.WATCH:
        fields["dispatch_risk_reason"] = (
            f"Only {round(slack)} min slack, vehicle not moving" if stalled
            else f"Only {round(slack)} min slack to pickup"
        )
    else:
        fields["dispatch_risk_reason"] = f"{round(slack)} min slack"
    return fields


def _drive_min(origin, destination):
    """Drive time in minutes via the cached, traffic-aware Google helper, or None."""
    if not origin or not destination:
        return None
    dt = get_drive_time(origin, destination)
    if not dt or dt.get("duration_seconds") is None:
        return None
    return round(dt["duration_seconds"] / 60)


def _within_grace(leg, now):
    """True if the leg's pickup is upcoming or only recently overdue (not stale)."""
    if not getattr(leg, "pickup_location", ""):
        return False
    tt = effective_pickup_dt(leg)
    if tt is None:
        return False
    return (now - tt).total_seconds() / 60 <= PAST_PICKUP_GRACE_MIN


def evaluate_driver(vehicle, legs, now=None, refresh_allowed=True):
    """
    Chain-aware per-driver evaluation. Returns {leg_id: fields} for the leg(s) to flag:
      - FREE driver: feasibility to his next upcoming pickup.
      - MID-TRIP driver (picked-up / on-location): his current drop-off (ETA only)
        PLUS his next pickup, chained through finishing the current job
        (time-to-current-dropoff + DROPOFF_SERVICE_MIN + drive(dropoff -> next pickup)).
    Empty dict = nothing to flag.

    `refresh_allowed` (Lever 4 cadence gate) is threaded into every evaluate() call:
    when False, stored ETAs are reused without any Google call and only the bands
    recompute.
    """
    now = now or timezone.now()
    if vehicle is None or not getattr(vehicle, "samsara_enabled", False):
        return {}
    open_legs = sorted(
        (l for l in legs if (l.status or "") not in _DONE),
        key=lambda l: (l.pickup_time or datetime.max.time()),
    )
    if not open_legs:
        return {}

    out = {}
    mid = next((l for l in open_legs if (l.status or "") in _ON_TRIP), None)
    if mid is not None:
        # Current job: ETA to the drop-off he's running (informational, no deadline).
        dropoff_target = {"leg": mid, "kind": "dropoff",
                          "location": mid.dropoff_location, "target_time": None}
        mid_fields = evaluate(vehicle, dropoff_target, now, refresh_allowed=refresh_allowed)
        if mid_fields:
            out[mid.id] = mid_fields
        # Next pickup, CHAINED: finish the current drop-off, then drive on to it.
        nxt = next((l for l in open_legs if l.id != mid.id and _within_grace(l, now)), None)
        if nxt is not None:
            nxt_tt = effective_pickup_dt(nxt)
            pickup_target = {"leg": nxt, "kind": "next_pickup",
                             "location": nxt.pickup_location, "target_time": nxt_tt}
            if _can_reuse_eta(vehicle, nxt, nxt.pickup_location, nxt_tt, now, refresh_allowed):
                # Reuse the stored chained ETA: no Google calls (neither the dropoff
                # ETA nor the dropoff->next hop). Bands still recompute below.
                nxt_fields = evaluate(
                    vehicle, pickup_target, now,
                    eta_override=nxt.dispatch_eta_minutes,
                    eta_origin=(getattr(nxt, "dispatch_eta_origin_lat", None),
                                getattr(nxt, "dispatch_eta_origin_lng", None)),
                    refresh_allowed=refresh_allowed)
                if nxt_fields:
                    out[nxt.id] = nxt_fields
            else:
                to_dropoff = mid_fields.get("dispatch_eta_minutes") if mid_fields else None
                dropoff_to_next = _drive_min(mid.dropoff_location, nxt.pickup_location)
                if to_dropoff is not None and dropoff_to_next is not None:
                    chained = to_dropoff + DROPOFF_SERVICE_MIN + dropoff_to_next
                    nxt_fields = evaluate(vehicle, pickup_target, now, eta_override=chained)
                    if nxt_fields:
                        out[nxt.id] = nxt_fields
    else:
        target = choose_active_target(open_legs, now)
        if target is not None:
            fields = evaluate(vehicle, target, now, refresh_allowed=refresh_allowed)
            if fields:
                out[target["leg"].id] = fields
    return out


def _pickup_phrase(minutes_to_pickup):
    if minutes_to_pickup is None:
        return ""
    if minutes_to_pickup >= 0:
        return f"pickup in {minutes_to_pickup} min"
    return f"pickup {abs(minutes_to_pickup)} min ago"


def _fmt_duration(minutes):
    """Compact human duration: 55 -> '55 min', 249 -> '4h 9m', 360 -> '6h'."""
    minutes = max(0, int(round(minutes)))
    if minutes < 60:
        return f"{minutes} min"
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m" if m else f"{h}h"


def _fmt_stopped(minutes):
    """Chip-sized stopped duration: 12 -> '12m', 385 -> '6h 25m'."""
    if minutes is None:
        return ""
    minutes = max(0, int(minutes))
    if minutes < 60:
        return f"{minutes}m"
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m" if m else f"{h}h"


def _is_airport_pickup(leg):
    """True when the pickup is AT an airport terminal (an arrival). Pure string
    check on the leg's pickup_location — no DB query, safe at render."""
    from dispatching.analytics import is_airport_location
    return is_airport_location(getattr(leg, "pickup_location", "") or "")


def build_panel_context(leg, now=None):
    """
    The Samsara live-tracking PANEL's own display logic (the component's brain).

    Feasibility-based: the warning is about whether the driver can actually reach
    the pickup in time. The sweep already folded any current-job time into
    dispatch_eta_minutes for the chained "next_pickup", so this stays a pure read.

      at_risk   — can't make an upcoming pickup, OR overdue and on the way (red)
      tight     — thin slack, or stalled in the departure window (amber)
      on_track  — comfortable, or en route to a drop-off (visually quiet);
                  with > PANEL_WAIT_SLACK_MIN slack it becomes a "waiting" card
                  (pickup countdown + vehicle distance, no arrival projection)
      no_signal — vehicle mapped to Samsara but no usable GPS/ETA (grey)

    An already-overdue pickup where he is NOT on the way (usually just stale status)
    stays quiet — except an arrival whose flight is at the gate while he's still
    > PANEL_STAGE_WARN_MIN out. Returns a dict or None.

    Pure & render-safe: reads only persisted leg fields + the clock.
    """
    now = now or timezone.now()
    if (getattr(leg, "status", "") or "") in _DONE:
        return None
    if not getattr(leg, "driver_id", None):
        return None
    if not bool(getattr(leg, "dispatch_eta_is_fresh", False)):
        return None

    vehicle = getattr(leg, "dispatch_vehicle_label", "") or ""
    kind = getattr(leg, "dispatch_eta_target", "") or ""
    eta = getattr(leg, "dispatch_eta_minutes", None)

    if eta is None:
        # Vehicle IS mapped to Samsara but we couldn't get an ETA (stale GPS /
        # no drive time). Say so in grey instead of vanishing — "we should know
        # where this car is and don't" is itself a signal. Un-mapped / affiliate
        # vehicles never reach here (the sweep writes nothing for them).
        if (getattr(leg, "dispatch_risk_status", "") or "") == "unknown":
            return {"state": "no_signal", "headline": "No live GPS",
                    "evidence": getattr(leg, "dispatch_risk_reason", "") or "",
                    "eta_minutes": None, "minutes_to_pickup": None, "vehicle": vehicle,
                    "eta_clock": None, "target_clock": None, "target_label": "",
                    "moving": None, "stationary_minutes": None}
        return None

    # Display extras shared by every state: the ETA as a wall-clock arrival time,
    # the target's own clock time, and the vehicle's movement snapshot.
    target_time = getattr(leg, "dispatch_eta_target_time", None)
    stationary = getattr(leg, "dispatch_stationary_minutes", None)
    display = {
        "vehicle": vehicle,
        "eta_clock": now + timedelta(minutes=eta),
        "target_clock": target_time,
        "target_label": {"dropoff": "drop-off", "next_pickup": "next pickup"}.get(kind, "pickup"),
        "moving": getattr(leg, "dispatch_is_moving", None),
        "stationary_minutes": stationary,
        "stopped_label": _fmt_stopped(stationary),
    }

    # Mid-trip current leg: ETA to the drop-off, no deadline (visually silent).
    if kind == "dropoff":
        return {"state": "on_track", "headline": f"~{eta} min to drop-off",
                "evidence": "", "eta_minutes": eta, "minutes_to_pickup": None, **display}

    if target_time is None:
        return {"state": "on_track", "headline": f"~{eta} min to pickup",
                "evidence": "", "eta_minutes": eta, "minutes_to_pickup": None, **display}

    minutes_to_pickup = round((target_time - now).total_seconds() / 60)
    slack = minutes_to_pickup - eta
    on_the_way = (getattr(leg, "status", "") or "") == "on-the-way"
    chained_note = " · after current trip" if kind == "next_pickup" else ""
    base = {
        "evidence": f"ETA {eta} min · {_pickup_phrase(minutes_to_pickup)}{chained_note}",
        "eta_minutes": eta, "minutes_to_pickup": minutes_to_pickup, **display,
    }

    # Won't make it.
    if slack < 0:
        if minutes_to_pickup > 0:
            # Upcoming pickup he can't reach in time -> warn (feasibility), on-the-way or not.
            return {"state": "at_risk", "headline": f"~{-slack} min late projected", **base}
        # Pickup time already passed.
        if on_the_way:
            return {"state": "at_risk", "headline": f"~{-slack} min late", **base}
        if _is_airport_pickup(leg) and eta > PANEL_STAGE_WARN_MIN:
            return {"state": "tight", "headline": f"Flight landed · vehicle ~{eta} min out", **base}
        return None  # overdue + not started + not an arrival -> stay quiet (stale status)

    # Tight: thin slack, or stopped when he should be rolling. The stall test is
    # gated on SLACK (pickup_policy.should_flag_not_moving), not on raw minutes to
    # pickup — a car parked 5 min from a pickup 45 min out is not a stall, and the
    # old PANEL_DEPARTURE_WINDOW_MIN rule called it one. Movement must be KNOWN
    # false; an unknown movement state never raises anything.
    is_moving = display["moving"]
    stalled = (is_moving is False and stationary is not None
               and stationary >= PANEL_DWELL_MIN
               and pickup_policy.should_flag_not_moving(slack, False))
    if slack < PANEL_TIGHT_BUFFER_MIN or stalled:
        headline = "Vehicle not moving" if stalled else f"{slack} min buffer"
        return {"state": "tight", "headline": headline, **base}

    # Comfortable. With lots of slack the driver isn't en route yet — projecting
    # an arrival ("arrives ~231 min early") is noise. Show a waiting card instead:
    # pickup countdown + how far the vehicle currently sits from it.
    if slack > PANEL_WAIT_SLACK_MIN:
        label = "Next pickup" if kind == "next_pickup" else "Pickup"
        return {"state": "on_track", "waiting": True,
                "headline": f"{label} in {_fmt_duration(minutes_to_pickup)}",
                **{**base, "eta_clock": None}}

    return {"state": "on_track", "headline": f"Arrives ~{slack} min early", **base}
