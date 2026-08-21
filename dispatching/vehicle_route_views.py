"""Where the car is right now — the right-click menu's vehicle data source.

Two JSON endpoints, each keyed the way the surface that calls them is keyed:

  * `leg_vehicle_route`   — a leg id. Resolves the car its driver is in that day
    and returns directions from that car to BOTH ends of this leg, with the end it
    is actually heading for marked.
  * `fleet_vehicle_route` — a FleetVehicle id, for the fleet pages, where the
    vehicle is the subject and there is no pickup to head for.

Both return the same payload shape, so the menu has one renderer.

DB-ONLY, like the rest of the fleet module. The position comes from the
samsara_* columns the 3-minute poller maintains, never from a live API call in
the request path — docs/fleet-management.md explains why (a synchronous outbound
call in a render path already caused a worker-timeout incident once). That also
means these endpoints are a couple of indexed reads: no cache, no timeout risk,
no Samsara rate limit, and they answer identically when Samsara is unreachable.
"""

import logging

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET

from dispatching import vehicle_routing
from dispatching.samsara_service import resolve_assigned_fleet_vehicle
from drivers.models import FleetVehicle
from reservations.models import Leg

logger = logging.getLogger(__name__)


def _payload(vehicle, note="", routes=None):
    """
    The one shape both endpoints return.

    Every key is always present, so the menu never has to guess whether a miss
    means "no data" or "we didn't look". `note` carries the reason there is no
    link when there isn't one — "no driver on this trip yet", "not mapped to
    Samsara" — because a menu that renders nothing reads as a broken feature
    rather than as an honest answer.

    `routes` is the trip's two ends (pickup, drop-off), each already a directions
    URL from this car, one of them flagged `next`. The fleet pages pass none —
    no job is in view there — and the menu falls back to `map_url`, the plain pin
    that is also all a position without any routable address can honestly offer.
    """
    live = None
    if vehicle is not None:
        url, _ = vehicle_routing.live_link(
            vehicle.samsara_last_latitude,
            vehicle.samsara_last_longitude,
        )
        if url:
            fresh = vehicle.samsara_is_fresh
            live = {
                "map_url": url,
                "routes": routes or [],
                # Trimmed the way the reverse-geo strings are everywhere else.
                "place": vehicle_routing.short_place(
                    vehicle.samsara_last_location_label),
                # Only a FRESH sample gets to claim the car is moving. A gateway
                # that went quiet mid-drive leaves "driving" sitting in the
                # column forever, and a menu reading "Moving · 38h ago" is a
                # straight contradiction — the position is still worth opening,
                # the motion is not.
                "moving": bool(fresh and vehicle.samsara_movement_status == "driving"),
                "fresh": fresh,
                "age": vehicle.samsara_age_display(),
            }

    return {
        # The bare unit number, not "#001 · Chevrolet Suburban". The menu row is
        # one line of context under an action; make and model are known from the
        # number and were only ever taking up width.
        "vehicle_number": vehicle.vehicle_number if vehicle is not None else "",
        "live": live,
        "note": note,
    }


def _position_note(vehicle):
    """Why this car has no position, or "" when it has one."""
    if not vehicle.samsara_enabled:
        return f"#{vehicle.vehicle_number} isn't mapped to Samsara — no position."
    if vehicle.samsara_last_latitude is None:
        return f"#{vehicle.vehicle_number} hasn't reported a position yet."
    return ""


def _missing_address_note(routes):
    """
    Why an end of the trip isn't offered. A row that quietly isn't there reads as
    a bug in the menu; "no drop-off address on this trip" reads as an answer, and
    tells the dispatcher where to go fix it.
    """
    kinds = {r["kind"] for r in routes}
    if kinds == {"pickup", "dropoff"}:
        return ""
    if not kinds:
        return ("No pickup or drop-off address on this trip — "
                "this is just the car's position.")
    if "pickup" in kinds:
        return "No drop-off address on this trip, so only the pickup can be routed."
    return "No pickup address on this trip, so only the drop-off can be routed."


@require_GET
@login_required(login_url="login")
def leg_vehicle_route(request, leg_id):
    """Directions from the car assigned to this leg to both ends of that leg."""
    if not request.user.is_staff:
        return JsonResponse({"error": "Staff only."}, status=403)

    leg = get_object_or_404(
        Leg.objects.select_related("reservation", "driver"), id=leg_id
    )

    vehicle = resolve_assigned_fleet_vehicle(leg)
    if vehicle is None:
        return JsonResponse(_payload(
            None,
            note=(
                "No car assigned to this driver for that day."
                if getattr(leg, "driver_id", None)
                else "No driver on this trip yet."
            ),
        ))

    # Both ends, in trip order, with the one he is actually driving to marked.
    routes = vehicle_routing.leg_routes(
        vehicle.samsara_last_latitude,
        vehicle.samsara_last_longitude,
        leg.status,
        leg.pickup_location,
        leg.dropoff_location,
    )

    note = _position_note(vehicle) or _missing_address_note(routes)
    return JsonResponse(_payload(vehicle, note=note, routes=routes))


@require_GET
@login_required(login_url="login")
def fleet_vehicle_route(request, pk):
    """Where one vehicle is now, for the fleet pages. No job in view, so no route."""
    if not request.user.is_staff:
        return JsonResponse({"error": "Staff only."}, status=403)

    vehicle = get_object_or_404(FleetVehicle, pk=pk)
    return JsonResponse(_payload(vehicle, note=_position_note(vehicle)))
