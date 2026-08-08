"""The right-click trip menu's data source.

One tiny JSON endpoint keyed on leg id. Every dispatcher surface already stamps
`data-leg-id` on its rows, chips and timeline bars, so the menu works on the
dashboard, the schedule board and the capacity planner without any of them
having to pre-render link payloads into the page.

The external URLs themselves come from reservations.trip_links — this module
only adds the two INTERNAL links (reservation, leg history) that the pure
link builder deliberately doesn't know about.
"""

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views.decorators.http import require_GET

from reservations.models import Leg
from reservations.trip_links import leg_trip_links


@require_GET
@login_required(login_url="login")
def leg_trip_links_json(request, leg_id):
    """Map + flight-tracker links for one leg, for the right-click menu."""
    if not request.user.is_staff:
        return JsonResponse({"error": "Staff only."}, status=403)

    leg = get_object_or_404(
        Leg.objects.select_related(
            "reservation", "reservation__customer", "flight_information"
        ).prefetch_related("legstop_set__location", "legflight_set__flight"),
        id=leg_id,
    )

    payload = leg_trip_links(leg)
    payload["reservation_url"] = (
        reverse("reservation_details", args=[leg.reservation.uuid])
        if leg.reservation_id
        else None
    )
    payload["leg_history_url"] = reverse("leg_history", args=[leg.id])
    return JsonResponse(payload)
