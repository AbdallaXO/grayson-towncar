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
from rates.models import Rate, Vehicle
import json
from users.models import TravelAgent
import logging
from .hubspot_service import sync_reservation_to_hubspot
from django.http import JsonResponse
from .models import Lead
from .forms import LeadForm

logger = logging.getLogger(__name__)


# Create your views here.


def index(request):
    """Returns the Landing Page"""
    vehicles = Vehicle.objects.prefetch_related(
        Prefetch(
            "rates",
            queryset=Rate.objects.select_related(
                "route", "route__origin", "route__destination"
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


def test_quote(request):
    """Test view for location-based quote form with lead capture"""
    vehicles = Vehicle.objects.prefetch_related(
        Prefetch(
            "rates",
            queryset=Rate.objects.select_related(
                "route", "route__origin", "route__destination"
            ),
        )
    ).all()
    
    # Get all unique locations from routes
    locations = set()
    for vehicle in vehicles:
        for rate in vehicle.rates.all():
            locations.add(rate.route.origin)
            locations.add(rate.route.destination)
    
    # Create rates map with location pairs as keys
    rates_json: dict[str, dict[str, dict]] = {}
    for v in vehicles:
        routes: dict[str, dict] = {}
        for r in v.rates.all():
            # Create a key that's direction-agnostic by sorting origin and destination IDs
            location_ids = sorted([str(r.route.origin.id), str(r.route.destination.id)])
            key = f"{location_ids[0]}-{location_ids[1]}"
            
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

    if request.method == "POST":
        try:
            data = json.loads(request.body)
            
            # Create form instance with the submitted data
            form = LeadForm({
                'first_name': data['first_name'],
                'last_name': data['last_name'],
                'email': data['email'],
                'phone': data['phone']
            })
            
            if form.is_valid():
                # Create lead instance but don't save yet
                lead = form.save(commit=False)
                
                # Add additional fields
                lead.vehicle_id = data.get('vehicle_id')
                lead.pickup_location = data.get('pickup_location')
                lead.dropoff_location = data.get('dropoff_location')
                lead.trip_type = 'oneway' if data.get('trip_type') == '1' else 'roundtrip'
                lead.estimated_price = data.get('estimated_price')
                
                # Save the lead
                lead.save()
                
                # Log the lead creation
                logger.info(f"New lead created: {lead}")
                
                return JsonResponse({
                    "success": True,
                    "lead_id": lead.id
                })
            else:
                return JsonResponse({
                    "success": False,
                    "errors": form.errors
                }, status=400)
            
        except json.JSONDecodeError:
            return JsonResponse({"success": False, "error": "Invalid JSON data"}, status=400)
        except Exception as e:
            logger.error(f"Error creating lead: {str(e)}")
            return JsonResponse({"success": False, "error": str(e)}, status=500)

    context = {
        "vehicles": vehicles,
        "locations": sorted(locations, key=lambda x: x.name),
        "rates_json": json.dumps(rates_json),
        "form": LeadForm()  # Add empty form to context
    }
    return render(request, "reservations/test_quote.html", context)


# reservations/views.py (add this view to your existing views.py)
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.utils.decorators import method_decorator
from django.views import View

logger = logging.getLogger(__name__)

@method_decorator(csrf_exempt, name='dispatch')
class QuoteFormHandlerView(View):
    """
    Handles AJAX POST requests from quote forms created with the quote_form template tag.
    This view processes lead creation and returns JSON responses.
    """
    
    def post(self, request):
        try:
            data = json.loads(request.body)
            
            # Create form instance with the submitted data
            form = LeadForm({
                'first_name': data.get('first_name', ''),
                'last_name': data.get('last_name', ''),
                'email': data.get('email', ''),
                'phone': data.get('phone', '')
            })
            
            if form.is_valid():
                # Create lead instance but don't save yet
                lead = form.save(commit=False)
                
                # Add additional fields
                lead.vehicle_id = data.get('vehicle_id')
                lead.pickup_location = data.get('pickup_location')
                lead.dropoff_location = data.get('dropoff_location')
                lead.trip_type = 'oneway' if data.get('trip_type') == '1' else 'roundtrip'
                lead.estimated_price = data.get('estimated_price')
                
                # Save the lead
                lead.save()
                
                # Log the lead creation
                logger.info(f"New lead created: {lead}")
                
                return JsonResponse({
                    "success": True,
                    "lead_id": lead.id
                })
            else:
                return JsonResponse({
                    "success": False,
                    "errors": form.errors
                }, status=400)
                
        except json.JSONDecodeError:
            return JsonResponse({
                "success": False, 
                "error": "Invalid JSON data"
            }, status=400)
        except Exception as e:
            logger.error(f"Error creating lead: {str(e)}")
            return JsonResponse({
                "success": False, 
                "error": str(e)
            }, status=500)
    
    def get(self, request):
        """Handle GET requests (not allowed for this endpoint)"""
        return JsonResponse({
            "success": False,
            "error": "Only POST requests are allowed"
        }, status=405)

# Convenience function-based view wrapper
quote_form_handler = QuoteFormHandlerView.as_view()