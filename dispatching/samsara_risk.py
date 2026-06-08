"""
Samsara Phase 2 — schedule-aware ETA + late-risk logic.

Pure-ish functions (only external dependency is the cached, traffic-aware
get_drive_time). The background sweep in samsara_scheduler.py calls these and
persists the result onto the Leg; nothing here is called in a request path.

The question we answer per driver: "where is the vehicle relative to the
driver's NEXT relevant stop, and will he make it?"
"""
import logging
from datetime import datetime

from django.utils import timezone

from drivers.utils import get_drive_time

logger = logging.getLogger(__name__)

# Tunables.
WATCH_SLACK_MIN = 10        # < this much spare time to a pickup => "watch"
IDLE_NEAR_PICKUP_MIN = 30   # within this window + not moving => at least "watch"
# How long after a scheduled pickup we still surface a live "late" badge. Beyond
# this the leg is treated as stale/handled by the normal status workflow (a pickup
# overdue by hours that nobody marked done is noise, not a live-ETA signal).
PAST_PICKUP_GRACE_MIN = 45

# Live-tracking PANEL display tunables.
PANEL_TIGHT_BUFFER_MIN = 10      # spare time under this => "tight"
PANEL_DWELL_MIN = 8              # stationary at least this long => stalled candidate
PANEL_DEPARTURE_WINDOW_MIN = 45  # only treat "not moving" as a problem this close to pickup
# For an ALREADY-OVERDUE pickup where the driver isn't on the way (usually just stale
# status), we stay quiet — except an arrival whose flight is already at the gate while
# the vehicle is still more than this many minutes out (amber warning, he should be moving).
PANEL_STAGE_WARN_MIN = 10
# Minutes a driver spends at a drop-off before he's free for the next pickup. Used by the
# chain: time-to-current-dropoff + this + drive(dropoff -> next pickup).
DROPOFF_SERVICE_MIN = 5

# Leg statuses meaning the driver is still heading TO the pickup.
_HEADING_TO_PICKUP = {"in-progress", "confirmed", "on-the-way", None, ""}
# Leg statuses meaning the driver has the guest / is at pickup -> next stop is dropoff.
_ON_TRIP = {"picked-up", "on-location"}
_DONE = {"completed", "cancelled"}


def effective_pickup_dt(leg):
    """
    Flight-aware pickup datetime for a leg. For a same-day arrival flight, use the
    best-available flight arrival (so a delay widens the window correctly);
    otherwise the scheduled pickup. Returns an aware datetime or None.
    """
    flight = getattr(leg, "controlling_flight", None)
    if flight:
        arr = flight.best_arrival_local()
        if arr and timezone.localdate(arr) == leg.pickup_date:
            return arr
    if not leg.pickup_date or not leg.pickup_time:
        return None
    naive = datetime.combine(leg.pickup_date, leg.pickup_time)
    return timezone.make_aware(naive) if timezone.is_naive(naive) else naive


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


def evaluate(vehicle, target, now=None, eta_override=None):
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

    if not vehicle.samsara_is_fresh or vehicle.samsara_last_latitude is None \
            or vehicle.samsara_last_longitude is None:
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

    if eta_override is not None:
        drive_min = eta_override
    else:
        origin = f"{vehicle.samsara_last_latitude},{vehicle.samsara_last_longitude}"
        dt = get_drive_time(origin, target["location"])
        if not dt or dt.get("duration_seconds") is None:
            fields["dispatch_risk_status"] = "unknown"
            fields["dispatch_risk_reason"] = "Could not compute drive time"
            return fields
        drive_min = round(dt["duration_seconds"] / 60)
    fields["dispatch_eta_minutes"] = drive_min

    # Dropoff target: no hard deadline -> ETA only, no risk band.
    if target["kind"] == "dropoff":
        fields["dispatch_risk_reason"] = f"~{drive_min} min to dropoff"
        return fields

    # Pickup target: band the slack.
    target_time = target["target_time"]
    leg = target["leg"]
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

    if slack < 0:
        fields["dispatch_risk_status"] = "at_risk"
        fields["dispatch_risk_reason"] = (
            f"ETA {drive_min} min vs pickup in {round(minutes_to_target)} min"
        )
    elif slack < WATCH_SLACK_MIN:
        fields["dispatch_risk_status"] = "watch"
        fields["dispatch_risk_reason"] = f"Only {round(slack)} min slack to pickup"
    elif minutes_to_target <= IDLE_NEAR_PICKUP_MIN and is_not_moving:
        fields["dispatch_risk_status"] = "watch"
        fields["dispatch_risk_reason"] = (
            f"Pickup in {round(minutes_to_target)} min, vehicle not moving"
        )
    else:
        fields["dispatch_risk_status"] = "on_time"
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


