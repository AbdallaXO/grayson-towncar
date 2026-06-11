from django.shortcuts import render
from .models import Rate, Vehicle, Location
from django.db.models import Prefetch, Case, When
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
import json
import logging

logger = logging.getLogger(__name__)


# Showcase presentation per vehicle type. Static images are the optimized
# assets the landing page already ships (the Vehicle.image media files are
# not guaranteed to exist in every environment).
VEHICLE_PRESENTATION = {
    "towncar": {
        "kicker": "Executive Sedan",
        "blurb": "Quiet, comfortable and discreet — perfect for couples and small families.",
        "image": "images/towncar.webp",
        "party": "1–4 guests",
    },
    "mini_van": {
        "kicker": "Family Minivan",
        "blurb": "Easy boarding with room for strollers — ideal for young families.",
        "image": "images/minivan.webp",
        "party": "Up to 5 guests",
    },
    "suv": {
        "kicker": "Luxury SUV",
        "blurb": "Generous space for larger families, with every suitcase along for the ride.",
        "image": "images/suburban.webp",
        "party": "Up to 6 guests",
    },
    "van": {
        "kicker": "Group Transport",
        "blurb": "Premium group travel — ten seats and room for all the luggage.",
        "image": "images/van.webp",
        "party": "7–10 guests",
    },
    "Van(14 Pax)": {
        "kicker": "Sprinter · Flagship",
        "blurb": "Our largest vehicle — whole-party travel for reunions and big groups.",
        "image": "images/sprinter.webp",
        "party": "11–14 guests",
    },
}

_DEFAULT_PRESENTATION = {
    "kicker": "Private Transfer",
    "blurb": "Professional, licensed and insured drivers on every trip.",
    "image": "images/suburban.webp",
    "party": "",
}


def _route_category_slugs(route):
    """Space-separated filter categories for a route, matched on location names."""
    text = f"{route.origin.name} {route.destination.name}".lower()
    categories = []
    if "airport" in text or "mco" in text:
        categories.append("airport")
    if "disney" in text:
        categories.append("disney")
    if "universal" in text:
        categories.append("universal")
    if "port canaveral" in text or "cruise" in text:
        categories.append("cruise")
    return " ".join(categories) or "other"


def index(request):
    vehicles = list(
        Vehicle.objects.prefetch_related(
            Prefetch(
                "rates",
                queryset=Rate.objects.select_related(
                    "route", "route__origin", "route__destination"
                ).order_by(
                    # First, prioritize Orlando International Airport
                    Case(
                        When(
                            route__origin__name="Orlando International Airport", then=0
                        ),
                        default=1,
                    ),
                    # Then order by origin name, then destination name
                    "route__origin__name",
                    "route__destination__name",
                ),
            )
        ).all()
    )
    for vehicle in vehicles:
        vehicle.rate_list = list(vehicle.rates.all())
        for rate in vehicle.rate_list:
            rate.category_slugs = _route_category_slugs(rate.route)
        vehicle.pres = VEHICLE_PRESENTATION.get(
            vehicle.vehicle_type, _DEFAULT_PRESENTATION
        )
    structured_data = {
        "@type": "Offer",
        "description": "Comprehensive transportation rates for Orlando airport, Disney, and Universal transfers",
    }
    context = {"vehicles": vehicles, "additional_data": structured_data}
    return render(request, "rates/index.html", context)
