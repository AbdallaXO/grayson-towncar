from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.contrib import messages
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.core.cache import cache
from django.db.models import Sum, Q, Count, Prefetch
from django.utils import timezone
from django.views.decorators.http import require_POST, require_http_methods
from django import forms
from decimal import Decimal
from django.db import transaction
import stripe
import stripe.error
import logging
import json
import threading
import uuid
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from django.views.generic import ListView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.template.loader import render_to_string
from django.db.models import Prefetch
from django.db.models import OuterRef, Subquery

# App imports

from reservations.models import Reservation, Leg, Customer, Flight
from payment.models import Payment
from reservations.forms import ReservationAdminForm, CustomerForm, LegForm
from drivers.models import (
    Driver,
    DriverPayment,
    LegPayment,
    DriverVehicleAssignment,
    FleetVehicle,
)
from payment.utils import get_or_create_stripe_customer
from rates.models import Vehicle, Rate
from users.emails import send_reservation_confirmation
from reservations.conversions import send_purchase_event
from payment.webhook import save_card_to_customer
from .utils import get_comprehensive_statistics, get_filtered_legs_queryset, calculate_vehicle_statistics
from .aeroapi_service import AeroAPIService
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


# Permission helpers
def can_view_revenue(user):
    """Check if user can view revenue information (admins only)"""
    return user.is_superuser


def can_view_statistics(user):
    """Check if user can view statistics page (admins only)"""
    return user.is_superuser


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
    if not request.user.is_staff:
        return redirect("home")

    selected_date = request.GET.get("date")
    driver_filter = request.GET.get("driver")
    trip_type_filter = request.GET.get("trip_type")
    
    try:
        selected_date = (
            datetime.strptime(selected_date, "%Y-%m-%d").date()
            if selected_date
            else timezone.localdate()
        )
    except ValueError:
        selected_date = timezone.localdate()

    # Get all legs for the selected date, excluding refunded reservations
    legs_query = Leg.objects.filter(pickup_date=selected_date).exclude(reservation__refund_status='completed')
    
    # Apply driver filter if specified
    if driver_filter:
        if driver_filter == "unassigned":
            legs_query = legs_query.filter(driver__isnull=True)
        else:
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
            "driver__profile",
            "flight_information",
            "cruise_information",
        )
        .prefetch_related(
            "reservation__legs",
            Prefetch("reservation__payments", queryset=Payment.objects.order_by('-created_at')),
        )
        .order_by("pickup_time")
    )
    
    # Apply trip type filter if specified (filter in Python since it's a computed property)
    if trip_type_filter:
        filtered_legs = []
        for leg in legs:
            if leg.get_trip_type() == trip_type_filter:
                filtered_legs.append(leg)
        legs = filtered_legs

    # Get all drivers for assignment dropdown
    drivers = list(Driver.objects.select_related("profile").all())

    # Inhouse vehicle assignments for the selected date
    inhouse_drivers = (
        Driver.objects.filter(driver_type="inhouse")
        .select_related("profile")
        .order_by("profile__first_name", "profile__last_name", "profile__username")
    )
    inhouse_assignments = DriverVehicleAssignment.objects.filter(
        date=selected_date, driver__in=inhouse_drivers
    ).select_related("driver", "driver__profile", "vehicle")
    assignment_map = {
        assignment.driver_id: assignment for assignment in inhouse_assignments
    }
    inhouse_driver_rows = [
        {"driver": driver, "assignment": assignment_map.get(driver.id)}
        for driver in inhouse_drivers
    ]
    def _inhouse_vehicle_sort_key(row):
        assignment = row.get("assignment")
        vehicle_number = None
        if assignment and assignment.vehicle and assignment.vehicle.vehicle_number:
            vehicle_number = assignment.vehicle.vehicle_number.lstrip("#").strip()
        if vehicle_number:
            try:
                vehicle_number = int(vehicle_number)
            except ValueError:
                pass
            return (0, vehicle_number)
        driver = row["driver"]
        return (1, str(driver))

    inhouse_driver_rows.sort(key=_inhouse_vehicle_sort_key)


    for driver in drivers:
        display_name = str(driver)
        if driver.driver_type == "inhouse":
            assignment = assignment_map.get(driver.id)
            if assignment and assignment.vehicle and assignment.vehicle.vehicle_number:
                vehicle_number = assignment.vehicle.vehicle_number
                vehicle_number = vehicle_number.lstrip("#").strip()
                display_name = f"{display_name} - #{vehicle_number}"
        driver.dashboard_display_name = display_name

    # Calculate total revenue from legs on this day (only for admins)
    if can_view_revenue(request.user):
        total_revenue = sum(leg.reservation.total_price for leg in legs)
    else:
        total_revenue = None

    context = {
        "legs": legs,
        "selected_date": selected_date,
        "driver_filter": driver_filter,
        "trip_type_filter": trip_type_filter,
        "total_legs": len(legs),
        "total_revenue": total_revenue,
        "can_view_revenue": can_view_revenue(request.user),
        "drivers": drivers,
        "inhouse_driver_rows": inhouse_driver_rows,
        "inhouse_vehicles": FleetVehicle.objects.order_by("vehicle_number"),
    }

    return render(request, "dispatching/legs_filter.html", context)


