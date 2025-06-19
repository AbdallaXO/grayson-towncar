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
from django.db.models import Prefetch, Case, When
from rates.models import Rate, Vehicle
import json
from users.models import TravelAgent
import logging
from django.http import JsonResponse
from .forms import LeadForm
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.views import View
from .conversions import send_lead_event, send_initiate_checkout_event

logger = logging.getLogger(__name__)


# Create your views here.


def index(request):
    """Returns the Landing Page"""
    vehicles = Vehicle.objects.prefetch_related(
        Prefetch(
            "rates",
            queryset=Rate.objects.select_related(
                "route", "route__origin", "route__destination"
            ).order_by(
                # First, prioritize Orlando International Airport
                Case(
                    When(route__origin__name="Orlando International Airport", then=0),
                    default=1
                ),
                # Then order by origin name, then destination name
                "route__origin__name",
                "route__destination__name"
            ),
        )
    ).all()
    rates_json: dict[str, dict[str, dict]] = {}
    for v in vehicles:
        routes: dict[str, dict] = {}
        for r in v.rates.all():
            routes[str(r.id)] = {
                "id": r.id,
                "name": str(r.route),
                "oneway": float(r.oneway_price),
                "round": float(r.round_trip_price),
                "reserve_url": reverse(
                    "reserve", args=[r.id]
                ),  # Changed to snake_case to match JS
            }
        rates_json[str(v.id)] = routes

    context = {
        "vehicles": vehicles,
        "rates_json": json.dumps(rates_json),  # safe‑dump for JS
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

            # Send InitiateCheckout event to Meta Conversions API
            try:
                send_initiate_checkout_event(reservation, request)
                logger.info("Successfully sent InitiateCheckout event to Meta Conversions API")
            except Exception as e:
                logger.error(f"Error sending InitiateCheckout event to Meta Conversions API: {str(e)}")

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


def fleet(request):
    """Returns the Fleet Page showing all vehicles with descriptions"""
    vehicles = Vehicle.objects.all().order_by('capacity')
    
    # Add descriptions directly to vehicles
    for vehicle in vehicles:
        if vehicle.vehicle_type == 'towncar':
            vehicle.title = 'Luxury Town Car'
            vehicle.description = 'Experience classic elegance with our premium town cars. Perfect for business travel, airport transfers, and special occasions.'
            vehicle.features = [
                'Seats up to 4 passengers comfortably',
                'Accommodates 4 pieces of luggage',
                'Professional chauffeur service',
                'Climate-controlled interior'
            ]
            vehicle.best_for = 'Business travel, airport transfers, special events'
        elif vehicle.vehicle_type == 'suv':
            vehicle.title = 'SUV'
            vehicle.description = 'Spacious and versatile, our luxury SUVs provide the perfect blend of comfort and capacity for larger groups.'
            vehicle.features = [
                'Seats up to 6 passengers',
                'Accommodates 6 pieces of luggage',
                'Ample legroom and headspace',
                'Premium leather seating'
            ]
            vehicle.best_for = 'Family travel, group outings, airport transfers'
        elif vehicle.vehicle_type == 'mini_van':
            vehicle.title = 'Premium Minivan'
            vehicle.description = 'Our comfortable minivans are ideal for families and small groups, offering excellent space and convenience.'
            vehicle.features = [
                'Seats up to 5 passengers',
                'Accommodates 5 pieces of luggage',
                'Sliding doors for easy access',
                'Child safety seat compatible'
            ]
            vehicle.best_for = 'Family vacations, airport transfers, theme park transportation'
        elif vehicle.vehicle_type == 'van':
            vehicle.title = 'Passenger Van'
            vehicle.description = 'Our spacious passenger vans are perfect for larger groups, offering maximum capacity without sacrificing comfort.'
            vehicle.features = [
                'Seats up to 10 passengers',
                'Accommodates 11 pieces of luggage',
                'High roof for easy movement',
                'Multiple seating configurations'
            ]
            vehicle.best_for = 'Large groups, corporate events, airport transfers'
        elif vehicle.vehicle_type == 'Van(14 Pax)':
            vehicle.title = '14 Passenger Van'
            vehicle.description = 'Our largest passenger van is ideal for big groups, corporate events, and large family gatherings with maximum capacity and comfort.'
            vehicle.features = [
                'Seats up to 14 passengers',
                'Accommodates 12-14 pieces of luggage',
                'Extra spacious interior',
                'Perfect for large groups'
            ]
            vehicle.best_for = 'Large groups, corporate events, family gatherings'
    
    context = {
        'vehicles': vehicles,
    }
    return render(request, "reservations/fleet.html", context)


@method_decorator(csrf_exempt, name="dispatch")
class QuoteFormHandlerView(View):
    """
    Handles AJAX POST requests from quote forms created with the quote_form template tag.
    This view processes lead creation and returns JSON responses.
    """

    def post(self, request):
        try:
            data = json.loads(request.body)

            # Create form instance with the submitted data
            form = LeadForm(
                {
                    "first_name": data.get("first_name", ""),
                    "last_name": data.get("last_name", ""),
                    "email": data.get("email", ""),
                    "phone": data.get("phone", ""),
                    "pickup_date": data.get("pickup_date", ""),
                }
            )

            if form.is_valid():
                # Create lead instance but don't save yet
                lead = form.save(commit=False)

                # Add additional fields
                lead.vehicle_id = data.get("vehicle_id")
                lead.pickup_location = data.get("pickup_location")
                lead.dropoff_location = data.get("dropoff_location")
                lead.trip_type = (
                    "oneway" if data.get("trip_type") == "1" else "roundtrip"
                )
                lead.estimated_price = data.get("estimated_price")

                # Save the lead
                lead.save()

                # Log the lead creation
                logger.info(f"New lead created: {lead}")

                # Send lead event to Meta Conversions API
                try:
                    send_lead_event(lead, request)
                    logger.info("Successfully sent lead event to Meta Conversions API")
                except Exception as e:
                    logger.error(f"Error sending lead event to Meta Conversions API: {str(e)}")

                return JsonResponse({"success": True, "lead_id": lead.id})
            else:
                return JsonResponse(
                    {"success": False, "errors": form.errors}, status=400
                )

        except json.JSONDecodeError:
            return JsonResponse(
                {"success": False, "error": "Invalid JSON data"}, status=400
            )
        except Exception as e:
            logger.error(f"Error creating lead: {str(e)}")
            return JsonResponse({"success": False, "error": str(e)}, status=500)

    def get(self, request):
        """Handle GET requests (not allowed for this endpoint)"""
        return JsonResponse(
            {"success": False, "error": "Only POST requests are allowed"}, status=405
        )


# Convenience function-based view wrapper
quote_form_handler = QuoteFormHandlerView.as_view()