def evaluate_driver(vehicle, legs, now=None):
    """
    Chain-aware per-driver evaluation. Returns {leg_id: fields} for the leg(s) to flag:
      - FREE driver: feasibility to his next upcoming pickup.
      - MID-TRIP driver (picked-up / on-location): his current drop-off (ETA only)
        PLUS his next pickup, chained through finishing the current job
        (time-to-current-dropoff + DROPOFF_SERVICE_MIN + drive(dropoff -> next pickup)).
    Empty dict = nothing to flag.
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
        mid_fields = evaluate(vehicle, dropoff_target, now)
        if mid_fields:
            out[mid.id] = mid_fields
        # Next pickup, CHAINED: finish the current drop-off, then drive on to it.
        nxt = next((l for l in open_legs if l.id != mid.id and _within_grace(l, now)), None)
        if nxt is not None:
            to_dropoff = mid_fields.get("dispatch_eta_minutes") if mid_fields else None
            dropoff_to_next = _drive_min(mid.dropoff_location, nxt.pickup_location)
            if to_dropoff is not None and dropoff_to_next is not None:
                chained = to_dropoff + DROPOFF_SERVICE_MIN + dropoff_to_next
                pickup_target = {"leg": nxt, "kind": "next_pickup",
                                 "location": nxt.pickup_location,
                                 "target_time": effective_pickup_dt(nxt)}
                nxt_fields = evaluate(vehicle, pickup_target, now, eta_override=chained)
                if nxt_fields:
                    out[nxt.id] = nxt_fields
    else:
        target = choose_active_target(open_legs, now)
        if target is not None:
            fields = evaluate(vehicle, target, now)
            if fields:
                out[target["leg"].id] = fields
    return out


def _pickup_phrase(minutes_to_pickup):
    if minutes_to_pickup is None:
        return ""
    if minutes_to_pickup >= 0:
        return f"pickup in {minutes_to_pickup} min"
    return f"pickup {abs(minutes_to_pickup)} min ago"


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
      on_track  — comfortable, or en route to a drop-off (visually silent)

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
    eta = getattr(leg, "dispatch_eta_minutes", None)
    if eta is None:
        return None

    vehicle = getattr(leg, "dispatch_vehicle_label", "") or ""
    kind = getattr(leg, "dispatch_eta_target", "") or ""

    # Mid-trip current leg: ETA to the drop-off, no deadline (visually silent).
    if kind == "dropoff":
        return {"state": "on_track", "headline": f"~{eta} min to drop-off",
                "evidence": "", "eta_minutes": eta, "minutes_to_pickup": None, "vehicle": vehicle}

    target_time = getattr(leg, "dispatch_eta_target_time", None)
    if target_time is None:
        return {"state": "on_track", "headline": f"~{eta} min to pickup",
                "evidence": "", "eta_minutes": eta, "minutes_to_pickup": None, "vehicle": vehicle}

    minutes_to_pickup = round((target_time - now).total_seconds() / 60)
    slack = minutes_to_pickup - eta
    on_the_way = (getattr(leg, "status", "") or "") == "on-the-way"
    chained_note = " · after current trip" if kind == "next_pickup" else ""
    base = {
        "evidence": f"ETA {eta} min · {_pickup_phrase(minutes_to_pickup)}{chained_note}",
        "eta_minutes": eta, "minutes_to_pickup": minutes_to_pickup, "vehicle": vehicle,
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

    # Tight: thin slack, or stalled inside the departure window.
    is_moving = getattr(leg, "dispatch_is_moving", None)
    stationary = getattr(leg, "dispatch_stationary_minutes", None)
    stalled = (is_moving is False and stationary is not None
               and stationary >= PANEL_DWELL_MIN and minutes_to_pickup <= PANEL_DEPARTURE_WINDOW_MIN)
    if slack < PANEL_TIGHT_BUFFER_MIN or stalled:
        headline = "Vehicle not moving" if stalled else f"{slack} min buffer"
        return {"state": "tight", "headline": headline, **base}

    return {"state": "on_track", "headline": f"Arrives ~{slack} min early", **base}
