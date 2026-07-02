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
    CRUISE_LINES,
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
from datetime import timedelta, date
from django.db.models import Q
from .models import Lead, Quote, Reservation

logger = logging.getLogger(__name__)


# UTM source normalization map
_UTM_SOURCE_MAP = {
    "facebook": "meta",
    "fb": "meta",
    "ig": "meta",
    "instagram": "meta",
    "meta": "meta",
    "google": "google",
    "gclid": "google",
}


def _normalize_utm_source(source):
    """Normalize UTM source values (e.g. facebook/fb/ig → meta)."""
    return _UTM_SOURCE_MAP.get(source.lower().strip(), source.lower().strip())


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
            cruise1_form,
            leg1_form,
            flight2_form,
            cruise2_form,
            leg2_form,
        ) = returns_post_form(request, trip_type, rate)

        forms_valid = validate_forms(
            customer_form,
            reservation_form,
            flight1_form,
            cruise1_form,
            leg1_form,
            flight2_form,
            cruise2_form,
            leg2_form,
            trip_type,
        )
        if forms_valid:
            # Clean up recent unpaid duplicates from the same person + route + date
            # so only the latest (most accurate) submission survives.
            # Matches on last_name + pickup_date (not time, since that may be the fix)
            # to avoid collisions when a travel agent books different clients back-to-back.
            email = customer_form.cleaned_data.get("email", "").strip().lower()
            last_name = customer_form.cleaned_data.get("last_name", "").strip()
            pickup_date = leg1_form.cleaned_data.get("pickup_date")
            cutoff = timezone.now() - timedelta(minutes=10)
            stale_dupes = Reservation.objects.filter(
                customer__email__iexact=email,
                customer__last_name__iexact=last_name,
                rate=rate,
                legs__pickup_date=pickup_date,
                status="confirmed",
                created_at__gte=cutoff,
            ).exclude(payments__status="paid")
            if stale_dupes.exists():
                count = stale_dupes.count()
                stale_dupes.delete()
                logger.info(
                    f"Cleaned up {count} unpaid duplicate reservation(s) for {email}"
                )

            customer = customer_form.save()
            reservation = reservation_form.save(
                customer=customer,
                trip_type=trip_type,
                rate=rate,
                base_price=price,
                vehicle=rate.vehicle,
            )

            # Capture and save UTM parameters for Google Ads and Meta Ads attribution
            # Check both POST data (from form) and cookies (for returning visitors)
            utm_params = [
                "gclid",
                "fbclid",  # Facebook Click ID
                "utm_source",
                "utm_medium",
                "utm_campaign",
                "utm_term",
                "utm_content",
                "referrer_host",  # first-touch external referrer (organic AI/search fallback)
            ]
            for param in utm_params:
                # Try POST first (from form submission), then cookies (for returning visitors)
                value = request.POST.get(param) or request.COOKIES.get(param)
                if value:
                    setattr(reservation, param, value)
                    logger.info(f"Captured parameter {param}: {value} (from {'POST' if request.POST.get(param) else 'cookie'})")
            
            # Auto-detect Meta/Facebook traffic if fbclid present but no utm_source
            if reservation.fbclid and not reservation.utm_source:
                reservation.utm_source = "meta"
                reservation.utm_medium = "cpc"
                logger.info("Auto-detected Meta traffic from fbclid")

            # Normalize UTM sources
            if reservation.utm_source:
                reservation.utm_source = _normalize_utm_source(reservation.utm_source)

            # Save the reservation with UTM data (booking_source derived below
            # AFTER travel_agent assignment so agent attribution wins).
            reservation.save()

            # If user is logged in and is a travel agent, tag the reservation
            if request.user.is_authenticated:
                try:
                    travel_agent = TravelAgent.objects.get(user=request.user)
                    reservation.travel_agent = travel_agent
                    # Track who created the reservation
                    reservation.created_by = request.user
                    reservation.modified_by = request.user
                    reservation.last_modified_at = timezone.now()
                    reservation.save()
                except TravelAgent.DoesNotExist:
                    # Not a travel agent, but still track if user is logged in
                    if request.user.is_superuser:
                        reservation.created_by = request.user
                        reservation.modified_by = request.user
                        reservation.last_modified_at = timezone.now()
                        reservation.save()
                    pass  # User is not a travel agent, continue normally

            # Derive canonical booking_source / repeat-customer flag for KPI
            # reporting. Done after travel_agent assignment so agent bookings
            # are correctly attributed. We pass request=None because the
            # public booking flow should never tag as "phone" — staff bookings
            # come through ReservationAdmin.save_model.
            from reservations.attribution import derive_booking_source, derive_is_repeat
            reservation.booking_source = derive_booking_source(reservation, request=None)
            reservation.is_repeat_booking = derive_is_repeat(reservation)
            Reservation.objects.filter(pk=reservation.pk).update(
                booking_source=reservation.booking_source,
                is_repeat_booking=reservation.is_repeat_booking,
            )

            leg1 = leg1_form.save(commit=False)
            leg1.reservation = reservation

            if flight1_form and any(flight1_form.cleaned_data.values()):
                flight1 = flight1_form.save()
                leg1.flight_information = flight1
            else:
                leg1.flight_information = None
            
            if cruise1_form and any(cruise1_form.cleaned_data.values()):
                cruise1 = cruise1_form.save()
                leg1.cruise_information = cruise1
            else:
                leg1.cruise_information = None
            leg1.save()

            if trip_type == "round_trip":
                leg2 = leg2_form.save(commit=False)
                leg2.reservation = reservation

                if flight2_form and any(flight2_form.cleaned_data.values()):
                    flight2 = flight2_form.save()
                    leg2.flight_information = flight2
                else:
                    leg2.flight_information = None
                
                if cruise2_form and any(cruise2_form.cleaned_data.values()):
                    cruise2 = cruise2_form.save()
                    leg2.cruise_information = cruise2
                else:
                    leg2.cruise_information = None
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
            cruise1_form,
            leg1_form,
            flight2_form,
            cruise2_form,
            leg2_form,
        ) = _initalize_form(trip_type, rate, price)

    context = {
        "customer_form": customer_form,
        "reservation_form": reservation_form,
        "flight1_form": flight1_form,
        "cruise1_form": cruise1_form,
        "flight2_form": flight2_form,
        "cruise2_form": cruise2_form,
        "leg1_form": leg1_form,
        "leg2_form": leg2_form,
        "route": rate.route,
        "price": price,
        "trip_type": trip_type.replace("_", " "),
        "vehicle": rate.vehicle,
        "airlines": AIRLINES,
        "cruise_lines": CRUISE_LINES,
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
    return render(request, "reservations/tos_wrapper.html")


def privacy(request):
    return render(request, "reservations/privacy_wrapper.html")


@method_decorator(csrf_exempt, name="dispatch")
class QuoteFormHandlerView(View):
    """
    Handles AJAX POST requests from quote forms created with the quote_form template tag.
    This view processes lead creation and returns JSON responses.
    """

    def post(self, request):
        try:
            data = json.loads(request.body)

            # Sanitize pickup_date: blanks, smart-quote placeholders ("") and any
            # non-ISO value become None instead of crashing the lead save with
            # '"" value has an invalid date format' (was returning a 500).
            raw_date = data.get("pickup_date")
            if isinstance(raw_date, str):
                cleaned_date = raw_date.strip().strip('"“”‘’\'')
                try:
                    date.fromisoformat(cleaned_date)
                    data["pickup_date"] = cleaned_date
                except ValueError:
                    data["pickup_date"] = None

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
                        # Match on normalized (last-10-digits) phone so different
                        # formats of the same number — "+1 301-555-1234" vs
                        # "3015551234" — still dedupe. Raw phone__iexact missed
                        # these, spawning duplicate leads while GHL (which
                        # normalizes) correctly reused one contact.
                        normalized_phone = Lead.normalize_phone(phone)
                        if normalized_phone:
                            query &= Q(normalized_phone=normalized_phone)
                        else:
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
                        today = date.today()
                        pickup_date = date.fromisoformat(data.get("pickup_date"))
                        days_until_trip = (pickup_date - today).days
                        if 0 <= days_until_trip <= 14:
                            existing_lead.priority = "high"
                            updated = True

                    if updated:
                        existing_lead.save()

                    logger.info(f"Created new quote for existing lead: {existing_lead}")

                    # Shared event_id (per quote submission) so the browser-pixel
                    # Lead fired by guest-quote.js and this server-side CAPI Lead
                    # dedupe to ONE event in Meta instead of double-counting.
                    lead_event_id = f"quote_{quote.id}"

                    # Run notifications in background threads to avoid blocking the response
                    from threading import Thread

                    def send_notifications():
                        """Send notifications in background thread"""
                        local_logger = logging.getLogger(__name__)
                        
                        # Send ntfy notification for updated lead
                        try:
                            send_lead_notification(existing_lead)
                            local_logger.info(
                                "Successfully sent ntfy notification for updated lead"
                            )
                        except Exception as e:
                            local_logger.error(
                                f"Error sending ntfy notification for updated lead: {str(e)}"
                            )

                        # Send lead event to Meta Conversions API
                        try:
                            send_lead_event(existing_lead, request, event_id=lead_event_id)
                            local_logger.info(
                                "Successfully sent lead event to Meta Conversions API"
                            )
                        except Exception as e:
                            local_logger.error(
                                f"Error sending lead event to Meta Conversions API: {str(e)}"
                            )

                    # Start background thread for notifications (non-blocking)
                    notification_thread = Thread(target=send_notifications, daemon=True)
                    notification_thread.start()

                    # Return response immediately (don't wait for notifications)
                    return JsonResponse(
                        {
                            "success": True,
                            "lead_id": existing_lead.id,
                            "quote_id": quote.id,
                            "event_id": lead_event_id,
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

                # Capture and save UTM parameters (set by main.html script).
                # referrer_host is the first-touch external referrer — it's what
                # lets the lead-analytics source breakdown attribute organic
                # ChatGPT / Bing / Perplexity / etc. visits that arrive untagged.
                utm_params = [
                    "gclid",
                    "fbclid",  # Facebook Click ID
                    "utm_source",
                    "utm_medium",
                    "utm_campaign",
                    "utm_term",
                    "utm_content",
                    "referrer_host",
                ]
                for param in utm_params:
                    # POST first (book_form posts these as hidden fields), then
                    # fall back to the cookie set client-side for returning visitors.
                    value = request.POST.get(param) or request.COOKIES.get(param)
                    if value:
                        setattr(lead, param, value)
                        logger.info(f"Captured parameter {param} for lead: {value}")
                
                # Auto-detect Meta/Facebook traffic if fbclid present but no utm_source
                if lead.fbclid and not lead.utm_source:
                    lead.utm_source = "meta"
                    lead.utm_medium = "cpc"
                    logger.info("Auto-detected Meta traffic from fbclid for lead")

                # Normalize UTM sources
                if lead.utm_source:
                    lead.utm_source = _normalize_utm_source(lead.utm_source)

                # Set high/medium priority based on trip date
                if lead.pickup_date:
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

                # Custom / unmatched route: no online rate, so the site couldn't quote
                # it instantly. Rather than leaving the guest on the "we'll reach out
                # shortly" promise (the first automated touch is 30 min-9 hrs out and
                # price-less), file a HIGH ops task so a human sends a real price fast —
                # these custom/long routes are the high-ticket jobs most worth chasing.
                if not lead.estimated_price:
                    try:
                        from ops.services import create_task
                        from ops.models import OperationalTask

                        route_label = f"{pickup_location or '?'} → {dropoff_location or '?'}"
                        is_oneway = data.get("trip_type") == "1"
                        create_task(
                            task_type=OperationalTask.TaskType.MANUAL,
                            title=(
                                f"QUOTE NEEDED — {lead.first_name} {lead.last_name}: "
                                f"{route_label}"
                            )[:200],
                            description=(
                                "Custom route with no online rate — send this guest a "
                                "price.\n\n"
                                f"Route: {route_label}\n"
                                f"Trip: {'One way' if is_oneway else 'Round trip'}\n"
                                f"Pickup date: {lead.pickup_date or '—'}\n"
                                f"Phone: {lead.phone or '—'}   Email: {lead.email or '—'}"
                            ),
                            priority=OperationalTask.Priority.HIGH,
                            lead=lead,
                            metadata={"source": "quote_form_no_rate"},
                        )
                    except Exception as e:
                        logger.error(
                            f"Could not create QUOTE NEEDED task for lead {lead.id}: {e}"
                        )

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

                # Shared event_id (per quote submission) so the browser-pixel
                # Lead fired by guest-quote.js and this server-side CAPI Lead
                # dedupe to ONE event in Meta instead of double-counting.
                lead_event_id = f"quote_{quote.id}"

                # Run notifications in background threads to avoid blocking the response
                from threading import Thread

                def send_notifications():
                    """Send notifications in background thread"""
                    local_logger = logging.getLogger(__name__)
                    
                    # Send ntfy notification for new lead
                    try:
                        send_lead_notification(lead)
                        local_logger.info("Successfully sent ntfy notification for new lead")
                    except Exception as e:
                        local_logger.error(
                            f"Error sending ntfy notification for new lead: {str(e)}"
                        )

                    # Send lead event to Meta Conversions API
                    try:
                        send_lead_event(lead, request, event_id=lead_event_id)
                        local_logger.info("Successfully sent lead event to Meta Conversions API")
                    except Exception as e:
                        local_logger.error(
                            f"Error sending lead event to Meta Conversions API: {str(e)}"
                        )

                # Start background thread for notifications (non-blocking)
                notification_thread = Thread(target=send_notifications, daemon=True)
                notification_thread.start()

                # Return response immediately (don't wait for notifications)
                return JsonResponse(
                    {"success": True, "lead_id": lead.id, "quote_id": quote.id,
                     "event_id": lead_event_id}
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


def check_time_availability(request):
    """
    AJAX endpoint to check if a time slot is available (not blocked).
    Returns JSON with availability status and error message if blocked.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Only GET requests are allowed"}, status=405)
    
    pickup_date = request.GET.get("pickup_date")
    pickup_time = request.GET.get("pickup_time")
    
    if not pickup_date or not pickup_time:
        return JsonResponse({"error": "pickup_date and pickup_time are required"}, status=400)
    
    try:
        from datetime import datetime
        from .models import BlockedTimeSlot
        
        # Parse the date and time
        date_obj = datetime.strptime(pickup_date, "%Y-%m-%d").date()
        time_obj = datetime.strptime(pickup_time, "%H:%M").time()
        
        # Check availability
        is_available, blocked_slot = BlockedTimeSlot.is_time_slot_available(date_obj, time_obj)
        
        if is_available:
            return JsonResponse({
                "available": True,
                "message": None
            })
        else:
            # Create user-friendly error message
            if blocked_slot.reason:
                error_msg = (
                    f"We are fully booked for {date_obj.strftime('%B %d, %Y')} "
                    f"from {blocked_slot.start_time.strftime('%I:%M %p')} to "
                    f"{blocked_slot.end_time.strftime('%I:%M %p')}. "
                    f"{blocked_slot.reason}. "
                    f"Please contact the office at 407-212-7190 or try a different time."
                )
            else:
                error_msg = (
                    f"We are fully booked for {date_obj.strftime('%B %d, %Y')} "
                    f"from {blocked_slot.start_time.strftime('%I:%M %p')} to "
                    f"{blocked_slot.end_time.strftime('%I:%M %p')}. "
                    f"Please contact the office at 407-212-7190 or try a different time."
                )
            
            return JsonResponse({
                "available": False,
                "message": error_msg,
                "blocked_slot": {
                    "date": blocked_slot.date.strftime("%Y-%m-%d"),
                    "start_time": blocked_slot.start_time.strftime("%H:%M"),
                    "end_time": blocked_slot.end_time.strftime("%H:%M"),
                    "reason": blocked_slot.reason or ""
                }
            })
            
    except ValueError as e:
        return JsonResponse({"error": f"Invalid date or time format: {str(e)}"}, status=400)
    except Exception as e:
        logger.error(f"Error checking time availability: {str(e)}")
        return JsonResponse({"error": "An error occurred checking availability"}, status=500)
