from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.contrib import messages
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db.models import Sum, Q, Count
from django.utils import timezone
from django.views.decorators.http import require_POST
from django import forms
from decimal import Decimal
import stripe
import stripe.error
import logging
import json
from datetime import datetime, timedelta
from django.views.generic import ListView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.template.loader import render_to_string
from django.db.models import Prefetch
from django.db.models import OuterRef, Subquery

# App imports

from reservations.models import Reservation, Leg, Customer, Flight
from reservations.forms import ReservationAdminForm, CustomerForm, LegForm
from drivers.models import Driver
from payment.utils import get_or_create_stripe_customer
from payment.models import Payment
from rates.models import Vehicle, Rate
from .utils import get_comprehensive_statistics, get_filtered_legs_queryset, calculate_vehicle_statistics
from .forms import (
    DispatcherCustomerForm,
    DispatcherReservationForm,
    DispatcherLegForm,
    DispatcherFlightForm,
    DispatcherLegFormSet,
    DispatcherFlightFormSet,
    DispatcherPricingForm,
    TripTypeForm,
)

# Configure logging and Stripe
logger = logging.getLogger(__name__)
stripe.api_key = settings.STRIPE_SECRET_KEY


class DateForm(forms.Form):
    """Simple form for date selection."""

    date = forms.DateField(widget=forms.SelectDateWidget)


@login_required(login_url="login")
def index(request):
    """
    Dispatcher Dashboard: Shows all legs with date filtering functionality.
    Includes driver assignment and status update capabilities.

    Args:
        request: The HTTP request

    Returns:
        Rendered template with legs for the selected date
    """
    if not request.user.is_superuser:
        return redirect("home")

    selected_date = request.GET.get("date")
    driver_filter = request.GET.get("driver")
    
    try:
        selected_date = (
            datetime.strptime(selected_date, "%Y-%m-%d").date()
            if selected_date
            else timezone.localdate()
        )
    except ValueError:
        selected_date = timezone.localdate()

    # Get all legs for the selected date
    legs_query = Leg.objects.filter(pickup_date=selected_date)
    
    # Apply driver filter if specified
    if driver_filter:
        legs_query = legs_query.filter(driver_id=driver_filter)
    
    legs = (
        legs_query
        .select_related(
            "reservation",
            "reservation__customer",
            "reservation__vehicle",
            "reservation__travel_agent",
            "reservation__travel_agent__user",
            "driver",
        )
        .prefetch_related(
            "reservation__legs",
            "reservation__payments",
        )
        .order_by("pickup_time")
    )

    # Get all drivers for assignment dropdown
    drivers = Driver.objects.all()

    # Calculate total revenue from legs on this day
    total_revenue = sum(leg.reservation.total_price for leg in legs)

    context = {
        "legs": legs,
        "selected_date": selected_date,
        "driver_filter": driver_filter,
        "total_legs": legs.count(),
        "total_revenue": total_revenue,
        "drivers": drivers,
    }

    return render(request, "dispatching/legs_filter.html", context)


class ReservationListView(LoginRequiredMixin, UserPassesTestMixin, ListView):
    model = Reservation
    template_name = "dispatching/all_reservations.html"
    context_object_name = "reservations"
    paginate_by = 10

    def test_func(self):
        return self.request.user.is_superuser

    def get_queryset(self):
        queryset = (
            Reservation.objects.select_related("customer", "vehicle", "rate", "travel_agent", "travel_agent__user")
            .prefetch_related("legs", "payments")
            .order_by("-created_at")
        )

        # Apply filters
        search_query = self.request.GET.get("search_q")
        if search_query:
            queryset = queryset.filter(
                Q(customer__first_name__icontains=search_query)
                | Q(customer__last_name__icontains=search_query)
                | Q(customer__email__icontains=search_query)
                | Q(customer__phone_number__icontains=search_query)
                | Q(id__icontains=search_query)
            )

        time_filter = self.request.GET.get("time_filter")
        if time_filter == "week":
            queryset = queryset.filter(
                created_at__gte=timezone.now() - timedelta(days=7)
            )
        elif time_filter == "month":
            queryset = queryset.filter(
                created_at__gte=timezone.now() - timedelta(days=30)
            )

        status_filter = self.request.GET.get("status")
        if status_filter:
            if status_filter == "need_payment":
                # Filter for reservations with no payments
                queryset = queryset.filter(payments__isnull=True)
            else:
                queryset = queryset.filter(status=status_filter)

        # Add is_first_leg property to each leg - optimized to avoid N+1
        for reservation in queryset:
            legs_list = list(reservation.legs.all())
            if legs_list:
                first_leg = min(legs_list, key=lambda x: x.pickup_time)
                for leg in legs_list:
                    leg.is_first_leg = leg.id == first_leg.id

        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        queryset = self.get_queryset()

        # Annotate counts in a single query
        stats = queryset.aggregate(
            total_count=Count("id"),
            pending_count=Count("id", filter=Q(status="pending")),
            confirmed_count=Count("id", filter=Q(status="confirmed")),
            need_payment_count=Count("id", filter=Q(payments__isnull=True)),
            total_revenue=Sum("total_price", filter=Q(payments__status="paid")),
        )

        # Add statistics to context
        context.update(
            {
                "total_reservations": stats["total_count"],
                "pending_reservations": stats["pending_count"],
                "confirmed_reservations": stats["confirmed_count"],
                "need_payment_count": stats["need_payment_count"],
                "total_revenue": stats["total_revenue"] or 0,
                "search_query": self.request.GET.get("search_q", ""),
                "status_filter": self.request.GET.get("status", ""),
                "time_filter": self.request.GET.get("time_filter", "all"),
            }
        )
        return context

    def get(self, request, *args, **kwargs):
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            # Handle AJAX request for real-time search
            queryset = self.get_queryset()
            context = self.get_context_data(object_list=queryset)
            html = render_to_string(
                "dispatching/includes/reservation_list.html", context, request=request
            )
            return JsonResponse(
                {
                    "html": html,
                    "total_count": context["total_reservations"],
                    "pending_count": context["pending_reservations"],
                    "confirmed_count": context["confirmed_reservations"],
                    "need_payment_count": context["need_payment_count"],
                    "total_revenue": context["total_revenue"],
                }
            )
        return super().get(request, *args, **kwargs)


