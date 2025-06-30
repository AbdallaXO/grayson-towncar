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
import logging
import json
from datetime import datetime, timedelta
from django.views.generic import ListView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.template.loader import render_to_string
from django.db.models import Prefetch
from django.db.models import OuterRef, Subquery

# App imports

from reservations.models import Reservation, Leg, Customer
from reservations.forms import ReservationAdminForm, CustomerForm, LegForm
from drivers.models import Driver
from payment.utils import get_or_create_stripe_customer
from payment.models import Payment
from rates.models import Vehicle

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
    try:
        selected_date = (
            datetime.strptime(selected_date, "%Y-%m-%d").date()
            if selected_date
            else timezone.localdate()
        )
    except ValueError:
        selected_date = timezone.localdate()

    # Get all legs for the selected date
    legs = (
        Leg.objects.filter(pickup_date=selected_date)
        .select_related(
            "reservation",
            "reservation__customer",
            "reservation__vehicle",
            "driver",
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
            Reservation.objects.select_related("customer", "vehicle", "rate")
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
            queryset = queryset.filter(status=status_filter)

        # Add is_first_leg property to each leg
        for reservation in queryset:
            for leg in reservation.legs.all():
                first_leg = reservation.legs.order_by("pickup_time").first()
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
            total_revenue=Sum("total_price", filter=Q(payments__status="paid")),
        )

        # Add statistics to context
        context.update(
            {
                "total_reservations": stats["total_count"],
                "pending_reservations": stats["pending_count"],
                "confirmed_reservations": stats["confirmed_count"],
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
    today = timezone.localdate()

    # Subquery to get the latest payment ID for each reservation
    latest_payment_subquery = (
        Payment.objects.filter(reservation_id=OuterRef("reservation_id"))
        .order_by("-id")
        .values("id")[:1]
    )

    # Base queryset with all necessary related fields
    legs_query = Leg.objects.select_related(
        "reservation",
        "reservation__customer",
        "reservation__vehicle",
        "driver",
        "flight_information",
    ).annotate(latest_payment_id=Subquery(latest_payment_subquery))

    # Apply date filters
    if date_from and date_to:
        try:
            from_date = datetime.strptime(date_from, "%Y-%m-%d").date()
            to_date = datetime.strptime(date_to, "%Y-%m-%d").date()
            legs_query = legs_query.filter(pickup_date__range=[from_date, to_date])
        except ValueError:
            pass
    elif time_filter == "week":
        # This week
        start_date = today - timedelta(days=today.weekday())
        end_date = start_date + timedelta(days=6)
        legs_query = legs_query.filter(pickup_date__range=[start_date, end_date])
    elif time_filter == "next_week":
        # Next week
        start_date = today + timedelta(days=(7 - today.weekday()))
        end_date = start_date + timedelta(days=6)
        legs_query = legs_query.filter(pickup_date__range=[start_date, end_date])
    elif date_filter:
        try:
            filter_date = datetime.strptime(date_filter, "%Y-%m-%d").date()
            legs_query = legs_query.filter(pickup_date=filter_date)
        except ValueError:
            pass
    else:
        # Default: show all future legs
        legs_query = legs_query.filter(pickup_date__gte=today)

    # Apply status filter
    if status_filter:
        legs_query = legs_query.filter(status=status_filter)

    # Apply vehicle filter
    if vehicle_filter:
        legs_query = legs_query.filter(
            reservation__vehicle__vehicle_type=vehicle_filter
        )

    # Apply trip type filter (arrival/return/other)
    if trip_type_filter:
        # We'll filter this in Python since it's a computed property
        pass  # Will be handled after queryset evaluation

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

    # Get all latest payments in one query
    latest_payment_ids = [
        leg.latest_payment_id for leg in page_obj if leg.latest_payment_id
    ]
    if latest_payment_ids:
        latest_payments = Payment.objects.in_bulk(latest_payment_ids)
        for leg in page_obj:
            if leg.latest_payment_id:
                leg.reservation.latest_payment = latest_payments.get(
                    leg.latest_payment_id
                )
            else:
                leg.reservation.latest_payment = None
    else:
        for leg in page_obj:
            leg.reservation.latest_payment = None

    # Apply trip type filter if specified (filter in Python since it's a computed property)
    if trip_type_filter:
        filtered_legs = []
        for leg in page_obj:
            if leg.get_trip_type() == trip_type_filter:
                filtered_legs.append(leg)
        page_obj.object_list = filtered_legs
        page_obj._object_list = filtered_legs

    # Calculate trip type statistics
    trip_type_stats = {"arrival": 0, "return": 0, "other": 0}

    # Calculate vehicle statistics
    vehicle_stats = {}

    # Count trip types and vehicles for all legs (not just current page)
    all_legs = legs_query.order_by("pickup_date", "pickup_time")
    for leg in all_legs:
        # Count trip types
        trip_type = leg.get_trip_type()
        trip_type_stats[trip_type] += 1

        # Count vehicles with detailed statistics
        if leg.reservation.vehicle:
            vehicle_type = leg.reservation.vehicle.vehicle_type
            vehicle_name = leg.reservation.vehicle.get_vehicle_type_display()
            
            if vehicle_type not in vehicle_stats:
                vehicle_stats[vehicle_type] = {
                    'name': vehicle_name,
                    'total': 0,
                    'arrivals': 0,
                    'returns': 0,
                    'other': 0
                }
            
            vehicle_stats[vehicle_type]['total'] += 1
            
            # Count by trip type
            if trip_type == 'arrival':
                vehicle_stats[vehicle_type]['arrivals'] += 1
            elif trip_type == 'return':
                vehicle_stats[vehicle_type]['returns'] += 1
            else:
                vehicle_stats[vehicle_type]['other'] += 1

    # Calculate current page statistics
    current_page_stats = {
        "arrival": 0,
        "return": 0,
        "other": 0,
        "total_revenue": 0
    }
    
    for leg in page_obj:
        # Count trip types for current page
        trip_type = leg.get_trip_type()
        current_page_stats[trip_type] += 1
        
        # Sum revenue for current page using leg's revenue share
        if leg.revenue_share:
            current_page_stats["total_revenue"] += leg.revenue_share
        else:
            # Fallback: divide reservation total by number of legs
            leg_count = leg.reservation.legs.count()
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
        "trip_type_stats": trip_type_stats,  # Add statistics
        "vehicle_stats": vehicle_stats,  # Add vehicle statistics
        "current_page_stats": current_page_stats,  # Add current page statistics
        "drivers": drivers,
        "vehicles": vehicles,  # Add vehicles to context
        "today_count": today_count,
        "page_obj": page_obj,
    }

    return render(request, "dispatching/legs_list.html", context)


@login_required(login_url="login")
def vehicle_statistics_modal(request):
    """
    AJAX view to get detailed vehicle statistics for modal display.
    """
    if not request.user.is_superuser:
        return JsonResponse({"error": "Permission denied"}, status=403)
    
    # Get filter parameters (same as legs_list)
    date_filter = request.GET.get("date")
    date_from = request.GET.get("date_from")
    date_to = request.GET.get("date_to")
    status_filter = request.GET.get("status")
    time_filter = request.GET.get("time_filter", "all")
    today = timezone.localdate()
    
    # Base queryset (same as legs_list)
    latest_payment_subquery = (
        Payment.objects.filter(reservation_id=OuterRef("reservation_id"))
        .order_by("-id")
        .values("id")[:1]
    )
    
    legs_query = Leg.objects.select_related(
        "reservation",
        "reservation__customer", 
        "reservation__vehicle",
        "driver",
        "flight_information",
    ).annotate(latest_payment_id=Subquery(latest_payment_subquery))
    
    # Apply same filters as legs_list
    if date_from and date_to:
        try:
            from_date = datetime.strptime(date_from, "%Y-%m-%d").date()
            to_date = datetime.strptime(date_to, "%Y-%m-%d").date()
            legs_query = legs_query.filter(pickup_date__range=[from_date, to_date])
        except ValueError:
            pass
    elif time_filter == "week":
        start_date = today - timedelta(days=today.weekday())
        end_date = start_date + timedelta(days=6)
        legs_query = legs_query.filter(pickup_date__range=[start_date, end_date])
    elif time_filter == "next_week":
        start_date = today + timedelta(days=(7 - today.weekday()))
        end_date = start_date + timedelta(days=6)
        legs_query = legs_query.filter(pickup_date__range=[start_date, end_date])
    elif date_filter:
        try:
            filter_date = datetime.strptime(date_filter, "%Y-%m-%d").date()
            legs_query = legs_query.filter(pickup_date=filter_date)
        except ValueError:
            pass
    else:
        legs_query = legs_query.filter(pickup_date__gte=today)
    
    if status_filter:
        legs_query = legs_query.filter(status=status_filter)
    
    # Get all legs for statistics
    all_legs = list(legs_query.order_by("pickup_date", "pickup_time"))
    
    # Calculate detailed vehicle statistics
    vehicle_stats = {}
    
    for leg in all_legs:
        if leg.reservation.vehicle:
            vehicle_type = leg.reservation.vehicle.vehicle_type
            vehicle_name = leg.reservation.vehicle.get_vehicle_type_display()
            trip_type = leg.get_trip_type()
            
            if vehicle_type not in vehicle_stats:
                vehicle_stats[vehicle_type] = {
                    'name': vehicle_name,
                    'total': 0,
                    'arrivals': 0,
                    'returns': 0,
                    'other': 0,
                    'completed': 0,
                    'in_progress': 0,
                    'picked_up': 0,
                    'on_location': 0
                }
            
            vehicle_stats[vehicle_type]['total'] += 1
            # Fix the key mapping to match the dictionary keys
            if trip_type == 'return':
                vehicle_stats[vehicle_type]['returns'] += 1
            elif trip_type == 'arrival':
                vehicle_stats[vehicle_type]['arrivals'] += 1
            else:
                vehicle_stats[vehicle_type][trip_type] += 1
            
            # Count by status
            if leg.status:
                if leg.status == 'completed':
                    vehicle_stats[vehicle_type]['completed'] += 1
                elif leg.status == 'in-progress':
                    vehicle_stats[vehicle_type]['in_progress'] += 1
                elif leg.status == 'picked-up':
                    vehicle_stats[vehicle_type]['picked_up'] += 1
                elif leg.status == 'on-location':
                    vehicle_stats[vehicle_type]['on_location'] += 1
    
    # Convert to list for template
    vehicle_stats_list = []
    for vehicle_type, stats in vehicle_stats.items():
        stats['vehicle_type'] = vehicle_type
        vehicle_stats_list.append(stats)
    
    # Sort by total rides (descending)
    vehicle_stats_list.sort(key=lambda x: x['total'], reverse=True)
    
    context = {
        'vehicle_stats': vehicle_stats_list,
        'total_legs': len(all_legs)
    }
    
    html = render_to_string('dispatching/includes/vehicle_statistics_modal.html', context, request=request)
    return JsonResponse({'html': html})


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
                    "on-location",
                    "picked-up",
                    "completed",
                ]
                if value in valid_statuses:
                    leg.status = value
                    leg.save()
                    logger.info(f"Updated leg {leg_id} status to {value}")
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
    Updates the private notes for a reservation.

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

        if not reservation_id:
            return JsonResponse(
                {"success": False, "error": "Missing reservation ID"}, status=400
            )

        # Get the reservation
        reservation = get_object_or_404(Reservation, uuid=reservation_id)

        # Update private notes
        reservation.private_notes = private_notes
        reservation.save(update_fields=["private_notes"])

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
        except Exception as e:
            logger.error(f"Error fetching payment methods: {e}")

    if request.method == "POST":
        action = request.POST.get("action")
        amount_str = request.POST.get("amount")
        description = request.POST.get(
            "description", f"Payment for Reservation #{reservation.id}"
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
    today = timezone.localdate()
    
    # Base queryset
    latest_payment_subquery = (
        Payment.objects.filter(reservation_id=OuterRef("reservation_id"))
        .order_by("-id")
        .values("id")[:1]
    )
    
    legs_query = Leg.objects.select_related(
        "reservation",
        "reservation__customer", 
        "reservation__vehicle",
        "driver",
        "flight_information",
    ).annotate(latest_payment_id=Subquery(latest_payment_subquery))
    
    # Apply filters
    if date_from and date_to:
        try:
            from_date = datetime.strptime(date_from, "%Y-%m-%d").date()
            to_date = datetime.strptime(date_to, "%Y-%m-%d").date()
            legs_query = legs_query.filter(pickup_date__range=[from_date, to_date])
        except ValueError:
            pass
    elif time_filter == "week":
        start_date = today - timedelta(days=today.weekday())
        end_date = start_date + timedelta(days=6)
        legs_query = legs_query.filter(pickup_date__range=[start_date, end_date])
    elif time_filter == "next_week":
        start_date = today + timedelta(days=(7 - today.weekday()))
        end_date = start_date + timedelta(days=6)
        legs_query = legs_query.filter(pickup_date__range=[start_date, end_date])
    elif date_filter:
        try:
            filter_date = datetime.strptime(date_filter, "%Y-%m-%d").date()
            legs_query = legs_query.filter(pickup_date=filter_date)
        except ValueError:
            pass
    else:
        legs_query = legs_query.filter(pickup_date__gte=today)
    
    if status_filter:
        legs_query = legs_query.filter(status=status_filter)
    
    # Get all legs for statistics
    all_legs = list(legs_query.order_by("pickup_date", "pickup_time"))
    
    # Calculate comprehensive statistics
    vehicle_stats = {}
    trip_type_stats = {"arrival": 0, "return": 0, "other": 0}
    status_stats = {"completed": 0, "in-progress": 0, "picked-up": 0, "on-location": 0}
    total_revenue = Decimal("0.00")
    
    for leg in all_legs:
        # Trip type statistics
        trip_type = leg.get_trip_type()
        trip_type_stats[trip_type] += 1
        
        # Status statistics
        if leg.status:
            status_stats[leg.status] += 1
        
        # Revenue calculation
        if leg.reservation:
            total_revenue += leg.reservation.total_price
        
        # Vehicle statistics
        if leg.reservation.vehicle:
            vehicle_type = leg.reservation.vehicle.vehicle_type
            vehicle_name = leg.reservation.vehicle.get_vehicle_type_display()
            
            if vehicle_type not in vehicle_stats:
                vehicle_stats[vehicle_type] = {
                    'name': vehicle_name,
                    'total': 0,
                    'arrivals': 0,
                    'returns': 0,
                    'other': 0,
                    'completed': 0,
                    'in_progress': 0,
                    'picked_up': 0,
                    'on_location': 0,
                    'revenue': Decimal("0.00")
                }
            
            vehicle_stats[vehicle_type]['total'] += 1
            
            # Trip type counts
            if trip_type == 'return':
                vehicle_stats[vehicle_type]['returns'] += 1
            elif trip_type == 'arrival':
                vehicle_stats[vehicle_type]['arrivals'] += 1
            else:
                vehicle_stats[vehicle_type]['other'] += 1
            
            # Status counts
            if leg.status:
                if leg.status == 'completed':
                    vehicle_stats[vehicle_type]['completed'] += 1
                elif leg.status == 'in-progress':
                    vehicle_stats[vehicle_type]['in_progress'] += 1
                elif leg.status == 'picked-up':
                    vehicle_stats[vehicle_type]['picked_up'] += 1
                elif leg.status == 'on-location':
                    vehicle_stats[vehicle_type]['on_location'] += 1
            
            # Revenue
            if leg.reservation:
                vehicle_stats[vehicle_type]['revenue'] += leg.reservation.total_price
    
    # Convert to lists and sort
    vehicle_stats_list = []
    for vehicle_type, stats in vehicle_stats.items():
        stats['vehicle_type'] = vehicle_type
        vehicle_stats_list.append(stats)
    vehicle_stats_list.sort(key=lambda x: x['total'], reverse=True)
    
    context = {
        'vehicle_stats': vehicle_stats_list,
        'trip_type_stats': trip_type_stats,
        'status_stats': status_stats,
        'total_legs': len(all_legs),
        'total_revenue': total_revenue,
        'filter_date': date_filter,
        'date_from': date_from,
        'date_to': date_to,
        'status_filter': status_filter,
        'time_filter': time_filter,
    }
    
    return render(request, "dispatching/statistics.html", context)
