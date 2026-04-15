"""
Experimental guest-needs-first quote page.

This is a separate, isolated view for an A/B test lead form that asks
guests about their trip needs first (passengers, luggage, car seats)
and then recommends a vehicle — instead of forcing the guest to choose
a vehicle type upfront.

Does NOT modify or replace the existing quote form in any way.
Reuses the existing QuoteFormHandlerView at /quote-form-handler/ for
lead submission.
"""

import json

from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Prefetch
from django.shortcuts import render
from django.urls import reverse

from rates.models import Vehicle, Rate

# Map vehicle_type values to known static image filenames
VEHICLE_IMAGE_MAP = {
    "towncar": "images/towncar.webp",
    "suv": "images/suv.webp",
    "mini_van": "images/minivan.webp",
    "van": "images/van.webp",
    "Van(14 Pax)": "images/van.webp",
}


@staff_member_required
def guest_quote_page(request):
    """Render the experimental guest-needs-first quote page."""

    vehicles = Vehicle.objects.prefetch_related(
        Prefetch(
            "rates",
            queryset=Rate.objects.select_related(
                "route", "route__origin", "route__destination"
            ),
        )
    ).all()  # ordered by capacity (Vehicle.Meta.ordering)

    # Collect unique locations from all routes
    locations = set()
    for v in vehicles:
        for r in v.rates.all():
            locations.add(r.route.origin)
            locations.add(r.route.destination)

    # Build rates_json — same structure as quote_tags.py for compatibility
    rates_json = {}
    for v in vehicles:
        routes = {}
        for r in v.rates.all():
            loc_ids = sorted([str(r.route.origin.id), str(r.route.destination.id)])
            key = f"{loc_ids[0]}-{loc_ids[1]}"
            routes[key] = {
                "id": r.id,
                "name": str(r.route),
                "origin": str(r.route.origin),
                "destination": str(r.route.destination),
                "oneway": float(r.oneway_price),
                "round": float(r.round_trip_price),
                "reserve_url": reverse("reserve", args=[r.id]),
            }
        rates_json[str(v.id)] = routes

    # Build vehicles_json for the client-side recommendation engine
    vehicles_json = []
    for v in vehicles:
        vehicles_json.append(
            {
                "id": v.id,
                "vehicle_type": v.vehicle_type,
                "display_name": v.get_vehicle_type_display(),
                "capacity": v.capacity,
                "luggage_capacity": v.luggage_capacity,
                "carseats_capacity": v.carseats_capacity,
                "image": VEHICLE_IMAGE_MAP.get(v.vehicle_type, "images/towncar.webp"),
            }
        )

    return render(
        request,
        "reservations/guest_quote.html",
        {
            "locations": sorted(locations, key=lambda loc: loc.name),
            "vehicles_json": json.dumps(vehicles_json),
            "rates_json": json.dumps(rates_json),
            "quote_endpoint_url": reverse("quote_form_handler"),
        },
    )
