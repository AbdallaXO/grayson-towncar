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
    send_lead_notification,
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
from django.utils import timezone
from datetime import timedelta
from django.db.models import Q
from .models import Lead, Quote

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
                    default=1,
                ),
                # Then order by origin name, then destination name
                "route__origin__name",
                "route__destination__name",
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

            # Capture and save UTM parameters for Google Ads attribution
            utm_params = [
                "gclid",
                "utm_source",
                "utm_medium",
                "utm_campaign",
                "utm_term",
                "utm_content",
            ]
            for param in utm_params:
                value = request.POST.get(param)
                if value:
                    setattr(reservation, param, value)
                    logger.info(f"Captured UTM parameter {param}: {value}")

            # Save the reservation with UTM data
            reservation.save()

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
                logger.info(
                    "Successfully sent InitiateCheckout event to Meta Conversions API"
                )
            except Exception as e:
                logger.error(
                    f"Error sending InitiateCheckout event to Meta Conversions API: {str(e)}"
                )

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
                # Check for existing leads to prevent duplicates
                email = data.get("email", "").strip()
                phone = data.get("phone", "").strip()
                pickup_location = data.get("pickup_location", "").strip()
                dropoff_location = data.get("dropoff_location", "").strip()
                pickup_date = data.get("pickup_date", "")
                trip_type = "oneway" if data.get("trip_type") == "1" else "roundtrip"

                existing_lead = None
                if email or phone:
                    # Only consider it a duplicate if it's the same person with the same trip details
                    # within the last 7 days (to catch actual duplicates, not just same person)
                    cutoff_time = timezone.now() - timedelta(days=7)

                    # Build query to find exact duplicates
                    query = Q(created_at__gte=cutoff_time)

                    if email:
                        query &= Q(email__iexact=email)
                    if phone:
                        query &= Q(phone__iexact=phone)

                    # Also check if it's the same trip details
                    if pickup_location and dropoff_location and pickup_date:
                        query &= Q(pickup_location__iexact=pickup_location)
                        query &= Q(dropoff_location__iexact=dropoff_location)
                        query &= Q(pickup_date=pickup_date)
                        query &= Q(trip_type=trip_type)

                    existing_lead = Lead.objects.filter(query).first()

                if existing_lead:
                    # Instead of creating a duplicate, create a new quote for the existing lead
                    quote = Quote.objects.create(
                        lead=existing_lead,
                        pickup_location=data.get("pickup_location", ""),
                        dropoff_location=data.get("dropoff_location", ""),
                        pickup_date=data.get("pickup_date"),
                        trip_type="oneway"
                        if data.get("trip_type") == "1"
                        else "roundtrip",
                        vehicle_id=data.get("vehicle_id"),
                        estimated_price=data.get("estimated_price"),
                        status="pending",
                        is_current=True,  # This will automatically unmark other quotes
                    )

                    # Update lead fields if they're missing or if this is a different trip
                    updated = False
                    if (
                        data.get("pickup_location")
                        and data.get("pickup_location") != existing_lead.pickup_location
                    ):
                        existing_lead.pickup_location = data.get("pickup_location")
                        updated = True
                    if (
                        data.get("dropoff_location")
                        and data.get("dropoff_location")
                        != existing_lead.dropoff_location
                    ):
                        existing_lead.dropoff_location = data.get("dropoff_location")
                        updated = True
                    if data.get("vehicle_id") and not existing_lead.vehicle_id:
                        existing_lead.vehicle_id = data.get("vehicle_id")
                        updated = True
                    if (
                        data.get("estimated_price")
                        and not existing_lead.estimated_price
                    ):
                        existing_lead.estimated_price = data.get("estimated_price")
                        updated = True
                    if data.get("pickup_date") and not existing_lead.pickup_date:
                        existing_lead.pickup_date = data.get("pickup_date")
                        updated = True

                    # Reset status to "new" since they're requesting another quote
                    if existing_lead.status in ["lost", "converted"]:
                        existing_lead.status = "new"
                        updated = True

                    # Set high/medium priority based on trip date
                    if data.get("pickup_date"):
                        from datetime import date

                        today = date.today()
                        pickup_date = date.fromisoformat(data.get("pickup_date"))
                        days_until_trip = (pickup_date - today).days
                        if 0 <= days_until_trip <= 14:
                            existing_lead.priority = "high"
                            updated = True

                    if updated:
                        existing_lead.save()

                    logger.info(f"Created new quote for existing lead: {existing_lead}")

                    # Send ntfy notification for updated lead
                    try:
                        send_lead_notification(existing_lead)
                        logger.info(
                            "Successfully sent ntfy notification for updated lead"
                        )
                    except Exception as e:
                        logger.error(
                            f"Error sending ntfy notification for updated lead: {str(e)}"
                        )

                    # Send lead event to Meta Conversions API
                    try:
                        send_lead_event(existing_lead, request)
                        logger.info(
                            "Successfully sent lead event to Meta Conversions API"
                        )
                    except Exception as e:
                        logger.error(
                            f"Error sending lead event to Meta Conversions API: {str(e)}"
                        )

                    return JsonResponse(
                        {
                            "success": True,
                            "lead_id": existing_lead.id,
                            "quote_id": quote.id,
                            "message": "New quote created for existing lead",
                        }
                    )

                # Create new lead if no duplicate found
                lead = form.save(commit=False)

                # Set trip details from the form data
                lead.pickup_location = pickup_location
                lead.dropoff_location = dropoff_location
                lead.trip_type = trip_type
                lead.vehicle_id = data.get("vehicle_id")
                lead.estimated_price = data.get("estimated_price")

                # Set high/medium priority based on trip date
                if lead.pickup_date:
                    from datetime import date

                    today = date.today()
                    days_until_trip = (lead.pickup_date - today).days
                    if 0 <= days_until_trip <= 14:
                        lead.priority = "high"
                    else:
                        lead.priority = "medium"
                else:
                    lead.priority = "medium"

                # Save the lead
                lead.save()

                # Create the first quote for this lead
                quote = Quote.objects.create(
                    lead=lead,
                    pickup_location=data.get("pickup_location", ""),
                    dropoff_location=data.get("dropoff_location", ""),
                    pickup_date=data.get("pickup_date"),
                    trip_type="oneway" if data.get("trip_type") == "1" else "roundtrip",
                    vehicle_id=data.get("vehicle_id"),
                    estimated_price=data.get("estimated_price"),
                    status="pending",
                    is_current=True,
                )

                # Log the lead creation
                logger.info(f"New lead created with quote: {lead}")

                # Send ntfy notification for new lead
                try:
                    send_lead_notification(lead)
                    logger.info("Successfully sent ntfy notification for new lead")
                except Exception as e:
                    logger.error(
                        f"Error sending ntfy notification for new lead: {str(e)}"
                    )

                # Send lead event to Meta Conversions API
                try:
                    send_lead_event(lead, request)
                    logger.info("Successfully sent lead event to Meta Conversions API")
                except Exception as e:
                    logger.error(
                        f"Error sending lead event to Meta Conversions API: {str(e)}"
                    )

                return JsonResponse(
                    {"success": True, "lead_id": lead.id, "quote_id": quote.id}
                )
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