@login_required(login_url="login")
def reservation_details(request, id):
    """
    Detailed view for a reservation with all relevant information.

    Args:
        request: The HTTP request
        id: The UUID of the reservation

    Returns:
        Rendered template with detailed reservation information
    """
    if not request.user.is_superuser:
        return redirect("home")

    # Get the reservation with all related data
    reservation = get_object_or_404(
        Reservation.objects.prefetch_related(
            "legs__flight_information", "legs__driver", "payments"
        ).select_related("customer", "vehicle", "rate"),
        uuid=id,
    )

    # Get all drivers for assignment dropdown
    drivers = Driver.objects.all()

    # Calculate payment details
    payments = reservation.payments.all()
    payment_status = "Paid" if payments.exists() else "Unpaid"
    payment_method = (
        payments.first().payment_type.title() if payments.exists() else "N/A"
    )

    # Ensure Stripe public key is available
    stripe_key = settings.STRIPE_PUBLIC_KEY
    if not stripe_key:
        logger.error("Stripe public key is not configured")
    else:
        logger.info(f"Stripe public key is configured ✅")

    context = {
        "reservation": reservation,
        "total_legs": reservation.legs.count(),
        "drivers": drivers,
        "payment_status": payment_status,
        "payment_method": payment_method,
        "total_cost": {
            "base": reservation.base_price,
            "additional": reservation.additional_charges,
            "total": reservation.total_price,
        },
        "STRIPE_PUBLIC_KEY": stripe_key,
    }

    return render(request, "dispatching/reservation_view.html", context)


@login_required(login_url="login")
def modify_reservation(request, id):
    """
    Update an existing reservation, its customer, and legs.

    Args:
        request: The HTTP request
        id: The UUID of the reservation

    Returns:
        Redirect to reservation details on success or form with errors
    """
    if not request.user.is_superuser:
        return redirect("home")

    reservation = get_object_or_404(
        Reservation.objects.prefetch_related("legs"), uuid=id
    )

    if request.method == "POST":
        customer_form = CustomerForm(request.POST, instance=reservation.customer)
        reservation_form = ReservationAdminForm(request.POST, instance=reservation)

        if customer_form.is_valid() and reservation_form.is_valid():
            # Save customer
            customer = customer_form.save()

            # Save reservation with commit=False first
            updated_reservation = reservation_form.save(commit=False)

            updated_reservation.customer = customer
            # Save the reservation
            updated_reservation.save()

            # Process leg forms
            leg_forms = []
            for i in range(1, 3):  # Support up to 2 legs
                leg_prefix = f"leg_{i}"

                # Create a dictionary with all possible leg form fields
                leg_data = {}
                for field in request.POST:
                    if field.startswith(leg_prefix):
                        leg_data[field] = request.POST.get(field)

                # Check if any meaningful data was submitted
                has_data = False
                for key, value in leg_data.items():
                    if value and not key.endswith(
                        "-id"
                    ):  # Ignore empty values and ID fields
                        has_data = True
                        break

                if has_data:
                    leg_instance = (
                        reservation.legs.all()[i - 1]
                        if reservation.legs.count() >= i
                        else None
                    )
                    leg_form = LegForm(
                        request.POST, instance=leg_instance, prefix=leg_prefix
                    )
                    if leg_form.is_valid():
                        leg = leg_form.save(commit=False)
                        leg.reservation = updated_reservation
                        leg.save()

            messages.success(
                request, f"Reservation {updated_reservation.uuid} updated successfully."
            )
            return redirect("reservation_details", id=updated_reservation.uuid)
        else:
            messages.error(request, "Please correct the errors in the form.")
    else:
        customer_form = CustomerForm(instance=reservation.customer)
        reservation_form = ReservationAdminForm(instance=reservation)
        leg_forms = [
            LegForm(instance=leg, prefix=f"leg_{i + 1}")
            for i, leg in enumerate(reservation.legs.all())
        ]
        if not leg_forms:
            leg_forms.append(LegForm(prefix="leg_1"))

    context = {
        "reservation": reservation,
        "customer_form": customer_form,
        "reservation_form": reservation_form,
        "leg_forms": leg_forms,
    }

    return render(request, "dispatching/modify_reservation.html", context)


@login_required(login_url="login")
def legs_list(request):
    """
    Display a filterable list of all upcoming legs.

    Args:
        request: The HTTP request
    Returns:
        Rendered template with filtered legs
    """
    if not request.user.is_superuser:
        return redirect("home")

    # Get filter parameters
    date_filter = request.GET.get("date")
    date_from = request.GET.get("date_from")
    date_to = request.GET.get("date_to")
    status_filter = request.GET.get("status")
    time_filter = request.GET.get("time_filter", "all")
    trip_type_filter = request.GET.get("trip_type")  # New filter for arrival/return
    vehicle_filter = request.GET.get("vehicle")  # New filter for vehicle type
    driver_filter = request.GET.get("driver")  # New filter for driver
    today = timezone.localdate()

    # Get filtered legs using utils
    legs_query = get_filtered_legs_queryset(
        date_filter=date_filter,
        date_from=date_from,
        date_to=date_to,
        status_filter=status_filter,
        time_filter=time_filter,
        driver_filter=driver_filter
    )

    # Apply vehicle filter
    if vehicle_filter:
        legs_query = legs_query.filter(
            reservation__vehicle__vehicle_type=vehicle_filter
        )

    # Get today's count in a single query
    today_count = legs_query.filter(pickup_date=today).count()

    # Order by pickup date first, then pickup time for better readability
    legs = legs_query.order_by("pickup_date", "pickup_time")

    # PAGINATION: Show 20 legs per page
    paginator = Paginator(legs, 20)
    page = request.GET.get("page")
    try:
        page_obj = paginator.page(page)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    # Get all drivers in a single query
    drivers = Driver.objects.select_related("profile").all()

    # Get all vehicles for filter dropdown
    vehicles = Vehicle.objects.all()

    # Apply trip type filter if specified (filter in Python since it's a computed property)
    if trip_type_filter:
        filtered_legs = []
        for leg in page_obj:
            if leg.get_trip_type() == trip_type_filter:
                filtered_legs.append(leg)
        page_obj.object_list = filtered_legs
        page_obj._object_list = filtered_legs

    # Calculate statistics using utils - reuse the already fetched data
    vehicle_stats = calculate_vehicle_statistics(page_obj)
    
    # Calculate trip type statistics
    trip_type_stats = {"arrival": 0, "return": 0, "other": 0}
    for leg in page_obj:
        trip_type = leg.get_trip_type()
        trip_type_stats[trip_type] += 1

    # Calculate current page statistics in a single pass
    current_page_stats = {
        "arrival": 0,
        "return": 0,
        "other": 0,
        "total_revenue": 0
    }
    
    # Pre-calculate leg counts for each reservation to avoid N+1 queries
    reservation_leg_counts = {}
    for leg in page_obj:
        reservation_id = leg.reservation.id
        if reservation_id not in reservation_leg_counts:
            reservation_leg_counts[reservation_id] = len(leg.reservation.legs.all())
    
    for leg in page_obj:
        # Count trip types for current page
        trip_type = leg.get_trip_type()
        current_page_stats[trip_type] += 1
        
        # Sum revenue for current page using leg's revenue share
        if leg.revenue_share:
            current_page_stats["total_revenue"] += leg.revenue_share
        else:
            # Use pre-calculated leg count
            leg_count = reservation_leg_counts.get(leg.reservation.id, 1)
            if leg_count > 0:
                current_page_stats["total_revenue"] += leg.reservation.total_price / leg_count

    context = {
        "legs": page_obj,
        "filter_date": date_filter,
        "date_from": date_from,
        "date_to": date_to,
        "status_filter": status_filter,
        "time_filter": time_filter,
        "trip_type_filter": trip_type_filter,  # Add to context
        "vehicle_filter": vehicle_filter,  # Add vehicle filter to context
        "driver_filter": driver_filter,  # Add driver filter to context
        "trip_type_stats": trip_type_stats,  # Add statistics
        "vehicle_stats": vehicle_stats,  # Add vehicle statistics
        "current_page_stats": current_page_stats,  # Add current page statistics
        "drivers": drivers,
        "vehicles": vehicles,  # Add vehicles to context
        "today_count": today_count,
        "page_obj": page_obj,
    }

    return render(request, "dispatching/legs_list.html", context)