class ReservationListView(LoginRequiredMixin, UserPassesTestMixin, ListView):
    model = Reservation
    template_name = "dispatching/all_reservations.html"
    context_object_name = "reservations"
    paginate_by = 10

    def test_func(self):
        return self.request.user.is_staff

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

        # Exclude refunded reservations from default view (they're cancelled and archived)
        # BUT if filtering by 'cancelled' or 'pending', show all including refunded ones
        if status_filter not in ['cancelled', 'pending']:
            queryset = queryset.exclude(refund_status='completed')

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
        )
        
        # Only calculate revenue for admins
        if can_view_revenue(self.request.user):
            revenue_stats = queryset.aggregate(
                total_revenue=Sum("total_price", filter=Q(payments__status="paid")),
            )
            total_revenue = revenue_stats["total_revenue"] or 0
        else:
            total_revenue = None

        # Add statistics to context
        context.update(
            {
                "total_reservations": stats["total_count"],
                "pending_reservations": stats["pending_count"],
                "confirmed_reservations": stats["confirmed_count"],
                "need_payment_count": stats["need_payment_count"],
                "total_revenue": total_revenue,
                "can_view_revenue": can_view_revenue(self.request.user),
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
    if not request.user.is_staff:
        return redirect("home")

    # Get the reservation with all related data
    reservation = get_object_or_404(
        Reservation.objects.prefetch_related(
            "legs__flight_information", 
            "legs__driver",
            "legs__driver_assigned_by",
            "legs__status_changed_by",
            Prefetch("payments", queryset=Payment.objects.order_by('-created_at'))
        ).select_related(
            "customer", 
            "vehicle", 
            "rate",
            "created_by",
            "modified_by"
        ),
        uuid=id,
    )

    # Get all drivers for assignment dropdown
    drivers = Driver.objects.all()

    # Calculate payment details - always use the LATEST payment (most recent)
    payments = reservation.payments.all().order_by('-created_at')
    latest_payment = payments.first() if payments.exists() else None
    payment_status = "Paid" if latest_payment and latest_payment.status == "paid" else "Unpaid"
    payment_method = (
        latest_payment.payment_type.title() if latest_payment else "N/A"
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
        "latest_payment": latest_payment,  # Pass latest payment to template
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
    if not request.user.is_staff:
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
            # Track who modified the reservation and when
            updated_reservation.modified_by = request.user
            updated_reservation.last_modified_at = timezone.now()
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
    if not request.user.is_staff:
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
    }
    
    # Only calculate revenue for admins
    if can_view_revenue(request.user):
        current_page_stats["total_revenue"] = 0
        
        # Pre-calculate leg counts for each reservation to avoid N+1 queries
        reservation_leg_counts = {}
        for leg in page_obj:
            reservation_id = leg.reservation.id
            if reservation_id not in reservation_leg_counts:
                # Use prefetched legs if available, otherwise fall back to query
                if hasattr(leg.reservation, '_prefetched_objects_cache') and 'legs' in leg.reservation._prefetched_objects_cache:
                    reservation_leg_counts[reservation_id] = len(leg.reservation._prefetched_objects_cache['legs'])
                else:
                    reservation_leg_counts[reservation_id] = len(leg.reservation.legs.all())
        
        for leg in page_obj:
            # Sum revenue for current page using leg's revenue share
            if leg.revenue_share:
                current_page_stats["total_revenue"] += leg.revenue_share
            else:
                # Use pre-calculated leg count
                leg_count = reservation_leg_counts.get(leg.reservation.id, 1)
                if leg_count > 0:
                    current_page_stats["total_revenue"] += leg.reservation.total_price / leg_count
    else:
        current_page_stats["total_revenue"] = None
    
    # Count trip types for current page (always calculate)
    for leg in page_obj:
        trip_type = leg.get_trip_type()
        current_page_stats[trip_type] += 1

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
        "can_view_revenue": can_view_revenue(request.user),
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

    if not request.user.is_staff:
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
        
        # Prevent driver assignment on refunded reservations
        if field == "driver" and leg.reservation.refund_status == 'completed':
            logger.warning(f"Attempted to assign driver to refunded reservation {leg.reservation.id}")
            return JsonResponse({
                "success": False,
                "error": "Cannot assign driver to a refunded reservation"
            }, status=400)

        if field == "driver":
            if value:
                try:
                    driver = Driver.objects.get(id=value)
                    logger.info(f"Found driver with ID {value}")
                    leg.driver = driver
                    # Track who assigned the driver and when
                    leg.driver_assigned_by = request.user
                    leg.driver_assigned_at = timezone.now()
                    leg.save()
                    logger.info(
                        f"Updated leg {leg_id} with driver {driver.profile.username if hasattr(driver, 'profile') else driver.id} by {request.user.username}"
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
                # Track who unassigned the driver
                leg.driver_assigned_by = request.user
                leg.driver_assigned_at = timezone.now()
                leg.save()
                logger.info(f"Removed driver from leg {leg_id} by {request.user.username}")
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
                    # Track who changed the status and when
                    leg.status_changed_by = request.user
                    leg.status_changed_at = timezone.now()
                    leg.save()
                    logger.info(f"Updated leg {leg_id} status to {value} by {request.user.username}")
                    
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
def update_inhouse_vehicle_assignment(request):
    """
    Update or clear an inhouse driver's vehicle assignment for a specific date.
    """
    if not request.user.is_staff:
        return JsonResponse(
            {"success": False, "error": "Permission denied"}, status=403
        )

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError as e:
        return JsonResponse(
            {"success": False, "error": f"Invalid JSON: {str(e)}"}, status=400
        )

    driver_id = data.get("driver_id")
    date_str = data.get("date")
    vehicle_id = data.get("vehicle_id")

    if not driver_id or not date_str:
        return JsonResponse(
            {"success": False, "error": "Missing required data"}, status=400
        )

    try:
        assignment_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return JsonResponse(
            {"success": False, "error": "Invalid date format"}, status=400
        )

    try:
        driver = Driver.objects.get(id=driver_id)
    except Driver.DoesNotExist:
        return JsonResponse(
            {"success": False, "error": "Driver not found"}, status=404
        )

    if driver.driver_type != "inhouse":
        return JsonResponse(
            {"success": False, "error": "Driver is not inhouse"}, status=400
        )

    if not vehicle_id:
        DriverVehicleAssignment.objects.filter(
            driver=driver, date=assignment_date
        ).delete()
        return JsonResponse({"success": True, "cleared": True})

    try:
        vehicle = FleetVehicle.objects.get(id=vehicle_id)
    except FleetVehicle.DoesNotExist:
        return JsonResponse(
            {"success": False, "error": "Vehicle not found"}, status=404
        )

    assignment, _ = DriverVehicleAssignment.objects.get_or_create(
        driver=driver, date=assignment_date
    )
    assignment.vehicle = vehicle
    assignment.save()

    return JsonResponse(
        {"success": True, "vehicle_id": assignment.vehicle_id}
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
    if not request.user.is_staff:
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
            # Track who modified the reservation
            reservation.modified_by = request.user
            reservation.last_modified_at = timezone.now()
            update_fields.extend(["modified_by", "last_modified_at"])
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
            # Retrieve ALL payment methods (card, link, etc.) - not just cards
            payment_methods = stripe.PaymentMethod.list(
                customer=customer.stripe_customer_id
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

                # Prepare statement descriptor (appears on customer's bank statement)
                # Stripe limits this to 22 characters, so truncate if needed
                statement_desc = description[:22] if len(description) > 22 else description

                checkout_session_params = {
                    "customer": stripe_customer_id,
                    "line_items": [{"price": price.id, "quantity": 1}],
                    "mode": "payment",
                    "success_url": success_url_with_context,
                    "cancel_url": cancel_url_with_context,
                    "payment_intent_data": {
                        "setup_future_usage": "off_session",  # Allow saving the card for future use
                        "description": description,  # Shows in Stripe dashboard and receipts
                        "statement_descriptor_suffix": statement_desc,  # Shows on customer's bank statement
                        "metadata": {
                            "payment_description": description,
                        },
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

            elif action == "use_saved_card":
                # Validate amount
                if not amount_str:
                    messages.error(request, "Amount is required for processing payment.")
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

                # Validate payment method selection
                if not selected_payment_method:
                    messages.error(request, "Please select a saved payment method.")
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

                # Verify the payment method belongs to this customer
                try:
                    payment_method = stripe.PaymentMethod.retrieve(selected_payment_method)
                    if payment_method.customer != stripe_customer_id:
                        messages.error(
                            request,
                            "Selected payment method does not belong to this customer."
                        )
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
                except stripe.error.StripeError as e:
                    logger.error(f"Error retrieving payment method: {e}")
                    messages.error(request, f"Error validating payment method: {str(e)}")
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

                # Create PaymentIntent with saved card
                try:
                    # Prepare statement descriptor (appears on customer's bank statement)
                    # Stripe limits this to 22 characters, so truncate if needed
                    statement_desc = description[:22] if len(description) > 22 else description

                    payment_intent = stripe.PaymentIntent.create(
                        amount=amount_in_cents,
                        currency="usd",
                        customer=stripe_customer_id,
                        payment_method=selected_payment_method,
                        off_session=True,  # Important for using saved card
                        confirm=True,  # Confirm immediately
                        description=description,  # Shows in Stripe dashboard and receipts
                        statement_descriptor_suffix=statement_desc,  # Shows on customer's bank statement
                        metadata={
                            "reservation_uuid": str(reservation.uuid),
                            "reservation_id": reservation.id,
                            "customer_id": reservation.customer.id,
                            "initiated_by": "dispatcher",
                            "dispatcher_action": action,
                            "payment_amount_cents": amount_in_cents,
                            "payment_description": description,
                        },
                    )

                    # Handle payment result
                    if payment_intent.status == "succeeded":
                        # Payment successful - create Payment record and update reservation
                        final_amount = Decimal(payment_intent.amount) / 100

                        # Calculate amount owed BEFORE this payment
                        amount_owed_before = reservation.amount_owed

                        # Save card details to customer if card payment
                        if payment_intent.payment_method:
                            try:
                                pm = stripe.PaymentMethod.retrieve(
                                    payment_intent.payment_method
                                )
                                if pm.type == "card":
                                    save_card_to_customer(
                                        stripe_customer_id, payment_intent.payment_method
                                    )
                            except Exception as e:
                                logger.warning(
                                    f"Could not save card details: {e}, continuing with payment"
                                )

                        # Create Payment record
                        payment, created = Payment.objects.get_or_create(
                            reservation=reservation,
                            customer=customer,
                            stripe_payment_intent_id=payment_intent.id,
                            defaults={
                                "amount": final_amount,
                                "description": description,
                                "payment_type": "pay_now",
                                "status": "paid",
                                "stripe_customer_id": stripe_customer_id,
                                "stripe_payment_method_id": payment_intent.payment_method,
                            },
                        )

                        if not created:
                            # Update existing payment
                            payment.amount = final_amount
                            payment.description = description
                            payment.status = "paid"
                            payment.stripe_payment_method_id = payment_intent.payment_method
                            payment.save()

                        # Automatic total_price adjustment logic:
                        # If amount owed was $0 (or nearly $0), this is a NEW charge, so add to total_price
                        # Otherwise, this is a payment toward existing balance, don't add
                        should_add_to_total = amount_owed_before <= Decimal("0.01")

                        if should_add_to_total:
                            reservation.total_price += final_amount
                            logger.info(
                                f"Auto-added ${final_amount} to reservation total (was ${reservation.total_price - final_amount}, "
                                f"now ${reservation.total_price}) - detected as new charge"
                            )

                        # Update reservation status
                        reservation.status = "confirmed"

                        with transaction.atomic():
                            if should_add_to_total:
                                reservation.save(update_fields=["status", "total_price"])
                            else:
                                reservation.save(update_fields=["status"])
                            payment.save()

                        # Send confirmation email after successful payment
                        try:
                            send_reservation_confirmation(reservation)
                            logger.info(f"Confirmation email sent for dispatcher payment on reservation {reservation.uuid}")
                        except Exception as e:
                            logger.error(f"Error sending confirmation email for dispatcher payment on reservation {reservation.uuid}: {e}")
                            # Don't fail payment processing if email fails

                        # Send purchase event to Meta - use None to default to reservation.total_price (matches Google Analytics)
                        # Generate event_id from payment intent for deduplication
                        try:
                            import time
                            event_id = f"{payment_intent_id}_{int(time.time())}" if payment_intent_id else None
                            send_purchase_event(reservation, value=None, event_id=event_id)
                        except Exception as e:
                            logger.warning(f"Error sending purchase event: {e}")

                        messages.success(
                            request,
                            f"Payment of ${final_amount:.2f} processed successfully using saved card."
                        )
                        logger.info(
                            f"Payment processed successfully for reservation {reservation.uuid} using saved card"
                        )
                        return redirect("reservation_details", id=reservation.uuid)

                    elif payment_intent.status == "requires_action":
                        # 3D Secure authentication required
                        # For off_session payments, we need to handle this differently
                        # Redirect to a page where customer can complete authentication
                        messages.warning(
                            request,
                            "This payment requires additional authentication. Please use 'Make a Payment' option to complete."
                        )
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

                    else:
                        # Payment failed or requires attention
                        error_message = (
                            payment_intent.last_payment_error.message
                            if payment_intent.last_payment_error
                            else f"Payment status: {payment_intent.status}"
                        )
                        messages.error(request, f"Payment failed: {error_message}")

                        # Create failed payment record
                        Payment.objects.create(
                            reservation=reservation,
                            customer=customer,
                            stripe_payment_intent_id=payment_intent.id,
                            amount=Decimal(amount_str),
                            payment_type="pay_now",
                            status="failed",
                            stripe_customer_id=stripe_customer_id,
                            stripe_payment_method_id=selected_payment_method,
                        )

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

                except stripe.error.CardError as e:
                    # Card was declined
                    error_message = e.user_message if hasattr(e, "user_message") else str(e)
                    messages.error(request, f"Card error: {error_message}")
                    logger.error(
                        f"Card error processing saved card payment for reservation {reservation.uuid}: {e}"
                    )
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
                "entered_amount": amount_str
                if action in ["make_payment", "use_saved_card"]
                else None,
                "entered_description": description
                if action in ["make_payment", "use_saved_card"]
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
    if not request.user.is_staff:
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
            # Track who modified the reservation
            reservation.modified_by = request.user
            reservation.last_modified_at = timezone.now()
            reservation.save(update_fields=["status", "modified_by", "last_modified_at"])
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
    Only accessible to superusers (admins).
    """
    if not can_view_statistics(request.user):
        messages.error(request, "You don't have permission to access this page.")
        return redirect("dashboard")
    
    # Get filter parameters
    date_filter = request.GET.get("date")
    date_from = request.GET.get("date_from")
    date_to = request.GET.get("date_to")
    status_filter = request.GET.get("status")
    time_filter = request.GET.get("time_filter", "all")
    driver_filter = request.GET.get("driver")
    
    # Get pagination and grouping parameters
    group_by = request.GET.get("group_by", "day")
    page = int(request.GET.get("page", 1))
    per_page = int(request.GET.get("per_page", 50))
    
    # Validate group_by parameter
    if group_by not in ['day', 'week', 'month']:
        group_by = 'day'
    
    # Get comprehensive statistics using utils
    stats = get_comprehensive_statistics(
        date_filter=date_filter,
        date_from=date_from,
        date_to=date_to,
        status_filter=status_filter,
        time_filter=time_filter,
        driver_filter=driver_filter,
        group_by=group_by,
        page=page,
        per_page=per_page
    )
    
    context = {
        'vehicle_stats': stats['vehicle_stats'],
        'trip_type_stats': stats['trip_type_stats'],
        'status_stats': stats['status_stats'],
        'driver_stats': stats['driver_stats'],
        'daily_stats': stats['daily_stats'],
        'active_drivers_count': stats['active_drivers_count'],
        'total_legs': stats['total_legs'],
        'total_revenue': stats['total_revenue'],
        'filter_date': date_filter,
        'date_from': date_from,
        'date_to': date_to,
        'status_filter': status_filter,
        'time_filter': time_filter,
        'driver_filter': driver_filter,
        'group_by': group_by,
        'page': page,
        'per_page': per_page,
    }
    
    return render(request, "dispatching/statistics.html", context)


@login_required
@require_POST
def update_contact_info(request):
    """
    Update customer contact information via AJAX.
    """
    if not request.user.is_staff:
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
def refresh_flight_data(request):
    """
    Refresh flight data from AeroAPI for a specific leg.
    """
    if not request.user.is_staff:
        return JsonResponse(
            {"success": False, "error": "Permission denied"}, status=403
        )

    try:
        data = json.loads(request.body)
        leg_id = data.get("leg_id")

        if not leg_id:
            return JsonResponse(
                {"success": False, "error": "Missing leg ID"}, status=400
            )

        # Get the leg
        leg = get_object_or_404(Leg, id=leg_id)
        
        # Check if leg has flight information
        if not leg.flight_information:
            return JsonResponse(
                {"success": False, "error": "Leg does not have flight information"}, status=400
            )

        flight = leg.flight_information
        
        # Get flight identifier
        flight_ident = flight.get_flight_ident()
        if not flight_ident:
            return JsonResponse(
                {"success": False, "error": "Could not determine flight identifier"}, status=400
            )

        # Get the leg's pickup date to fetch flight data for the correct date
        flight_date = leg.pickup_date.strftime('%Y-%m-%d') if leg.pickup_date else None
        trip_type = leg.get_trip_type()  # 'arrival', 'return', or 'other'
        logger.info(f"Fetching flight data for leg pickup date: {flight_date}, trip type: {trip_type}")

        # Fetch flight data from AeroAPI
        aeroapi = AeroAPIService()
        flight_data = aeroapi.get_flight_info(flight_ident, flight_date=flight_date, trip_type=trip_type)

        logger.info(f"Flight data response: {flight_data}")

        if flight_data.get('status') != 'success':
            error_msg = flight_data.get('error', 'Unknown error')
            logger.error(f"AeroAPI error: {error_msg}")
            return JsonResponse({
                "success": False,
                "error": error_msg
            }, status=400)

        # Update flight model with AeroAPI data
        # Only update fields that have values (don't overwrite with empty strings)
        if flight_data.get('flight_iata'):
            flight.flight_iata = flight_data.get('flight_iata')
        if flight_data.get('origin'):
            flight.origin = flight_data.get('origin')
        if flight_data.get('destination'):
            flight.destination = flight_data.get('destination')
        # Use 'flight_status' for the actual flight status, fallback to 'status' for backwards compatibility
        flight_status = flight_data.get('flight_status') or flight_data.get('status', '')
        if flight_status:
            flight.status = flight_status
        
        # Handle datetime fields - only set if not None
        scheduled_arrival = flight_data.get('scheduled_arrival_local')
        if scheduled_arrival is not None:
            flight.scheduled_arrival_local = scheduled_arrival
        elif scheduled_arrival is None and flight_data.get('scheduled_arrival_local') is None:
            # Keep existing value if new value is None
            pass
        
        estimated_arrival = flight_data.get('estimated_arrival_local')
        if estimated_arrival is not None:
            flight.estimated_arrival_local = estimated_arrival
        
        # Handle gate arrival times
        scheduled_gate_arrival = flight_data.get('scheduled_gate_arrival_local')
        if scheduled_gate_arrival is not None:
            flight.scheduled_gate_arrival_local = scheduled_gate_arrival
        
        estimated_gate_arrival = flight_data.get('estimated_gate_arrival_local')
        if estimated_gate_arrival is not None:
            flight.estimated_gate_arrival_local = estimated_gate_arrival
        
        # Handle actual arrival times (prioritize actual over estimated)
        # BUT: Clear old actual times if flight is scheduled for the future (stale data from previous flights)
        now = timezone.now()
        actual_arrival = flight_data.get('actual_runway_arrival_local')
        actual_gate_arrival = flight_data.get('actual_gate_arrival_local')
        
        # Check if flight is scheduled for the future
        is_future_flight = False
        if scheduled_arrival and scheduled_arrival > now:
            is_future_flight = True
        elif scheduled_gate_arrival and scheduled_gate_arrival > now:
            is_future_flight = True
        
        if is_future_flight:
            # For future flights, clear any actual arrival times (they're from old flight data)
            flight.actual_arrival_local = None
            flight.actual_gate_arrival_local = None
            logger.info(f"Cleared stale actual arrival times for future flight (leg {leg.id})")
        else:
            # For past/current flights, use actual times if provided
            if actual_arrival is not None:
                flight.actual_arrival_local = actual_arrival
            if actual_gate_arrival is not None:
                flight.actual_gate_arrival_local = actual_gate_arrival
        
        if flight_data.get('terminal'):
            flight.terminal = flight_data.get('terminal')
        if flight_data.get('gate'):
            flight.gate = flight_data.get('gate')
        if flight_data.get('baggage_claim'):
            flight.baggage_claim = flight_data.get('baggage_claim')
        
        flight.last_updated = flight_data.get('last_updated', timezone.now())
        
        try:
            flight.save()
        except Exception as e:
            logger.error(f"Error saving flight data: {e}")
            return JsonResponse({
                "success": False,
                "error": f"Error saving flight data: {str(e)}"
            }, status=500)

        # Return updated flight data
        return JsonResponse({
            "success": True,
            "message": "Flight data refreshed successfully",
            "flight_data": {
                "flight_iata": flight.flight_iata or "",
                "origin": flight.origin or "",
                "destination": flight.destination or "",
                "status": flight.status or "",
                "scheduled_arrival_local": flight.scheduled_arrival_local.strftime('%Y-%m-%d %I:%M %p') if flight.scheduled_arrival_local else "",
                "estimated_arrival_local": flight.estimated_arrival_local.strftime('%Y-%m-%d %I:%M %p') if flight.estimated_arrival_local else "",
                "actual_arrival_local": flight.actual_arrival_local.strftime('%Y-%m-%d %I:%M %p') if flight.actual_arrival_local else "",
                "scheduled_gate_arrival_local": flight.scheduled_gate_arrival_local.strftime('%Y-%m-%d %I:%M %p') if flight.scheduled_gate_arrival_local else "",
                "estimated_gate_arrival_local": flight.estimated_gate_arrival_local.strftime('%Y-%m-%d %I:%M %p') if flight.estimated_gate_arrival_local else "",
                "actual_gate_arrival_local": flight.actual_gate_arrival_local.strftime('%Y-%m-%d %I:%M %p') if flight.actual_gate_arrival_local else "",
                "terminal": flight.terminal or "",
                "gate": flight.gate or "",
                "baggage_claim": flight.baggage_claim or "",
                "last_updated": flight.last_updated.strftime('%Y-%m-%d %I:%M %p') if flight.last_updated else "",
            }
        })

    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)
    except Exception as e:
        logger.error(f"Error refreshing flight data: {e}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


def _flight_refresh_cache_key(task_id):
    return f"flight_refresh:{task_id}"


def _refresh_single_flight(leg):
    """Refresh flight data for a single leg."""
    try:
        flight = leg.flight_information
        flight_ident = flight.get_flight_ident()

        if not flight_ident:
            return {
                "leg_id": leg.id,
                "success": False,
                "error": "Could not determine flight identifier",
            }

        # Get the leg's pickup date to fetch flight data for the correct date
        flight_date = leg.pickup_date.strftime("%Y-%m-%d") if leg.pickup_date else None
        trip_type = leg.get_trip_type()

        # Create a new AeroAPI instance for this thread (thread-safe)
        aeroapi = AeroAPIService()

        # Fetch flight data from AeroAPI
        flight_data = aeroapi.get_flight_info(
            flight_ident, flight_date=flight_date, trip_type=trip_type
        )

        # Handle rate limiting
        if flight_data.get("status") == "rate_limited":
            retry_after = flight_data.get("retry_after", 60)
            return {
                "leg_id": leg.id,
                "success": False,
                "error": f"Rate limit exceeded. Please wait {retry_after} seconds.",
                "rate_limited": True,
                "retry_after": retry_after,
            }

        if flight_data.get("status") != "success":
            error_msg = flight_data.get("error", "Unknown error")
            return {
                "leg_id": leg.id,
                "success": False,
                "error": error_msg,
            }

        # Update flight model with AeroAPI data
        if flight_data.get("flight_iata"):
            flight.flight_iata = flight_data.get("flight_iata")
        if flight_data.get("origin"):
            flight.origin = flight_data.get("origin")
        if flight_data.get("destination"):
            flight.destination = flight_data.get("destination")

        flight_status = flight_data.get("flight_status") or flight_data.get("status", "")
        if flight_status:
            flight.status = flight_status

        # Handle datetime fields
        scheduled_arrival = flight_data.get("scheduled_arrival_local")
        if scheduled_arrival is not None:
            flight.scheduled_arrival_local = scheduled_arrival

        estimated_arrival = flight_data.get("estimated_arrival_local")
        if estimated_arrival is not None:
            flight.estimated_arrival_local = estimated_arrival

        scheduled_gate_arrival = flight_data.get("scheduled_gate_arrival_local")
        if scheduled_gate_arrival is not None:
            flight.scheduled_gate_arrival_local = scheduled_gate_arrival

        estimated_gate_arrival = flight_data.get("estimated_gate_arrival_local")
        if estimated_gate_arrival is not None:
            flight.estimated_gate_arrival_local = estimated_gate_arrival

        # Handle actual arrival times (prioritize actual over estimated)
        # BUT: Clear old actual times if flight is scheduled for the future
        now = timezone.now()
        actual_arrival = flight_data.get("actual_runway_arrival_local")
        actual_gate_arrival = flight_data.get("actual_gate_arrival_local")

        is_future_flight = False
        if scheduled_arrival and scheduled_arrival > now:
            is_future_flight = True
        elif scheduled_gate_arrival and scheduled_gate_arrival > now:
            is_future_flight = True

        if is_future_flight:
            flight.actual_arrival_local = None
            flight.actual_gate_arrival_local = None
            logger.info(
                f"Cleared stale actual arrival times for future flight (leg {leg.id})"
            )
        else:
            if actual_arrival is not None:
                flight.actual_arrival_local = actual_arrival
            if actual_gate_arrival is not None:
                flight.actual_gate_arrival_local = actual_gate_arrival

        if flight_data.get("terminal"):
            flight.terminal = flight_data.get("terminal")
        if flight_data.get("gate"):
            flight.gate = flight_data.get("gate")
        if flight_data.get("baggage_claim"):
            flight.baggage_claim = flight_data.get("baggage_claim")

        flight.last_updated = flight_data.get("last_updated", timezone.now())
        flight.save()

        return {
            "leg_id": leg.id,
            "success": True,
            "flight_data": {
                "flight_iata": flight.flight_iata or "",
                "origin": flight.origin or "",
                "destination": flight.destination or "",
                "status": flight.status or "",
                "scheduled_arrival_local": flight.scheduled_arrival_local.strftime(
                    "%Y-%m-%d %I:%M %p"
                )
                if flight.scheduled_arrival_local
                else "",
                "estimated_arrival_local": flight.estimated_arrival_local.strftime(
                    "%Y-%m-%d %I:%M %p"
                )
                if flight.estimated_arrival_local
                else "",
                "actual_arrival_local": flight.actual_arrival_local.strftime(
                    "%Y-%m-%d %I:%M %p"
                )
                if flight.actual_arrival_local
                else "",
                "scheduled_gate_arrival_local": flight.scheduled_gate_arrival_local.strftime(
                    "%Y-%m-%d %I:%M %p"
                )
                if flight.scheduled_gate_arrival_local
                else "",
                "estimated_gate_arrival_local": flight.estimated_gate_arrival_local.strftime(
                    "%Y-%m-%d %I:%M %p"
                )
                if flight.estimated_gate_arrival_local
                else "",
                "actual_gate_arrival_local": flight.actual_gate_arrival_local.strftime(
                    "%Y-%m-%d %I:%M %p"
                )
                if flight.actual_gate_arrival_local
                else "",
                "terminal": flight.terminal or "",
                "gate": flight.gate or "",
                "baggage_claim": flight.baggage_claim or "",
                "last_updated": flight.last_updated.strftime("%Y-%m-%d %I:%M %p")
                if flight.last_updated
                else "",
            },
        }
    except Exception as e:
        logger.error(f"Error refreshing flight for leg {leg.id}: {e}")
        return {
            "leg_id": leg.id,
            "success": False,
            "error": str(e),
        }


def _run_bulk_flight_refresh(task_id, leg_ids):
    import time

    task_key = _flight_refresh_cache_key(task_id)
    timeout_seconds = 60 * 60
    started_at = timezone.now().isoformat()

    try:
        legs = list(
            Leg.objects.filter(id__in=leg_ids, flight_information__isnull=False).select_related(
                "flight_information"
            )
        )

        if not legs:
            cache.set(
                task_key,
                {
                    "status": "failed",
                    "error": "No arrival flights found to refresh. Only arrival trips are refreshed.",
                    "total": 0,
                    "processed": 0,
                    "success_count": 0,
                    "failure_count": 0,
                    "results": [],
                    "started_at": started_at,
                    "finished_at": timezone.now().isoformat(),
                },
                timeout=timeout_seconds,
            )
            return

        results = []
        success_count = 0
        failure_count = 0
        total_legs = len(legs)

        cache.set(
            task_key,
            {
                "status": "running",
                "total": total_legs,
                "processed": 0,
                "success_count": 0,
                "failure_count": 0,
                "results": [],
                "started_at": started_at,
            },
            timeout=timeout_seconds,
        )

        for index, leg in enumerate(legs):
            result = _refresh_single_flight(leg)
            results.append(result)

            if result.get("success"):
                success_count += 1
            else:
                failure_count += 1

            cache.set(
                task_key,
                {
                    "status": "running",
                    "total": total_legs,
                    "processed": index + 1,
                    "success_count": success_count,
                    "failure_count": failure_count,
                    "results": results,
                    "started_at": started_at,
                },
                timeout=timeout_seconds,
            )

            if index < total_legs - 1:
                time.sleep(6)

        message = (
            f"Refreshed {success_count} flight(s) successfully"
            + (f", {failure_count} failed" if failure_count > 0 else "")
        )

        cache.set(
            task_key,
            {
                "status": "completed",
                "message": message,
                "total": total_legs,
                "processed": total_legs,
                "success_count": success_count,
                "failure_count": failure_count,
                "results": results,
                "started_at": started_at,
                "finished_at": timezone.now().isoformat(),
            },
            timeout=timeout_seconds,
        )
    except Exception as e:
        logger.error(f"Error in bulk refresh thread: {e}")
        cache.set(
            task_key,
            {
                "status": "failed",
                "error": str(e),
                "total": 0,
                "processed": 0,
                "success_count": 0,
                "failure_count": 0,
                "results": [],
                "started_at": started_at,
                "finished_at": timezone.now().isoformat(),
            },
            timeout=timeout_seconds,
        )


@login_required
@require_POST
def refresh_all_flights(request):
    """
    Bulk refresh flight data from AeroAPI for multiple legs.
    Only refreshes "arrival" trips (pickup at airport, dropoff at destination).
    Accepts either a list of leg_ids or a date to refresh all arrival flights for that date.
    """
    if not request.user.is_staff:
        return JsonResponse(
            {"success": False, "error": "Permission denied"}, status=403
        )

    try:
        data = json.loads(request.body)
        leg_ids = data.get("leg_ids", [])
        date = data.get("date")
        
        # If date is provided, get all legs for that date with flight information
        if date:
            try:
                from datetime import datetime
                target_date = datetime.strptime(date, "%Y-%m-%d").date()
                legs = Leg.objects.filter(
                    pickup_date=target_date,
                    flight_information__isnull=False
                ).select_related('flight_information')
                leg_ids = list(legs.values_list('id', flat=True))
            except ValueError:
                return JsonResponse(
                    {"success": False, "error": "Invalid date format"}, status=400
                )
        
        if not leg_ids:
            return JsonResponse(
                {"success": False, "error": "No legs to refresh"}, status=400
            )
        
        # Get all legs with flight information
        legs = Leg.objects.filter(
            id__in=leg_ids,
            flight_information__isnull=False
        ).select_related('flight_information')
        
        # Filter to only include "arrival" trip types (pickup at airport, dropoff at destination)
        # We need to filter in Python since get_trip_type() is a computed property
        arrival_legs = [leg for leg in legs if leg.get_trip_type() == 'arrival']
        legs = arrival_legs
        
        if not legs:
            return JsonResponse({
                "success": False,
                "error": "No arrival flights found to refresh. Only arrival trips are refreshed."
            }, status=400)
        
        task_id = uuid.uuid4().hex
        task_key = _flight_refresh_cache_key(task_id)

        cache.set(
            task_key,
            {
                "status": "queued",
                "total": len(legs),
                "processed": 0,
                "success_count": 0,
                "failure_count": 0,
                "results": [],
                "started_at": timezone.now().isoformat(),
            },
            timeout=60 * 60,
        )

        worker = threading.Thread(
            target=_run_bulk_flight_refresh, args=(task_id, [leg.id for leg in legs]), daemon=True
        )
        worker.start()

        return JsonResponse(
            {
                "success": True,
                "status": "started",
                "task_id": task_id,
                "total": len(legs),
            },
            status=202,
        )
        
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)
    except Exception as e:
        logger.error(f"Error in bulk refresh: {e}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required
@require_http_methods(["GET"])
def refresh_all_flights_status(request, task_id):
    if not request.user.is_staff:
        return JsonResponse(
            {"success": False, "error": "Permission denied"}, status=403
        )

    task_key = _flight_refresh_cache_key(task_id)
    data = cache.get(task_key)
    if not data:
        return JsonResponse(
            {"success": False, "error": "Refresh task not found"}, status=404
        )

    return JsonResponse({"success": True, **data})


@login_required
@require_POST
def update_leg_info(request):
    """
    Update leg information including flight details via AJAX.
    """
    if not request.user.is_staff:
        return JsonResponse(
            {"success": False, "error": "Permission denied"}, status=403
        )

    try:
        data = json.loads(request.body)
        leg_id = data.get("leg_id")
        leg_data = data.get("leg_data", {})
        flight_data = data.get("flight_data", {})
        cruise_data = data.get("cruise_data", {})

        if not leg_id:
            return JsonResponse(
                {"success": False, "error": "Missing leg ID"}, status=400
            )

        # Get the leg with related objects
        leg = get_object_or_404(
            Leg.objects.select_related('driver', 'driver__profile', 'flight_information', 'cruise_information'), 
            id=leg_id
        )

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

        # Handle cruise information (only if cruise_data is provided in the request)
        cruise_to_delete = None
        if cruise_data:
            cruise_line = cruise_data.get("cruise_line", "").strip() if cruise_data.get("cruise_line") else ""
            cruise_ship = cruise_data.get("ship_name", "").strip() if cruise_data.get("ship_name") else ""
            
            if cruise_line or cruise_ship:
                # Create or update cruise information
                if leg.cruise_information:
                    cruise = leg.cruise_information
                    cruise.cruise_line = cruise_line
                    cruise.ship_name = cruise_ship
                    cruise.save()
                else:
                    # Create new cruise information
                    from reservations.models import Cruise
                    cruise = Cruise.objects.create(
                        cruise_line=cruise_line,
                        ship_name=cruise_ship
                    )
                    leg.cruise_information = cruise
                    update_fields.append("cruise_information")
            else:
                # If both fields are empty, remove cruise information
                if leg.cruise_information:
                    # Get reference to cruise before removing relationship
                    cruise_to_delete = leg.cruise_information
                    # Remove the relationship
                    leg.cruise_information = None
                    update_fields.append("cruise_information")

        # Save the leg if any fields were updated
        if update_fields:
            try:
                leg.save(update_fields=update_fields)
            except Exception as e:
                logger.error(f"Error saving leg {leg.id} with update_fields: {e}")
                # If save with update_fields fails (e.g., "did not affect any rows"), 
                # try saving without it - this can happen if the leg was already updated
                try:
                    # Re-apply the cruise_information change if needed
                    if 'cruise_information' in update_fields and cruise_to_delete:
                        leg.cruise_information = None
                    leg.save()
                except Exception as save_error:
                    logger.error(f"Error saving leg {leg.id} without update_fields: {save_error}")
                    return JsonResponse({
                        "success": False,
                        "error": f"Failed to save leg: {str(save_error)}"
                    }, status=500)
        
        # After saving the leg, delete the cruise if it was removed
        if cruise_to_delete:
            try:
                cruise_to_delete.delete()
            except Exception as e:
                logger.warning(f"Could not delete cruise {cruise_to_delete.id}: {e}")

        # Refresh leg from database to get latest data including driver
        leg.refresh_from_db()
        
        return JsonResponse({
            "success": True,
            "message": "Leg information updated successfully",
            "leg": {
                "pickup_date": leg.pickup_date.isoformat() if leg.pickup_date else None,
                "pickup_time": leg.pickup_time.strftime("%H:%M") if leg.pickup_time else None,
                "pickup_location": leg.pickup_location,
                "dropoff_location": leg.dropoff_location,
                "private_notes": leg.private_notes,
                "driver_id": leg.driver.id if leg.driver else None,
                "driver_name": leg.driver.profile.username if leg.driver and leg.driver.profile else None,
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
    if not request.user.is_staff:
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
    if not request.user.is_staff:
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
    if not request.user.is_staff:
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
            # Form validation failed - show specific error messages
            error_details = []
            for field, errors in form.errors.items():
                field_label = form.fields[field].label if field in form.fields else field.replace('_', ' ').title()
                for error in errors:
                    error_details.append(f"{field_label}: {error}")
            
            if form.non_field_errors():
                for error in form.non_field_errors():
                    error_details.append(error)
            
            if error_details:
                # Show first 5 errors in the message
                if len(error_details) <= 5:
                    error_msg = "Please fix the following errors:<br>• " + "<br>• ".join(error_details)
                else:
                    error_msg = "Please fix the following errors:<br>• " + "<br>• ".join(error_details[:5]) + f"<br>... and {len(error_details) - 5} more error(s). See the form fields below for details."
                messages.error(request, error_msg)
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
    if not request.user.is_staff:
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
            # Validate that at least one leg is provided
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
            
            if not legs_data:
                messages.error(request, "At least one trip leg is required. Please add leg details.")
            else:
                booking_data['legs_data'] = legs_data
                booking_data['flights_data'] = flights_data
                booking_data['step'] = 4
                request.session['dispatcher_booking'] = booking_data
                
                return redirect('dispatcher_booking_pricing')
        else:
            # Formset validation failed - show specific error messages
            error_details = []
            
            # Collect leg form errors
            for i, leg_form in enumerate(leg_formset):
                if leg_form.errors:
                    leg_num = i + 1
                    for field, errors in leg_form.errors.items():
                        if field != 'DELETE':
                            field_label = leg_form.fields[field].label if field in leg_form.fields else field.replace('_', ' ').title()
                            for error in errors:
                                error_details.append(f"Leg {leg_num} - {field_label}: {error}")
            
            # Collect flight form errors
            for i, flight_form in enumerate(flight_formset):
                if flight_form.errors:
                    leg_num = i + 1
                    for field, errors in flight_form.errors.items():
                        if field != 'DELETE':
                            field_label = flight_form.fields[field].label if field in flight_form.fields else field.replace('_', ' ').title()
                            for error in errors:
                                error_details.append(f"Leg {leg_num} Flight - {field_label}: {error}")
            
            # Collect non-form errors
            if leg_formset.non_form_errors():
                for error in leg_formset.non_form_errors():
                    error_details.append(f"Form Error: {error}")
            if flight_formset.non_form_errors():
                for error in flight_formset.non_form_errors():
                    error_details.append(f"Flight Form Error: {error}")
            
            if error_details:
                # Show first 5 errors in the message, then indicate if there are more
                if len(error_details) <= 5:
                    error_msg = "Please fix the following errors:<br>• " + "<br>• ".join(error_details)
                else:
                    error_msg = "Please fix the following errors:<br>• " + "<br>• ".join(error_details[:5]) + f"<br>... and {len(error_details) - 5} more error(s). See the form fields below for details."
                messages.error(request, error_msg)
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
    if not request.user.is_staff:
        return redirect("home")
    
    booking_data = request.session.get('dispatcher_booking')
    if not booking_data or not booking_data.get('legs_data'):
        messages.error(request, "Please complete all previous steps first.")
        return redirect('dispatcher_booking_legs')
    
    customer = get_object_or_404(Customer, id=booking_data['customer_id'])
    
    if request.method == "POST":
        form = DispatcherPricingForm(request.POST)
        
        if form.is_valid():
            # Validate pricing values
            base_price = form.cleaned_data['manual_base_price']
            additional_charges = form.cleaned_data.get('additional_charges', Decimal('0.00'))
            total_price = form.cleaned_data['total_price']
            
            if base_price < 0:
                messages.error(request, "Base price cannot be negative.")
            elif total_price < 0:
                messages.error(request, "Total price cannot be negative.")
            elif total_price != base_price + additional_charges:
                messages.error(request, "Total price must equal base price plus additional charges.")
            else:
                # Save pricing data to session
                pricing_data = {
                    'manual_base_price': str(base_price),
                    'additional_charges': str(additional_charges),
                    'total_price': str(total_price),
                    'private_notes': form.cleaned_data.get('private_notes', ''),
                }
                
                booking_data['pricing_data'] = pricing_data
                booking_data['step'] = 5
                request.session['dispatcher_booking'] = booking_data
                
                return redirect('dispatcher_booking_review')
        else:
            # Form validation failed - show specific error messages
            error_details = []
            for field, errors in form.errors.items():
                field_label = form.fields[field].label if field in form.fields else field.replace('_', ' ').title()
                for error in errors:
                    error_details.append(f"{field_label}: {error}")
            
            if form.non_field_errors():
                for error in form.non_field_errors():
                    error_details.append(error)
            
            if error_details:
                # Show first 5 errors in the message
                if len(error_details) <= 5:
                    error_msg = "Please fix the following errors:<br>• " + "<br>• ".join(error_details)
                else:
                    error_msg = "Please fix the following errors:<br>• " + "<br>• ".join(error_details[:5]) + f"<br>... and {len(error_details) - 5} more error(s). See the form fields below for details."
                messages.error(request, error_msg)
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
    if not request.user.is_staff:
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
            flight_info = flights_data[i].copy()
            # Add display name for airline if available
            if flight_info.get('airline'):
                from reservations.utils import get_airline_display_name
                flight_info['airline_display_name'] = get_airline_display_name(flight_info['airline'])
            combined_leg['flight_info'] = flight_info
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
                # Validate required data before creating reservation
                if not booking_data.get('legs_data'):
                    messages.error(request, "Cannot create reservation: No trip legs found. Please go back and add leg details.")
                elif not booking_data.get('pricing_data'):
                    messages.error(request, "Cannot create reservation: Pricing information is missing. Please go back and set pricing.")
                elif not booking_data.get('reservation_data'):
                    messages.error(request, "Cannot create reservation: Reservation details are missing. Please start over.")
                else:
                    # Create the actual reservation and legs
                    reservation = create_dispatcher_reservation(booking_data)
                    
                    # Clear session data
                    del request.session['dispatcher_booking']
                    
                    messages.success(
                        request, 
                        f"Reservation #{reservation.id} created successfully for {customer.get_full_name()}!"
                    )
                    return redirect('reservation_details', id=reservation.uuid)
                
            except Customer.DoesNotExist:
                logger.error(f"Customer not found for booking: {booking_data.get('customer_id')}")
                messages.error(request, "Error: Customer not found. Please start over.")
            except Vehicle.DoesNotExist:
                logger.error(f"Vehicle not found for booking: {booking_data.get('reservation_data', {}).get('manual_vehicle')}")
                messages.error(request, "Error: Selected vehicle not found. Please go back and select a valid vehicle.")
            except (ValueError, KeyError) as e:
                logger.error(f"Invalid data in booking: {str(e)}")
                messages.error(request, f"Error: Invalid data provided. {str(e)} Please check all fields and try again.")
            except Exception as e:
                logger.error(f"Error creating dispatcher reservation: {str(e)}", exc_info=True)
                error_msg = str(e)
                # Make error message more user-friendly
                if "pickup_date" in error_msg.lower() or "date" in error_msg.lower():
                    messages.error(request, "Error: Invalid date format in trip legs. Please check all dates and try again.")
                elif "pickup_time" in error_msg.lower() or "time" in error_msg.lower():
                    messages.error(request, "Error: Invalid time format in trip legs. Please check all times and try again.")
                elif "leg" in error_msg.lower():
                    messages.error(request, "Error: Problem creating trip legs. Please verify all leg details are correct.")
                else:
                    messages.error(request, f"Error creating reservation: {error_msg}. Please check all information and try again.")
        
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
    Raises specific exceptions with clear error messages
    """
    # Validate required data
    if not booking_data.get('customer_id'):
        raise ValueError("Customer ID is missing from booking data")
    if not booking_data.get('reservation_data'):
        raise ValueError("Reservation data is missing from booking data")
    if not booking_data.get('pricing_data'):
        raise ValueError("Pricing data is missing from booking data")
    if not booking_data.get('legs_data'):
        raise ValueError("Legs data is missing from booking data. At least one trip leg is required.")
    
    customer = Customer.objects.get(id=booking_data['customer_id'])
    reservation_data = booking_data['reservation_data']
    pricing_data = booking_data['pricing_data']
    legs_data = booking_data['legs_data']
    flights_data = booking_data.get('flights_data', [])
    
    # Validate vehicle
    if not reservation_data.get('manual_vehicle'):
        raise ValueError("Vehicle selection is missing")
    
    try:
        vehicle = Vehicle.objects.get(id=reservation_data['manual_vehicle'])
    except Vehicle.DoesNotExist:
        raise ValueError(f"Vehicle with ID {reservation_data['manual_vehicle']} not found")
    
    # Try to find an existing rate for this vehicle (for system compatibility)
    rate = Rate.objects.filter(vehicle=vehicle).first()
    
    # Get the current user from thread-local storage (set by middleware)
    from reservations.middleware import get_current_user
    current_user = get_current_user()
    
    # Validate pricing
    try:
        base_price = Decimal(pricing_data.get('manual_base_price', '0'))
        additional_charges = Decimal(pricing_data.get('additional_charges', '0'))
        total_price = Decimal(pricing_data.get('total_price', '0'))
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid pricing values: {str(e)}")
    
    # Create reservation within transaction
    from django.db import transaction
    with transaction.atomic():
        reservation = Reservation.objects.create(
            customer=customer,
            vehicle=vehicle,
            rate=rate,  # May be None, which is OK for dispatcher bookings
            trip_type=booking_data.get('trip_type', 'one_way'),
            passenger_count=int(reservation_data.get('passenger_count', 1)),
            luggage_count=int(reservation_data.get('luggage_count', 1)),
            store_stop=reservation_data.get('store_stop') == 'True',
            special_requests=reservation_data.get('special_requests', ''),
            need_carseats=reservation_data.get('need_carseats') == 'True',
            rf_carseats=int(reservation_data.get('rf_carseats', 0)),
            ff_carseats=int(reservation_data.get('ff_carseats', 0)),
            booster_seats=int(reservation_data.get('booster_seats', 0)),
            base_price=base_price,
            additional_charges=additional_charges,
            total_price=total_price,
            private_notes=pricing_data.get('private_notes', ''),
            status='confirmed',  # Dispatcher bookings are confirmed by default
            created_by=current_user,  # Track who created the reservation
            modified_by=current_user,  # Track who last modified
            last_modified_at=timezone.now()
        )
        
        # Create legs
        if not legs_data:
            raise ValueError("Cannot create reservation: No trip legs provided")
        
        for i, leg_data in enumerate(legs_data):
            # Validate required leg fields
            if not leg_data.get('pickup_date'):
                raise ValueError(f"Leg {i+1}: Pickup date is required")
            if not leg_data.get('pickup_time'):
                raise ValueError(f"Leg {i+1}: Pickup time is required")
            if not leg_data.get('pickup_location'):
                raise ValueError(f"Leg {i+1}: Pickup location is required")
            if not leg_data.get('dropoff_location'):
                raise ValueError(f"Leg {i+1}: Dropoff location is required")
            
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
            
            # Parse date and time
            from datetime import datetime, time
            try:
                pickup_date = datetime.strptime(leg_data['pickup_date'], '%Y-%m-%d').date()
            except (ValueError, TypeError) as e:
                raise ValueError(f"Leg {i+1}: Invalid pickup date format: {leg_data.get('pickup_date')}")
            
            pickup_time_str = leg_data.get('pickup_time')
            pickup_time = None
            if pickup_time_str:
                try:
                    pickup_time = datetime.strptime(pickup_time_str, '%H:%M:%S').time()
                except ValueError:
                    try:
                        pickup_time = datetime.strptime(pickup_time_str, '%H:%M').time()
                    except ValueError:
                        raise ValueError(f"Leg {i+1}: Invalid pickup time format: {pickup_time_str}")
            
            # Parse driver pay amount if provided
            driver_pay_amount = None
            if leg_data.get('driver_pay_amount'):
                try:
                    driver_pay_amount = Decimal(leg_data.get('driver_pay_amount', '0'))
                except (ValueError, TypeError):
                    # If invalid, just set to None
                    driver_pay_amount = None
            
            leg = Leg.objects.create(
                reservation=reservation,
                flight_information=flight,
                pickup_date=pickup_date,
                pickup_time=pickup_time,
                pickup_location=leg_data.get('pickup_location', ''),
                dropoff_location=leg_data.get('dropoff_location', ''),
                private_notes=leg_data.get('private_notes', ''),
                driver_pay_amount=driver_pay_amount
            )
    
    return reservation


@login_required(login_url="login")
def dispatcher_booking_cancel(request):
    """
    Cancel dispatcher booking and clear session
    """
    if not request.user.is_staff:
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
    if not request.user.is_staff:
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


@login_required(login_url="login")
@require_http_methods(["POST"])
def add_leg_to_reservation(request):
    """
    Add a new leg to an existing reservation.
    
    Args:
        request: The HTTP request containing leg data
        
    Returns:
        JSON response with success status and leg data
    """
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    
    try:
        data = json.loads(request.body)
        reservation_id = data.get('reservation_id')
        leg_data = data.get('leg_data', {})
        flight_data = data.get('flight_data', {})
        
        if not reservation_id:
            return JsonResponse({"success": False, "error": "Reservation ID is required"})
        
        # Get the reservation
        reservation = get_object_or_404(Reservation, uuid=reservation_id)
        
        # Validate required leg fields
        required_fields = ['pickup_date', 'pickup_time', 'pickup_location', 'dropoff_location']
        for field in required_fields:
            if not leg_data.get(field):
                return JsonResponse({"success": False, "error": f"{field.replace('_', ' ').title()} is required"})
        
        # Convert string dates to proper date/time objects
        from datetime import datetime, date, time
        
        pickup_date = datetime.strptime(leg_data['pickup_date'], '%Y-%m-%d').date()
        pickup_time = datetime.strptime(leg_data['pickup_time'], '%H:%M').time()
        
        # Create the leg
        leg = Leg.objects.create(
            reservation=reservation,
            pickup_date=pickup_date,
            pickup_time=pickup_time,
            pickup_location=leg_data['pickup_location'],
            dropoff_location=leg_data['dropoff_location'],
            private_notes=leg_data.get('private_notes', ''),
            status='in-progress'
        )
        
        # Create flight information if provided
        if flight_data.get('airline') or flight_data.get('flight_number'):
            flight = Flight.objects.create(
                airline=flight_data.get('airline', ''),
                flight_number=flight_data.get('flight_number', '')
            )
            leg.flight_information = flight
            leg.save()
        
        # Update reservation pricing if needed (recalculate revenue share for all legs)
        for existing_leg in reservation.legs.all():
            existing_leg.save()  # This will recalculate revenue share
        
        logger.info(f"Added new leg {leg.id} to reservation {reservation.id}")
        
        return JsonResponse({
            "success": True,
            "leg": {
                "id": leg.id,
                "pickup_date": leg.pickup_date.isoformat(),
                "pickup_time": leg.pickup_time.isoformat(),
                "pickup_location": leg.pickup_location,
                "dropoff_location": leg.dropoff_location,
                "private_notes": leg.private_notes,
                "flight_info": {
                    "airline": leg.flight_information.airline if leg.flight_information else '',
                    "flight_number": leg.flight_information.flight_number if leg.flight_information else ''
                }
            }
        })
        
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON data"}, status=400)
    except Exception as e:
        logger.error(f"Error adding leg to reservation: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required(login_url="login")
def driver_payment_management(request):
    """
    Driver Payment Management Dashboard
    Shows legs for selected driver with ability to set driver pay amounts
    """
    if not request.user.is_staff:
        return redirect("home")

    selected_driver_id = request.GET.get("driver")
    selected_driver = None
    legs = []
    total_pay = 0
    total_pay_completed = 0

    # Get only drivers who have unpaid legs for dropdown
    drivers = Driver.objects.select_related("profile").filter(
        legs__payment_status='unpaid'
    ).distinct().order_by("profile__first_name", "profile__last_name")

    if selected_driver_id:
        try:
            selected_driver = get_object_or_404(Driver.objects.select_related('profile'), id=selected_driver_id)
            
            # Get only unpaid legs for the driver with optimized queries
            legs = (
                    Leg.objects
                    .select_related(
                        "reservation",
                        "reservation__customer",
                        "reservation__vehicle",
                        "reservation__travel_agent",
                        "reservation__travel_agent__user",
                        "flight_information",
                    )
                    .prefetch_related(
                        Prefetch("reservation__payments", queryset=Payment.objects.order_by('-created_at')),
                    )
                    .filter(driver=selected_driver, payment_status='unpaid')
                    .order_by("pickup_date", "pickup_time")
                )
            
            # Calculate total pay amounts
            total_pay = sum(leg.driver_pay_amount or 0 for leg in legs)
            # Calculate total pay for completed legs only
            total_pay_completed = sum(
                leg.driver_pay_amount or 0 
                for leg in legs 
                if leg.status == 'completed'
            )
            
        except (ValueError, Driver.DoesNotExist):
            messages.error(request, "Invalid driver selected")
            selected_driver = None

    context = {
        "drivers": drivers,
        "selected_driver": selected_driver,
        "selected_driver_id": selected_driver_id,
        "legs": legs,
        "total_pay": total_pay,
        "total_pay_completed": total_pay_completed,
        "leg_count": len(legs),
    }

    return render(request, "dispatching/driver_payment_management.html", context)


@login_required(login_url="login")
@require_http_methods(["POST"])
def update_driver_pay_amount(request):
    """
    Update driver pay amount for a specific leg via AJAX
    """
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

    try:
        data = json.loads(request.body)
        leg_id = data.get("leg_id")
        driver_pay_amount = data.get("driver_pay_amount")

        if not leg_id:
            return JsonResponse({"success": False, "error": "Missing leg ID"}, status=400)
        
        # Handle empty or null values as 0
        if driver_pay_amount is None or driver_pay_amount == "":
            driver_pay_amount = 0

        # Get the leg
        leg = get_object_or_404(Leg, id=leg_id)
        
        # Validate the amount
        try:
            from decimal import Decimal
            amount_decimal = Decimal(str(driver_pay_amount))
            
            # Check for reasonable limits
            if amount_decimal < 0:
                return JsonResponse({"success": False, "error": "Amount cannot be negative"}, status=400)
            if amount_decimal > Decimal('9999.99'):
                return JsonResponse({"success": False, "error": "Amount too large (max $9999.99)"}, status=400)
                
        except (ValueError, TypeError) as e:
            return JsonResponse({"success": False, "error": "Invalid amount format"}, status=400)
        
        # Update the driver pay amount
        leg.driver_pay_amount = amount_decimal
        leg.save(update_fields=['driver_pay_amount'])
        
        # Update reservation profit calculations if needed
        try:
            leg.reservation.update_profit_calculations()
        except Exception as e:
            logger.warning(f"Could not update reservation profit calculations: {e}")

        logger.info(f"Updated driver pay amount for leg {leg_id} to {amount_decimal}")
        
        return JsonResponse({
            "success": True,
            "message": "Driver pay amount updated successfully",
            "new_amount": float(leg.driver_pay_amount),
        })

    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON data"}, status=400)
    except Exception as e:
        logger.error(f"Error updating driver pay amount: {str(e)}")
        return JsonResponse({"success": False, "error": f"Server error: {str(e)}"}, status=500)


@login_required(login_url="login")
@require_http_methods(["POST"])
def process_driver_payment(request):
    """
    Process payment for a driver's unpaid legs via AJAX
    """
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

    try:
        data = json.loads(request.body)
        driver_id = data.get("driver_id")
        leg_ids = data.get("leg_ids", [])  # Optional: specific legs to process
        
        if not driver_id:
            return JsonResponse({"success": False, "error": "Missing driver ID"}, status=400)
        
        driver = get_object_or_404(Driver, id=driver_id)
        
        # Get unpaid legs for this driver that are completed
        unpaid_legs = Leg.objects.filter(
            driver=driver,
            payment_status='unpaid',
            status='completed'  # Only process completed legs
        )
        
        # If specific leg IDs provided, filter to those
        if leg_ids:
            unpaid_legs = unpaid_legs.filter(id__in=leg_ids)
        
        # Only process legs that have a driver_pay_amount > 0
        unpaid_legs = unpaid_legs.filter(driver_pay_amount__gt=0)
        
        if not unpaid_legs.exists():
            return JsonResponse({
                "success": False,
                "error": "No completed unpaid legs with driver pay amount found for this driver"
            }, status=400)
        
        # Calculate total
        payment_total = sum(leg.driver_pay_amount or 0 for leg in unpaid_legs)
        
        # Group legs by reservation for notes
        reservation_legs = {}
        for leg in unpaid_legs:
            if leg.reservation:
                if leg.reservation not in reservation_legs:
                    reservation_legs[leg.reservation] = []
                reservation_legs[leg.reservation].append(leg)
        
        # Create notes similar to admin action
        from django.utils import timezone
        notes = []
        notes.append(f"Payment Summary for {driver.profile.get_full_name()}")
        notes.append(f"Payment Date: {timezone.now().strftime('%B %d, %Y')}")
        notes.append(f"Total Legs: {unpaid_legs.count()}")
        notes.append("\nReservation Details:")
        notes.append("-" * 50)
        
        for reservation, legs in reservation_legs.items():
            leg_total = sum(leg.driver_pay_amount or 0 for leg in legs)
            notes.append(
                f"\nReservation #{reservation.id} - {reservation.customer.get_full_name()}"
            )
            for leg in legs:
                notes.append(
                    f"  • {leg.pickup_date.strftime('%m/%d/%Y')} | "
                    f"{leg.pickup_location} → {leg.dropoff_location} | "
                    f"Payment: ${leg.driver_pay_amount or 0:.2f}"
                )
            if len(legs) > 1:
                notes.append(f"  Subtotal: ${leg_total:.2f}")
        
        notes.append("\n" + "-" * 50)
        notes.append(f"TOTAL PAYMENT: ${payment_total:.2f}")
        notes.append(f"Payment Method: {driver.payment_method or 'Direct Deposit'}")
        notes.append(f"Reference: Auto-{timezone.now().strftime('%Y%m%d')}")
        
        # Create payment using the model method
        from drivers.models import DriverPayment
        payment = DriverPayment.create_payment(
            driver=driver,
            legs=list(unpaid_legs),
            payment_method=driver.payment_method or "direct deposit",
            reference_number=f"Auto-{timezone.now().strftime('%Y%m%d')}",
            notes="\n".join(notes),
            created_by=request.user,
        )
        
        logger.info(f"Processed payment {payment.id} for driver {driver} with {unpaid_legs.count()} legs. Total: ${payment_total}")
        
        return JsonResponse({
            "success": True,
            "message": f"Payment processed successfully for {unpaid_legs.count()} leg(s). Total: ${payment_total:.2f}",
            "payment_id": payment.id,
            "legs_processed": unpaid_legs.count(),
            "total_amount": float(payment_total),
        })
        
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON data"}, status=400)
    except Exception as e:
        logger.error(f"Error processing driver payment: {str(e)}", exc_info=True)
        return JsonResponse({"success": False, "error": f"Server error: {str(e)}"}, status=500)


@login_required(login_url="login")
@require_http_methods(["POST"])
def delete_leg(request):
    """
    Delete a leg from a reservation via AJAX
    """
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

    try:
        data = json.loads(request.body)
        leg_id = data.get("leg_id")

        if not leg_id:
            return JsonResponse({"success": False, "error": "Missing leg ID"}, status=400)
        
        # Get the leg
        leg = get_object_or_404(Leg, id=leg_id)
        reservation = leg.reservation
        
        # Check if this is the last leg
        total_legs = reservation.legs.count()
        if total_legs <= 1:
            return JsonResponse({
                "success": False, 
                "error": "Cannot delete the last leg of a reservation. Delete the entire reservation instead."
            }, status=400)
        
        # Store leg info for logging
        leg_info = f"Leg {leg_id}: {leg.pickup_date} {leg.pickup_time} - {leg.pickup_location} to {leg.dropoff_location}"
        
        # Delete the leg
        leg.delete()
        
        # Update reservation profit calculations
        try:
            reservation.update_profit_calculations()
        except Exception as e:
            logger.warning(f"Could not update reservation profit calculations after leg deletion: {e}")

        logger.info(f"Deleted {leg_info} from reservation {reservation.id}")
        
        return JsonResponse({
            "success": True,
            "message": "Leg deleted successfully",
        })

    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON data"}, status=400)
    except Exception as e:
        logger.error(f"Error deleting leg: {str(e)}")
        return JsonResponse({"success": False, "error": f"Server error: {str(e)}"}, status=500)


@login_required(login_url="login")
@require_http_methods(["POST"])
def delete_reservation(request):
    """
    Delete a reservation via AJAX.
    Only allows deletion if reservation has no payments.
    """
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

    try:
        data = json.loads(request.body)
        reservation_uuid = data.get("reservation_uuid")

        if not reservation_uuid:
            return JsonResponse({"success": False, "error": "Missing reservation UUID"}, status=400)
        
        # Get the reservation
        reservation = get_object_or_404(Reservation, uuid=reservation_uuid)
        
        # Check if reservation has any payments
        payment_count = reservation.payments.count()
        if payment_count > 0:
            return JsonResponse({
                "success": False, 
                "error": f"Cannot delete reservation with {payment_count} payment(s). Please remove payments first or contact support."
            }, status=400)
        
        # Store reservation info for logging
        reservation_info = f"Reservation #{reservation.id} - {reservation.customer.get_full_name()}"
        
        # Delete the reservation (this will cascade delete legs, etc.)
        reservation.delete()
        
        logger.info(f"Deleted {reservation_info} by user {request.user.username}")
        
        return JsonResponse({
            "success": True,
            "message": "Reservation deleted successfully",
        })

    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON data"}, status=400)
    except Exception as e:
        logger.error(f"Error deleting reservation: {str(e)}", exc_info=True)
        return JsonResponse({"success": False, "error": f"Server error: {str(e)}"}, status=500)


@login_required
@require_POST
def request_refund(request):
    """
    Staff can request a refund for a reservation.
    Creates a refund request that admins can process.
    """
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

    try:
        data = json.loads(request.body)
        reservation_uuid = data.get("reservation_uuid")
        refund_reason = data.get("refund_reason", "").strip()
        refund_amount = data.get("refund_amount")

        if not reservation_uuid:
            return JsonResponse({"success": False, "error": "Missing reservation UUID"}, status=400)
        
        if not refund_reason:
            return JsonResponse({"success": False, "error": "Refund reason is required"}, status=400)

        # Get the reservation
        reservation = get_object_or_404(Reservation, uuid=reservation_uuid)
        
        # Check if refund already completed - no new refunds allowed
        if reservation.refund_status == 'completed':
            return JsonResponse({
                "success": False, 
                "error": "Refund has already been completed. Cannot create a new refund request."
            }, status=400)
        
        # Check if refund already requested (admins can override this for non-completed refunds)
        if reservation.refund_status != 'none' and not request.user.is_superuser:
            return JsonResponse({
                "success": False, 
                "error": f"Refund already {reservation.get_refund_status_display().lower()}"
            }, status=400)
        
        # For admins, allow creating new refund requests even if one exists (except completed)
        # This allows them to request a refund after a previous one was rejected, etc.
        if reservation.refund_status != 'none' and request.user.is_superuser:
            # Log that admin is overriding existing refund status
            logger.info(f"Admin {request.user.username} creating new refund request for reservation {reservation.id} (previous status: {reservation.refund_status})")
        
        # Validate refund amount
        # For admins, allow refunds even if total_paid is 0 (they might be refunding after partial refunds)
        max_refund = reservation.total_paid if reservation.total_paid > 0 else reservation.total_price
        
        if refund_amount:
            try:
                refund_amount = Decimal(str(refund_amount))
                if refund_amount <= 0:
                    return JsonResponse({"success": False, "error": "Refund amount must be greater than 0"}, status=400)
                if refund_amount > max_refund:
                    return JsonResponse({
                        "success": False, 
                        "error": f"Refund amount cannot exceed ${max_refund}"
                    }, status=400)
            except (ValueError, TypeError):
                return JsonResponse({"success": False, "error": "Invalid refund amount"}, status=400)
        else:
            # Default to full refund if not specified
            refund_amount = max_refund if max_refund > 0 else reservation.total_price
        
        # Create refund request
        reservation.refund_status = 'requested'
        reservation.refund_requested_by = request.user
        reservation.refund_requested_at = timezone.now()
        reservation.refund_reason = refund_reason
        reservation.refund_amount = refund_amount
        reservation.save()
        
        # Send email notification to admin
        from users.emails import send_refund_request_notification
        send_refund_request_notification(reservation)
        
        logger.info(f"Refund requested for reservation {reservation.id} by {request.user.username}")
        
        return JsonResponse({
            "success": True,
            "message": "Refund request submitted successfully. Admin will review and process it.",
        })

    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON data"}, status=400)
    except Exception as e:
        logger.error(f"Error requesting refund: {str(e)}", exc_info=True)
        return JsonResponse({"success": False, "error": f"Server error: {str(e)}"}, status=500)


@login_required
def refund_management(request):
    """
    Admin page to view and manage refund requests.
    Shows a queue of all refund requests with ability to process them.
    """
    if not request.user.is_superuser:
        messages.error(request, "You don't have permission to access this page.")
        return redirect("dashboard")
    
    # Get filter parameters
    status_filter = request.GET.get("status", "")
    
    # Get all reservations with refund requests (include all statuses for filtering)
    if status_filter:
        # If filtering by specific status, only get that status
        refund_requests = Reservation.objects.filter(
            refund_status=status_filter
        ).select_related(
            'customer',
            'refund_requested_by',
            'refund_processed_by'
        ).order_by('-refund_requested_at')
    else:
        # Default: show active refund requests (not completed/rejected)
        refund_requests = Reservation.objects.filter(
            refund_status__in=['requested', 'processing', 'approved']
        ).select_related(
            'customer',
            'refund_requested_by',
            'refund_processed_by'
        ).order_by('-refund_requested_at')
    
    # Count by status
    status_counts = {
        'requested': Reservation.objects.filter(refund_status='requested').count(),
        'processing': Reservation.objects.filter(refund_status='processing').count(),
        'approved': Reservation.objects.filter(refund_status='approved').count(),
        'completed': Reservation.objects.filter(refund_status='completed').count(),
        'rejected': Reservation.objects.filter(refund_status='rejected').count(),
    }
    
    context = {
        'refund_requests': refund_requests,
        'status_filter': status_filter,
        'status_counts': status_counts,
    }
    
    return render(request, "dispatching/refund_management.html", context)


@login_required
@require_POST
def process_refund(request):
    """
    Admin can process, approve, or reject a refund request.
    """
    if not request.user.is_superuser:
        return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

    try:
        data = json.loads(request.body)
        reservation_uuid = data.get("reservation_uuid")
        action = data.get("action")  # 'approve', 'reject', 'process'
        refund_notes = data.get("refund_notes", "").strip()

        if not reservation_uuid or not action:
            return JsonResponse({"success": False, "error": "Missing required fields"}, status=400)
        
        if action not in ['approve', 'reject', 'process']:
            return JsonResponse({"success": False, "error": "Invalid action"}, status=400)

        # Get the reservation
        reservation = get_object_or_404(Reservation, uuid=reservation_uuid)
        
        if reservation.refund_status == 'none':
            return JsonResponse({"success": False, "error": "No refund request found"}, status=400)
        
        if action == 'reject':
            reservation.refund_status = 'rejected'
            reservation.refund_processed_by = request.user
            reservation.refund_processed_at = timezone.now()
            reservation.refund_notes = refund_notes
            reservation.save()
            
            logger.info(f"Refund rejected for reservation {reservation.id} by {request.user.username}")
            
            return JsonResponse({
                "success": True,
                "message": "Refund request rejected",
            })
        
        elif action == 'approve':
            # Approve and automatically process the refund
            reservation.refund_status = 'approved'
            reservation.refund_processed_by = request.user
            reservation.refund_processed_at = timezone.now()
            reservation.refund_notes = refund_notes
            reservation.save()
            
            logger.info(f"Refund approved for reservation {reservation.id} by {request.user.username}, processing now...")
            
            # Automatically process the refund after approval
            # Get all paid payments for this reservation
            paid_payments = reservation.payments.filter(status='paid').order_by('-created_at')
            
            if not paid_payments.exists():
                return JsonResponse({
                    "success": False, 
                    "error": "No paid payments found for this reservation"
                }, status=400)
            
            # Process refund through Stripe
            refund_amount = reservation.refund_amount
            refunded_amount = Decimal('0.00')
            refund_errors = []
            
            for payment in paid_payments:
                if refunded_amount >= refund_amount:
                    break
                
                remaining_to_refund = refund_amount - refunded_amount
                amount_to_refund = min(remaining_to_refund, payment.amount)
                
                try:
                    # Get Stripe payment intent ID from payment
                    if not payment.stripe_payment_intent_id:
                        refund_errors.append(f"Payment #{payment.id} has no Stripe payment intent ID")
                        continue
                    
                    # Create refund in Stripe
                    refund = stripe.Refund.create(
                        payment_intent=payment.stripe_payment_intent_id,
                        amount=int(amount_to_refund * 100),  # Convert to cents
                        reason='requested_by_customer',
                    )
                    
                    refunded_amount += amount_to_refund
                    
                    # Update payment record to reflect refund
                    payment.refunded_amount = (payment.refunded_amount or Decimal('0.00')) + amount_to_refund
                    payment.stripe_refund_id = refund.id  # Store the latest refund ID
                    
                    # If full payment is refunded, mark as refunded; otherwise keep as paid with refunded_amount
                    if payment.refunded_amount >= payment.amount:
                        payment.status = 'refunded'
                    # If partial refund, keep status as 'paid' but track refunded_amount
                    
                    payment.save()
                    logger.info(f"Refunded ${amount_to_refund} for payment {payment.id} via Stripe. Total refunded: ${payment.refunded_amount}")
                    
                except stripe.error.StripeError as e:
                    refund_errors.append(f"Stripe error for payment #{payment.id}: {str(e)}")
                    logger.error(f"Stripe refund error: {e}")
                except Exception as e:
                    refund_errors.append(f"Error processing payment #{payment.id}: {str(e)}")
                    logger.error(f"Refund processing error: {e}")
            
            if refund_errors and refunded_amount == 0:
                return JsonResponse({
                    "success": False,
                    "error": f"Failed to process refund: {'; '.join(refund_errors)}"
                }, status=500)
            
            # Update reservation status to completed
            reservation.refund_status = 'completed'
            reservation.status = 'cancelled'  # Cancel the reservation
            if refund_errors:
                reservation.refund_notes = (refund_notes or "") + f"\n\nRefund processing notes: {'; '.join(refund_errors)}"
            reservation.save()
            
            logger.info(f"Refund processed for reservation {reservation.id} by {request.user.username}. Amount: ${refunded_amount}")
            
            return JsonResponse({
                "success": True,
                "message": f"Refund approved and processed successfully. Amount refunded: ${refunded_amount}",
                "warnings": refund_errors if refund_errors else None,
            })
        
        elif action == 'process':
            # Process the actual Stripe refund
            if reservation.refund_status != 'approved':
                return JsonResponse({
                    "success": False, 
                    "error": "Refund must be approved before processing"
                }, status=400)
            
            # Get all paid payments for this reservation
            paid_payments = reservation.payments.filter(status='paid').order_by('-created_at')
            
            if not paid_payments.exists():
                return JsonResponse({
                    "success": False, 
                    "error": "No paid payments found for this reservation"
                }, status=400)
            
            # Process refund through Stripe
            refund_amount = reservation.refund_amount
            refunded_amount = Decimal('0.00')
            refund_errors = []
            
            for payment in paid_payments:
                if refunded_amount >= refund_amount:
                    break
                
                remaining_to_refund = refund_amount - refunded_amount
                amount_to_refund = min(remaining_to_refund, payment.amount)
                
                try:
                    # Get Stripe payment intent ID from payment
                    if not payment.stripe_payment_intent_id:
                        refund_errors.append(f"Payment #{payment.id} has no Stripe payment intent ID")
                        continue
                    
                    # Create refund in Stripe
                    refund = stripe.Refund.create(
                        payment_intent=payment.stripe_payment_intent_id,
                        amount=int(amount_to_refund * 100),  # Convert to cents
                        reason='requested_by_customer',
                    )
                    
                    refunded_amount += amount_to_refund
                    
                    # Update payment record to reflect refund
                    payment.refunded_amount = (payment.refunded_amount or Decimal('0.00')) + amount_to_refund
                    payment.stripe_refund_id = refund.id  # Store the latest refund ID
                    
                    # If full payment is refunded, mark as refunded; otherwise keep as paid with refunded_amount
                    if payment.refunded_amount >= payment.amount:
                        payment.status = 'refunded'
                    # If partial refund, keep status as 'paid' but track refunded_amount
                    
                    payment.save()
                    logger.info(f"Refunded ${amount_to_refund} for payment {payment.id} via Stripe. Total refunded: ${payment.refunded_amount}")
                    
                except stripe.error.StripeError as e:
                    refund_errors.append(f"Stripe error for payment #{payment.id}: {str(e)}")
                    logger.error(f"Stripe refund error: {e}")
                except Exception as e:
                    refund_errors.append(f"Error processing payment #{payment.id}: {str(e)}")
                    logger.error(f"Refund processing error: {e}")
            
            if refund_errors and refunded_amount == 0:
                return JsonResponse({
                    "success": False,
                    "error": f"Failed to process refund: {'; '.join(refund_errors)}"
                }, status=500)
            
            # Update reservation status
            reservation.refund_status = 'completed'
            reservation.refund_processed_by = request.user
            reservation.refund_processed_at = timezone.now()
            # Cancel the reservation so it doesn't show in legs list or allow driver assignment
            reservation.status = 'cancelled'
            if refund_notes:
                reservation.refund_notes = refund_notes
            if refund_errors:
                reservation.refund_notes = (refund_notes or "") + f"\n\nRefund processing notes: {'; '.join(refund_errors)}"
            reservation.save()
            
            logger.info(f"Refund processed for reservation {reservation.id} by {request.user.username}. Amount: ${refunded_amount}")
            
            return JsonResponse({
                "success": True,
                "message": f"Refund processed successfully. Amount refunded: ${refunded_amount}",
                "warnings": refund_errors if refund_errors else None,
            })

    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON data"}, status=400)
    except Exception as e:
        logger.error(f"Error processing refund: {str(e)}", exc_info=True)
        return JsonResponse({"success": False, "error": f"Server error: {str(e)}"}, status=500)