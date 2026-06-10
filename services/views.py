from django.shortcuts import render
from django.urls import reverse
from django.db.models import Prefetch, Case, When
from rates.models import Rate, Vehicle
import json


# Create your views here.
def index(request):
    return render(request, "services/index.html")


def orlando_airport_transportation(request):
    """Orlando Airport Transportation landing page with quote form"""
    # Get vehicles with prefetched rates, prioritizing MCO routes
    vehicles = Vehicle.objects.prefetch_related(
        Prefetch(
            "rates",
            queryset=Rate.objects.select_related(
                "route", "route__origin", "route__destination"
            ).order_by(
                # First, prioritize Orlando International Airport
                Case(
                    When(route__origin__name="Orlando International Airport", then=0),
                    default=1,
                ),
                # Then order by origin name, then destination name
                "route__origin__name",
                "route__destination__name",
            ),
        )
    ).all()

    # Create rates JSON for JavaScript
    rates_json = {}
    for v in vehicles:
        routes = {}
        for r in v.rates.all():
            routes[str(r.id)] = {
                "id": r.id,
                "name": str(r.route),
                "oneway": float(r.oneway_price),
                "round": float(r.round_trip_price),
                "reserve_url": reverse("reserve", args=[r.id]),
            }
        rates_json[str(v.id)] = routes

    # Structured data for SEO
    structured_data = {
        "@context": "https://schema.org",
        "@type": "Service",
        "name": "Orlando Airport Transportation",
        "description": "Premium MCO airport transfers to Disney World, Universal Studios & Orlando hotels. Free car seats, flight tracking, meet & greet service.",
        "provider": {
            "@type": "LocalBusiness",
            "name": "Grayson Towncar",
            "telephone": "407-212-7190",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": "Orlando",
                "addressRegion": "FL",
                "addressCountry": "US",
            },
        },
        "areaServed": {"@type": "Airport", "name": "Orlando International Airport (MCO)", "iataCode": "MCO"},
        "serviceType": "Airport Transportation",
        "url": "https://www.graysontowncar.com/services/orlando-airport-transportation/",
    }

    context = {
        "vehicles": vehicles,
        "rates_json": json.dumps(rates_json),
        "structured_data": json.dumps(structured_data),
        "canonical_url": request.build_absolute_uri(
            "/services/orlando-airport-transportation/"
        ),
    }

    return render(request, "services/orlando-airport-transportation.html", context)


def disney_world_transportation(request):
    return render(request, "services/disney-world-transportation.html")


def universal_orlando_transportation(request):
    return render(request, "services/universal-orlando-transportation.html")


def port_canaveral_transportation(request):
    return render(request, "services/port-canaveral-transportation.html")


def corporate_transportation(request):
    return render(request, "services/corporate-transportation.html")


def epic_universe_transportation(request):
    return render(
        request,
        "services/epic-universe-transportation.html",
        {"canonical_url": request.build_absolute_uri("/services/epic-universe-transportation/")},
    )


def mco_terminal_c_transportation(request):
    return render(
        request,
        "services/mco-terminal-c-transportation.html",
        {"canonical_url": request.build_absolute_uri("/services/mco-terminal-c-transportation/")},
    )


def car_seats_transportation(request):
    return render(
        request,
        "services/car-seats.html",
        {"canonical_url": request.build_absolute_uri("/services/car-seats/")},
    )