@login_required
@require_POST
def update_leg_assignment(request):
    """
    Update a leg's driver assignment or status via AJAX.

    Args:
        request: The HTTP request with JSON payload

    Returns:
        JsonResponse indicating success or failure
    """
    logger.info("Received update_leg_assignment request")

    if not request.user.is_superuser:
        logger.warning(f"Permission denied for user {request.user.username}")
        return JsonResponse(
            {"success": False, "error": "Permission denied"}, status=403
        )

    try:
        try:
            data = json.loads(request.body)
            logger.info(f"Received data: {data}")
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error: {str(e)}")
            return JsonResponse(
                {"success": False, "error": f"Invalid JSON: {str(e)}"}, status=400
            )

        leg_id = data.get("leg_id")
        field = data.get("field")
        value = data.get("value")

        logger.info(
            f"Processing request - leg_id: {leg_id}, field: {field}, value: {value}"
        )

        if not leg_id or not field:
            logger.warning("Missing required fields")
            return JsonResponse(
                {"success": False, "error": "Missing required data"}, status=400
            )

        # Get the leg
        try:
            leg = Leg.objects.get(id=leg_id)
            logger.info(f"Found leg for {leg.reservation}")
        except Leg.DoesNotExist:
            logger.warning(f"Leg with ID {leg_id} not found")
            return JsonResponse(
                {"success": False, "error": "Leg not found"}, status=404
            )

        if field == "driver":
            if value:
                try:
                    driver = Driver.objects.get(id=value)
                    logger.info(f"Found driver with ID {value}")
                    leg.driver = driver
                    leg.save()
                    logger.info(
                        f"Updated leg {leg_id} with driver {driver.profile.username if hasattr(driver, 'profile') else driver.id}"
                    )
                except Driver.DoesNotExist:
                    logger.warning(f"Driver with ID {value} not found")
                    return JsonResponse(
                        {"success": False, "error": "Driver not found"}, status=404
                    )
                except AttributeError as e:
                    logger.error(
                        f"Attribute error: {str(e)} - check if driver has profile attribute"
                    )
                    return JsonResponse(
                        {"success": False, "error": f"Driver profile error: {str(e)}"},
                        status=500,
                    )
                except Exception as e:
                    logger.error(f"Error updating driver: {str(e)}")
                    return JsonResponse(
                        {"success": False, "error": f"Error updating driver: {str(e)}"},
                        status=500,
                    )
            else:
                leg.driver = None
                leg.save()
                logger.info(f"Removed driver from leg {leg_id}")
        elif field == "status":
            try:
                # Update the LEG status, not the reservation status
                valid_statuses = [
                    "in-progress",
                    "confirmed",
                    "on-the-way",
                    "on-location",
                    "picked-up",
                    "completed",
                ]
                if value in valid_statuses:
                    leg.status = value
                    leg.save()
                    logger.info(f"Updated leg {leg_id} status to {value}")
                    
                    # Check if reservation should be auto-completed
                    if value == "completed":
                        reservation_updated = leg.reservation.check_and_update_completion_status()
                        if reservation_updated:
                            logger.info(f"Auto-completed reservation {leg.reservation.id} - all legs completed")
                else:
                    logger.warning(f"Invalid status value: {value}")
                    return JsonResponse(
                        {"success": False, "error": f"Invalid status value: {value}"},
                        status=400,
                    )
            except Exception as e:
                logger.error(f"Error updating status: {str(e)}")
                return JsonResponse(
                    {"success": False, "error": f"Error updating status: {str(e)}"},
                    status=500,
                )
        else:
            logger.warning(f"Invalid field: {field}")
            return JsonResponse(
                {"success": False, "error": "Invalid field"}, status=400
            )

        return JsonResponse({"success": True})

    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return JsonResponse(
            {"success": False, "error": f"Server error: {str(e)}"}, status=500
        )


@login_required
@require_POST
def update_private_notes(request):
    """
    Updates the private notes and special requests for a reservation.

    Args:
        request: The HTTP request with JSON payload

    Returns:
        JsonResponse indicating success or failure
    """
    if not request.user.is_superuser:
        return JsonResponse(
            {"success": False, "error": "Permission denied"}, status=403
        )

    try:
        data = json.loads(request.body)
        reservation_id = data.get("reservation_id")
        private_notes = data.get("private_notes")
        special_requests = data.get("special_requests")

        if not reservation_id:
            return JsonResponse(
                {"success": False, "error": "Missing reservation ID"}, status=400
            )

        # Get the reservation
        reservation = get_object_or_404(Reservation, uuid=reservation_id)

        # Update fields
        update_fields = []
        if private_notes is not None:
            reservation.private_notes = private_notes
            update_fields.append("private_notes")
        
        if special_requests is not None:
            reservation.special_requests = special_requests
            update_fields.append("special_requests")

        if update_fields:
            reservation.save(update_fields=update_fields)

        return JsonResponse({"success": True})

    except Exception as e:
        logger.error(f"Error updating private notes: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)})


def create_checkout_session(request, reservation_id):
    """
    Create a Stripe checkout session for a reservation payment.

    Args:
        request: The HTTP request
        reservation_id: The UUID of the reservation

    Returns:
        Redirect to Stripe checkout or error response
    """
    reservation = get_object_or_404(Reservation, uuid=reservation_id)
    logger.info(f"{request.user} Making a Checkout Session for {reservation.customer}")

    stripe_customer = get_or_create_stripe_customer(reservation)
    success_url = request.build_absolute_uri(
        reverse("payment_success") + f"?q={reservation.uuid}"
    )
    cancel_url = request.build_absolute_uri(
        reverse("payment_cancel") + f"?q={reservation.uuid}"
    )

    try:
        checkout_session = stripe.checkout.Session.create(
            customer=stripe_customer.id,
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": int(reservation.total_price * 100),
                        "product_data": {
                            "name": f"Reservation #{reservation.id}",
                            "description": f"Transportation service",
                        },
                    },
                    "quantity": 1,
                }
            ],
            mode="payment",
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={
                "reservation_id": reservation.id,
                "customer_id": reservation.customer.id,
            },
        )
        logger.info(f"Created checkout session: {checkout_session.id}")
        return redirect(checkout_session.url, code=303)
    except stripe.error.StripeError as e:
        logger.error(f"Stripe error: {str(e)}")
        return JsonResponse({"error": str(e)}, status=400)
    except Exception as e:
        logger.error(f"Error creating checkout session: {str(e)}")
        return JsonResponse({"error": str(e)}, status=400)


