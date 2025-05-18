from django.http.response import (
    HttpResponse,
    HttpResponsePermanentRedirect,
    HttpResponseRedirect,
)
from django.shortcuts import render, get_object_or_404, redirect
from rates.models import Rate
from .utils import (
    _initalize_form,
    get_form_details,
    returns_post_form,
    validate_forms,
    AIRLINES,
    extra_charges,
)
from django.shortcuts import render, reverse
from django.db.models import Prefetch
from rates.models import Rate, Vehicle, Location, Route, Lead
import json
from users.models import TravelAgent
import logging
from .hubspot_service import sync_reservation_to_hubspot
from rates.forms import LeadForm
logger = logging.getLogger(__name__)


# Create your views here.


def index(request):
    """
    Home page view that includes the quote widget in the hero section.
    """
    # Get all vehicles, locations, and rates with efficient prefetching
    vehicles = Vehicle.objects.prefetch_related(
        Prefetch("rates", queryset=Rate.objects.select_related("route", "route__origin", "route__destination"))
    ).all()
    
    locations = Location.objects.all()
    
    # Initialize an empty form
    form = LeadForm()
    
    # Prepare data for the quote widget
    rates_json = {}
    for vehicle in vehicles:
        routes = {}
        for rate in vehicle.rates.all():
            route = rate.route
            routes[str(rate.id)] = {
                "id": rate.id,
                "name": str(route),
                "origin_id": route.origin_id,
                "destination_id": route.destination_id,
                "origin_name": route.origin.name,
                "destination_name": route.destination.name,
                "oneway": float(rate.oneway_price),
                "round": float(rate.round_trip_price),
                "reserve_url": reverse("reserve", args=[rate.id]),
            }
        rates_json[str(vehicle.id)] = routes
    
    # Prepare locations for the quote widget
    locations_json = {str(loc.id): {"id": loc.id, "name": loc.name} for loc in locations}
    
    # Prepare routes for the quote widget
    routes = Route.objects.all().select_related('origin', 'destination')
    populated_routes = [
        {
            "origin_id": route.origin_id,
            "destination_id": route.destination_id,
            "origin_name": route.origin.name,
            "destination_name": route.destination.name,
        }
        for route in routes
    ]
    
    # Context for template
    context = {
        "page_title": "Home",
        "vehicles": vehicles,
        "locations": locations,
        "rates_json": json.dumps(rates_json),
        "locations_json": json.dumps(locations_json),
        "populated_routes": json.dumps(populated_routes),
        "form": form,
    }
    return render(request, "reservations/index.html", context)


def reservation_form(
    request, pk
) -> HttpResponsePermanentRedirect | HttpResponseRedirect | HttpResponse:
    """Returns a Reservation Form either oneway or roundtrip with a car type & rate & route or 404"""
    rate = get_object_or_404(Rate.objects.select_related("route", "vehicle"), pk=pk)
    trip_type, price = get_form_details(request, rate)
    if request.method == "POST":
        (
            customer_form,
            reservation_form,
            flight1_form,
            leg1_form,
            flight2_form,
            leg2_form,
        ) = returns_post_form(request, trip_type, rate)

        forms_valid = validate_forms(
            customer_form,
            reservation_form,
            flight1_form,
            leg1_form,
            flight2_form,
            leg2_form,
            trip_type,
        )
        if forms_valid:
            customer = customer_form.save()
            reservation = reservation_form.save(
                customer=customer,
                trip_type=trip_type,
                rate=rate,
                base_price=price,
                vehicle=rate.vehicle,
            )

            # If user is logged in and is a travel agent, tag the reservation
            if request.user.is_authenticated:
                try:
                    travel_agent = TravelAgent.objects.get(user=request.user)
                    reservation.travel_agent = travel_agent
                    reservation.save()
                except TravelAgent.DoesNotExist:
                    pass  # User is not a travel agent, continue normally

            leg1 = leg1_form.save(commit=False)
            leg1.reservation = reservation

            if flight1_form and any(flight1_form.cleaned_data.values()):
                flight1 = flight1_form.save()
                leg1.flight_information = flight1
            else:
                leg1.flight_information = None
            leg1.save()

            if trip_type == "round_trip":
                leg2 = leg2_form.save(commit=False)
                leg2.reservation = reservation

                if flight2_form and any(flight2_form.cleaned_data.values()):
                    flight2 = flight2_form.save()
                    leg2.flight_information = flight2
                else:
                    leg2.flight_information = None
                leg2.save()

            extra_charges(reservation)
            return redirect("create_checkout_session", reservation_id=reservation.uuid)
    else:
        (
            customer_form,
            reservation_form,
            flight1_form,
            leg1_form,
            flight2_form,
            leg2_form,
        ) = _initalize_form(trip_type, rate, price)

    context = {
        "customer_form": customer_form,
        "reservation_form": reservation_form,
        "flight1_form": flight1_form,
        "flight2_form": flight2_form,
        "leg1_form": leg1_form,
        "leg2_form": leg2_form,
        "route": rate.route,
        "price": price,
        "trip_type": trip_type.replace("_", " "),
        "vehicle": rate.vehicle,
        "airlines": AIRLINES,
        "canonical_url": request.build_absolute_uri("/rates-booking/"),
    }
    return render(request, "reservations/book_form.html", context)


def about_us(request):
    structured_data = {
        "@type": "AboutPage",
        "description": "Learn about Grayson Towncar's mission and commitment to transportation.",
    }
    return render(
        request, "reservations/about.html", {"structured_data": structured_data}
    )


def faqs(request):
    return render(request, "reservations/faqs.html")


def tos(request):
    return render(request, "reservations/tos.html")