def save_card(request, reservation_id):
    """
    Create a Stripe checkout session for saving a card.

    Args:
        request: The HTTP request
        reservation_id: The UUID of the reservation

    Returns:
        Redirect to Stripe checkout or error page
    """
    reservation = get_object_or_404(Reservation, uuid=reservation_id)
    success_url = request.build_absolute_uri(
        reverse("payment_success") + f"?q={reservation.uuid}"
    )
    cancel_url = request.build_absolute_uri(
        reverse("payment_cancel") + f"?q={reservation.uuid}"
    )

    try:
        stripe_customer = get_or_create_stripe_customer(reservation)

        checkout_session_params = {
            "customer": stripe_customer.id,
            "payment_method_types": ["card"],
            "mode": "setup",
            "billing_address_collection": "auto",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "metadata": {
                "reservation_uuid": str(reservation.uuid),
                "reservation_id": reservation.id,
                "customer_id": reservation.customer.id,
                "initiated_by": "dispatcher",
                "dispatcher_action": "save_card",
            },
            "client_reference_id": reservation.uuid,
        }

        session = stripe.checkout.Session.create(**checkout_session_params)
        return redirect(session.url, code=303)
    except stripe.error.StripeError as e:
        return render(request, "stripe/error.html", {"error": e})
    except Exception as e:
        logger.error(f"Unexpected error in save_card: {e}")
        return render(
            request, "stripe/error.html", {"error": "An unexpected error occurred"}
        )





def dispatcher_payment_portal(request, reservation_id):
    """
    A portal for dispatchers to process payments or save cards for reservations.

    Args:
        request: The HTTP request
        reservation_id: The UUID of the reservation

    Returns:
        Rendered form or redirect to Stripe checkout
    """
    reservation = get_object_or_404(Reservation, uuid=reservation_id)
    # Redirect URLs: These are where Stripe sends the user back.
    base_success_url = request.build_absolute_uri(reverse("payment_success"))
    base_cancel_url = request.build_absolute_uri(reverse("payment_cancel"))

    success_url_with_context = (
        f"{base_success_url}?q={reservation.uuid}&source=dispatcher_portal"
    )
    cancel_url_with_context = (
        f"{base_cancel_url}?q={reservation.uuid}&source=dispatcher_portal"
    )

    # Check if customer has saved payment methods
    customer = reservation.customer
    has_saved_cards = False
    payment_methods = []

    if hasattr(customer, "stripe_customer_id") and customer.stripe_customer_id:
        try:
            payment_methods = stripe.PaymentMethod.list(
                customer=customer.stripe_customer_id, type="card"
            )
            has_saved_cards = len(payment_methods.data) > 0
        except stripe.error.StripeError as e:
            logger.error(f"Error fetching payment methods: {e}")
            # If there's a Stripe error (like invalid customer ID), clear it and create new customer
            if "No such customer" in str(e):
                logger.info(f"Clearing invalid Stripe customer ID for customer {customer.id}")
                customer.stripe_customer_id = None
                customer.save()
        except Exception as e:
            logger.error(f"Unexpected error fetching payment methods: {e}")

    if request.method == "POST":
        action = request.POST.get("action")
        amount_str = request.POST.get("amount")
        description = request.POST.get(
            "description", f"Trip Fare for Res ID #{reservation.id}"
        )
        selected_payment_method = request.POST.get("payment_method_id")

        try:
            # Check for existing stripe customer ID first
            if hasattr(customer, "stripe_customer_id") and customer.stripe_customer_id:
                stripe_customer_id = customer.stripe_customer_id
            else:
                # Only create if doesn't exist
                stripe_customer = get_or_create_stripe_customer(reservation)
                stripe_customer_id = stripe_customer.id
                
            # Verify the customer ID is still valid by attempting to retrieve it
            try:
                stripe.Customer.retrieve(stripe_customer_id)
            except stripe.error.StripeError as e:
                if "No such customer" in str(e):
                    logger.info(f"Stripe customer {stripe_customer_id} no longer exists, creating new one")
                    stripe_customer = get_or_create_stripe_customer(reservation)
                    stripe_customer_id = stripe_customer.id

            if action == "make_payment":
                # Validate amount
                if not amount_str:
                    messages.error(request, "Amount is required for making a payment.")
                    return render(
                        request,
                        "dispatching/dispatcher_payment_portal.html",
                        {
                            "reservation": reservation,
                            "selected_action": action,
                            "entered_description": description,
                            "has_saved_cards": has_saved_cards,
                            "payment_methods": payment_methods.data
                            if has_saved_cards
                            else [],
                        },
                    )
                try:
                    amount_decimal = Decimal(amount_str)
                    if amount_decimal <= 0:
                        raise ValueError("Payment amount must be positive.")
                    amount_in_cents = int(amount_decimal * 100)
                except ValueError as e:
                    messages.error(request, str(e))
                    return render(
                        request,
                        "dispatching/dispatcher_payment_portal.html",
                        {
                            "reservation": reservation,
                            "selected_action": action,
                            "entered_amount": amount_str,
                            "entered_description": description,
                            "has_saved_cards": has_saved_cards,
                            "payment_methods": payment_methods.data
                            if has_saved_cards
                            else [],
                        },
                    )

                # Create a new product with the custom description
                product = stripe.Product.create(
                    name=description,
                    metadata={
                        "reservation_uuid": str(reservation.uuid),
                        "reservation_id": reservation.id,
                    },
                )

                # Create price for the new product
                price = stripe.Price.create(
                    currency="usd",
                    unit_amount=amount_in_cents,
                    product=product.id,
                )

                checkout_session_params = {
                    "customer": stripe_customer_id,
                    "line_items": [{"price": price.id, "quantity": 1}],
                    "mode": "payment",
                    "success_url": success_url_with_context,
                    "cancel_url": cancel_url_with_context,
                    "payment_intent_data": {
                        "setup_future_usage": "off_session"  # Allow saving the card for future use
                    },
                    "metadata": {
                        "reservation_uuid": str(reservation.uuid),
                        "reservation_id": reservation.id,
                        "customer_id": reservation.customer.id,
                        "initiated_by": "dispatcher",
                        "dispatcher_action": action,
                        "payment_amount_cents": amount_in_cents,
                        "payment_description": description,
                    },
                }

                session = stripe.checkout.Session.create(**checkout_session_params)
                return redirect(session.url, code=303)

            elif action == "save_card":
                checkout_session_params = {
                    "customer": stripe_customer_id,
                    "payment_method_types": ["card"],
                    "mode": "setup",
                    "success_url": success_url_with_context,
                    "cancel_url": cancel_url_with_context,
                    "metadata": {
                        "reservation_uuid": str(reservation.uuid),
                        "reservation_id": reservation.id,
                        "customer_id": reservation.customer.id,
                        "initiated_by": "dispatcher",
                        "dispatcher_action": action,
                    },
                }

                session = stripe.checkout.Session.create(**checkout_session_params)
                return redirect(session.url, code=303)

        except stripe.error.StripeError as e:
            logger.error(
                f"Stripe error for dispatcher action on reservation {reservation.uuid}: {e}"
            )
            messages.error(request, f"Payment system error: {str(e)}")
        except Exception as e:
            logger.error(
                f"Unexpected error during dispatcher payment action for {reservation.uuid}: {e}"
            )
            messages.error(request, "An unexpected error occurred. Please try again.")

        # If any error, re-render form with messages
        return render(
            request,
            "dispatching/dispatcher_payment_portal.html",
            {
                "reservation": reservation,
                "selected_action": action,
                "entered_amount": amount_str if action == "make_payment" else None,
                "entered_description": description
                if action == "make_payment"
                else None,
                "has_saved_cards": has_saved_cards,
                "payment_methods": payment_methods.data if has_saved_cards else [],
            },
        )

    # GET request
    return render(
        request,
        "dispatching/dispatcher_payment_portal.html",
        {
            "reservation": reservation,
            "has_saved_cards": has_saved_cards,
            "payment_methods": payment_methods.data if has_saved_cards else [],
        },
    )


def charge_saved_card(request, reservation_id):
    """
    Charge a previously saved card for a reservation.

    Args:
        request: The HTTP request
        reservation_id: The UUID of the reservation

    Returns:
        JSON response with result or error
    """
    reservation = get_object_or_404(Reservation, uuid=reservation_id)
    customer = reservation.customer

    # First check if customer already has a Stripe ID
    if not hasattr(customer, "stripe_customer_id") or not customer.stripe_customer_id:
        return JsonResponse(
            {
                "error": "Customer has no saved payment methods. Please collect payment information first."
            },
            status=400,
        )

    try:
        # Use existing customer ID instead of creating a new one
        stripe_customer_id = customer.stripe_customer_id

        # Get saved payment methods for this customer
        payment_methods = stripe.PaymentMethod.list(
            customer=stripe_customer_id, type="card"
        )

        # Check if customer has any saved payment methods
        if not payment_methods.data:
            return JsonResponse(
                {"error": "No saved payment methods found for this customer."},
                status=400,
            )

        # Use the most recent payment method by default
        payment_method_id = payment_methods.data[0].id

        # Create a payment intent
        payment_intent = stripe.PaymentIntent.create(
            amount=int(reservation.total_price * 100),
            currency="usd",
            customer=stripe_customer_id,
            payment_method=payment_method_id,
            off_session=True,  # Important for using saved card
            confirm=True,  # Confirm the payment immediately
            metadata={
                "reservation_id": reservation.id,
                "customer_id": reservation.customer.id,
                "payment_type": "saved_card",
            },
        )

        # Handle the payment result
        if payment_intent.status == "succeeded":
            # Update your reservation status or create payment record
            # ...

            return JsonResponse(
                {
                    "success": True,
                    "message": "Payment processed successfully",
                    "payment_intent_id": payment_intent.id,
                }
            )
        else:
            return JsonResponse(
                {
                    "success": False,
                    "status": payment_intent.status,
                    "message": "Payment requires additional action or failed",
                }
            )

    except stripe.error.CardError as e:
        # Card was declined
        err = e.error
        return JsonResponse({"error": f"Card error: {err.message}"}, status=400)
    except stripe.error.StripeError as e:
        logger.error(f"Stripe error: {str(e)}")
        return JsonResponse({"error": str(e)}, status=400)
    except Exception as e:
        logger.error(f"Error charging saved card: {str(e)}")
        return JsonResponse({"error": str(e)}, status=400)


@login_required
@require_POST
def update_reservation_status(request):
    """
    Update a reservation's status via AJAX.
    """
    if not request.user.is_superuser:
        return JsonResponse(
            {"success": False, "error": "Permission denied"}, status=403
        )

    try:
        data = json.loads(request.body)
        reservation_id = data.get("reservation_id")
        status = data.get("status")

        if not reservation_id or not status:
            return JsonResponse(
                {"success": False, "error": "Missing required data"}, status=400
            )

        # Get the reservation
        reservation = get_object_or_404(Reservation, uuid=reservation_id)

        # Update status
        valid_statuses = ["pending", "confirmed", "completed", "cancelled"]
        if status in valid_statuses:
            reservation.status = status
            reservation.save()
            return JsonResponse({"success": True, "status": status})
        else:
            return JsonResponse(
                {"success": False, "error": "Invalid status"}, status=400
            )

    except Exception as e:
        logger.error(f"Error updating reservation status: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required(login_url="login")
def statistics_page(request):
    """
    Dedicated statistics page showing comprehensive vehicle and trip statistics.
    """
    if not request.user.is_superuser:
        return redirect("home")
    
    # Get filter parameters
    date_filter = request.GET.get("date")
    date_from = request.GET.get("date_from")
    date_to = request.GET.get("date_to")
    status_filter = request.GET.get("status")
    time_filter = request.GET.get("time_filter", "all")
    driver_filter = request.GET.get("driver")
    
    # Get comprehensive statistics using utils
    stats = get_comprehensive_statistics(
        date_filter=date_filter,
        date_from=date_from,
        date_to=date_to,
        status_filter=status_filter,
        time_filter=time_filter,
        driver_filter=driver_filter
    )
    
    context = {
        'vehicle_stats': stats['vehicle_stats'],
        'trip_type_stats': stats['trip_type_stats'],
        'status_stats': stats['status_stats'],
        'driver_stats': stats['driver_stats'],
        'active_drivers_count': stats['active_drivers_count'],
        'total_legs': stats['total_legs'],
        'total_revenue': stats['total_revenue'],
        'filter_date': date_filter,
        'date_from': date_from,
        'date_to': date_to,
        'status_filter': status_filter,
        'time_filter': time_filter,
        'driver_filter': driver_filter,
    }
    
    return render(request, "dispatching/statistics.html", context)


@login_required
@require_POST
def update_contact_info(request):
    """
    Update customer contact information via AJAX.
    """
    if not request.user.is_superuser:
        return JsonResponse(
            {"success": False, "error": "Permission denied"}, status=403
        )

    try:
        data = json.loads(request.body)
        reservation_id = data.get("reservation_id")
        contact_data = data.get("contact_data", {})

        if not reservation_id:
            return JsonResponse(
                {"success": False, "error": "Missing reservation ID"}, status=400
            )

        # Get the reservation
        reservation = get_object_or_404(Reservation, uuid=reservation_id)
        customer = reservation.customer

        # Update customer fields
        for field, value in contact_data.items():
            if hasattr(customer, field) and value is not None:
                setattr(customer, field, value)

        # Save the customer
        customer.save()

        # Return updated customer data
        return JsonResponse({
            "success": True,
            "message": "Contact information updated successfully",
            "customer": {
                "full_name": customer.get_full_name(),
                "email": customer.email,
                "phone_number": customer.phone_number,
                "zipcode": customer.zipcode,
            }
        })

    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)
    except Exception as e:
        logger.error(f"Error updating contact info: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required
@require_POST
def update_leg_info(request):
    """
    Update leg information including flight details via AJAX.
    """
    if not request.user.is_superuser:
        return JsonResponse(
            {"success": False, "error": "Permission denied"}, status=403
        )

    try:
        data = json.loads(request.body)
        leg_id = data.get("leg_id")
        leg_data = data.get("leg_data", {})
        flight_data = data.get("flight_data", {})

        if not leg_id:
            return JsonResponse(
                {"success": False, "error": "Missing leg ID"}, status=400
            )

        # Get the leg
        leg = get_object_or_404(Leg, id=leg_id)

        # Update leg fields
        update_fields = []
        for field, value in leg_data.items():
            if hasattr(leg, field) and value is not None:
                # Handle date and time fields properly
                if field == 'pickup_date' and value:
                    from datetime import datetime
                    try:
                        # Convert string to date object
                        date_obj = datetime.strptime(value, '%Y-%m-%d').date()
                        setattr(leg, field, date_obj)
                        update_fields.append(field)
                    except ValueError:
                        return JsonResponse({"success": False, "error": "Invalid date format"}, status=400)
                elif field == 'pickup_time' and value:
                    from datetime import datetime
                    try:
                        # Convert string to time object
                        time_obj = datetime.strptime(value, '%H:%M').time()
                        setattr(leg, field, time_obj)
                        update_fields.append(field)
                    except ValueError:
                        return JsonResponse({"success": False, "error": "Invalid time format"}, status=400)
                else:
                    setattr(leg, field, value)
                    update_fields.append(field)

        # Handle flight information
        if flight_data.get("airline") or flight_data.get("flight_number"):
            # Create or update flight information
            if leg.flight_information:
                flight = leg.flight_information
                if flight_data.get("airline") is not None:
                    flight.airline = flight_data["airline"]
                if flight_data.get("flight_number") is not None:
                    flight.flight_number = flight_data["flight_number"]
                flight.save()
            else:
                # Create new flight information
                from reservations.models import Flight
                flight = Flight.objects.create(
                    airline=flight_data.get("airline", ""),
                    flight_number=flight_data.get("flight_number", "")
                )
                leg.flight_information = flight
                update_fields.append("flight_information")

        # Save the leg if any fields were updated
        if update_fields:
            leg.save(update_fields=update_fields)

        return JsonResponse({
            "success": True,
            "message": "Leg information updated successfully",
            "leg": {
                "pickup_date": leg.pickup_date.isoformat() if leg.pickup_date else None,
                "pickup_time": leg.pickup_time.strftime("%H:%M") if leg.pickup_time else None,
                "pickup_location": leg.pickup_location,
                "dropoff_location": leg.dropoff_location,
                "private_notes": leg.private_notes,
                "flight_info": {
                    "airline": leg.flight_information.airline if leg.flight_information else "",
                    "flight_number": leg.flight_information.flight_number if leg.flight_information else "",
                } if leg.flight_information else {"airline": "", "flight_number": ""}
            }
        })

    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)
    except Exception as e:
        logger.error(f"Error updating leg info: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


# ============================================================================
# DISPATCHER BOOKING SYSTEM - Multi-Step Flow
# ============================================================================

@login_required(login_url="login")
def dispatcher_booking_start(request):
    """
    Step 1: Trip type selection for dispatcher booking
    """
    if not request.user.is_superuser:
        return redirect("home")
    
    if request.method == "POST":
        form = TripTypeForm(request.POST)
        if form.is_valid():
            trip_type = form.cleaned_data['trip_type']
            num_legs = form.cleaned_data.get('num_legs', 1)
            
            # Store in session for next steps
            request.session['dispatcher_booking'] = {
                'trip_type': trip_type,
                'num_legs': num_legs if trip_type == 'multi_leg' else (2 if trip_type == 'round_trip' else 1),
                'step': 1
            }
            
            return redirect('dispatcher_booking_customer')
    else:
        form = TripTypeForm()
    
    context = {
        'form': form,
        'step': 1,
        'step_title': 'Select Trip Type',
        'step_description': 'Choose the type of trip for this reservation'
    }
    
    return render(request, 'dispatching/booking/step_trip_type.html', context)


@login_required(login_url="login")
def dispatcher_booking_customer(request):
    """
    Step 2: Customer information collection
    """
    if not request.user.is_superuser:
        return redirect("home")
    
    # Check if we have booking session
    booking_data = request.session.get('dispatcher_booking')
    if not booking_data:
        messages.error(request, "Please start the booking process from the beginning.")
        return redirect('dispatcher_booking_start')
    
    if request.method == "POST":
        form = DispatcherCustomerForm(request.POST)
        if form.is_valid():
            customer = form.save()
            
            # Update session with customer ID
            booking_data['customer_id'] = customer.id
            booking_data['step'] = 2
            request.session['dispatcher_booking'] = booking_data
            
            messages.success(request, f"Customer {customer.get_full_name()} saved successfully.")
            return redirect('dispatcher_booking_reservation')
    else:
        form = DispatcherCustomerForm()
    
    context = {
        'form': form,
        'step': 2,
        'step_title': 'Customer Information',
        'step_description': 'Enter customer contact details',
        'booking_data': booking_data
    }
    
    return render(request, 'dispatching/booking/step_customer.html', context)


@login_required(login_url="login")
def dispatcher_booking_reservation(request):
    """
    Step 3: Reservation details (pricing, vehicle, passengers, etc.)
    """
    if not request.user.is_superuser:
        return redirect("home")
    
    booking_data = request.session.get('dispatcher_booking')
    if not booking_data or not booking_data.get('customer_id'):
        messages.error(request, "Please complete the customer information step first.")
        return redirect('dispatcher_booking_customer')
    
    customer = get_object_or_404(Customer, id=booking_data['customer_id'])
    
    if request.method == "POST":
        form = DispatcherReservationForm(request.POST)
        if form.is_valid():
            # Save reservation details to session (don't create reservation yet)
            reservation_data = {}
            for field in form.cleaned_data:
                value = form.cleaned_data[field]
                if hasattr(value, 'id'):  # Handle model instances
                    reservation_data[field] = value.id
                else:
                    reservation_data[field] = str(value) if value is not None else None
            
            booking_data['reservation_data'] = reservation_data
            booking_data['step'] = 3
            request.session['dispatcher_booking'] = booking_data
            
            return redirect('dispatcher_booking_legs')
    else:
        form = DispatcherReservationForm()
    
    context = {
        'form': form,
        'customer': customer,
        'step': 3,
        'step_title': 'Reservation Details',
        'step_description': 'Set pricing, vehicle type, and passenger details',
        'booking_data': booking_data
    }
    
    return render(request, 'dispatching/booking/step_reservation.html', context)


@login_required(login_url="login")
def dispatcher_booking_legs(request):
    """
    Step 4: Trip legs and flight information
    """
    if not request.user.is_superuser:
        return redirect("home")
    
    booking_data = request.session.get('dispatcher_booking')
    if not booking_data or not booking_data.get('reservation_data'):
        messages.error(request, "Please complete the reservation details step first.")
        return redirect('dispatcher_booking_reservation')
    
    customer = get_object_or_404(Customer, id=booking_data['customer_id'])
    num_legs = booking_data.get('num_legs', 1)
    
    if request.method == "POST":
        leg_formset = DispatcherLegFormSet(request.POST, prefix='legs')
        flight_formset = DispatcherFlightFormSet(request.POST, prefix='flights')
        
        if leg_formset.is_valid() and flight_formset.is_valid():
            # Save legs and flights data to session
            legs_data = []
            flights_data = []
            
            for form in leg_formset:
                if form.cleaned_data and not form.cleaned_data.get('DELETE', False):
                    leg_data = {}
                    for field, value in form.cleaned_data.items():
                        if field != 'DELETE':
                            leg_data[field] = str(value) if value is not None else None
                    legs_data.append(leg_data)
            
            for form in flight_formset:
                if form.cleaned_data and not form.cleaned_data.get('DELETE', False):
                    flight_data = {}
                    for field, value in form.cleaned_data.items():
                        if field != 'DELETE':
                            flight_data[field] = str(value) if value is not None else None
                    flights_data.append(flight_data)
            
            booking_data['legs_data'] = legs_data
            booking_data['flights_data'] = flights_data
            booking_data['step'] = 4
            request.session['dispatcher_booking'] = booking_data
            
            return redirect('dispatcher_booking_pricing')
    else:
        # Initialize formsets with the right number of forms
        leg_formset = DispatcherLegFormSet(prefix='legs', initial=[{} for _ in range(num_legs)])
        flight_formset = DispatcherFlightFormSet(prefix='flights', initial=[{} for _ in range(num_legs)])
    
    context = {
        'leg_formset': leg_formset,
        'flight_formset': flight_formset,
        'customer': customer,
        'num_legs': num_legs,
        'step': 4,
        'step_title': 'Trip Details',
        'step_description': f'Enter details for {num_legs} trip leg(s)',
        'booking_data': booking_data
    }
    
    return render(request, 'dispatching/booking/step_legs.html', context)


@login_required(login_url="login")
def dispatcher_booking_pricing(request):
    """
    Step 5: Pricing and final details
    """
    if not request.user.is_superuser:
        return redirect("home")
    
    booking_data = request.session.get('dispatcher_booking')
    if not booking_data or not booking_data.get('legs_data'):
        messages.error(request, "Please complete all previous steps first.")
        return redirect('dispatcher_booking_legs')
    
    customer = get_object_or_404(Customer, id=booking_data['customer_id'])
    
    if request.method == "POST":
        form = DispatcherPricingForm(request.POST)
        
        if form.is_valid():
            # Save pricing data to session
            pricing_data = {
                'manual_base_price': str(form.cleaned_data['manual_base_price']),
                'additional_charges': str(form.cleaned_data.get('additional_charges', Decimal('0.00'))),
                'total_price': str(form.cleaned_data['total_price']),
                'private_notes': form.cleaned_data.get('private_notes', ''),
            }
            
            booking_data['pricing_data'] = pricing_data
            booking_data['step'] = 5
            request.session['dispatcher_booking'] = booking_data
            
            return redirect('dispatcher_booking_review')
    else:
        # Pre-populate with any existing pricing data
        initial_data = booking_data.get('pricing_data', {})
        form = DispatcherPricingForm(initial=initial_data)
    
    # Get reservation data for context
    reservation_data = booking_data.get('reservation_data', {})
    vehicle = None
    if reservation_data.get('manual_vehicle'):
        vehicle = Vehicle.objects.get(id=reservation_data['manual_vehicle'])
    
    context = {
        'form': form,
        'customer': customer,
        'vehicle': vehicle,
        'legs_data': booking_data.get('legs_data', []),
        'flights_data': booking_data.get('flights_data', []),
        'step': 5,
        'step_title': 'Pricing & Notes',
        'step_description': 'Set pricing and add any final notes',
        'booking_data': booking_data
    }
    
    return render(request, 'dispatching/booking/step_pricing.html', context)


@login_required(login_url="login")
def dispatcher_booking_review(request):
    """
    Step 6: Review and confirm reservation
    """
    if not request.user.is_superuser:
        return redirect("home")
    
    booking_data = request.session.get('dispatcher_booking')
    if not booking_data or not booking_data.get('pricing_data'):
        messages.error(request, "Please complete all previous steps first.")
        return redirect('dispatcher_booking_pricing')
    
    customer = get_object_or_404(Customer, id=booking_data['customer_id'])
    
    # Reconstruct data for review
    reservation_data = booking_data.get('reservation_data', {})
    pricing_data = booking_data.get('pricing_data', {})
    legs_data = booking_data.get('legs_data', [])
    flights_data = booking_data.get('flights_data', [])
    
    # Combine legs and flights data for easier template access
    combined_legs = []
    for i, leg_data in enumerate(legs_data):
        combined_leg = leg_data.copy()
        
        # Convert string dates back to date/time objects for template filters
        if combined_leg.get('pickup_date'):
            try:
                from datetime import datetime
                combined_leg['pickup_date'] = datetime.strptime(combined_leg['pickup_date'], '%Y-%m-%d').date()
            except (ValueError, TypeError):
                pass
                
        if combined_leg.get('pickup_time'):
            try:
                from datetime import datetime
                # Handle both HH:MM:SS and HH:MM formats
                try:
                    combined_leg['pickup_time'] = datetime.strptime(combined_leg['pickup_time'], '%H:%M:%S').time()
                except ValueError:
                    combined_leg['pickup_time'] = datetime.strptime(combined_leg['pickup_time'], '%H:%M').time()
            except (ValueError, TypeError):
                pass
        
        if i < len(flights_data) and flights_data[i]:
            combined_leg['flight_info'] = flights_data[i]
        else:
            combined_leg['flight_info'] = None
        combined_legs.append(combined_leg)
    
    # Get vehicle for display
    vehicle = None
    if reservation_data.get('manual_vehicle'):
        vehicle = Vehicle.objects.get(id=reservation_data['manual_vehicle'])
    
    if request.method == "POST":
        if 'confirm' in request.POST:
            try:
                # Create the actual reservation and legs
                reservation = create_dispatcher_reservation(booking_data)
                
                # Clear session data
                del request.session['dispatcher_booking']
                
                messages.success(
                    request, 
                    f"Reservation #{reservation.id} created successfully for {customer.get_full_name()}!"
                )
                return redirect('reservation_details', id=reservation.uuid)
                
            except Exception as e:
                logger.error(f"Error creating dispatcher reservation: {str(e)}")
                messages.error(request, f"Error creating reservation: {str(e)}")
        
        elif 'back' in request.POST:
            return redirect('dispatcher_booking_pricing')
    
    context = {
        'customer': customer,
        'reservation_data': reservation_data,
        'pricing_data': pricing_data,
        'legs_data': combined_legs,  # Use combined legs data
        'flights_data': flights_data,
        'vehicle': vehicle,
        'step': 6,
        'step_title': 'Review & Confirm',
        'step_description': 'Review all details and create the reservation',
        'booking_data': booking_data
    }
    
    return render(request, 'dispatching/booking/step_review.html', context)


def create_dispatcher_reservation(booking_data):
    """
    Helper function to create reservation from session data
    """
    customer = Customer.objects.get(id=booking_data['customer_id'])
    reservation_data = booking_data['reservation_data']
    pricing_data = booking_data['pricing_data']
    legs_data = booking_data['legs_data']
    flights_data = booking_data['flights_data']
    
    # Get vehicle
    vehicle = Vehicle.objects.get(id=reservation_data['manual_vehicle'])
    
    # Try to find an existing rate for this vehicle (for system compatibility)
    rate = Rate.objects.filter(vehicle=vehicle).first()
    
    # Create reservation
    reservation = Reservation.objects.create(
        customer=customer,
        vehicle=vehicle,
        rate=rate,  # May be None, which is OK for dispatcher bookings
        trip_type=booking_data['trip_type'],
        passenger_count=int(reservation_data.get('passenger_count', 1)),
        luggage_count=int(reservation_data.get('luggage_count', 1)),
        store_stop=reservation_data.get('store_stop') == 'True',
        special_requests=reservation_data.get('special_requests', ''),
        need_carseats=reservation_data.get('need_carseats') == 'True',
        rf_carseats=int(reservation_data.get('rf_carseats', 0)),
        ff_carseats=int(reservation_data.get('ff_carseats', 0)),
        booster_seats=int(reservation_data.get('booster_seats', 0)),
        base_price=Decimal(pricing_data.get('manual_base_price', '0')),
        additional_charges=Decimal(pricing_data.get('additional_charges', '0')),
        total_price=Decimal(pricing_data.get('total_price', '0')),
        private_notes=pricing_data.get('private_notes', ''),
        status='confirmed'  # Dispatcher bookings are confirmed by default
    )
    
    # Create legs
    for i, leg_data in enumerate(legs_data):
        # Create flight if provided
        flight = None
        if i < len(flights_data) and flights_data[i]:
            flight_info = flights_data[i]
            if flight_info.get('airline') or flight_info.get('flight_number'):
                flight = Flight.objects.create(
                    airline=flight_info.get('airline', ''),
                    flight_number=flight_info.get('flight_number', ''),
                    flight_type=flight_info.get('flight_type', '')
                )
        
        # Create leg
        from datetime import datetime, time
        pickup_date = datetime.strptime(leg_data['pickup_date'], '%Y-%m-%d').date() if leg_data.get('pickup_date') else None
        pickup_time_str = leg_data.get('pickup_time')
        pickup_time = None
        if pickup_time_str:
            try:
                pickup_time = datetime.strptime(pickup_time_str, '%H:%M:%S').time()
            except ValueError:
                try:
                    pickup_time = datetime.strptime(pickup_time_str, '%H:%M').time()
                except ValueError:
                    pickup_time = None
        
        leg = Leg.objects.create(
            reservation=reservation,
            flight_information=flight,
            pickup_date=pickup_date,
            pickup_time=pickup_time,
            pickup_location=leg_data.get('pickup_location', ''),
            dropoff_location=leg_data.get('dropoff_location', ''),
            private_notes=leg_data.get('private_notes', ''),
            driver_pay_amount=Decimal(leg_data.get('driver_pay_amount', '0')) if leg_data.get('driver_pay_amount') else None
        )
    
    return reservation


@login_required(login_url="login")
def dispatcher_booking_cancel(request):
    """
    Cancel dispatcher booking and clear session
    """
    if not request.user.is_superuser:
        return redirect("home")
    
    if 'dispatcher_booking' in request.session:
        del request.session['dispatcher_booking']
    
    messages.info(request, "Booking process cancelled.")
    return redirect('dashboard')


@login_required
def customer_search_api(request):
    """
    AJAX endpoint to search for existing customers
    """
    if not request.user.is_superuser:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    
    query = request.GET.get('q', '').strip()
    
    if not query or len(query) < 2:
        return JsonResponse({"success": False, "error": "Query too short"})
    
    # Clean query for phone number search (remove common formatting)
    phone_query = ''.join(filter(str.isdigit, query))
    
    # Search customers by multiple fields
    search_conditions = Q(first_name__icontains=query) | Q(last_name__icontains=query) | Q(email__icontains=query)
    
    # Add phone number search with different formats
    if phone_query:
        search_conditions |= (
            Q(phone_number__icontains=query) |  # Original query
            Q(phone_number__icontains=phone_query)  # Digits only
        )
    
    customers = Customer.objects.filter(search_conditions).order_by('-created_at')[:10]  # Limit to 10 results
    
    results = []
    for customer in customers:
        results.append({
            'id': customer.id,
            'first_name': customer.first_name,
            'last_name': customer.last_name,
            'email': customer.email,
            'phone_number': customer.phone_number,
            'zipcode': customer.zipcode,
            'full_name': customer.get_full_name(),
            'reservation_count': customer.reservation_count,
            'is_returning': customer.is_returning,
        })
    
    return JsonResponse({
        "success": True,
        "customers": results,
        "count": len(results)
    })
