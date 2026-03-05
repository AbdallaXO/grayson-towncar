from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponse
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
import csv
import io
from urllib.parse import urlencode
from concurrent.futures import ThreadPoolExecutor, as_completed
from django.views.generic import ListView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.template.loader import render_to_string
from django.db.models import Prefetch
from django.db.models import OuterRef, Subquery, Exists

# App imports

from reservations.models import Reservation, Leg, Customer, Flight, LegStatus, RefundRequest
from reservations.utils import _run_in_background
from payment.models import Payment
from reservations.forms import ReservationAdminForm, CustomerForm, LegForm
from .confirmation_sms import leg_to_row
from drivers.models import (
    Driver,
    DriverPayment,
    LegPayment,
    DriverVehicleAssignment,
    FleetVehicle,
    DriverWeeklySchedule,
)
from payment.utils import get_or_create_stripe_customer
from rates.models import Vehicle, Rate
from users.emails import send_reservation_confirmation
from reservations.conversions import send_purchase_event
from payment.webhook import save_card_to_customer
from .utils import get_comprehensive_statistics, get_filtered_legs_queryset, calculate_vehicle_statistics, detect_leg_flags
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

# django-simple-history helpers for history views
from simple_history.utils import get_history_manager_for_model
from simple_history.template_utils import HistoricalRecordContextHelper

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
    legs_query = Leg.objects.filter(pickup_date=selected_date).exclude(reservation__status='cancelled').exclude(status='cancelled')
    
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
            Prefetch(
                "status_history",
                queryset=LegStatus.objects.order_by('-timestamp').select_related('updated_by')
            ),
        )
        .annotate(
            has_pending_refund=Exists(
                RefundRequest.objects.filter(
                    reservation_id=OuterRef('reservation_id'),
                    status__in=['requested', 'processing', 'approved'],
                )
            )
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
        .prefetch_related("weekly_schedule")
        .order_by("profile__first_name", "profile__last_name", "profile__username")
    )
    inhouse_assignments = DriverVehicleAssignment.objects.filter(
        date=selected_date, driver__in=inhouse_drivers
    ).select_related("driver", "driver__profile", "vehicle")
    assignment_map = {
        assignment.driver_id: assignment for assignment in inhouse_assignments
    }
    _selected_dow = selected_date.weekday()  # 0=Mon … 6=Sun
    inhouse_driver_rows = []
    for _driver in inhouse_drivers:
        _is_off = False
        for _entry in _driver.weekly_schedule.all():
            if _entry.day_of_week == _selected_dow:
                _is_off = not _entry.is_available
                break
        _assignment = assignment_map.get(_driver.id)
        # If driver has a vehicle assigned today, treat them as working regardless of schedule
        if _is_off and _assignment and _assignment.vehicle_id:
            _is_off = False
        inhouse_driver_rows.append({
            "driver": _driver,
            "assignment": _assignment,
            "is_off_today": _is_off,
        })
    def _inhouse_vehicle_sort_key(row):
        # Off-today drivers sink to bottom; within each group: assigned first, then by vehicle#/name
        off_bucket = 2 if row.get("is_off_today") else 0
        assignment = row.get("assignment")
        vehicle_number = None
        if assignment and assignment.vehicle and assignment.vehicle.vehicle_number:
            vehicle_number = assignment.vehicle.vehicle_number.lstrip("#").strip()
        if vehicle_number:
            try:
                vehicle_number = int(vehicle_number)
            except ValueError:
                pass
            return (off_bucket, vehicle_number)
        return (off_bucket + 1, str(row["driver"]))

    inhouse_driver_rows.sort(key=_inhouse_vehicle_sort_key)

    # Count legs per inhouse driver on the selected date (independent of any driver filter)
    _inhouse_driver_ids = [row["driver"].id for row in inhouse_driver_rows]
    _leg_count_qs = (
        Leg.objects.filter(pickup_date=selected_date, driver_id__in=_inhouse_driver_ids)
        .values("driver_id")
        .annotate(_cnt=Count("id"))
    )
    _inhouse_leg_counts = {r["driver_id"]: r["_cnt"] for r in _leg_count_qs}
    for row in inhouse_driver_rows:
        row["leg_count"] = _inhouse_leg_counts.get(row["driver"].id, 0)

    inhouse_assigned_count = sum(
        1 for row in inhouse_driver_rows if row["assignment"] and row["assignment"].vehicle
    )

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
    # Use per-leg revenue share (reservation price / number of legs) for accuracy
    if can_view_revenue(request.user):
        total_revenue = sum(
            leg.revenue_share or leg.calculate_revenue_share()
            for leg in legs
        )
    else:
        total_revenue = None

    # Calculate driver coverage (in-house vs affiliate)
    driver_coverage = {"inhouse": 0, "affiliate": 0, "unassigned": 0}
    for leg in legs:
        if leg.driver:
            driver_coverage[leg.driver.driver_type] += 1
        else:
            driver_coverage["unassigned"] += 1
    total_legs_count = len(legs)
    driver_coverage["total"] = total_legs_count
    driver_coverage["inhouse_pct"] = round(driver_coverage["inhouse"] / total_legs_count * 100) if total_legs_count > 0 else 0
    driver_coverage["affiliate_pct"] = round(driver_coverage["affiliate"] / total_legs_count * 100) if total_legs_count > 0 else 0
    driver_coverage["unassigned_pct"] = round(driver_coverage["unassigned"] / total_legs_count * 100) if total_legs_count > 0 else 0

    def _vehicle_sort_key(vehicle):
        vehicle_number = (vehicle.vehicle_number or "").lstrip("#").strip()
        if vehicle_number:
            try:
                return (0, int(vehicle_number))
            except ValueError:
                return (1, vehicle_number)
        return (2, "")

    inhouse_vehicles = sorted(
        FleetVehicle.objects.all(), key=_vehicle_sort_key
    )

    # Compute real-time dispatch flags for today's legs
    today = timezone.localdate()
    if selected_date == today:
        now = timezone.localtime().replace(tzinfo=None)
        for leg in legs:
            leg.dispatch_flags = detect_leg_flags(leg, now)
            # Set worst flag level for row highlighting
            if any(f['level'] == 'danger' for f in leg.dispatch_flags):
                leg.dispatch_flag_level = 'danger'
            elif leg.dispatch_flags:
                leg.dispatch_flag_level = 'warning'
            else:
                leg.dispatch_flag_level = ''
    else:
        for leg in legs:
            leg.dispatch_flags = []
            leg.dispatch_flag_level = ''

    # Annotate each leg with estimated cleared time and duration
    from dispatching.scheduler import estimate_job_end_time
    for leg in legs:
        try:
            end_dt = estimate_job_end_time(leg, selected_date)
            pickup_dt = datetime.combine(selected_date, leg.pickup_time)
            dur_mins = int((end_dt - pickup_dt).total_seconds() // 60)
            leg.cleared_time = end_dt.strftime('%I:%M %p').lstrip('0')
            hrs, mins = divmod(dur_mins, 60)
            if hrs > 0 and mins > 0:
                leg.duration_display = f"{hrs} hr {mins} mins"
            elif hrs > 0:
                leg.duration_display = f"{hrs} hr"
            else:
                leg.duration_display = f"{mins} mins"
        except Exception:
            leg.cleared_time = None
            leg.duration_display = None

        # Actual cleared time from status history (if completed)
        leg.actual_cleared_time = None
        leg.actual_duration_display = None
        if leg.status == 'completed':
            for sh in leg.status_history.all():
                if sh.status == 'completed':
                    actual_dt = timezone.localtime(sh.timestamp)
                    leg.actual_cleared_time = actual_dt.strftime('%I:%M %p').lstrip('0')
                    actual_dur = int((actual_dt.replace(tzinfo=None) - pickup_dt).total_seconds() // 60)
                    if actual_dur > 0:
                        ah, am = divmod(actual_dur, 60)
                        if ah > 0 and am > 0:
                            leg.actual_duration_display = f"{ah} hr {am} mins"
                        elif ah > 0:
                            leg.actual_duration_display = f"{ah} hr"
                        else:
                            leg.actual_duration_display = f"{am} mins"
                    break

    context = {
        "legs": legs,
        "selected_date": selected_date,
        "driver_filter": driver_filter,
        "trip_type_filter": trip_type_filter,
        "total_legs": len(legs),
        "total_revenue": total_revenue,
        "driver_coverage": driver_coverage,
        "can_view_revenue": can_view_revenue(request.user),
        "drivers": drivers,
        "inhouse_driver_rows": inhouse_driver_rows,
        "inhouse_vehicles": inhouse_vehicles,
        "inhouse_assigned_count": inhouse_assigned_count,
    }

    return render(request, "dispatching/legs_filter.html", context)


@login_required(login_url="login")
def export_legs_dashboard_csv(request):
    """
    Export the legs dashboard view to CSV for a selected date, with filters.
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

    legs_query = (
        Leg.objects.filter(pickup_date=selected_date)
        .exclude(reservation__status="cancelled")
        .exclude(status="cancelled")
    )

    if driver_filter:
        if driver_filter == "unassigned":
            legs_query = legs_query.filter(driver__isnull=True)
        else:
            legs_query = legs_query.filter(driver_id=driver_filter)

    legs = list(
        legs_query.select_related(
            "reservation",
            "reservation__customer",
            "reservation__vehicle",
            "reservation__travel_agent",
            "reservation__travel_agent__user",
            "driver",
            "driver__profile",
            "flight_information",
            "cruise_information",
        ).order_by("pickup_time")
    )

    if trip_type_filter:
        legs = [leg for leg in legs if leg.get_trip_type() == trip_type_filter]

    if not legs:
        messages.warning(
            request,
            f"No legs found for {selected_date}.",
        )
        query = urlencode(
            {
                "date": selected_date.strftime("%Y-%m-%d"),
                **({"driver": driver_filter} if driver_filter else {}),
                **({"trip_type": trip_type_filter} if trip_type_filter else {}),
            }
        )
        return redirect(f"{reverse('dashboard')}?{query}")

    fieldnames = [
        "leg_id",
        "reservation_id",
        "guest_name",
        "pickup_date",
        "pickup_time",
        "pickup_location",
        "dropoff_location",
        "trip_type",
        "vehicle_type",
        "passenger_count",
        "car_seats",
        "assigned_driver",
        "status",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for leg in legs:
        row = leg_to_row(leg)
        row.update(
            {
                "reservation_id": leg.reservation.id if leg.reservation else "",
                "assigned_driver": str(leg.driver) if leg.driver else "Unassigned",
                "status": leg.status or "",
            }
        )
        writer.writerow(row)

    csv_bytes = output.getvalue().encode("utf-8")
    response = HttpResponse(csv_bytes, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="legs_dashboard_{selected_date}.csv"'
    )
    return response


class ReservationListView(LoginRequiredMixin, UserPassesTestMixin, ListView):
    model = Reservation
    template_name = "dispatching/all_reservations.html"
    context_object_name = "reservations"
    paginate_by = 10

    def test_func(self):
        return self.request.user.is_staff

    def _get_filtered_queryset(self, include_related=True):
        if include_related:
            queryset = Reservation.objects.select_related(
                "customer",
                "vehicle",
                "rate",
                "travel_agent",
                "travel_agent__user",
            ).prefetch_related("legs", "payments")
        else:
            queryset = Reservation.objects.select_related("customer")

        queryset = queryset.order_by("-created_at")

        search_query = self.request.GET.get("search_q")
        if search_query:
            search_query = search_query.strip()
            parts = search_query.split()
            if len(parts) >= 2:
                # Multi-word search: try first+last name combo AND individual word matches
                first_part = parts[0]
                last_part = " ".join(parts[1:])
                queryset = queryset.filter(
                    Q(customer__first_name__icontains=first_part, customer__last_name__icontains=last_part)
                    | Q(customer__first_name__icontains=search_query)
                    | Q(customer__last_name__icontains=search_query)
                    | Q(customer__email__icontains=search_query)
                    | Q(customer__phone_number__icontains=search_query)
                    | Q(id__icontains=search_query)
                )
            else:
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
                queryset = queryset.filter(payments__isnull=True)
            else:
                queryset = queryset.filter(status=status_filter)

        if status_filter not in ["cancelled", "pending"]:
            queryset = queryset.exclude(status="cancelled")

        return queryset

    def get_queryset(self):
        return self._get_filtered_queryset(include_related=True)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        queryset = self._get_filtered_queryset(include_related=False)

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
    from reservations.models import LegStatus

    reservation = get_object_or_404(
        Reservation.objects.prefetch_related(
            Prefetch(
                "legs",
                queryset=Leg.objects.select_related(
                    "flight_information",
                    "cruise_information",
                    "driver",
                    "driver__profile",
                    "driver_assigned_by",
                    "status_changed_by"
                ).prefetch_related(
                    Prefetch(
                        "status_history",
                        queryset=LegStatus.objects.order_by('-timestamp').select_related('updated_by')
                    )
                )
            ),
            Prefetch("payments", queryset=Payment.objects.order_by('-created_at')),
            Prefetch(
                "refund_requests",
                queryset=RefundRequest.objects.select_related('requested_by', 'processed_by').prefetch_related('legs').order_by('-requested_at')
            ),
        ).select_related(
            "customer",
            "vehicle",
            "rate",
            "travel_agent",
            "travel_agent__user",
            "created_by",
            "modified_by"
        ),
        uuid=id,
    )

    # Get all drivers for assignment dropdown
    drivers = Driver.objects.select_related("profile").all()

    # Calculate payment details using prefetched data (no extra queries)
    payments = reservation.payments.all()
    latest_payment = payments[0] if payments else None
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
        "total_legs": len(reservation.legs.all()),
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


def _build_history_with_deltas(model_class, historical_records, foreign_keys_are_objs=True):
    """
    Attach history_delta_changes to each historical record (except the first),
    using the same logic as django-simple-history's SimpleHistoryAdmin.
    historical_records should be ordered by -history_date (newest first).
    """
    previous = None
    for current in historical_records:
        if previous is None:
            previous = current
            continue
        delta = previous.diff_against(current, foreign_keys_are_objs=foreign_keys_are_objs)
        helper = HistoricalRecordContextHelper(model_class, previous)
        previous.history_delta_changes = helper.context_for_delta_changes(delta)
        previous = current
    return list(historical_records)


@login_required(login_url="login")
def reservation_history(request, id):
    """
    Full audit log for a reservation (same data as admin History, in app view).
    """
    if not request.user.is_staff:
        return redirect("home")

    reservation = get_object_or_404(Reservation, uuid=id)
    history_manager = get_history_manager_for_model(Reservation)

    historical = list(
        history_manager.filter(uuid=reservation.uuid)
        .select_related("history_user")
        .order_by("-history_date")
    )
    _build_history_with_deltas(Reservation, historical)

    context = {
        "reservation": reservation,
        "history_records": historical,
        "page_title": f"Reservation history — {reservation}",
    }
    return render(request, "dispatching/reservation_history.html", context)


@login_required(login_url="login")
def leg_history(request, id):
    """
    Full audit log for a leg (same data as admin History, in app view).
    Used from reservation view and All Legs.
    """
    if not request.user.is_staff:
        return redirect("home")

    leg = get_object_or_404(
        Leg.objects.select_related("reservation"),
        id=id,
    )
    history_manager = get_history_manager_for_model(Leg)
    pk_attr = leg._meta.pk.attname
    pk_value = getattr(leg, pk_attr)

    historical = list(
        history_manager.filter(**{pk_attr: pk_value})
        .select_related("history_user")
        .order_by("-history_date")
    )
    _build_history_with_deltas(Leg, historical)

    context = {
        "leg": leg,
        "reservation": leg.reservation,
        "history_records": historical,
        "page_title": f"Leg history — {leg.pickup_location} → {leg.dropoff_location}",
    }
    return render(request, "dispatching/leg_history.html", context)


@login_required(login_url="login")
def leg_history_partial(request, id):
    """
    Returns only the history table HTML for use in a modal (AJAX).
    Used by All Legs page.
    """
    if not request.user.is_staff:
        return HttpResponse(status=403)

    leg = get_object_or_404(
        Leg.objects.select_related("reservation"),
        id=id,
    )
    history_manager = get_history_manager_for_model(Leg)
    pk_attr = leg._meta.pk.attname
    pk_value = getattr(leg, pk_attr)

    historical = list(
        history_manager.filter(**{pk_attr: pk_value})
        .select_related("history_user")
        .order_by("-history_date")
    )
    _build_history_with_deltas(Leg, historical)

    context = {
        "leg": leg,
        "reservation": leg.reservation,
        "history_records": historical,
    }
    return render(request, "dispatching/leg_history_partial.html", context)


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
    trip_type_stats = {"arrival": 0, "return": 0, "cruise": 0, "other": 0}
    for leg in page_obj:
        trip_type = leg.get_trip_type()
        trip_type_stats[trip_type] = trip_type_stats.get(trip_type, 0) + 1

    # Calculate current page statistics in a single pass
    current_page_stats = {
        "arrival": 0,
        "return": 0,
        "cruise": 0,
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

    # Annotate each leg with estimated cleared time and duration
    from dispatching.scheduler import estimate_job_end_time
    for leg in page_obj:
        try:
            end_dt = estimate_job_end_time(leg, leg.pickup_date)
            pickup_dt = datetime.combine(leg.pickup_date, leg.pickup_time)
            dur_mins = int((end_dt - pickup_dt).total_seconds() // 60)
            leg.cleared_time = end_dt.strftime('%I:%M %p').lstrip('0')
            hrs, mins = divmod(dur_mins, 60)
            if hrs > 0 and mins > 0:
                leg.duration_display = f"{hrs} hr {mins} mins"
            elif hrs > 0:
                leg.duration_display = f"{hrs} hr"
            else:
                leg.duration_display = f"{mins} mins"
        except Exception:
            leg.cleared_time = None
            leg.duration_display = None

        # Actual cleared time from status history (if completed)
        leg.actual_cleared_time = None
        leg.actual_duration_display = None
        if leg.status == 'completed':
            for sh in leg.status_history.all():
                if sh.status == 'completed':
                    actual_dt = timezone.localtime(sh.timestamp)
                    leg.actual_cleared_time = actual_dt.strftime('%I:%M %p').lstrip('0')
                    actual_dur = int((actual_dt.replace(tzinfo=None) - pickup_dt).total_seconds() // 60)
                    if actual_dur > 0:
                        ah, am = divmod(actual_dur, 60)
                        if ah > 0 and am > 0:
                            leg.actual_duration_display = f"{ah} hr {am} mins"
                        elif ah > 0:
                            leg.actual_duration_display = f"{ah} hr"
                        else:
                            leg.actual_duration_display = f"{am} mins"
                    break

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
        
        # Prevent driver assignment to cancelled reservations or cancelled legs
        if field == "driver" and leg.reservation.status == 'cancelled':
            logger.warning(f"Attempted to assign driver to cancelled reservation {leg.reservation.id}")
            return JsonResponse({
                "success": False,
                "error": "Cannot assign driver to a cancelled reservation"
            }, status=400)

        if field == "driver" and leg.status == 'cancelled':
            logger.warning(f"Attempted to assign driver to cancelled leg {leg.id}")
            return JsonResponse({
                "success": False,
                "error": "Cannot assign driver to a cancelled leg"
            }, status=400)

        # Check for pending refund warning (don't block, just warn)
        pending_refund_warning = None
        if field == "driver" and value:
            has_pending = RefundRequest.objects.filter(
                reservation=leg.reservation,
                status__in=['requested', 'processing', 'approved'],
            ).exists()
            if has_pending:
                pending_refund_warning = "Warning: This reservation has a pending refund request."

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
                    cache.delete(f"capacity_planner_{leg.pickup_date.isoformat()}")
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
                cache.delete(f"capacity_planner_{leg.pickup_date.isoformat()}")
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
                    "cancelled",
                ]
                if value in valid_statuses:
                    leg.status = value
                    # Track who changed the status and when
                    leg.status_changed_by = request.user
                    leg.status_changed_at = timezone.now()
                    leg.save()
                    logger.info(f"Updated leg {leg_id} status to {value} by {request.user.username}")

                    # Create a LegStatus entry to track this status change
                    from reservations.models import LegStatus
                    LegStatus.objects.create(
                        leg=leg,
                        status=value,
                        updated_by=request.user,
                        timestamp=timezone.now()
                    )
                    logger.info(f"Created LegStatus entry for leg {leg_id} with status {value}")

                    # Check if reservation should be auto-completed
                    if value == "completed":
                        reservation_updated = leg.reservation.check_and_update_completion_status()
                        if reservation_updated:
                            logger.info(f"Auto-completed reservation {leg.reservation.id} - all legs completed")

                        # Incrementally update route timing metrics
                        try:
                            from dispatching.analytics import update_single_route_timing_metric
                            update_single_route_timing_metric(leg)
                        except Exception as e:
                            logger.warning(f"Failed to update route metrics for leg {leg_id}: {e}")
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

        response_data = {"success": True}
        if pending_refund_warning:
            response_data["warning"] = pending_refund_warning
        return JsonResponse(response_data)

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
def copy_vehicle_assignments(request):
    """Copy vehicle assignments from the most recent previous date to a target date."""
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    date_str = data.get("date")
    if not date_str:
        return JsonResponse({"success": False, "error": "Date required"}, status=400)

    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return JsonResponse({"success": False, "error": "Invalid date format"}, status=400)

    # Find the most recent previous date with assignments
    prev = (
        DriverVehicleAssignment.objects.filter(date__lt=target_date)
        .order_by("-date")
        .values_list("date", flat=True)
        .first()
    )
    if not prev:
        return JsonResponse({"success": False, "error": "No previous assignments found"})

    prev_assignments = DriverVehicleAssignment.objects.filter(date=prev).select_related("driver", "vehicle")
    copied = 0
    result_map = {}
    for a in prev_assignments:
        obj, created = DriverVehicleAssignment.objects.get_or_create(
            driver=a.driver, date=target_date,
            defaults={"vehicle": a.vehicle},
        )
        if not created:
            obj.vehicle = a.vehicle
            obj.save()
        copied += 1
        result_map[str(a.driver_id)] = a.vehicle_id

    return JsonResponse({
        "success": True,
        "copied": copied,
        "source_date": prev.strftime("%Y-%m-%d"),
        "assignments": result_map,
    })


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

                        # Send confirmation email after successful payment (non-blocking)
                        _run_in_background(send_reservation_confirmation, reservation)
                        logger.info(f"Confirmation email queued for dispatcher payment on reservation {reservation.uuid}")

                        # Send purchase event to Meta in background (matches webhook.py pattern)
                        import time as _time
                        event_id = f"{payment_intent.id}_{int(_time.time())}"
                        _run_in_background(send_purchase_event, reservation, value=None, event_id=event_id)

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
        flight_data = aeroapi.get_flight_data(flight_ident, flight_date=flight_date, trip_type=trip_type)

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


def _best_flight_arrival_time(flight):
    """
    Pick the best arrival time based on flight status.

    - Flight not yet departed (Scheduled, Filed, etc.): use scheduled time only,
      because estimated is just a FlightAware prediction before takeoff.
    - Flight en route / delayed / landed / arrived: use the best real-time data
      (actual > estimated > scheduled).
    """
    status = (flight.status or "").strip().lower()
    # Statuses that mean the flight has NOT departed yet
    not_departed = status in ("", "scheduled", "filed", "not yet departed")

    if not_departed:
        # Pre-departure: only use scheduled times
        return flight.scheduled_gate_arrival_local or flight.scheduled_arrival_local

    # In-air or post-arrival: use best available real-time data
    return (
        flight.actual_gate_arrival_local
        or flight.estimated_gate_arrival_local
        or flight.actual_arrival_local
        or flight.estimated_arrival_local
        or flight.scheduled_gate_arrival_local
        or flight.scheduled_arrival_local
    )


@login_required
@require_POST
def match_leg_time_to_flight(request):
    """
    Set a leg's pickup date/time to match the flight's best available arrival time.
    Uses scheduled time for pre-departure flights, real-time data for en-route/landed.
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
        leg = get_object_or_404(Leg, id=leg_id)
        if not leg.flight_information:
            return JsonResponse(
                {"success": False, "error": "Leg has no flight information"},
                status=400,
            )
        if leg.get_trip_type() != "arrival":
            return JsonResponse(
                {"success": False, "error": "Only arrival legs can be matched to flight time"},
                status=400,
            )
        flight = leg.flight_information
        flight_dt = _best_flight_arrival_time(flight)
        if not flight_dt:
            return JsonResponse(
                {"success": False, "error": "Flight has no scheduled arrival time (refresh flight data first)"},
                status=400,
            )
        if timezone.is_aware(flight_dt):
            flight_dt = timezone.make_naive(
                flight_dt, timezone.get_current_timezone()
            )
        new_time = flight_dt.time()
        Leg.objects.filter(id=leg.id).update(pickup_time=new_time)
        return JsonResponse({
            "success": True,
            "message": "Leg pickup time updated to match flight arrival",
            "pickup_time": new_time.strftime("%H:%M"),
        })
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)
    except Exception as e:
        logger.error(f"Error matching leg time to flight: {e}", exc_info=True)
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required
@require_POST
def match_all_leg_times_to_flight(request):
    """
    Set pickup date/time to flight's best available arrival time for all arrival legs on a date.
    Uses scheduled time for pre-departure flights, real-time data for en-route/landed.
    """
    if not request.user.is_staff:
        return JsonResponse(
            {"success": False, "error": "Permission denied"}, status=403
        )
    try:
        data = json.loads(request.body)
        date_str = data.get("date")
        if not date_str:
            return JsonResponse(
                {"success": False, "error": "Missing date (YYYY-MM-DD)"}, status=400
            )
        try:
            from datetime import datetime as dt
            target_date = dt.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return JsonResponse(
                {"success": False, "error": "Invalid date format (use YYYY-MM-DD)"},
                status=400,
            )
        legs = list(
            Leg.objects.filter(
                pickup_date=target_date,
                flight_information__isnull=False,
            ).select_related("flight_information")
        )
        arrival_legs = [leg for leg in legs if leg.get_trip_type() == "arrival"]
        updated = 0
        for leg in arrival_legs:
            flight = leg.flight_information
            flight_dt = _best_flight_arrival_time(flight)
            if not flight_dt:
                continue
            if timezone.is_aware(flight_dt):
                flight_dt = timezone.make_naive(
                    flight_dt, timezone.get_current_timezone()
                )
            new_time = flight_dt.time()
            if leg.pickup_time != new_time:
                Leg.objects.filter(id=leg.id).update(pickup_time=new_time)
                updated += 1
        return JsonResponse({
            "success": True,
            "message": f"Updated {updated} arrival leg(s) to match flight arrival time.",
            "updated_count": updated,
            "total_arrival_legs": len(arrival_legs),
        })
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)
    except Exception as e:
        logger.error(f"Error matching all leg times: {e}", exc_info=True)
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required
@require_http_methods(["GET", "POST"])
def confirmations_view(request):
    """
    Next-day confirmation SMS: preview legs for a date, export CSV, or send texts via Twilio.
    Intended to run after validating flights (Refresh Arrival Flights / Match All Flight Times).
    """
    from django.utils.dateparse import parse_date
    from .confirmation_sms import (
        get_legs_for_confirmation,
        leg_to_row,
        get_confirmation_message,
        export_confirmations_csv,
        send_confirmations_for_date,
        send_confirmation_via_twilio,
        twilio_configured,
    )

    tomorrow = timezone.localdate() + timedelta(days=1)
    selected_date = tomorrow
    if request.GET.get("date"):
        parsed = parse_date(request.GET["date"])
        if parsed:
            selected_date = parsed

    if request.method == "POST":
        action = request.POST.get("action")
        post_date = request.POST.get("date")
        target = parse_date(post_date) if post_date else selected_date
        if not target:
            messages.error(request, "Invalid date.")
            return redirect("confirmations")

        if action == "export_csv":
            csv_bytes = export_confirmations_csv(target)
            if not csv_bytes:
                messages.warning(request, f"No legs found for {target}.")
                return redirect("confirmations")
            response = HttpResponse(csv_bytes, content_type="text/csv; charset=utf-8")
            response["Content-Disposition"] = (
                f'attachment; filename="confirmations_{target}.csv"'
            )
            return response

        if action == "send_single":
            # Send confirmation for one leg only (e.g. updated time or guest didn't get text)
            leg_id = request.POST.get("leg_id")
            if not leg_id or not twilio_configured():
                if not twilio_configured():
                    messages.error(request, "Twilio is not configured.")
                else:
                    messages.error(request, "Invalid leg.")
                return redirect(reverse("confirmations") + f"?date={target}")
            try:
                leg = Leg.objects.select_related(
                    "reservation", "reservation__customer", "flight_information", "cruise_information"
                ).get(id=int(leg_id), pickup_date=target)
            except (ValueError, Leg.DoesNotExist):
                messages.error(request, "Leg not found.")
                return redirect(reverse("confirmations") + f"?date={target}")
            row = leg_to_row(leg)
            message = get_confirmation_message(leg, row)
            ok, err = send_confirmation_via_twilio(leg, row, message)
            if ok:
                leg.confirmation_sms_sent_at = timezone.now()
                leg.save(update_fields=["confirmation_sms_sent_at"])
                messages.success(request, f"Confirmation sent to {row.get('guest_name', 'guest')} for leg #{leg_id}.")
            else:
                messages.error(request, f"Failed to send: {err}")
            return redirect(reverse("confirmations") + f"?date={target}")

        if action == "send_sms":
            if not twilio_configured():
                messages.error(
                    request,
                    "Twilio is not configured. Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_PHONE_NUMBER in .env",
                )
                return redirect("confirmations")
            # Parse excluded leg IDs from the form
            excluded_raw = request.POST.get("excluded_leg_ids", "")
            excluded_ids = set()
            if excluded_raw.strip():
                for x in excluded_raw.split(","):
                    x = x.strip()
                    if x.isdigit():
                        excluded_ids.add(int(x))
            _run_in_background(
                send_confirmations_for_date,
                target,
                skip_already_sent=True,
                excluded_leg_ids=excluded_ids,
            )
            messages.success(request, f"Sending confirmations for {target} in the background. Refresh in a moment to see updated statuses.")
            return redirect(reverse("confirmations") + f"?date={target}")

    legs = get_legs_for_confirmation(selected_date)
    rows = []
    for leg in legs:
        row = leg_to_row(leg)
        row["message_preview"] = get_confirmation_message(leg, row)
        row["leg"] = leg
        row["already_sent"] = bool(getattr(leg, "confirmation_sms_sent_at", None))
        rows.append(row)

    legs_filter_url = reverse("dashboard") + f"?date={selected_date.isoformat()}"

    return render(
        request,
        "dispatching/confirmations.html",
        {
            "selected_date": selected_date,
            "rows": rows,
            "twilio_configured": twilio_configured(),
            "legs_filter_url": legs_filter_url,
        },
    )


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
        flight_data = aeroapi.get_flight_data(
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

        # Handle datetime fields - always update to clear old data from previous flights
        flight.scheduled_arrival_local = flight_data.get("scheduled_arrival_local")
        flight.estimated_arrival_local = flight_data.get("estimated_arrival_local")
        flight.scheduled_gate_arrival_local = flight_data.get("scheduled_gate_arrival_local")
        flight.estimated_gate_arrival_local = flight_data.get("estimated_gate_arrival_local")

        # Handle actual arrival times - always update to clear old data
        now = timezone.now()
        scheduled_arrival = flight.scheduled_arrival_local
        scheduled_gate_arrival = flight.scheduled_gate_arrival_local

        is_future_flight = False
        if scheduled_arrival and scheduled_arrival > now:
            is_future_flight = True
        elif scheduled_gate_arrival and scheduled_gate_arrival > now:
            is_future_flight = True

        if is_future_flight:
            # Clear actual times for future flights (prevents old data from showing)
            flight.actual_arrival_local = None
            flight.actual_gate_arrival_local = None
            logger.info(
                f"Cleared stale actual arrival times for future flight (leg {leg.id})"
            )
        else:
            # For past/current flights, always update (even if None to clear old data)
            flight.actual_arrival_local = flight_data.get("actual_runway_arrival_local")
            flight.actual_gate_arrival_local = flight_data.get("actual_gate_arrival_local")

        # Update terminal, gate, and baggage claim - always update to clear old data
        flight.terminal = flight_data.get("terminal") or ""
        flight.gate = flight_data.get("gate") or ""
        flight.baggage_claim = flight_data.get("baggage_claim") or ""

        flight.last_updated = flight_data.get("last_updated", timezone.now())
        flight.save()

        # NOTE: Pickup times are NOT auto-updated here.
        # Use "Match All Flight Times" or per-leg "Match" to update pickup times manually.

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
    from concurrent.futures import ThreadPoolExecutor, as_completed

    task_key = _flight_refresh_cache_key(task_id)
    timeout_seconds = 60 * 60
    started_at = timezone.now().isoformat()
    BATCH_SIZE = 5  # AeroAPI Standard: up to 5 queries/sec

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

        # Process in batches of 5 (5/sec limit) so 45 flights ≈ 9 batches ≈ ~10 sec
        for offset in range(0, total_legs, BATCH_SIZE):
            batch = legs[offset : offset + BATCH_SIZE]
            with ThreadPoolExecutor(max_workers=BATCH_SIZE) as executor:
                batch_results = list(executor.map(_refresh_single_flight, batch))
            results.extend(batch_results)
            success_count += sum(1 for r in batch_results if r.get("success"))
            failure_count += sum(1 for r in batch_results if not r.get("success"))
            processed = min(offset + BATCH_SIZE, total_legs)
            cache.set(
                task_key,
                {
                    "status": "running",
                    "total": total_legs,
                    "processed": processed,
                    "success_count": success_count,
                    "failure_count": failure_count,
                    "results": results,
                    "started_at": started_at,
                },
                timeout=timeout_seconds,
            )

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
        # Pre-populate from session if customer was already saved (back-button support)
        initial_data = {}
        if booking_data.get('customer_id'):
            try:
                existing = Customer.objects.get(id=booking_data['customer_id'])
                initial_data = {
                    'first_name': existing.first_name,
                    'last_name': existing.last_name,
                    'email': existing.email,
                    'phone_number': existing.phone_number,
                    'zipcode': existing.zipcode,
                }
            except Customer.DoesNotExist:
                pass
        form = DispatcherCustomerForm(initial=initial_data)

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
        # Pre-populate from session if data exists (back-button support)
        initial_data = booking_data.get('reservation_data', {})
        form = DispatcherReservationForm(initial=initial_data)

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
        # Pre-populate from session if data exists (back-button support)
        legs_initial = booking_data.get('legs_data', [{} for _ in range(num_legs)])
        flights_initial = booking_data.get('flights_data', [{} for _ in range(num_legs)])
        # Pad with empty dicts if fewer than num_legs
        while len(legs_initial) < num_legs:
            legs_initial.append({})
        while len(flights_initial) < num_legs:
            flights_initial.append({})
        leg_formset = DispatcherLegFormSet(prefix='legs', initial=legs_initial)
        flight_formset = DispatcherFlightFormSet(prefix='flights', initial=flights_initial)
    
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
            gratuity_amount = form.cleaned_data.get('gratuity_amount') or Decimal('0.00')
            total_price = form.cleaned_data['total_price']

            if base_price < 0:
                messages.error(request, "Base price cannot be negative.")
            elif total_price < 0:
                messages.error(request, "Total price cannot be negative.")
            else:
                # Save pricing data to session
                pricing_data = {
                    'manual_base_price': str(base_price),
                    'additional_charges': str(additional_charges),
                    'gratuity_option': form.cleaned_data.get('gratuity_option', 'none'),
                    'gratuity_amount': str(gratuity_amount),
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
        gratuity_amount = Decimal(pricing_data.get('gratuity_amount', '0'))
        total_price = Decimal(pricing_data.get('total_price', '0'))
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid pricing values: {str(e)}")

    # Determine gratuity percentage (only set if 20% option was selected)
    gratuity_option = pricing_data.get('gratuity_option', 'none')
    gratuity_percentage = Decimal('20') if gratuity_option == '20' else None

    # Build special_requests — always append gratuity note to reservation
    special_requests = reservation_data.get('special_requests', '')
    num_legs = len(legs_data)
    if gratuity_amount > 0:
        gratuity_note = f"20% Gratuity Included (${gratuity_amount:.2f})"
        if special_requests:
            special_requests += f"\n{gratuity_note}"
        else:
            special_requests = gratuity_note

    # Calculate per-leg gratuity for multi-leg trips (split into each leg's notes)
    gratuity_per_leg = Decimal('0')
    if gratuity_amount > 0 and num_legs > 1:
        gratuity_per_leg = (gratuity_amount / num_legs).quantize(Decimal('0.01'))

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
            special_requests=special_requests,
            need_carseats=reservation_data.get('need_carseats') == 'True',
            rf_carseats=int(reservation_data.get('rf_carseats', 0)),
            ff_carseats=int(reservation_data.get('ff_carseats', 0)),
            booster_seats=int(reservation_data.get('booster_seats', 0)),
            base_price=base_price,
            additional_charges=additional_charges,
            gratuity_amount=gratuity_amount,
            gratuity_percentage=gratuity_percentage,
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
            
            # Build private_notes — append gratuity split for multi-leg trips
            private_notes = leg_data.get('private_notes', '')
            if gratuity_per_leg > 0:
                gratuity_note = f"${gratuity_per_leg:.2f} Gratuity Included"
                private_notes = f"{private_notes}\n{gratuity_note}".strip() if private_notes else gratuity_note

            leg = Leg.objects.create(
                reservation=reservation,
                flight_information=flight,
                pickup_date=pickup_date,
                pickup_time=pickup_time,
                pickup_location=leg_data.get('pickup_location', ''),
                dropoff_location=leg_data.get('dropoff_location', ''),
                private_notes=private_notes,
                driver_pay_amount=driver_pay_amount
            )

        # Recalculate revenue_share for all legs now that the full count is known.
        # Legs created earlier in the loop got revenue_share = total_price (count=1);
        # this corrects them all to total_price / num_legs.
        reservation.recalculate_leg_revenue_shares()

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
    parts = query.split()
    if len(parts) >= 2:
        # Multi-word: try first+last name combo AND individual word matches
        first_part = parts[0]
        last_part = " ".join(parts[1:])
        search_conditions = (
            Q(first_name__icontains=first_part, last_name__icontains=last_part)
            | Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(email__icontains=query)
        )
    else:
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
        
        # Recalculate revenue_share for all legs now that there is one more leg
        reservation.recalculate_leg_revenue_shares()
        
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
    completed_leg_count = 0

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
                        "cruise_information",
                    )
                    .prefetch_related(
                        Prefetch("reservation__payments", queryset=Payment.objects.order_by('-created_at')),
                    )
                    .filter(driver=selected_driver, payment_status='unpaid')
                    .order_by("pickup_date", "pickup_time")
                )
            
            # Calculate total pay amounts (use new fields if available)
            total_pay = sum(leg.total_driver_pay for leg in legs)
            # Calculate total pay for completed legs only
            completed_legs = [leg for leg in legs if leg.status == 'completed']
            total_pay_completed = sum(leg.total_driver_pay for leg in completed_legs)
            completed_leg_count = len(completed_legs)
            
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
        "completed_leg_count": completed_leg_count,
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
        
        # Support both old format (driver_pay_amount) and new format (separate fields)
        driver_pay_amount = data.get("driver_pay_amount")
        driver_base_pay = data.get("driver_base_pay")
        driver_gratuity = data.get("driver_gratuity")
        driver_additional = data.get("driver_additional")

        if not leg_id:
            return JsonResponse({"success": False, "error": "Missing leg ID"}, status=400)
        
        # Get the leg
        leg = get_object_or_404(Leg, id=leg_id)
        
        try:
            from decimal import Decimal
            
            # If new format is provided, use it; otherwise fall back to old format
            if driver_base_pay is not None or driver_gratuity is not None or driver_additional is not None:
                # New format: separate fields
                base_pay = Decimal(str(driver_base_pay or 0))
                gratuity = Decimal(str(driver_gratuity or 0))
                additional = Decimal(str(driver_additional or 0))
                
                # Validate amounts
                if base_pay < 0 or gratuity < 0 or additional < 0:
                    return JsonResponse({"success": False, "error": "Amounts cannot be negative"}, status=400)
                if base_pay > Decimal('9999.99') or gratuity > Decimal('9999.99') or additional > Decimal('9999.99'):
                    return JsonResponse({"success": False, "error": "Amounts cannot exceed $9999.99"}, status=400)
                
                # Update the leg with new fields
                leg.driver_base_pay = base_pay.quantize(Decimal("0.01"))
                leg.driver_gratuity = gratuity.quantize(Decimal("0.01"))
                leg.driver_additional = additional.quantize(Decimal("0.01"))
                
                # Update total for backward compatibility
                leg.driver_pay_amount = (base_pay + gratuity + additional).quantize(Decimal("0.01"))
                
                leg.save(update_fields=['driver_base_pay', 'driver_gratuity', 'driver_additional', 'driver_pay_amount', 'profit_estimate'])

                logger.info(f"Updated driver pay for leg {leg_id}: Base=${base_pay}, Gratuity=${gratuity}, Additional=${additional}, Total=${leg.driver_pay_amount}")
                
                return JsonResponse({
                    "success": True,
                    "message": "Driver pay updated successfully",
                    "driver_base_pay": float(leg.driver_base_pay),
                    "driver_gratuity": float(leg.driver_gratuity),
                    "driver_additional": float(leg.driver_additional),
                    "total": float(leg.driver_pay_amount),
                })
            else:
                # Old format: single driver_pay_amount field
                if driver_pay_amount is None or driver_pay_amount == "":
                    driver_pay_amount = 0
                
                amount_decimal = Decimal(str(driver_pay_amount))
                
                # Check for reasonable limits
                if amount_decimal < 0:
                    return JsonResponse({"success": False, "error": "Amount cannot be negative"}, status=400)
                if amount_decimal > Decimal('9999.99'):
                    return JsonResponse({"success": False, "error": "Amount too large (max $9999.99)"}, status=400)
                
                # Update the driver pay amount (legacy field)
                leg.driver_pay_amount = amount_decimal
                leg.save(update_fields=['driver_pay_amount', 'profit_estimate'])

                logger.info(f"Updated driver pay amount for leg {leg_id} to {amount_decimal}")
                
                return JsonResponse({
                    "success": True,
                    "message": "Driver pay amount updated successfully",
                    "new_amount": float(leg.driver_pay_amount),
                })
                
        except (ValueError, TypeError) as e:
            return JsonResponse({"success": False, "error": f"Invalid amount format: {str(e)}"}, status=400)
        
        # Update reservation profit calculations if needed
        try:
            leg.reservation.update_profit_calculations()
        except Exception as e:
            logger.warning(f"Could not update reservation profit calculations: {e}")

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
        send_email = bool(data.get("send_email"))
        
        if not driver_id:
            return JsonResponse({"success": False, "error": "Missing driver ID"}, status=400)
        
        driver = get_object_or_404(Driver, id=driver_id)

        # Validate email ahead of payment processing if requested
        recipient_email = None
        if send_email:
            if not driver.profile or not driver.profile.email:
                return JsonResponse({
                    "success": False,
                    "error": "Driver does not have an email on file"
                }, status=400)
            recipient_email = driver.profile.email
            try:
                from django.core.validators import validate_email
                from django.core.exceptions import ValidationError
                validate_email(recipient_email)
            except ValidationError:
                return JsonResponse({
                    "success": False,
                    "error": "Driver email on file is invalid"
                }, status=400)
        
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
        
        email_sent = False
        email_error = None
        if send_email and recipient_email:
            try:
                from users.emails import send_driver_payment_statement
                email_sent = send_driver_payment_statement(
                    driver=driver,
                    payment=payment,
                    legs=list(unpaid_legs),
                    recipient_email=recipient_email,
                )
                if not email_sent:
                    email_error = "Unable to send statement email"
            except Exception as e:
                logger.error(f"Error sending driver payment statement: {str(e)}", exc_info=True)
                email_error = "Error sending statement email"

        return JsonResponse({
            "success": True,
            "message": f"Payment processed successfully for {unpaid_legs.count()} leg(s). Total: ${payment_total:.2f}",
            "payment_id": payment.id,
            "legs_processed": unpaid_legs.count(),
            "total_amount": float(payment_total),
            "email_sent": email_sent,
            "email_error": email_error,
        })
        
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON data"}, status=400)
    except Exception as e:
        logger.error(f"Error processing driver payment: {str(e)}", exc_info=True)
        return JsonResponse({"success": False, "error": f"Server error: {str(e)}"}, status=500)


# ── Driver Pay Rates ─────────────────────────────────────────────────


@login_required(login_url="login")
def driver_pay_rates(request):
    """Pay rates management page — inhouse defaults + per-driver rates."""
    if not request.user.is_staff:
        return redirect("home")

    from drivers.models import Driver, DriverPayRate
    from rates.models import Route, Vehicle

    selected_driver_id = request.GET.get("driver")
    selected_driver = None
    driver_rates = []

    drivers = Driver.objects.select_related("profile").order_by(
        "driver_type", "profile__first_name"
    )
    routes = Route.objects.select_related("origin", "destination").order_by("id")
    vehicles = Vehicle.objects.order_by("capacity")

    if selected_driver_id:
        try:
            selected_driver = Driver.objects.select_related("profile").get(
                id=selected_driver_id
            )
            driver_rates = DriverPayRate.objects.filter(
                driver=selected_driver
            ).select_related("route__origin", "route__destination", "vehicle").order_by(
                "route__id", "direction", "vehicle__vehicle_type"
            )
        except Driver.DoesNotExist:
            pass

    # Build JSON map of existing rates for grid pre-fill: "routeId-vehicleId-direction" -> base_pay
    existing_rates_map = {}
    for rate in driver_rates:
        vid = str(rate.vehicle_id) if rate.vehicle_id else "all"
        key = f"{rate.route_id}-{vid}-{rate.direction}"
        existing_rates_map[key] = str(rate.base_pay)

    # Group rates by route for collapsed display
    from collections import OrderedDict

    grouped_rates = OrderedDict()
    for rate in driver_rates:
        route_key = rate.route_id
        if route_key not in grouped_rates:
            grouped_rates[route_key] = {
                "route": rate.route,
                "rates": [],
            }
        grouped_rates[route_key]["rates"].append(rate)

    context = {
        "drivers": drivers,
        "selected_driver": selected_driver,
        "selected_driver_id": selected_driver_id,
        "driver_rates": driver_rates,
        "grouped_rates": list(grouped_rates.values()),
        "routes": routes,
        "vehicles": vehicles,
        "existing_rates_json": json.dumps(existing_rates_map),
    }
    return render(request, "dispatching/driver_pay_rates.html", context)


@login_required(login_url="login")
@require_http_methods(["POST"])
def update_pay_rate(request):
    """Create or update a DriverPayRate via AJAX."""
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

    from drivers.models import Driver, DriverPayRate
    from rates.models import Route, Vehicle

    try:
        data = json.loads(request.body)
        driver_id = data.get("driver_id")
        route_id = data.get("route_id")
        vehicle_id = data.get("vehicle_id")  # None = all vehicles
        direction = data.get("direction", "both")
        if direction not in ("both", "forward", "reverse"):
            direction = "both"
        base_pay = data.get("base_pay")

        if not driver_id or not route_id or base_pay is None:
            return JsonResponse(
                {"success": False, "error": "Missing required fields"}, status=400
            )

        driver = Driver.objects.get(id=driver_id)
        route = Route.objects.get(id=route_id)
        vehicle = Vehicle.objects.get(id=vehicle_id) if vehicle_id else None

        rate, created = DriverPayRate.objects.update_or_create(
            driver=driver,
            route=route,
            vehicle=vehicle,
            direction=direction,
            defaults={"base_pay": base_pay},
        )

        return JsonResponse({
            "success": True,
            "rate_id": rate.id,
            "created": created,
            "base_pay": float(rate.base_pay),
        })

    except (Driver.DoesNotExist, Route.DoesNotExist, Vehicle.DoesNotExist):
        return JsonResponse(
            {"success": False, "error": "Driver, route, or vehicle not found"},
            status=404,
        )
    except Exception as e:
        return JsonResponse(
            {"success": False, "error": str(e)}, status=500
        )


@login_required(login_url="login")
@require_http_methods(["POST"])
def bulk_update_pay_rates(request):
    """Create or update multiple DriverPayRates in a single request."""
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

    from drivers.models import Driver, DriverPayRate
    from rates.models import Route, Vehicle

    try:
        data = json.loads(request.body)
        driver_id = data.get("driver_id")
        rates_list = data.get("rates", [])

        if not driver_id or not rates_list:
            return JsonResponse(
                {"success": False, "error": "Missing driver_id or rates"}, status=400
            )

        driver = Driver.objects.get(id=driver_id)

        # Pre-fetch all vehicles and routes in one query each
        vehicle_ids = {r["vehicle_id"] for r in rates_list if r.get("vehicle_id")}
        route_ids = {r["route_id"] for r in rates_list if r.get("route_id")}

        vehicles_map = {str(v.id): v for v in Vehicle.objects.filter(id__in=vehicle_ids)}
        routes_map = {str(r.id): r for r in Route.objects.filter(id__in=route_ids)}

        saved = 0
        errors = []
        for item in rates_list:
            route_id = str(item.get("route_id", ""))
            vehicle_id = str(item.get("vehicle_id", ""))
            direction = item.get("direction", "both")
            base_pay = item.get("base_pay")

            if direction not in ("both", "forward", "reverse"):
                direction = "both"

            route = routes_map.get(route_id)
            vehicle = vehicles_map.get(vehicle_id)

            if not route:
                errors.append(f"Route {route_id} not found")
                continue

            DriverPayRate.objects.update_or_create(
                driver=driver,
                route=route,
                vehicle=vehicle,
                direction=direction,
                defaults={"base_pay": base_pay},
            )
            saved += 1

        return JsonResponse({"success": True, "saved": saved, "errors": errors})

    except Driver.DoesNotExist:
        return JsonResponse(
            {"success": False, "error": "Driver not found"}, status=404
        )
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required(login_url="login")
@require_http_methods(["POST"])
def delete_pay_rate(request):
    """Delete a DriverPayRate via AJAX."""
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

    from drivers.models import DriverPayRate

    try:
        data = json.loads(request.body)
        rate_id = data.get("rate_id")
        if not rate_id:
            return JsonResponse(
                {"success": False, "error": "Missing rate_id"}, status=400
            )
        DriverPayRate.objects.filter(id=rate_id).delete()
        return JsonResponse({"success": True})
    except Exception as e:
        return JsonResponse(
            {"success": False, "error": str(e)}, status=500
        )


@login_required(login_url="login")
@require_http_methods(["POST"])
def update_inhouse_default_rate(request):
    """Update Route.inhouse_base_pay via AJAX."""
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

    from rates.models import Route

    try:
        data = json.loads(request.body)
        route_id = data.get("route_id")
        base_pay = data.get("base_pay")

        if not route_id:
            return JsonResponse(
                {"success": False, "error": "Missing route_id"}, status=400
            )

        route = Route.objects.get(id=route_id)
        if base_pay is None or base_pay == "":
            route.inhouse_base_pay = None
        else:
            route.inhouse_base_pay = base_pay
        route.save()

        return JsonResponse({
            "success": True,
            "base_pay": float(route.inhouse_base_pay) if route.inhouse_base_pay else None,
        })

    except Route.DoesNotExist:
        return JsonResponse(
            {"success": False, "error": "Route not found"}, status=404
        )
    except Exception as e:
        return JsonResponse(
            {"success": False, "error": str(e)}, status=500
        )


@login_required(login_url="login")
@require_http_methods(["POST"])
def update_night_bonus(request):
    """Update Driver.night_bonus via AJAX."""
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

    from drivers.models import Driver

    try:
        data = json.loads(request.body)
        driver_id = data.get("driver_id")
        night_bonus = data.get("night_bonus")

        if not driver_id or night_bonus is None:
            return JsonResponse(
                {"success": False, "error": "Missing fields"}, status=400
            )

        driver = Driver.objects.get(id=driver_id)
        driver.night_bonus = night_bonus
        driver.save(update_fields=["night_bonus"])

        return JsonResponse({
            "success": True,
            "night_bonus": float(driver.night_bonus),
        })

    except Driver.DoesNotExist:
        return JsonResponse(
            {"success": False, "error": "Driver not found"}, status=404
        )
    except Exception as e:
        return JsonResponse(
            {"success": False, "error": str(e)}, status=500
        )


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

        # Recalculate revenue_share for remaining legs (one fewer leg changes each share)
        try:
            reservation.recalculate_leg_revenue_shares()
        except Exception as e:
            logger.warning(f"Could not recalculate leg revenue shares after leg deletion: {e}")

        # Update reservation-level profit calculations
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
    Creates a RefundRequest record with policy-calculated suggestion.
    Supports three refund types: price_adjustment, partial_cancellation, full_cancellation.
    Also syncs flat refund_* fields on Reservation for backward compat.
    """
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

    try:
        data = json.loads(request.body)
        reservation_uuid = data.get("reservation_uuid")
        refund_reason = data.get("refund_reason", "").strip()
        refund_amount = data.get("refund_amount")
        refund_type = data.get("refund_type", "full_cancellation")
        leg_ids = data.get("leg_ids", [])

        if not reservation_uuid:
            return JsonResponse({"success": False, "error": "Missing reservation UUID"}, status=400)

        if not refund_reason:
            return JsonResponse({"success": False, "error": "Refund reason is required"}, status=400)

        if refund_type not in ('price_adjustment', 'partial_cancellation', 'full_cancellation'):
            return JsonResponse({"success": False, "error": "Invalid refund type"}, status=400)

        reservation = get_object_or_404(Reservation, uuid=reservation_uuid)

        # Check if a completed refund already exists (full cancellation) — no new full refunds
        if reservation.refund_status == 'completed' and refund_type == 'full_cancellation':
            return JsonResponse({
                "success": False,
                "error": "A full refund has already been completed for this reservation."
            }, status=400)

        # Non-superusers can't create new requests if one is already pending
        active_requests = RefundRequest.objects.filter(
            reservation=reservation,
            status__in=['requested', 'processing', 'approved'],
        )
        if active_requests.exists() and not request.user.is_superuser:
            return JsonResponse({
                "success": False,
                "error": "An active refund request already exists for this reservation."
            }, status=400)

        # Validate leg_ids belong to this reservation
        if leg_ids:
            valid_leg_ids = set(reservation.legs.values_list('id', flat=True))
            invalid = set(leg_ids) - valid_leg_ids
            if invalid:
                return JsonResponse({"success": False, "error": f"Invalid leg IDs: {list(invalid)}"}, status=400)

        # Calculate policy suggestion
        from reservations.refund_policy import calculate_refund_suggestion
        suggestion = calculate_refund_suggestion(reservation, leg_ids if leg_ids else None)
        suggested_amount = suggestion['total_suggested']

        # Validate refund amount
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
            refund_amount = suggested_amount if suggested_amount > 0 else (max_refund if max_refund > 0 else reservation.total_price)

        policy_override = refund_amount != suggested_amount

        # Create RefundRequest record
        refund_request = RefundRequest.objects.create(
            reservation=reservation,
            refund_type=refund_type,
            status='requested',
            amount=refund_amount,
            suggested_amount=suggested_amount,
            policy_override=policy_override,
            reason=refund_reason,
            requested_by=request.user,
        )

        # Attach specific legs and unassign their drivers
        if leg_ids:
            refund_request.legs.set(leg_ids)
            # Unassign drivers from legs being refunded
            affected_legs = Leg.objects.filter(id__in=leg_ids, driver__isnull=False)
            dates_to_invalidate = set()
            for leg in affected_legs:
                dates_to_invalidate.add(leg.pickup_date.isoformat())
                leg.driver = None
                leg.save(update_fields=['driver'])
            for date_str in dates_to_invalidate:
                cache.delete(f"capacity_planner_{date_str}")
        elif refund_type == 'full_cancellation':
            refund_request.legs.set(reservation.legs.all())
            # Unassign drivers from all legs
            dates_to_invalidate = set()
            for leg in reservation.legs.filter(driver__isnull=False):
                dates_to_invalidate.add(leg.pickup_date.isoformat())
                leg.driver = None
                leg.save(update_fields=['driver'])
            for date_str in dates_to_invalidate:
                cache.delete(f"capacity_planner_{date_str}")

        # Sync flat fields on Reservation for backward compat
        reservation.refund_status = 'requested'
        reservation.refund_requested_by = request.user
        reservation.refund_requested_at = timezone.now()
        reservation.refund_reason = refund_reason
        reservation.refund_amount = refund_amount
        reservation.save()

        # Send email notification to admin (background)
        from users.emails import send_refund_request_notification
        send_refund_request_notification(refund_request)

        logger.info(f"Refund requested for reservation {reservation.id} by {request.user.username} (type: {refund_type})")

        return JsonResponse({
            "success": True,
            "message": "Refund request submitted successfully. Admin will review and process it.",
            "refund_request_id": refund_request.id,
            "suggested_amount": str(suggested_amount),
            "policy_override": policy_override,
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
    Now queries RefundRequest model instead of flat Reservation fields.
    """
    if not request.user.is_superuser:
        messages.error(request, "You don't have permission to access this page.")
        return redirect("dashboard")

    status_filter = request.GET.get("status", "")

    base_qs = RefundRequest.objects.select_related(
        'reservation',
        'reservation__customer',
        'requested_by',
        'processed_by',
    ).prefetch_related('legs')

    if status_filter:
        refund_requests = base_qs.filter(status=status_filter).order_by('-requested_at')
    else:
        refund_requests = base_qs.filter(
            status__in=['requested', 'processing', 'approved']
        ).order_by('-requested_at')

    status_counts = {
        'requested': RefundRequest.objects.filter(status='requested').count(),
        'processing': RefundRequest.objects.filter(status='processing').count(),
        'approved': RefundRequest.objects.filter(status='approved').count(),
        'completed': RefundRequest.objects.filter(status='completed').count(),
        'rejected': RefundRequest.objects.filter(status='rejected').count(),
    }

    context = {
        'refund_requests': refund_requests,
        'status_filter': status_filter,
        'status_counts': status_counts,
    }

    return render(request, "dispatching/refund_management.html", context)


def _process_stripe_refund(reservation, refund_amount):
    """
    Helper: process Stripe refund across paid payments. Returns (refunded_amount, errors, stripe_ids).
    """
    paid_payments = reservation.payments.filter(status='paid').order_by('-created_at')
    refunded_amount = Decimal('0.00')
    refund_errors = []
    stripe_ids = []

    for payment in paid_payments:
        if refunded_amount >= refund_amount:
            break

        remaining_to_refund = refund_amount - refunded_amount
        amount_to_refund = min(remaining_to_refund, payment.amount)

        try:
            if not payment.stripe_payment_intent_id:
                refund_errors.append(f"Payment #{payment.id} has no Stripe payment intent ID")
                continue

            refund = stripe.Refund.create(
                payment_intent=payment.stripe_payment_intent_id,
                amount=int(amount_to_refund * 100),
                reason='requested_by_customer',
            )

            refunded_amount += amount_to_refund
            stripe_ids.append(refund.id)

            payment.refunded_amount = (payment.refunded_amount or Decimal('0.00')) + amount_to_refund
            payment.stripe_refund_id = refund.id
            if payment.refunded_amount >= payment.amount:
                payment.status = 'refunded'
            payment.save()
            logger.info(f"Refunded ${amount_to_refund} for payment {payment.id} via Stripe.")

        except stripe.error.StripeError as e:
            refund_errors.append(f"Stripe error for payment #{payment.id}: {str(e)}")
            logger.error(f"Stripe refund error: {e}")
        except Exception as e:
            refund_errors.append(f"Error processing payment #{payment.id}: {str(e)}")
            logger.error(f"Refund processing error: {e}")

    return refunded_amount, refund_errors, stripe_ids


@login_required
@require_POST
def process_refund(request):
    """
    Admin can approve or reject a RefundRequest.
    Branches logic by refund_type:
      - PRICE_ADJUSTMENT: Stripe refund only, no cancellations
      - PARTIAL_CANCELLATION: Stripe refund + cancel selected legs
      - FULL_CANCELLATION: Stripe refund + cancel all legs + reservation
    Also syncs flat refund_* fields on Reservation for backward compat.
    """
    if not request.user.is_superuser:
        return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

    try:
        data = json.loads(request.body)
        refund_request_id = data.get("refund_request_id")
        # Backward compat: also accept reservation_uuid for legacy callers
        reservation_uuid = data.get("reservation_uuid")
        action = data.get("action")  # 'approve', 'reject'
        refund_notes = data.get("refund_notes", "").strip()

        if not action or action not in ['approve', 'reject']:
            return JsonResponse({"success": False, "error": "Invalid action. Use 'approve' or 'reject'."}, status=400)

        # Get the RefundRequest
        if refund_request_id:
            rr = get_object_or_404(RefundRequest, id=refund_request_id)
        elif reservation_uuid:
            # Backward compat: find the latest active RefundRequest for this reservation
            reservation = get_object_or_404(Reservation, uuid=reservation_uuid)
            rr = RefundRequest.objects.filter(
                reservation=reservation,
                status__in=['requested', 'processing', 'approved'],
            ).order_by('-requested_at').first()
            if not rr:
                return JsonResponse({"success": False, "error": "No active refund request found"}, status=400)
        else:
            return JsonResponse({"success": False, "error": "Missing refund_request_id or reservation_uuid"}, status=400)

        reservation = rr.reservation

        # Allow admin to override refund_type before processing
        new_refund_type = data.get("refund_type")
        if new_refund_type and new_refund_type in ('price_adjustment', 'partial_cancellation', 'full_cancellation'):
            rr.refund_type = new_refund_type
            rr.save(update_fields=['refund_type'])

        # ── REJECT ──
        if action == 'reject':
            rr.status = 'rejected'
            rr.processed_by = request.user
            rr.processed_at = timezone.now()
            rr.notes = refund_notes
            rr.save()

            # Sync flat fields
            reservation.refund_status = 'rejected'
            reservation.refund_processed_by = request.user
            reservation.refund_processed_at = timezone.now()
            reservation.refund_notes = refund_notes
            reservation.save()

            logger.info(f"Refund #{rr.id} rejected for reservation {reservation.id} by {request.user.username}")
            return JsonResponse({"success": True, "message": "Refund request rejected."})

        # ── APPROVE ──
        refund_amount = rr.amount
        if not refund_amount or refund_amount <= 0:
            return JsonResponse({"success": False, "error": "No refund amount set"}, status=400)

        # Process Stripe refund
        refunded_amount, refund_errors, stripe_ids = _process_stripe_refund(reservation, refund_amount)

        if refund_errors and refunded_amount == 0:
            return JsonResponse({
                "success": False,
                "error": f"Failed to process refund: {'; '.join(refund_errors)}"
            }, status=500)

        # Store Stripe IDs on RefundRequest
        rr.stripe_refund_ids = stripe_ids

        # Branch by refund type
        dates_to_invalidate = set()

        if rr.refund_type == 'price_adjustment':
            # Just refund money, no cancellations
            rr.status = 'completed'
            rr.processed_by = request.user
            rr.processed_at = timezone.now()
            rr.notes = refund_notes
            rr.save()

        elif rr.refund_type == 'partial_cancellation':
            # Cancel selected legs, keep reservation active
            legs_to_cancel = rr.legs.all()
            for leg in legs_to_cancel:
                dates_to_invalidate.add(leg.pickup_date.isoformat())
                leg.status = 'cancelled'
                leg.payment_status = 'canceled'
                leg.driver = None
                leg.save()

            rr.status = 'completed'
            rr.processed_by = request.user
            rr.processed_at = timezone.now()
            rr.notes = refund_notes
            rr.save()

            # If ALL legs are now cancelled, cancel the reservation too
            active_legs = reservation.legs.exclude(status='cancelled')
            if not active_legs.exists():
                reservation.status = 'cancelled'

        elif rr.refund_type == 'full_cancellation':
            # Cancel all legs + reservation
            for leg in reservation.legs.all():
                dates_to_invalidate.add(leg.pickup_date.isoformat())
                if leg.status != 'cancelled':
                    leg.status = 'cancelled'
                    leg.payment_status = 'canceled'
                    leg.driver = None
                    leg.save()

            reservation.status = 'cancelled'

            rr.status = 'completed'
            rr.processed_by = request.user
            rr.processed_at = timezone.now()
            rr.notes = refund_notes
            rr.save()

        # Sync flat fields on Reservation
        reservation.refund_status = 'completed'
        reservation.refund_processed_by = request.user
        reservation.refund_processed_at = timezone.now()
        reservation.refund_notes = refund_notes
        if refund_errors:
            reservation.refund_notes = (refund_notes or "") + f"\n\nRefund processing notes: {'; '.join(refund_errors)}"
        reservation.save()

        # Invalidate capacity planner cache for affected dates
        for date_str in dates_to_invalidate:
            cache.delete(f"capacity_planner_{date_str}")

        logger.info(
            f"Refund #{rr.id} ({rr.refund_type}) processed for reservation {reservation.id} "
            f"by {request.user.username}. Amount: ${refunded_amount}"
        )

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


@login_required
@require_POST
def refund_suggestion(request):
    """
    API endpoint: return policy-calculated refund suggestion for given reservation + leg_ids.
    Used by frontend to show tier breakdown before submitting a refund request.
    """
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

    try:
        data = json.loads(request.body)
        reservation = get_object_or_404(Reservation, uuid=data.get('reservation_uuid'))
        leg_ids = data.get('leg_ids')

        from reservations.refund_policy import calculate_refund_suggestion
        suggestion = calculate_refund_suggestion(reservation, leg_ids)

        return JsonResponse({
            'success': True,
            'total_suggested': str(suggestion['total_suggested']),
            'has_zero_refund_legs': suggestion['has_zero_refund_legs'],
            'leg_details': [
                {
                    'leg_id': d['leg_id'],
                    'refund_percentage': d['refund_percentage'],
                    'suggested_amount': str(d['suggested_amount']),
                    'revenue_share': str(d['revenue_share']),
                    'tier': d['tier'],
                    'pickup_location': d['pickup_location'],
                    'dropoff_location': d['dropoff_location'],
                }
                for d in suggestion['leg_details']
            ],
        })
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON data"}, status=400)
    except Exception as e:
        logger.error(f"Error calculating refund suggestion: {str(e)}", exc_info=True)
        return JsonResponse({"success": False, "error": f"Server error: {str(e)}"}, status=500)


@login_required
def analytics_dashboard(request):
    """
    Analytics dashboard showing key operational metrics.

    This dashboard focuses on metrics that DON'T require LegStatus timestamps,
    so it works immediately with historical data.

    Shows:
    - Demand patterns (peak hours, trip type distribution)
    - Driver utilization (legs per day, revenue)
    - In-house vs affiliate coverage
    - Revenue trends
    """
    from datetime import datetime, timedelta
    from django.db.models import Count, Sum, Q, Avg
    from reservations.models import Leg, DemandPattern, DriverDailyCapacity, RouteTimingMetric
    from drivers.models import Driver
    from dispatching.analytics import categorize_location

    # Date range selection (default: last 30 days)
    days_back = int(request.GET.get('days', 30))
    end_date = timezone.now().date()
    start_date = end_date - timedelta(days=days_back)

    # Get all completed legs in date range — evaluate once as a list
    # to avoid re-executing the queryset on each Python loop
    legs_list = list(
        Leg.objects.filter(
            pickup_date__gte=start_date,
            pickup_date__lte=end_date,
            status='completed'
        )
        .exclude(reservation__status="cancelled")
        .exclude(status="cancelled")
        .select_related('driver', 'driver__profile', 'reservation')
    )

    # Overall statistics
    total_legs = len(legs_list)
    total_revenue = sum((leg.revenue_share or Decimal('0.00')) for leg in legs_list)

    # Single pass through legs to compute all metrics at once
    trip_type_counts = {}
    route_data = {}
    driver_stats = {}
    hourly_demand = {hour: {'arrival': 0, 'return': 0, 'cruise': 0, 'other': 0, 'total': 0} for hour in range(24)}
    inhouse_count = 0
    affiliate_count = 0

    for leg in legs_list:
        trip_type = leg.get_trip_type()
        revenue = leg.revenue_share or Decimal('0.00')

        # Trip type breakdown
        trip_type_counts[trip_type] = trip_type_counts.get(trip_type, 0) + 1

        # Driver type breakdown
        if leg.driver:
            if leg.driver.driver_type == 'inhouse':
                inhouse_count += 1
            else:
                affiliate_count += 1

            # Driver performance
            driver_id = leg.driver.id
            if driver_id not in driver_stats:
                driver_stats[driver_id] = {
                    'name': str(leg.driver),
                    'driver_type': leg.driver.driver_type,
                    'legs': 0,
                    'revenue': Decimal('0.00'),
                    'days_worked': set()
                }
            driver_stats[driver_id]['legs'] += 1
            driver_stats[driver_id]['revenue'] += revenue
            driver_stats[driver_id]['days_worked'].add(leg.pickup_date)

        # Top routes
        pickup_cat = categorize_location(leg.pickup_location)
        dropoff_cat = categorize_location(leg.dropoff_location)
        route_key = f"{pickup_cat} → {dropoff_cat}"
        if route_key not in route_data:
            route_data[route_key] = {'count': 0, 'revenue': Decimal('0.00'), 'trip_type': trip_type}
        route_data[route_key]['count'] += 1
        route_data[route_key]['revenue'] += revenue

        # Hourly demand
        hour = leg.pickup_time.hour
        hourly_demand[hour][trip_type] += 1
        hourly_demand[hour]['total'] += 1

    inhouse_percentage = (inhouse_count / total_legs * 100) if total_legs > 0 else 0

    # Sort routes by volume
    top_routes = sorted(route_data.items(), key=lambda x: x[1]['count'], reverse=True)[:10]

    # Calculate average legs per day for each driver
    for driver_id, stats in driver_stats.items():
        days_count = len(stats['days_worked'])
        stats['avg_legs_per_day'] = stats['legs'] / days_count if days_count > 0 else 0
        stats['days_worked'] = days_count

    # Sort drivers by total legs
    top_drivers = sorted(driver_stats.values(), key=lambda x: x['legs'], reverse=True)[:10]

    # Find peak hours
    peak_hours = sorted(hourly_demand.items(), key=lambda x: x[1]['total'], reverse=True)[:5]

    # Daily trends (last 7 days) — single annotated query instead of 7 × 4 queries
    daily_trend_start = end_date - timedelta(days=6)
    daily_trends_qs = (
        Leg.objects.filter(
            pickup_date__gte=daily_trend_start,
            pickup_date__lte=end_date,
            status='completed'
        )
        .values('pickup_date')
        .annotate(
            total=Count('id'),
            revenue=Sum('revenue_share'),
            inhouse=Count('id', filter=Q(driver__driver_type='inhouse')),
            affiliate=Count('id', filter=Q(driver__driver_type='affiliate')),
        )
    )
    daily_trends = {}
    for entry in daily_trends_qs:
        daily_trends[entry['pickup_date']] = {
            'total': entry['total'],
            'revenue': entry['revenue'] or Decimal('0.00'),
            'inhouse': entry['inhouse'],
            'affiliate': entry['affiliate'],
        }
    # Fill in dates with no data
    for i in range(7):
        date = end_date - timedelta(days=i)
        if date not in daily_trends:
            daily_trends[date] = {'total': 0, 'revenue': Decimal('0.00'), 'inhouse': 0, 'affiliate': 0}
    daily_trends = dict(sorted(daily_trends.items(), reverse=True))

    # Route timing metrics (show what we have, even if limited)
    timing_metrics = RouteTimingMetric.objects.all()[:20]  # Top 20 routes with data

    # Top route timing data for quick reference section
    top_route_timing = list(
        RouteTimingMetric.objects.filter(sample_count__gte=3)
        .order_by('-sample_count')[:5]
        .values(
            'pickup_location_category', 'dropoff_location_category',
            'trip_type', 'sample_count',
            'avg_drive_time', 'median_drive_time', 'p75_drive_time',
            'avg_airport_dwell_time', 'median_airport_dwell_time', 'p75_airport_dwell_time',
            'median_total_time', 'p75_total_time',
        )
    )
    for rt in top_route_timing:
        sc = rt['sample_count']
        if sc >= 20:
            rt['confidence'] = 'high'
            rt['confidence_label'] = 'High'
        elif sc >= 10:
            rt['confidence'] = 'medium'
            rt['confidence_label'] = 'Medium'
        else:
            rt['confidence'] = 'low'
            rt['confidence_label'] = 'Low'

    # Calculate max hourly demand for chart scaling
    max_hourly_demand = max([hour_data['total'] for hour_data in hourly_demand.values()]) if hourly_demand else 1

    # Calculate average daily volume
    avg_daily_volume = round(total_legs / days_back, 1) if days_back > 0 else 0

    context = {
        'days_back': days_back,
        'start_date': start_date,
        'end_date': end_date,
        'total_legs': total_legs,
        'total_revenue': total_revenue,
        'avg_daily_volume': avg_daily_volume,
        'trip_type_counts': trip_type_counts,
        'inhouse_count': inhouse_count,
        'affiliate_count': affiliate_count,
        'inhouse_percentage': round(inhouse_percentage, 1),
        'top_routes': top_routes,
        'top_drivers': top_drivers,
        'hourly_demand': hourly_demand,
        'max_hourly_demand': max_hourly_demand,
        'peak_hours': peak_hours,
        'daily_trends': daily_trends,
        'timing_metrics': timing_metrics,
        'top_route_timing': top_route_timing,
    }

    return render(request, 'dispatching/analytics_dashboard.html', context)


@login_required(login_url="login")
def capacity_planner(request):
    """
    Daily Capacity Planner: helps dispatchers schedule drivers for a specific date.
    Shows driver timelines, unassigned jobs with suggestions, batching opportunities.
    """
    if not request.user.is_staff:
        return redirect("dashboard")

    from datetime import timedelta
    from django.db.models import Prefetch
    from reservations.models import Leg, LegStatus
    from drivers.models import Driver
    from dispatching.scheduler import (
        build_driver_schedules,
        suggest_assignments,
        get_coverage_stats,
        preload_timing_cache,
        clear_timing_cache,
        estimate_job_end_time,
    )

    # Preload route timing metrics into memory (1 query instead of ~1400)
    preload_timing_cache()

    # Date selection (default: today)
    selected_date_str = request.GET.get("date")
    try:
        selected_date = (
            datetime.strptime(selected_date_str, "%Y-%m-%d").date()
            if selected_date_str
            else timezone.localdate()
        )
    except (ValueError, TypeError):
        selected_date = timezone.localdate()

    prev_date = selected_date - timedelta(days=1)
    next_date = selected_date + timedelta(days=1)
    today = timezone.localdate()

    # Query all legs for the selected date
    legs = (
        Leg.objects.filter(pickup_date=selected_date)
        .exclude(reservation__status='cancelled')
        .exclude(status='cancelled')
        .select_related(
            "reservation",
            "reservation__customer",
            "reservation__vehicle",
            "driver",
            "driver__profile",
            "flight_information",
            "cruise_information",
        )
        .prefetch_related(
            Prefetch(
                "status_history",
                queryset=LegStatus.objects.order_by('-timestamp').select_related('updated_by')
            ),
        )
        .order_by("pickup_time")
    )
    legs_list = list(legs)

    # Get drivers
    inhouse_drivers = (
        Driver.objects.filter(driver_type="inhouse")
        .select_related("profile")
        .prefetch_related("weekly_schedule")
        .order_by("profile__first_name")
    )
    all_drivers = Driver.objects.select_related("profile").all()

    # Vehicle assignments for this date
    inhouse_assignments = DriverVehicleAssignment.objects.filter(
        date=selected_date, driver__in=inhouse_drivers
    ).select_related("driver", "driver__profile", "vehicle", "vehicle__vehicle_type")
    assignment_map = {a.driver_id: a for a in inhouse_assignments}
    eligible_driver_ids = set(assignment_map.keys())
    eligible_drivers = [d for d in inhouse_drivers if d.id in eligible_driver_ids]

    # Fleet vehicles for quick-assign panel
    inhouse_vehicles = FleetVehicle.objects.select_related("vehicle_type").all().order_by("vehicle_number")
    vehicle_assign_rows = [
        {"driver": d, "assignment": assignment_map.get(d.id)}
        for d in inhouse_drivers
    ]
    # Sort: assigned drivers first (by vehicle number), then unassigned
    vehicle_assign_rows.sort(
        key=lambda r: (
            r["assignment"] is None,
            r["assignment"].vehicle.vehicle_number if r["assignment"] and r["assignment"].vehicle else "",
        )
    )

    # Heavy scheduling computation — cache for 60s keyed by date.
    # LocMemCache (single worker) stores Python objects directly; no serialization needed.
    # Suggestions reference leg IDs, not ORM instances, so cached results are safe to reuse.
    _sched_cache_key = f"capacity_planner_{selected_date.isoformat()}"
    _cached_sched = cache.get(_sched_cache_key)

    _unassigned_legs = [leg for leg in legs_list if leg.driver is None]

    if _cached_sched is not None:
        driver_schedules, suggestions, coverage = _cached_sched
    else:
        driver_schedules = build_driver_schedules(legs_list, all_drivers, selected_date)
        _inhouse_for_suggestions = {did: s for did, s in driver_schedules.items() if s.driver_type == 'inhouse'}
        suggestions = suggest_assignments(_unassigned_legs, _inhouse_for_suggestions, selected_date)
        coverage = get_coverage_stats(legs_list)
        cache.set(_sched_cache_key, (driver_schedules, suggestions, coverage), 60)

    inhouse_schedules = {
        did: sched for did, sched in driver_schedules.items()
        if sched.driver_type == 'inhouse'
    }

    # Annotate legs with estimated cleared time and duration (runs every request — fast, prefetched)
    for leg in legs_list:
        try:
            end_dt = estimate_job_end_time(leg, selected_date)
            pickup_dt = datetime.combine(selected_date, leg.pickup_time)
            dur_mins = int((end_dt - pickup_dt).total_seconds() // 60)
            leg.cleared_time = end_dt.strftime('%I:%M %p').lstrip('0')
            hrs, mins = divmod(dur_mins, 60)
            if hrs > 0 and mins > 0:
                leg.duration_display = f"{hrs} hr {mins} mins"
            elif hrs > 0:
                leg.duration_display = f"{hrs} hr"
            else:
                leg.duration_display = f"{mins} mins"
        except Exception:
            leg.cleared_time = None
            leg.duration_display = None

        # Actual cleared time from status history (if completed)
        leg.actual_cleared_time = None
        leg.actual_duration_display = None
        if leg.status == 'completed':
            for sh in leg.status_history.all():
                if sh.status == 'completed':
                    actual_dt = timezone.localtime(sh.timestamp)
                    leg.actual_cleared_time = actual_dt.strftime('%I:%M %p').lstrip('0')
                    pickup_dt = datetime.combine(selected_date, leg.pickup_time)
                    actual_dur = int((actual_dt.replace(tzinfo=None) - pickup_dt).total_seconds() // 60)
                    if actual_dur > 0:
                        ah, am = divmod(actual_dur, 60)
                        if ah > 0 and am > 0:
                            leg.actual_duration_display = f"{ah} hr {am} mins"
                        elif ah > 0:
                            leg.actual_duration_display = f"{ah} hr"
                        else:
                            leg.actual_duration_display = f"{am} mins"
                    break

    suggestion_map = {s.leg_id: s for s in suggestions}

    # Group legs by hour
    legs_by_hour = {}
    for leg in legs_list:
        h = leg.pickup_time.hour
        legs_by_hour.setdefault(h, []).append(leg)

    # Timeline display range
    hours_with_legs = list(legs_by_hour.keys())
    display_start = min(hours_with_legs) if hours_with_legs else 6
    display_end = max(hours_with_legs) + 1 if hours_with_legs else 22
    display_start = min(display_start, 6)
    display_end = max(display_end, 22)
    timeline_hours = list(range(display_start, display_end + 1))
    total_display_minutes = (display_end - display_start + 1) * 60

    # Build in-house timeline data — only drivers with vehicles assigned for the day
    inhouse_timeline = []
    for driver in eligible_drivers:
        sched = driver_schedules.get(driver.id)
        if not sched:
            continue

        # Calculate position/width percentages for each slot
        for slot in sched.slots:
            slot_start_min = (slot.pickup_time.hour - display_start) * 60 + slot.pickup_time.minute
            slot_end_min = (slot.estimated_end_time.hour - display_start) * 60 + slot.estimated_end_time.minute
            duration = max(slot_end_min - slot_start_min, 15)

            slot.position_pct = round(max(0, slot_start_min / total_display_minutes * 100), 1)
            slot.width_pct = round(min(duration / total_display_minutes * 100, 100 - slot.position_pct), 1)

        # Calculate end-time marker positions for each slot
        for slot in sched.slots:
            end_min = (slot.estimated_end_time.hour - display_start) * 60 + slot.estimated_end_time.minute
            slot.end_position_pct = round(max(0, end_min / total_display_minutes * 100), 1)
            slot.end_time_display = slot.estimated_end_time.strftime('%I:%M').lstrip('0')

        # Calculate gaps between consecutive slots
        gaps = []
        for i in range(len(sched.slots) - 1):
            cur_end = sched.slots[i].estimated_end_time
            nxt_start = datetime.combine(selected_date, sched.slots[i + 1].pickup_time)
            gap_min = int((nxt_start - cur_end).total_seconds() / 60)
            # Gap bar position/width
            end_min = (cur_end.hour - display_start) * 60 + cur_end.minute
            start_min = (sched.slots[i + 1].pickup_time.hour - display_start) * 60 + sched.slots[i + 1].pickup_time.minute
            gap_pos = round(max(0, end_min / total_display_minutes * 100), 1)
            gap_width = round(max(0, (start_min - end_min) / total_display_minutes * 100), 1)
            if gap_min >= 60:
                gh, gm = divmod(gap_min, 60)
                gap_display = f"{gh}h,{gm}m" if gm else f"{gh}h"
            else:
                gap_display = f"{gap_min}m"
            gaps.append({
                'after_leg': sched.slots[i].leg_id,
                'before_leg': sched.slots[i + 1].leg_id,
                'gap_minutes': gap_min,
                'gap_display': gap_display,
                'is_tight': gap_min < 20,
                'is_critical': gap_min < 10,
                'position_pct': gap_pos,
                'width_pct': gap_width,
            })

        inhouse_timeline.append({
            'driver': driver,
            'schedule': sched,
            'gaps': gaps,
            'total_legs': sched.total_legs,
            'total_revenue': sched.total_revenue,
        })

    # Build per-driver availability for the selected date (for auto-assign modal defaults)
    driver_availability = {}
    for d in eligible_drivers:
        is_avail, start_h, end_h, pref = d.get_availability_for_date(selected_date)
        driver_availability[d.id] = {
            "is_available": is_avail,
            "start_hour": start_h,
            "end_hour": end_h,
            "preference": pref,
        }

    context = {
        'selected_date': selected_date,
        'prev_date': prev_date,
        'next_date': next_date,
        'today': today,
        'is_today': selected_date == today,
        'is_past': selected_date < today,
        'legs': legs_list,
        'total_legs': len(legs_list),
        'unassigned_legs': _unassigned_legs,
        'suggestion_map': suggestion_map,
        'inhouse_timeline': inhouse_timeline,
        'coverage': coverage,
        'legs_by_hour': legs_by_hour,
        'timeline_hours': timeline_hours,
        'display_start': display_start,
        'display_end': display_end,
        'inhouse_drivers': list(inhouse_drivers),
        'eligible_drivers': eligible_drivers,
        'inhouse_vehicles': inhouse_vehicles,
        'vehicle_assign_rows': vehicle_assign_rows,
        'driver_availability_json': json.dumps(driver_availability),
    }

    clear_timing_cache()
    return render(request, 'dispatching/daily_capacity_planner.html', context)


def _create_schedule_snapshot(target_date, user, trigger):
    """Save current driver assignments for a date. Returns the snapshot or None if nothing to save."""
    from reservations.models import ScheduleSnapshot, ScheduleSnapshotEntry

    assigned_legs = Leg.objects.filter(
        pickup_date=target_date, driver__isnull=False
    ).select_related('driver', 'driver_assigned_by')

    if not assigned_legs.exists():
        return None

    snapshot = ScheduleSnapshot.objects.create(
        schedule_date=target_date,
        created_by=user,
        trigger=trigger,
        assigned_count=assigned_legs.count(),
    )

    entries = [
        ScheduleSnapshotEntry(
            snapshot=snapshot,
            leg=leg,
            driver=leg.driver,
            driver_assigned_by=leg.driver_assigned_by,
            driver_assigned_at=leg.driver_assigned_at,
        )
        for leg in assigned_legs
    ]
    ScheduleSnapshotEntry.objects.bulk_create(entries)
    return snapshot


@login_required
def auto_assign_drivers(request):
    """
    Auto-assign inhouse drivers to unassigned legs for a given date.
    Two modes controlled by `apply` flag:
      - apply=False (default): Preview — run suggestions, build proposed schedules, return without saving.
      - apply=True: Apply — run suggestions and save assignments to DB.
    """
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    if request.method != "POST":
        return JsonResponse({"success": False, "error": "POST required"}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    date_str = data.get("date")
    if not date_str:
        return JsonResponse({"success": False, "error": "Date required"}, status=400)
    apply_mode = data.get("apply", False)
    raw_driver_hours = data.get("driver_hours", {})  # {driver_id_str: {start: int, end: int}}
    excluded_leg_ids = data.get("excluded_leg_ids", [])  # legs to skip
    raw_manual = data.get("manual_assignments", {})  # {leg_id_str: driver_id} overrides
    raw_preferences = data.get("driver_preferences", {})  # {driver_id_str: "prefer_arrival"}

    from datetime import datetime as dt
    from dispatching.scheduler import (
        build_driver_schedules, suggest_assignments,
        ScheduleSlot, estimate_job_end_time,
    )
    from dispatching.analytics import categorize_location
    from copy import deepcopy
    from decimal import Decimal

    try:
        target_date = dt.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return JsonResponse({"success": False, "error": "Invalid date format"}, status=400)

    # Get all legs for this date (exclude cancelled reservations)
    legs = list(
        Leg.objects.filter(pickup_date=target_date)
        .exclude(reservation__status="cancelled")
        .exclude(status="cancelled")
        .select_related("driver", "driver__profile", "reservation", "reservation__vehicle",
                        "reservation__customer")
    )

    # Get inhouse drivers with vehicle assignments for this date
    eligible_driver_ids = set(
        DriverVehicleAssignment.objects.filter(
            date=target_date, driver__driver_type="inhouse"
        ).values_list("driver_id", flat=True)
    )
    inhouse_drivers = list(
        Driver.objects.filter(driver_type="inhouse", id__in=eligible_driver_ids)
        .select_related("profile")
    )
    schedules = build_driver_schedules(legs, inhouse_drivers, target_date)

    # Parse per-driver time windows: {driver_id: (start_hour, end_hour)}
    driver_hours = {}
    for did_str, hours in raw_driver_hours.items():
        try:
            driver_hours[int(did_str)] = (int(hours["start"]), int(hours["end"]))
        except (ValueError, KeyError, TypeError):
            continue

    # Parse per-driver trip preferences: {driver_id: "prefer_arrival"}
    driver_preferences = {}
    for did_str, pref in raw_preferences.items():
        try:
            if pref:
                driver_preferences[int(did_str)] = str(pref)
        except (ValueError, TypeError):
            continue

    # Parse manual assignments: {leg_id: driver_id}
    manual_assignments = {}
    for lid_str, did in raw_manual.items():
        try:
            manual_assignments[int(lid_str)] = int(did)
        except (ValueError, TypeError):
            continue

    legs_by_id = {l.id: l for l in legs}
    drivers_by_id = {d.id: d for d in inhouse_drivers}

    # Get unassigned legs (excluding user-excluded ones)
    excluded_set = set(excluded_leg_ids)
    unassigned = [l for l in legs if not l.driver and l.id not in excluded_set]

    # Separate manually-assigned legs from auto-assign pool
    manual_leg_ids = set(manual_assignments.keys())
    auto_unassigned = [l for l in unassigned if l.id not in manual_leg_ids]

    # Run suggestion engine on remaining unassigned legs
    suggestions = suggest_assignments(auto_unassigned, schedules, target_date,
                                      driver_hours=driver_hours or None,
                                      driver_preferences=driver_preferences or None) if auto_unassigned else []

    # Merge: auto suggestions + manual overrides
    valid_suggestions = [
        s for s in suggestions
        if s.suggested_driver_id and legs_by_id.get(s.leg_id) and drivers_by_id.get(s.suggested_driver_id)
    ]
    # Build final assignment map: {leg_id: driver_id}
    final_assignments = {}
    for s in valid_suggestions:
        final_assignments[s.leg_id] = s.suggested_driver_id
    for lid, did in manual_assignments.items():
        if legs_by_id.get(lid) and drivers_by_id.get(did):
            final_assignments[lid] = did

    assigned_count = len(final_assignments)
    remaining = len(unassigned) - assigned_count

    if apply_mode:
        # ── Apply mode: save assignments to DB ──
        _create_schedule_snapshot(target_date, request.user, 'before_auto_assign')

        saved = 0
        for lid, did in final_assignments.items():
            leg = legs_by_id[lid]
            driver = drivers_by_id[did]
            try:
                leg.driver = driver
                leg.driver_assigned_by = request.user
                leg.driver_assigned_at = timezone.now()
                leg.save()
                saved += 1
            except Exception:
                continue

        cache.delete(f"capacity_planner_{target_date.isoformat()}")
        return JsonResponse({
            "success": True,
            "assigned": saved,
            "remaining": len(unassigned) - saved,
            "message": f"Assigned {saved} legs to inhouse drivers.",
        })

    # ── Preview mode: build proposed schedules without saving ──
    proposed = deepcopy(schedules)
    new_leg_ids = set()

    # Helper to build a ScheduleSlot from a leg
    def _leg_to_slot(leg):
        pickup_cat = categorize_location(leg.pickup_location)
        dropoff_cat = categorize_location(leg.dropoff_location)
        end_time = estimate_job_end_time(leg, target_date)
        customer_name = ""
        if leg.reservation and leg.reservation.customer:
            customer_name = leg.reservation.customer.get_full_name()
        flight_info = None
        has_flight = False
        try:
            if leg.flight_information:
                has_flight = True
                flight_info = str(leg.flight_information)
        except Exception:
            pass
        return ScheduleSlot(
            leg_id=leg.id, pickup_time=leg.pickup_time,
            pickup_location=leg.pickup_location, pickup_category=pickup_cat,
            dropoff_location=leg.dropoff_location, dropoff_category=dropoff_cat,
            trip_type=leg.get_trip_type(), estimated_end_time=end_time,
            reservation_id=leg.reservation_id, customer_name=customer_name,
            status=leg.status or 'pending', has_flight=has_flight,
            flight_info=flight_info, revenue=leg.revenue_share,
        )

    for lid, did in final_assignments.items():
        leg = legs_by_id[lid]
        proposed[did].slots.append(_leg_to_slot(leg))
        new_leg_ids.add(lid)

    # Remove excluded legs from existing schedules
    if excluded_set:
        for sched in proposed.values():
            sched.slots = [s for s in sched.slots if s.leg_id not in excluded_set]

    # Serialize driver schedules
    driver_schedules = []
    for schedule in sorted(proposed.values(), key=lambda s: s.driver_name):
        schedule.slots.sort(key=lambda s: s.pickup_time)
        if not schedule.slots:
            continue

        first_pickup = schedule.slots[0].pickup_time.strftime("%I:%M %p").lstrip("0")
        last_end = schedule.slots[-1].estimated_end_time.strftime("%I:%M %p").lstrip("0") if schedule.slots[-1].estimated_end_time else ""

        slots_data = []
        for slot in schedule.slots:
            # Look up vehicle type and store stop from the actual leg
            vtype = ""
            has_store_stop = False
            leg_obj = legs_by_id.get(slot.leg_id)
            if leg_obj and leg_obj.reservation:
                if leg_obj.reservation.vehicle:
                    vtype = str(leg_obj.reservation.vehicle.vehicle_type).upper()
                if slot.trip_type == 'arrival':
                    has_store_stop = bool(getattr(leg_obj.reservation, 'store_stop', False))
            slots_data.append({
                "leg_id": slot.leg_id,
                "pickup_time": slot.pickup_time.strftime("%I:%M %p").lstrip("0"),
                "end_time": slot.estimated_end_time.strftime("%I:%M %p").lstrip("0") if slot.estimated_end_time else "",
                "pickup_location": slot.pickup_location,
                "dropoff_location": slot.dropoff_location,
                "trip_type": slot.trip_type,
                "customer_name": slot.customer_name,
                "revenue": float(slot.revenue or 0),
                "status": slot.status,
                "is_new": slot.leg_id in new_leg_ids,
                "flight_info": slot.flight_info or "",
                "pickup_minutes": slot.pickup_time.hour * 60 + slot.pickup_time.minute,
                "vehicle_type": vtype,
                "store_stop": has_store_stop,
            })

        driver_schedules.append({
            "driver_id": schedule.driver_id,
            "driver_name": schedule.driver_name,
            "total_legs": schedule.total_legs,
            "existing_legs": sum(1 for s in slots_data if not s["is_new"]),
            "new_legs": sum(1 for s in slots_data if s["is_new"]),
            "total_revenue": float(schedule.total_revenue),
            "first_pickup": first_pickup,
            "last_end": last_end,
            "slots": slots_data,
        })

    # Build unassigned legs list (not assigned by auto or manual)
    assigned_leg_ids = set(final_assignments.keys())
    still_unassigned = []
    for leg in unassigned:
        if leg.id in assigned_leg_ids:
            continue
        customer_name = ""
        if leg.reservation and leg.reservation.customer:
            customer_name = leg.reservation.customer.get_full_name()
        vtype = getattr(getattr(leg.reservation, 'vehicle', None), 'vehicle_type', '') if leg.reservation else ''
        trip_type = leg.get_trip_type()
        has_store_stop = bool(getattr(leg.reservation, 'store_stop', False)) if leg.reservation and trip_type == 'arrival' else False
        still_unassigned.append({
            "leg_id": leg.id,
            "pickup_time": leg.pickup_time.strftime("%I:%M %p").lstrip("0") if leg.pickup_time else "",
            "pickup_location": leg.pickup_location,
            "dropoff_location": leg.dropoff_location,
            "trip_type": leg.get_trip_type(),
            "customer_name": customer_name,
            "revenue": float(leg.revenue_share or 0),
            "vehicle_type": str(vtype),
            "pickup_minutes": leg.pickup_time.hour * 60 + leg.pickup_time.minute if leg.pickup_time else 0,
            "store_stop": has_store_stop,
        })

    # Driver list for manual assignment dropdown
    driver_list = [
        {"id": d.id, "name": str(d)}
        for d in sorted(inhouse_drivers, key=lambda d: str(d))
    ]

    return JsonResponse({
        "success": True,
        "assigned": assigned_count,
        "remaining": remaining,
        "total": len(legs),
        "driver_schedules": driver_schedules,
        "unassigned_legs": still_unassigned,
        "driver_list": driver_list,
    })


@login_required
def reset_schedule(request):
    """
    Reset all driver assignments for a given date.
    Sets driver=None on every leg for that day.
    """
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    if request.method != "POST":
        return JsonResponse({"success": False, "error": "POST required"}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    date_str = data.get("date")
    if not date_str:
        return JsonResponse({"success": False, "error": "Date required"}, status=400)

    from datetime import datetime as dt
    try:
        target_date = dt.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return JsonResponse({"success": False, "error": "Invalid date format"}, status=400)

    # Auto-snapshot before resetting
    snapshot = _create_schedule_snapshot(target_date, request.user, 'before_reset')

    legs = Leg.objects.filter(
        pickup_date=target_date, driver__isnull=False
    ).exclude(reservation__status="cancelled").exclude(status="cancelled")
    count = legs.count()
    legs.update(driver=None, driver_assigned_by=None, driver_assigned_at=None)

    # Invalidate capacity planner cache so it rebuilds with fresh data
    cache.delete(f"capacity_planner_{target_date.isoformat()}")

    msg = f"Unassigned {count} legs for {date_str}."
    if snapshot:
        msg += f" Snapshot saved ({snapshot.assigned_count} assignments) — you can restore anytime."

    return JsonResponse({
        "success": True,
        "reset_count": count,
        "snapshot_id": snapshot.id if snapshot else None,
        "message": msg,
    })


@login_required
def save_schedule_snapshot(request):
    """Manually save a snapshot of the current schedule for a date."""
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "POST required"}, status=405)

    data = json.loads(request.body)
    date_str = data.get("date")
    label = data.get("label", "")
    notes = data.get("notes", "")

    from datetime import datetime as dt
    try:
        target_date = dt.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return JsonResponse({"success": False, "error": "Invalid date"}, status=400)

    snapshot = _create_schedule_snapshot(target_date, request.user, 'manual')
    if snapshot:
        update_fields = []
        if label:
            snapshot.label = label
            update_fields.append('label')
        if notes:
            snapshot.notes = notes
            update_fields.append('notes')
        if update_fields:
            snapshot.save(update_fields=update_fields)
        return JsonResponse({
            "success": True,
            "snapshot_id": snapshot.id,
            "assigned_count": snapshot.assigned_count,
            "message": f"Snapshot saved with {snapshot.assigned_count} assignments.",
        })
    else:
        return JsonResponse({
            "success": False,
            "error": "No assigned legs to snapshot.",
        }, status=400)


@login_required
def list_schedule_snapshots(request):
    """List available snapshots for a date."""
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    date_str = request.GET.get("date")
    from datetime import datetime as dt
    try:
        target_date = dt.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return JsonResponse({"success": False, "error": "Invalid date"}, status=400)

    from reservations.models import ScheduleSnapshot
    snapshots = ScheduleSnapshot.objects.filter(
        schedule_date=target_date
    ).select_related('created_by')[:20]

    result = []
    for s in snapshots:
        local_time = timezone.localtime(s.created_at)
        result.append({
            "id": s.id,
            "created_at": local_time.strftime("%b %d, %I:%M %p").replace(" 0", " "),
            "trigger": s.trigger,
            "trigger_display": s.get_trigger_display(),
            "label": s.label,
            "notes": s.notes,
            "assigned_count": s.assigned_count,
            "created_by": str(s.created_by) if s.created_by else "System",
        })

    return JsonResponse({"success": True, "snapshots": result})


@login_required
def restore_schedule_snapshot(request):
    """Restore a schedule from a snapshot."""
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "POST required"}, status=405)

    data = json.loads(request.body)
    snapshot_id = data.get("snapshot_id")

    from reservations.models import ScheduleSnapshot, ScheduleSnapshotEntry
    try:
        snapshot = ScheduleSnapshot.objects.get(id=snapshot_id)
    except ScheduleSnapshot.DoesNotExist:
        return JsonResponse({"success": False, "error": "Snapshot not found"}, status=404)

    # Auto-save current state before restoring (so restore is also undoable)
    _create_schedule_snapshot(snapshot.schedule_date, request.user, 'before_reset')

    entries = snapshot.entries.select_related('driver', 'driver_assigned_by')

    # Build a map of leg_id -> assignment from the snapshot
    assignment_map = {}
    for entry in entries:
        assignment_map[entry.leg_id] = entry

    # Get all legs for this date
    all_legs = Leg.objects.filter(pickup_date=snapshot.schedule_date)

    restored = 0
    cleared = 0
    for leg in all_legs:
        entry = assignment_map.get(leg.id)
        if entry:
            # Restore saved assignment
            leg.driver = entry.driver
            leg.driver_assigned_by = entry.driver_assigned_by
            leg.driver_assigned_at = entry.driver_assigned_at
            leg.save(update_fields=['driver', 'driver_assigned_by', 'driver_assigned_at'])
            restored += 1
        elif leg.driver is not None:
            # This leg was unassigned in the snapshot, clear it
            leg.driver = None
            leg.driver_assigned_by = None
            leg.driver_assigned_at = None
            leg.save(update_fields=['driver', 'driver_assigned_by', 'driver_assigned_at'])
            cleared += 1

    # Invalidate capacity planner cache so it rebuilds with fresh data
    cache.delete(f"capacity_planner_{snapshot.schedule_date.isoformat()}")

    return JsonResponse({
        "success": True,
        "restored": restored,
        "cleared": cleared,
        "message": f"Restored {restored} assignments from snapshot. {cleared} legs cleared.",
    })


@login_required
def delete_schedule_snapshot(request):
    """Delete a snapshot."""
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "POST required"}, status=405)

    data = json.loads(request.body)
    snapshot_id = data.get("snapshot_id")

    from reservations.models import ScheduleSnapshot
    try:
        snapshot = ScheduleSnapshot.objects.get(id=snapshot_id)
        snapshot.delete()
        return JsonResponse({"success": True, "message": "Snapshot deleted."})
    except ScheduleSnapshot.DoesNotExist:
        return JsonResponse({"success": False, "error": "Snapshot not found"}, status=404)


@login_required
def smart_schedule_builder(request):
    """
    Build an optimal schedule for a specific driver with parameters:
    - driver_id: which driver
    - date: target date
    - start_hour / end_hour: working window
    - pinned_leg_ids: legs that MUST be included
    - preferred_trip_type: 'arrival', 'return', 'cruise', 'other', or '' (no preference)
    - apply: if true, actually save the assignments. If false, just preview.
    """
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "POST required"}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    from datetime import datetime as dt
    from dispatching.scheduler import build_driver_schedules, build_smart_schedule
    from drivers.models import Driver as DriverModel

    # Parse parameters
    driver_id = data.get("driver_id")
    date_str = data.get("date")
    start_hour = int(data.get("start_hour", 0))
    end_hour = int(data.get("end_hour", 23))
    pinned_leg_ids = data.get("pinned_leg_ids", [])
    preferred_trip_type = data.get("preferred_trip_type", "")
    excluded_leg_ids = data.get("excluded_leg_ids", [])
    apply_assignments = data.get("apply", False)

    if not driver_id or not date_str:
        return JsonResponse({"success": False, "error": "driver_id and date are required"}, status=400)

    try:
        target_date = dt.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return JsonResponse({"success": False, "error": "Invalid date format"}, status=400)

    try:
        driver = DriverModel.objects.get(id=driver_id)
    except DriverModel.DoesNotExist:
        return JsonResponse({"success": False, "error": "Driver not found"}, status=404)

    # Get all legs for this date (exclude cancelled reservations)
    legs = list(
        Leg.objects.filter(pickup_date=target_date)
        .exclude(reservation__status="cancelled")
        .exclude(status="cancelled")
        .select_related("driver", "driver__profile", "reservation", "reservation__customer", "reservation__vehicle")
    )

    # Build existing schedule for this driver (already assigned legs)
    all_drivers = DriverModel.objects.select_related("profile").all()
    schedules = build_driver_schedules(legs, all_drivers, target_date)
    existing_schedule = schedules.get(driver.id)

    # Get unassigned legs + excluded existing legs (so they can be swapped/replaced)
    available_legs = [l for l in legs if not l.driver or l.id in excluded_leg_ids]

    # Run the smart scheduler
    result = build_smart_schedule(
        driver_id=driver.id,
        driver_name=str(driver),
        available_legs=available_legs,
        target_date=target_date,
        start_hour=start_hour,
        end_hour=end_hour,
        pinned_leg_ids=pinned_leg_ids,
        preferred_trip_type=preferred_trip_type or None,
        existing_schedule=existing_schedule,
        excluded_leg_ids=excluded_leg_ids,
    )

    # Format response
    timing_details = result.get('slot_timing_details', {})
    leg_map = {l.id: l for l in legs}
    schedule_data = []
    scheduled_leg_ids = set()
    for slot in result['schedule']:
        scheduled_leg_ids.add(slot.leg_id)
        is_existing = existing_schedule and any(
            s.leg_id == slot.leg_id for s in existing_schedule.slots
        )
        slot_data = {
            'leg_id': slot.leg_id,
            'pickup_time': slot.pickup_time.strftime('%I:%M %p').lstrip('0'),
            'pickup_minutes': slot.pickup_time.hour * 60 + slot.pickup_time.minute,
            'cleared_time': slot.estimated_end_time.strftime('%I:%M %p').lstrip('0'),
            'duration_minutes': int((slot.estimated_end_time - datetime.combine(target_date, slot.pickup_time)).total_seconds() // 60),
            'pickup_location': slot.pickup_location[:50],
            'dropoff_location': slot.dropoff_location[:50],
            'trip_type': slot.trip_type,
            'customer_name': slot.customer_name,
            'revenue': float(slot.revenue) if slot.revenue else 0,
            'is_existing': is_existing,
        }
        # Add job details from the leg's reservation
        leg_obj = leg_map.get(slot.leg_id)
        if leg_obj and leg_obj.reservation:
            res = leg_obj.reservation
            veh = res.vehicle
            slot_data['vehicle_type'] = str(veh.vehicle_type).upper() if veh else ''
            slot_data['passengers'] = res.passenger_count or 0
            slot_data['luggage'] = res.luggage_count or 0
            cs_parts = []
            if res.need_carseats:
                if res.rf_carseats: cs_parts.append(f"{res.rf_carseats} RF")
                if res.ff_carseats: cs_parts.append(f"{res.ff_carseats} FF")
                if res.booster_seats: cs_parts.append(f"{res.booster_seats} Bstr")
            slot_data['carseats'] = ", ".join(cs_parts)
        # Add timing details for new slots
        if slot.leg_id in timing_details:
            td = timing_details[slot.leg_id]
            slot_data['timing'] = {
                'reasoning': td.get('reasoning', ''),
                'pickup_category': td.get('pickup_category', ''),
                'dropoff_category': td.get('dropoff_category', ''),
                'job_drive_time': td.get('job_drive_time'),
                'reposition_from': td.get('reposition_from'),
                'reposition_to': td.get('reposition_to'),
                'reposition_drive_time': td.get('reposition_drive_time'),
                'buffer_minutes': td.get('buffer_minutes'),
                'est_end_time': td.get('est_end_time', ''),
            }
        schedule_data.append(slot_data)

    # Build alternatives: unassigned legs NOT in the built schedule
    alternatives = []
    for leg_alt in available_legs:
        if leg_alt.id in scheduled_leg_ids or leg_alt.id in excluded_leg_ids:
            continue
        res = leg_alt.reservation
        veh = res.vehicle if res else None
        alt_cs = []
        if res and res.need_carseats:
            if res.rf_carseats: alt_cs.append(f"{res.rf_carseats} RF")
            if res.ff_carseats: alt_cs.append(f"{res.ff_carseats} FF")
            if res.booster_seats: alt_cs.append(f"{res.booster_seats} Bstr")
        alternatives.append({
            'leg_id': leg_alt.id,
            'pickup_time': leg_alt.pickup_time.strftime('%I:%M %p').lstrip('0'),
            'pickup_minutes': leg_alt.pickup_time.hour * 60 + leg_alt.pickup_time.minute,
            'trip_type': leg_alt.get_trip_type(),
            'vehicle_type': str(veh.vehicle_type).upper() if veh else '',
            'pickup_location': (leg_alt.pickup_location or '')[:40],
            'dropoff_location': (leg_alt.dropoff_location or '')[:40],
            'passengers': res.passenger_count if res else 0,
            'luggage': res.luggage_count if res else 0,
            'carseats': ", ".join(alt_cs),
            'revenue': float(leg_alt.revenue_share) if leg_alt.revenue_share else 0,
        })

    response = {
        'success': True,
        'driver_name': str(driver),
        'schedule': schedule_data,
        'alternatives': alternatives,
        'total_legs': result['total_legs'],
        'existing_count': result['existing_count'],
        'new_count': result['new_count'],
        'total_revenue': float(result['total_revenue']),
        'utilization_pct': result['utilization_pct'],
        'pinned_included': result['pinned_included'],
        'pinned_failed': result['pinned_failed'],
        'warnings': result['warnings'],
        'applied': False,
    }

    # If apply=true, save the new assignments
    if apply_assignments:
        assigned = 0
        new_leg_ids = [
            s.leg_id for s in result['schedule']
            if not (existing_schedule and any(es.leg_id == s.leg_id for es in existing_schedule.slots))
        ]
        for lid in new_leg_ids:
            try:
                leg = Leg.objects.get(id=lid)
                if not leg.driver:  # safety check
                    leg.driver = driver
                    leg.driver_assigned_by = request.user
                    leg.driver_assigned_at = timezone.now()
                    leg.save()
                    assigned += 1
            except Leg.DoesNotExist:
                continue

        response['applied'] = True
        response['assigned_count'] = assigned
        response['message'] = f"Assigned {assigned} new legs to {driver}."
        cache.delete(f"capacity_planner_{target_date.isoformat()}")

    return JsonResponse(response)


@login_required
def update_drive_time(request):
    """
    Update a drive time estimate between two location categories.
    Called when a dispatcher spots an incorrect drive time in the schedule builder.
    """
    if not request.user.is_superuser:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "POST required"}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    from_cat = data.get("from_category", "").strip()
    to_cat = data.get("to_category", "").strip()
    minutes = data.get("minutes")

    if not from_cat or not to_cat or minutes is None:
        return JsonResponse({"success": False, "error": "from_category, to_category, and minutes are required"}, status=400)

    try:
        minutes = int(minutes)
        if minutes < 1 or minutes > 300:
            return JsonResponse({"success": False, "error": "Minutes must be between 1 and 300"}, status=400)
    except (ValueError, TypeError):
        return JsonResponse({"success": False, "error": "Invalid minutes value"}, status=400)

    from dispatching.scheduler import update_drive_time_estimate, DRIVE_TIME_ESTIMATES

    old_time = DRIVE_TIME_ESTIMATES.get((from_cat, to_cat), 'unknown')
    update_drive_time_estimate(from_cat, to_cat, minutes)

    return JsonResponse({
        "success": True,
        "message": f"Updated {from_cat} \u2194 {to_cat}: {old_time} \u2192 {minutes} min",
        "from_category": from_cat,
        "to_category": to_cat,
        "old_minutes": old_time if isinstance(old_time, int) else None,
        "new_minutes": minutes,
    })


@login_required(login_url="login")
def route_timing_reference(request):
    """Route timing reference page showing computed metrics from completed legs."""
    if not request.user.is_staff:
        return redirect("dashboard")

    from dispatching.scheduler import DRIVE_TIME_ESTIMATES
    from dispatching.analytics import (
        categorize_location, categorize_time_of_day, categorize_day_type,
        calculate_airport_dwell_time, calculate_drive_time,
        has_valid_status_chain, calculate_gate_to_completed_time,
    )
    import statistics
    from collections import defaultdict

    # Filters
    trip_type_filter = request.GET.get('trip_type', '')
    pickup_filter = request.GET.get('pickup', '')
    dropoff_filter = request.GET.get('dropoff', '')
    min_samples = int(request.GET.get('min_samples', 0))
    driver_filter = request.GET.get('driver', '')
    team_filter = request.GET.get('team', '')  # 'inhouse' or ''
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    # Show "Live" badge when filtering beyond defaults
    use_live = bool(driver_filter or date_from or date_to or trip_type_filter or pickup_filter or dropoff_filter)

    # Get all inhouse drivers for filter dropdown
    inhouse_drivers = list(Driver.objects.filter(driver_type='inhouse').select_related('profile').order_by('profile__first_name'))
    excluded_driver_count = sum(1 for d in inhouse_drivers if d.exclude_from_timing)

    # Always compute from raw completed legs (all-time by default)
    # NOTE: don't filter exclude_from_analytics here — we track excluded IDs
    # separately so the modal can show them with an "Include" button.
    # Always restrict to inhouse drivers, matching analytics.py filters.
    legs_qs = Leg.objects.filter(
        status='completed',
        driver__driver_type='inhouse',
    ).select_related(
        'driver', 'flight_information', 'reservation',
    ).prefetch_related('status_history')

    if driver_filter:
        # When viewing a specific driver, bypass exclude_from_timing so
        # dispatchers can inspect any individual driver's timing data.
        legs_qs = legs_qs.filter(driver_id=int(driver_filter))
    else:
        # Aggregate view: respect driver-level timing exclusions
        legs_qs = legs_qs.filter(driver__exclude_from_timing=False)
    if date_from:
        try:
            legs_qs = legs_qs.filter(pickup_date__gte=datetime.strptime(date_from, '%Y-%m-%d').date())
        except ValueError:
            pass
    if date_to:
        try:
            legs_qs = legs_qs.filter(pickup_date__lte=datetime.strptime(date_to, '%Y-%m-%d').date())
        except ValueError:
            pass

    # Compute metrics grouped by route + time_of_day + day_type
    buckets = defaultdict(lambda: {'dwell': [], 'drive': [], 'total': [], 'leg_ids': []})

    skipped_incomplete = 0
    skipped_excluded = 0
    fallback_total_only = 0
    for leg in legs_qs:
        # Track excluded legs in bucket IDs (so modal can show "Include" button)
        # but skip them from analytics calculations
        if leg.exclude_from_analytics:
            pickup_cat = categorize_location(leg.pickup_location)
            dropoff_cat = categorize_location(leg.dropoff_location)
            time_cat = categorize_time_of_day(leg.pickup_time)
            day_cat = categorize_day_type(leg.pickup_date)
            trip_type = leg.get_trip_type()
            has_store_stop = getattr(leg.reservation, 'store_stop', False) if leg.reservation else False
            if trip_type == 'arrival' and has_store_stop:
                trip_type = 'arrival_store'
            if trip_type_filter and trip_type != trip_type_filter:
                continue
            if pickup_filter and pickup_cat != pickup_filter:
                continue
            if dropoff_filter and dropoff_cat != dropoff_filter:
                continue
            key = (trip_type, pickup_cat, dropoff_cat, time_cat, day_cat)
            buckets[key]['leg_ids'].append(leg.id)
            skipped_excluded += 1
            continue

        valid_chain = has_valid_status_chain(leg)

        if not valid_chain:
            # Fallback: for arrivals, try gate → completed total time
            # (no dwell/drive split, but we get a usable total)
            gate_total = calculate_gate_to_completed_time(leg)
            if gate_total is not None:
                pickup_cat = categorize_location(leg.pickup_location)
                dropoff_cat = categorize_location(leg.dropoff_location)
                time_cat = categorize_time_of_day(leg.pickup_time)
                day_cat = categorize_day_type(leg.pickup_date)
                trip_type = leg.get_trip_type()
                has_store_stop = getattr(leg.reservation, 'store_stop', False) if leg.reservation else False
                if trip_type == 'arrival' and has_store_stop:
                    trip_type = 'arrival_store'
                if trip_type_filter and trip_type != trip_type_filter:
                    continue
                if pickup_filter and pickup_cat != pickup_filter:
                    continue
                if dropoff_filter and dropoff_cat != dropoff_filter:
                    continue
                key = (trip_type, pickup_cat, dropoff_cat, time_cat, day_cat)
                buckets[key]['total'].append(gate_total)
                buckets[key]['leg_ids'].append(leg.id)
                fallback_total_only += 1
            else:
                skipped_incomplete += 1
            continue

        pickup_cat = categorize_location(leg.pickup_location)
        dropoff_cat = categorize_location(leg.dropoff_location)
        time_cat = categorize_time_of_day(leg.pickup_time)
        day_cat = categorize_day_type(leg.pickup_date)
        trip_type = leg.get_trip_type()

        # Separate arrivals with store stop (Publix etc.) — they take longer
        has_store_stop = getattr(leg.reservation, 'store_stop', False) if leg.reservation else False
        if trip_type == 'arrival' and has_store_stop:
            trip_type = 'arrival_store'

        # trip_type is computed (arrival/return/cruise/other), not a DB field
        if trip_type_filter and trip_type != trip_type_filter:
            continue
        if pickup_filter and pickup_cat != pickup_filter:
            continue
        if dropoff_filter and dropoff_cat != dropoff_filter:
            continue

        dwell = calculate_airport_dwell_time(leg)
        drive = calculate_drive_time(leg)

        key = (trip_type, pickup_cat, dropoff_cat, time_cat, day_cat)
        if dwell is not None:
            buckets[key]['dwell'].append(dwell)
        if drive is not None:
            buckets[key]['drive'].append(drive)
            buckets[key]['leg_ids'].append(leg.id)
            total = (dwell + drive) if dwell is not None else drive
            buckets[key]['total'].append(total)

    # Build metrics_list from buckets (with IQR outlier filtering)
    from dispatching.analytics import iqr_filter

    def _stats(lst):
        if not lst:
            return {}
        r = {'avg': round(statistics.mean(lst))}
        if len(lst) >= 2:
            r['median'] = round(statistics.median(lst))
        if len(lst) >= 4:
            r['p75'] = round(statistics.quantiles(lst, n=4)[2])
        if len(lst) >= 10:
            r['p90'] = round(statistics.quantiles(lst, n=10)[8])
        return r

    TIME_LABELS = {
        'early_morning': 'Early Morning (4-7 AM)',
        'morning_rush': 'Morning Rush (7-10 AM)',
        'midday': 'Midday (10 AM - 2 PM)',
        'afternoon': 'Afternoon (2-6 PM)',
        'evening': 'Evening (6-10 PM)',
        'night': 'Night (10 PM - 4 AM)',
    }
    DAY_LABELS = {'weekday': 'Weekday', 'weekend': 'Weekend'}

    # Sort order: weekday first, then weekend
    DAY_ORDER = {'weekday': 0, 'weekend': 1}
    # Chronological: early morning → morning rush → midday → afternoon → evening → night
    TIME_ORDER = {
        'early_morning': 0, 'morning_rush': 1, 'midday': 2,
        'afternoon': 3, 'evening': 4, 'night': 5,
    }

    def _sort_key(item):
        key = item[0]
        # key = (trip_type, pickup_cat, dropoff_cat, time_cat, day_cat)
        _, _, _, time_cat, day_cat = key
        return (DAY_ORDER.get(day_cat, 9), TIME_ORDER.get(time_cat, 9))

    metrics_list = []
    for key, vals in sorted(buckets.items(), key=_sort_key):
        trip_type, pickup_cat, dropoff_cat, time_cat, day_cat = key
        # Apply IQR filtering to clean outliers
        vals['dwell'] = iqr_filter(vals['dwell'])
        vals['drive'] = iqr_filter(vals['drive'])
        vals['total'] = iqr_filter(vals['total'])
        sample_count = len(vals['drive'])
        if min_samples and sample_count < min_samples:
            continue

        confidence = 'high' if sample_count >= 20 else ('medium' if sample_count >= 10 else ('low' if sample_count >= 5 else 'none'))
        hardcoded = DRIVE_TIME_ESTIMATES.get((pickup_cat, dropoff_cat))

        metrics_list.append({
            'pickup_cat': pickup_cat,
            'dropoff_cat': dropoff_cat,
            'trip_type': trip_type,
            'time_cat': time_cat,
            'day_cat': day_cat,
            'time_label': TIME_LABELS.get(time_cat, time_cat),
            'day_label': DAY_LABELS.get(day_cat, day_cat),
            'sample_count': sample_count,
            'confidence': confidence,
            'dwell': _stats(vals['dwell']),
            'drive': _stats(vals['drive']),
            'total': _stats(vals['total']),
            'hardcoded_drive_time': hardcoded,
            'leg_ids': ','.join(str(i) for i in vals['leg_ids']),
        })

    total_routes = len(set((m['pickup_cat'], m['dropoff_cat']) for m in metrics_list))
    total_samples = sum(m['sample_count'] for m in metrics_list)
    high_confidence = sum(1 for m in metrics_list if m['confidence'] == 'high')

    # Build filter options from the computed metrics
    pickup_categories = sorted(set(m['pickup_cat'] for m in metrics_list))
    dropoff_categories = sorted(set(m['dropoff_cat'] for m in metrics_list))
    trip_types = sorted(set(m['trip_type'] for m in metrics_list))

    # Group metrics by route for card display
    grouped = {}
    for m in metrics_list:
        route_key = (m['pickup_cat'], m['dropoff_cat'])
        if route_key not in grouped:
            grouped[route_key] = {
                'pickup_cat': m['pickup_cat'],
                'dropoff_cat': m['dropoff_cat'],
                'hardcoded_drive_time': m['hardcoded_drive_time'],
                'rows': [],
            }
        grouped[route_key]['rows'].append(m)

    # Sort rows within each route group: weekday first, then chronologically
    for g in grouped.values():
        g['rows'].sort(key=lambda r: (
            DAY_ORDER.get(r.get('day_cat', ''), 9),
            TIME_ORDER.get(r.get('time_cat', ''), 9),
        ))

    route_groups = sorted(grouped.values(), key=lambda g: -sum(r['sample_count'] for r in g['rows']))

    context = {
        'route_groups': route_groups,
        'pickup_categories': pickup_categories,
        'dropoff_categories': dropoff_categories,
        'trip_types': trip_types,
        'trip_type_filter': trip_type_filter,
        'pickup_filter': pickup_filter,
        'dropoff_filter': dropoff_filter,
        'min_samples': min_samples,
        'driver_filter': driver_filter,
        'date_from': date_from,
        'date_to': date_to,
        'inhouse_drivers': inhouse_drivers,
        'excluded_driver_count': excluded_driver_count,
        'total_routes': total_routes,
        'total_samples': total_samples,
        'high_confidence': high_confidence,
        'drive_time_estimates': DRIVE_TIME_ESTIMATES,
        'use_live': use_live,
        'skipped_incomplete': skipped_incomplete,
        'skipped_excluded': skipped_excluded,
        'fallback_total_only': fallback_total_only,
    }

    return render(request, 'dispatching/route_timing_reference.html', context)


@login_required
def route_timing_leg_details(request):
    """AJAX endpoint: return leg details for a comma-separated list of leg IDs."""
    if not request.user.is_staff:
        return JsonResponse({"error": "Permission denied"}, status=403)

    from dispatching.analytics import calculate_airport_dwell_time, calculate_drive_time

    leg_ids_str = request.GET.get('ids', '')
    if not leg_ids_str:
        return JsonResponse({"legs": []})

    try:
        leg_ids = [int(x) for x in leg_ids_str.split(',') if x.strip()]
    except ValueError:
        return JsonResponse({"error": "Invalid IDs"}, status=400)

    legs = (
        Leg.objects.filter(id__in=leg_ids)
        .select_related('driver', 'driver__profile', 'flight_information', 'reservation__customer')
        .prefetch_related('status_history')
    )

    results = []
    for leg in legs:
        driver_name = ""
        if leg.driver and hasattr(leg.driver, 'profile'):
            driver_name = leg.driver.profile.get_full_name() or leg.driver.profile.username

        customer_name = ""
        if leg.reservation and leg.reservation.customer:
            customer_name = leg.reservation.customer.get_full_name()

        dwell = calculate_airport_dwell_time(leg)
        drive = calculate_drive_time(leg)

        results.append({
            'id': leg.id,
            'reservation_id': leg.reservation_id,
            'pickup_date': leg.pickup_date.strftime('%m/%d/%Y') if leg.pickup_date else '',
            'pickup_time': leg.pickup_time.strftime('%I:%M %p').lstrip('0') if leg.pickup_time else '',
            'driver': driver_name,
            'customer': customer_name,
            'pickup': leg.pickup_location or '',
            'dropoff': leg.dropoff_location or '',
            'dwell_min': dwell,
            'drive_min': drive,
            'total_min': (dwell + drive) if dwell is not None and drive is not None else drive,
            'excluded': leg.exclude_from_analytics,
        })

    return JsonResponse({"legs": results})


@login_required
def route_timing_exclude_leg(request):
    """AJAX endpoint: toggle exclude_from_analytics flag on a leg."""
    if not request.user.is_staff:
        return JsonResponse({"error": "Permission denied"}, status=403)
    if request.method != 'POST':
        return JsonResponse({"error": "POST required"}, status=405)

    data = json.loads(request.body)
    leg_id = data.get('leg_id')
    exclude = data.get('exclude', True)

    try:
        leg = Leg.objects.select_related('driver').get(id=leg_id)
        leg.exclude_from_analytics = exclude
        leg.save(update_fields=['exclude_from_analytics'])

        # Recalculate the affected bucket immediately so the change takes effect
        import threading
        from django.db import connection as _conn

        def _recalc_bucket(leg_obj):
            try:
                from dispatching.analytics import update_single_route_timing_metric
                update_single_route_timing_metric(leg_obj)
            except Exception:
                pass
            finally:
                _conn.close()

        threading.Thread(target=_recalc_bucket, args=(leg,), daemon=True).start()

        return JsonResponse({"success": True, "excluded": exclude})
    except Leg.DoesNotExist:
        return JsonResponse({"error": "Leg not found"}, status=404)


@login_required
def recalculate_route_metrics(request):
    """AJAX endpoint to recalculate route timing metrics with optional date filtering.
    Runs in a background thread so the request returns immediately without
    blocking the web server.
    """
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "POST required"}, status=405)

    data = json.loads(request.body)
    recent_days = data.get("recent_days")  # None = all data

    if recent_days is not None:
        try:
            recent_days = int(recent_days)
            if recent_days < 1:
                return JsonResponse({"success": False, "error": "recent_days must be >= 1"}, status=400)
        except (ValueError, TypeError):
            return JsonResponse({"success": False, "error": "Invalid recent_days value"}, status=400)

    import threading
    from django.db import connection

    def _run_recalculation(days):
        try:
            from dispatching.analytics import update_all_route_timing_metrics
            created, updated = update_all_route_timing_metrics(recent_days=days)
            logger.info(f"Route metrics recalculation complete: {created} created, {updated} updated")
        except Exception as e:
            logger.error(f"Route metrics recalculation failed: {e}", exc_info=True)
        finally:
            connection.close()

    thread = threading.Thread(target=_run_recalculation, args=(recent_days,), daemon=True)
    thread.start()

    label = f"last {recent_days} days" if recent_days else "all time"
    return JsonResponse({
        "success": True,
        "message": f"Recalculation started for {label}. This runs in the background — metrics will update shortly.",
    })


# ============================================================================
# DRIVER PERFORMANCE
# ============================================================================

@login_required(login_url="login")
def driver_performance(request):
    """Driver performance analytics — trip history with timing breakdowns."""
    if not request.user.is_staff:
        return redirect("home")

    from dispatching.analytics import (
        categorize_location, calculate_airport_dwell_time, calculate_drive_time,
        has_valid_status_chain,
    )
    from drivers.models import Driver

    # Filters
    selected_driver_id = request.GET.get('driver', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    # Default to last 30 days
    if not date_from:
        date_from = (timezone.localdate() - timedelta(days=30)).strftime('%Y-%m-%d')
    if not date_to:
        date_to = timezone.localdate().strftime('%Y-%m-%d')

    try:
        start_date = datetime.strptime(date_from, '%Y-%m-%d').date()
    except ValueError:
        start_date = timezone.localdate() - timedelta(days=30)
    try:
        end_date = datetime.strptime(date_to, '%Y-%m-%d').date()
    except ValueError:
        end_date = timezone.localdate()

    drivers = Driver.objects.filter(driver_type='inhouse').select_related('profile').order_by('profile__first_name')

    selected_driver = None
    driver_trips = []
    driver_summary = {}
    all_drivers_summary = []

    if selected_driver_id:
        # ── Detail mode: show individual driver's trips ──
        try:
            selected_driver = Driver.objects.select_related('profile').get(id=int(selected_driver_id))
        except (Driver.DoesNotExist, ValueError):
            selected_driver = None

        if selected_driver:
            # Only include legs that have at least one status history entry
            # (status tracking was added recently — older legs have no data)
            legs = (
                Leg.objects.filter(
                    driver=selected_driver,
                    pickup_date__gte=start_date,
                    pickup_date__lte=end_date,
                    status='completed',
                    status_history__isnull=False,
                )
                .distinct()
                .select_related(
                    'reservation__customer', 'reservation__vehicle',
                    'flight_information',
                )
                .prefetch_related('status_history')
                .order_by('-pickup_date', '-pickup_time')
            )

            total_drive = []
            total_dwell = []
            total_total = []

            for leg in legs:
                dwell = calculate_airport_dwell_time(leg)
                drive = calculate_drive_time(leg)
                total = None
                if dwell is not None and drive is not None:
                    total = dwell + drive
                elif drive is not None:
                    total = drive

                valid = has_valid_status_chain(leg)
                trip_type = leg.get_trip_type()
                customer_name = ''
                if leg.reservation and leg.reservation.customer:
                    customer_name = leg.reservation.customer.get_full_name()

                vehicle_name = ''
                if leg.reservation and leg.reservation.vehicle:
                    vehicle_name = str(leg.reservation.vehicle)

                pickup_cat = categorize_location(leg.pickup_location)
                dropoff_cat = categorize_location(leg.dropoff_location)

                # Flight details for arrivals
                flight_label = ''
                flight_origin = ''
                gate_arrival_at = None
                if leg.flight_information:
                    fi = leg.flight_information
                    airline = fi.airline_display_name or fi.airline or ''
                    fnum = fi.flight_number or ''
                    flight_label = f"{airline} {fnum}".strip()
                    # Fallback to flight_iata if no separate airline/number
                    if not flight_label and fi.flight_iata:
                        flight_label = fi.flight_iata
                    flight_origin = fi.origin or ''
                    gate_arrival_at = (
                        fi.actual_gate_arrival_local
                        or fi.estimated_gate_arrival_local
                        or fi.scheduled_gate_arrival_local
                    )
                    if gate_arrival_at and timezone.is_aware(gate_arrival_at):
                        gate_arrival_at = timezone.localtime(gate_arrival_at)

                # Extract status timestamps from prefetched history
                status_times = {}
                if hasattr(leg, '_prefetched_objects_cache') and 'status_history' in leg._prefetched_objects_cache:
                    for s in leg.status_history.all():
                        if s.status not in status_times:
                            ts = s.timestamp
                            if timezone.is_aware(ts):
                                ts = timezone.localtime(ts)
                            status_times[s.status] = ts

                # Compute durations between each status step
                def _safe_delta(start_key, end_key, max_min=300):
                    if start_key in status_times and end_key in status_times:
                        d = (status_times[end_key] - status_times[start_key]).total_seconds() / 60
                        if 0 < d < max_min:
                            return round(d)
                    return None

                conf_to_otw = _safe_delta('confirmed', 'on-the-way')
                otw_to_loc = _safe_delta('on-the-way', 'on-location')
                loc_to_pickup = _safe_delta('on-location', 'picked-up')
                pickup_to_done = _safe_delta('picked-up', 'completed')

                driver_trips.append({
                    'id': leg.id,
                    'reservation_uuid': leg.reservation.uuid if leg.reservation else None,
                    'pickup_date': leg.pickup_date,
                    'pickup_time': leg.pickup_time,
                    'pickup_location': leg.pickup_location or '',
                    'dropoff_location': leg.dropoff_location or '',
                    'pickup_cat': pickup_cat,
                    'dropoff_cat': dropoff_cat,
                    'trip_type': trip_type,
                    'customer': customer_name,
                    'vehicle': vehicle_name,
                    'dwell_min': dwell,
                    'drive_min': drive,
                    'total_min': total,
                    'valid_chain': valid,
                    'confirmed_at': status_times.get('confirmed'),
                    'otw_at': status_times.get('on-the-way'),
                    'on_location_at': status_times.get('on-location'),
                    'picked_up_at': status_times.get('picked-up'),
                    'completed_at': status_times.get('completed'),
                    'conf_to_otw_min': conf_to_otw,
                    'otw_to_location_min': otw_to_loc,
                    'loc_to_pickup_min': loc_to_pickup,
                    'pickup_to_done_min': pickup_to_done,
                    'store_stop': leg.reservation.store_stop if leg.reservation else False,
                    'flight_label': flight_label,
                    'flight_origin': flight_origin,
                    'gate_arrival_at': gate_arrival_at,
                })

                if drive is not None:
                    total_drive.append(drive)
                if dwell is not None:
                    total_dwell.append(dwell)
                if total is not None:
                    total_total.append(total)

            import statistics as stats_module

            # Separate trips by type for per-category averages
            arrival_trips = [t for t in driver_trips if t['trip_type'] == 'arrival']
            return_trips = [t for t in driver_trips if t['trip_type'] == 'return']
            cruise_trips = [t for t in driver_trips if t['trip_type'] == 'cruise']

            # Arrival stats — separate with/without store stop
            arr_dwells = [t['dwell_min'] for t in arrival_trips if t['dwell_min'] is not None]
            arr_totals_no_stop = [t['total_min'] for t in arrival_trips if t['total_min'] is not None and not t.get('store_stop')]
            arr_totals_with_stop = [t['total_min'] for t in arrival_trips if t['total_min'] is not None and t.get('store_stop')]

            # Return stats
            ret_drives = [t['drive_min'] for t in return_trips if t['drive_min'] is not None]
            ret_totals = [t['total_min'] for t in return_trips if t['total_min'] is not None]

            # Cruise stats
            cruise_totals_list = [t['total_min'] for t in cruise_trips if t['total_min'] is not None]

            driver_summary = {
                'total_trips': len(driver_trips),
                'valid_count': sum(1 for t in driver_trips if t['valid_chain']),
                # Arrivals
                'arrival_count': len(arrival_trips),
                'arrival_avg_dwell': round(stats_module.mean(arr_dwells)) if arr_dwells else None,
                'arrival_avg_total': round(stats_module.mean(arr_totals_no_stop)) if arr_totals_no_stop else None,
                'arrival_avg_total_stop': round(stats_module.mean(arr_totals_with_stop)) if arr_totals_with_stop else None,
                'arrival_count_no_stop': len(arr_totals_no_stop),
                'arrival_count_with_stop': len(arr_totals_with_stop),
                # Returns
                'return_count': len(return_trips),
                'return_avg_drive': round(stats_module.mean(ret_drives)) if ret_drives else None,
                'return_avg_total': round(stats_module.mean(ret_totals)) if ret_totals else None,
                # Cruises
                'cruise_count': len(cruise_trips),
                'cruise_avg_total': round(stats_module.mean(cruise_totals_list)) if cruise_totals_list else None,
            }
    else:
        # ── Overview mode: all drivers with summary stats ──
        # Single query for ALL inhouse driver legs (instead of per-driver loop)
        import statistics as stats_module
        from collections import defaultdict

        driver_ids = [drv.id for drv in drivers]
        driver_map = {drv.id: drv for drv in drivers}

        all_legs = (
            Leg.objects.filter(
                driver_id__in=driver_ids,
                pickup_date__gte=start_date,
                pickup_date__lte=end_date,
                status='completed',
                status_history__isnull=False,
            )
            .distinct()
            .select_related('flight_information')
            .prefetch_related('status_history')
        )

        # Group legs by driver in Python
        legs_by_driver = defaultdict(list)
        for leg in all_legs:
            legs_by_driver[leg.driver_id].append(leg)

        for drv_id, drv_legs in legs_by_driver.items():
            drv = driver_map.get(drv_id)
            if not drv:
                continue

            drive_times = []
            total_times = []
            valid_count = 0
            for leg in drv_legs:
                drive = calculate_drive_time(leg)
                dwell = calculate_airport_dwell_time(leg)
                if drive is not None:
                    drive_times.append(drive)
                    total = (dwell + drive) if dwell is not None else drive
                    total_times.append(total)
                if has_valid_status_chain(leg):
                    valid_count += 1

            all_drivers_summary.append({
                'driver': drv,
                'driver_name': drv.profile.get_full_name() or drv.profile.username,
                'total_trips': len(drv_legs),
                'valid_count': valid_count,
                'avg_drive': round(stats_module.mean(drive_times)) if drive_times else None,
                'med_drive': round(stats_module.median(drive_times)) if len(drive_times) >= 2 else None,
                'avg_total': round(stats_module.mean(total_times)) if total_times else None,
                'med_total': round(stats_module.median(total_times)) if len(total_times) >= 2 else None,
            })

        all_drivers_summary.sort(key=lambda d: d['total_trips'], reverse=True)

    context = {
        'drivers': drivers,
        'selected_driver': selected_driver,
        'selected_driver_id': selected_driver_id,
        'date_from': date_from,
        'date_to': date_to,
        'driver_trips': driver_trips,
        'driver_summary': driver_summary,
        'all_drivers_summary': all_drivers_summary,
    }
    return render(request, 'dispatching/driver_performance.html', context)


# ============================================================================
# SCHEDULER SETTINGS API
# ============================================================================

@login_required(login_url="login")
def get_scheduler_settings(request):
    """Return all scheduler tuning parameters as JSON."""
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    from dispatching.models import SchedulerSettings
    settings = SchedulerSettings.get_settings()
    return JsonResponse({
        "success": True,
        "settings": settings.to_dict(),
        "defaults": settings.get_defaults(),
    })


@login_required(login_url="login")
def update_scheduler_settings(request):
    """Update scheduler tuning parameters. Accepts JSON body with field:value pairs.
    Send {"reset": true} to reset all values to defaults."""
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "POST required"}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    from dispatching.models import SchedulerSettings
    settings = SchedulerSettings.get_settings()

    if data.get("reset"):
        settings.reset_to_defaults()
        return JsonResponse({
            "success": True,
            "message": "All settings reset to defaults",
            "settings": settings.to_dict(),
        })

    # Get valid field names
    valid_fields = set(settings.to_dict().keys())
    updated = []

    for field_name, value in data.items():
        if field_name not in valid_fields:
            continue
        try:
            value = int(value)
        except (ValueError, TypeError):
            return JsonResponse({
                "success": False,
                "error": f"Invalid value for {field_name}: must be an integer",
            }, status=400)
        setattr(settings, field_name, value)
        updated.append(field_name)

    if updated:
        settings.save()
        SchedulerSettings.clear_cache()

    return JsonResponse({
        "success": True,
        "message": f"Updated {len(updated)} settings",
        "updated": updated,
        "settings": settings.to_dict(),
    })


@login_required
def get_driver_weekly_schedules(request):
    """Return weekly schedule data for all inhouse drivers."""
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    drivers = Driver.objects.filter(driver_type="inhouse").select_related("profile").prefetch_related("weekly_schedule")
    result = []
    for d in drivers:
        entries = {}
        for entry in d.weekly_schedule.all():
            entries[entry.day_of_week] = {
                "is_available": entry.is_available,
                "start_hour": entry.start_hour,
                "end_hour": entry.end_hour,
                "preference": entry.preference,
            }
        result.append({
            "id": d.id,
            "name": str(d),
            "default_start_hour": d.default_start_hour,
            "default_end_hour": d.default_end_hour,
            "default_preference": d.default_preference,
            "weekly": entries,
        })
    return JsonResponse({"success": True, "drivers": result})


@login_required
@require_POST
def save_driver_weekly_schedules(request):
    """Save weekly schedule data for all inhouse drivers."""
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    drivers_data = data.get("drivers", [])
    updated_count = 0

    for d_data in drivers_data:
        driver_id = d_data.get("id")
        if not driver_id:
            continue

        try:
            driver = Driver.objects.get(id=driver_id, driver_type="inhouse")
        except Driver.DoesNotExist:
            continue

        # Update driver defaults + notes
        driver.default_start_hour = int(d_data.get("default_start_hour", 6))
        driver.default_end_hour = int(d_data.get("default_end_hour", 23))
        driver.default_preference = d_data.get("default_preference", "")
        if "notes" in d_data:
            driver.notes = d_data["notes"].strip() or None
        driver.save(update_fields=["default_start_hour", "default_end_hour", "default_preference", "notes"])

        # Update weekly entries
        weekly = d_data.get("weekly", {})
        for day_str, entry in weekly.items():
            day = int(day_str)
            DriverWeeklySchedule.objects.update_or_create(
                driver=driver,
                day_of_week=day,
                defaults={
                    "is_available": entry.get("is_available", True),
                    "start_hour": int(entry.get("start_hour", 6)),
                    "end_hour": int(entry.get("end_hour", 23)),
                    "preference": entry.get("preference", ""),
                },
            )
        updated_count += 1

    return JsonResponse({"success": True, "message": f"Updated schedules for {updated_count} drivers"})


@login_required(login_url="login")
def inhouse_schedule(request):
    """
    In-house driver availability manager.
    Shows each driver's weekly schedule (days + hours) and lets staff edit inline.
    Vehicle assignments for a specific date are handled on the Legs Dashboard.
    """
    if not request.user.is_staff:
        messages.error(request, "Permission denied.")
        return redirect("legs_list")

    DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    def _fmt_hour(h):
        """Return a short human-readable hour label like 2a, 12p, EOD."""
        if h == 0:  return "12a"
        if h < 12:  return f"{h}a"
        if h == 12: return "12p"
        if h == 23: return "EOD"
        return f"{h - 12}p"

    def _fmt_hour_long(h):
        """Return a full select-option label like '12 AM', '2 AM', '5 PM'."""
        if h == 0:  return "12 AM"
        if h < 12:  return f"{h} AM"
        if h == 12: return "12 PM"
        if h == 23: return "EOD (11 PM)"
        return f"{h - 12} PM"

    # All 24 hour choices for the time selects
    hour_choices = [{"value": h, "label": _fmt_hour_long(h)} for h in range(24)]

    inhouse_drivers = (
        Driver.objects.filter(driver_type="inhouse")
        .select_related("profile")
        .prefetch_related("weekly_schedule")
        .order_by("profile__first_name", "profile__last_name", "profile__username")
    )

    driver_rows = []
    for driver in inhouse_drivers:
        weekly_map = {entry.day_of_week: entry for entry in driver.weekly_schedule.all()}
        days = []
        for day_idx in range(7):
            entry = weekly_map.get(day_idx)
            sh = entry.start_hour if entry else driver.default_start_hour
            eh = entry.end_hour   if entry else driver.default_end_hour
            avail = entry.is_available if entry else True
            pref  = entry.preference   if entry else driver.default_preference
            days.append({
                "day_idx":    day_idx,
                "day_name":   DAY_NAMES[day_idx],
                "is_available": avail,
                "start_hour": sh,
                "end_hour":   eh,
                "preference": pref,
                "pill_label": f"{_fmt_hour(sh)}-{_fmt_hour(eh)}" if avail else "Off",
            })
        driver_rows.append({"driver": driver, "days": days})

    today = timezone.localdate()
    context = {
        "driver_rows": driver_rows,
        "hour_choices": hour_choices,
        "today": today,
        "today_legs_url": f"/dispatching/?date={today.strftime('%Y-%m-%d')}",
    }
    return render(request, "dispatching/inhouse_schedule.html", context)


# ── Swap Optimizer Endpoints ─────────────────────────────────────────

@login_required
def find_swap_suggestions(request):
    """Find swap chains to make room for an unplaceable leg."""
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "POST required"}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    date_str = data.get("date")
    leg_id = data.get("leg_id")
    if not date_str or not leg_id:
        return JsonResponse({"success": False, "error": "date and leg_id required"}, status=400)

    from dispatching.scheduler import (
        build_driver_schedules, load_all_driver_vtypes, preload_timing_cache,
    )
    from dispatching.swap_optimizer import find_swaps
    from reservations.models import Leg

    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return JsonResponse({"success": False, "error": "Invalid date"}, status=400)

    # Load target leg
    target_leg = (
        Leg.objects.filter(id=leg_id)
        .select_related(
            "reservation", "reservation__customer",
            "reservation__vehicle",
            "driver", "driver__profile", "flight_information",
        )
        .first()
    )
    if not target_leg:
        return JsonResponse({"success": False, "error": "Leg not found"}, status=404)

    # Preload timing cache
    preload_timing_cache()

    # Get eligible in-house drivers (with vehicle assignments for this date)
    eligible_driver_ids = set(
        DriverVehicleAssignment.objects.filter(
            date=target_date, driver__driver_type="inhouse"
        ).values_list("driver_id", flat=True)
    )
    inhouse_drivers = list(
        Driver.objects.filter(driver_type="inhouse", id__in=eligible_driver_ids)
        .select_related("profile")
    )

    # Load all legs for this date (assigned to in-house drivers)
    all_legs = list(
        Leg.objects.filter(pickup_date=target_date, driver__isnull=False, driver__driver_type="inhouse")
        .exclude(reservation__status="cancelled")
        .exclude(status="cancelled")
        .select_related(
            "reservation", "reservation__customer",
            "reservation__vehicle",
            "driver", "driver__profile", "flight_information",
        )
    )

    # Build current schedules
    schedules = build_driver_schedules(all_legs, inhouse_drivers, target_date)
    driver_vtypes = load_all_driver_vtypes(target_date)
    all_legs_by_id = {leg.id: leg for leg in all_legs}

    # Run swap search
    result = find_swaps(
        target_leg=target_leg,
        inhouse_schedules=schedules,
        all_legs_by_id=all_legs_by_id,
        driver_vtypes=driver_vtypes,
        target_date=target_date,
    )

    # Serialize solutions
    solutions_data = []
    for sol in result.solutions:
        moves_data = []
        for move in sol.moves:
            moves_data.append({
                "leg_id": move.leg_id,
                "pickup_time": move.leg_pickup_time,
                "route": move.leg_route,
                "from_driver_id": move.from_driver_id,
                "from_driver": move.from_driver_name,
                "to_driver_id": move.to_driver_id,
                "to_driver": move.to_driver_name,
                "buffer_minutes": move.buffer_minutes,
            })
        solutions_data.append({
            "score": sol.score,
            "depth": sol.depth,
            "target_driver": sol.target_driver_name,
            "target_driver_id": sol.target_driver_id,
            "target_buffer": sol.target_buffer_minutes,
            "moves": moves_data,
        })

    # Serialize diagnostic report (only present when no solutions found)
    diagnostic_data = []
    for d in result.diagnostic:
        diagnostic_data.append({
            "driver_name": d.driver_name,
            "vehicle_type": d.vehicle_type,
            "num_jobs": d.num_jobs,
            "skipped_reason": d.skipped_reason,
            "direct_feasible": d.direct_feasible,
            "direct_buffer": d.direct_buffer,
            "direct_fail_reason": d.direct_fail_reason,
            "displacements_tried": d.displacements_tried,
            "displacements_detail": d.displacements_detail,
        })

    return JsonResponse({
        "success": True,
        "solutions": solutions_data,
        "states_explored": result.states_explored,
        "time_ms": result.time_ms,
        "hit_time_limit": result.hit_time_limit,
        "hit_depth_limit": result.hit_depth_limit,
        "diagnostic": diagnostic_data,
    })


@login_required
def execute_swap(request):
    """Execute an approved swap — update leg driver assignments in a transaction."""
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "POST required"}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    date_str = data.get("date")
    moves = data.get("moves", [])
    if not date_str or not moves:
        return JsonResponse({"success": False, "error": "date and moves required"}, status=400)

    from reservations.models import Leg

    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return JsonResponse({"success": False, "error": "Invalid date"}, status=400)

    try:
        with transaction.atomic():
            for move in moves:
                leg_id = move.get("leg_id")
                to_driver_id = move.get("to_driver_id")
                if not leg_id or not to_driver_id:
                    continue
                leg = Leg.objects.select_for_update().get(id=leg_id)
                driver = Driver.objects.get(id=to_driver_id)
                leg.driver = driver
                leg.driver_assigned_by = request.user
                leg.driver_assigned_at = timezone.now()
                leg.save()
    except Leg.DoesNotExist:
        return JsonResponse({"success": False, "error": "Leg not found"}, status=404)
    except Driver.DoesNotExist:
        return JsonResponse({"success": False, "error": "Driver not found"}, status=404)
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)

    cache.delete(f"capacity_planner_{target_date.isoformat()}")
    return JsonResponse({"success": True, "applied": len(moves)})


@login_required
def execute_takeback(request):
    """Reassign a single affiliate leg to an inhouse driver."""
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "POST required"}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    leg_id = data.get("leg_id")
    driver_id = data.get("driver_id")
    date_str = data.get("date")
    if not leg_id or not driver_id or not date_str:
        return JsonResponse({"success": False, "error": "leg_id, driver_id, and date required"}, status=400)

    from reservations.models import Leg

    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return JsonResponse({"success": False, "error": "Invalid date"}, status=400)

    try:
        with transaction.atomic():
            leg = Leg.objects.select_for_update().get(id=leg_id)
            driver = Driver.objects.get(id=driver_id)
            leg.driver = driver
            leg.driver_assigned_by = request.user
            leg.driver_assigned_at = timezone.now()
            leg.save()
    except Leg.DoesNotExist:
        return JsonResponse({"success": False, "error": "Leg not found"}, status=404)
    except Driver.DoesNotExist:
        return JsonResponse({"success": False, "error": "Driver not found"}, status=404)
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)

    cache.delete(f"capacity_planner_{target_date.isoformat()}")
    return JsonResponse({"success": True})


@login_required
def swap_tester(request):
    """Standalone swap tester / debugger page."""
    if not request.user.is_staff:
        return redirect("dashboard")

    from dispatching.scheduler import (
        build_driver_schedules, suggest_assignments,
        preload_timing_cache, load_all_driver_vtypes,
        check_feasibility, get_compatible_vehicle_types,
    )
    from dispatching.models import SchedulerSettings
    from reservations.models import Leg

    preload_timing_cache()

    selected_date_str = request.GET.get("date")
    try:
        selected_date = (
            datetime.strptime(selected_date_str, "%Y-%m-%d").date()
            if selected_date_str
            else timezone.localdate()
        )
    except (ValueError, TypeError):
        selected_date = timezone.localdate()

    # All legs for the date
    legs = list(
        Leg.objects.filter(pickup_date=selected_date)
        .exclude(reservation__status="cancelled")
        .exclude(status="cancelled")
        .select_related(
            "reservation", "reservation__customer",
            "reservation__vehicle",
            "driver", "driver__profile", "flight_information",
        )
        .order_by("pickup_time")
    )

    # Single DVA query → builds both eligible_driver_ids and driver_vtypes
    driver_vtypes = load_all_driver_vtypes(selected_date)
    inhouse_drivers = list(
        Driver.objects.filter(driver_type="inhouse", id__in=driver_vtypes.keys())
        .select_related("profile")
        .order_by("profile__first_name")
    )

    # Build schedules and suggestions (pass driver_vtypes to avoid re-query)
    schedules = build_driver_schedules(legs, inhouse_drivers, selected_date)
    unassigned_legs = [l for l in legs if not l.driver]
    suggestions = suggest_assignments(
        unassigned_legs, schedules, selected_date, driver_vtypes=driver_vtypes
    ) if unassigned_legs else []
    suggestion_map = {s.leg_id: s for s in suggestions}

    # Build no-fit legs (unassigned legs where suggestion has no driver)
    nofit_legs = []
    for leg in unassigned_legs:
        s = suggestion_map.get(leg.id)
        if s and s.suggested_driver_id:
            continue  # has a suggestion, not no-fit
        trip_type = leg.get_trip_type()
        vtype = getattr(getattr(getattr(leg, "reservation", None), "vehicle", None), "vehicle_type", None)
        customer = ""
        if leg.reservation and leg.reservation.customer:
            customer = leg.reservation.customer.get_full_name()
        nofit_legs.append({
            "id": leg.id,
            "pickup_time": leg.pickup_time.strftime("%I:%M %p").lstrip("0") if leg.pickup_time else "",
            "pickup_location": leg.pickup_location,
            "dropoff_location": leg.dropoff_location,
            "trip_type": trip_type,
            "vehicle_type": str(vtype) if vtype else "",
            "customer": customer,
            "revenue": float(leg.revenue_share or 0),
        })

    # ── Affiliate takeback analysis ──────────────────────────
    cfg = SchedulerSettings.get_settings()
    affiliate_legs_list = [l for l in legs if l.driver and l.driver.driver_type == "affiliate"]
    affiliate_takeback = []
    for leg in affiliate_legs_list:
        trip_type = leg.get_trip_type()
        vtype = getattr(getattr(getattr(leg, "reservation", None), "vehicle", None), "vehicle_type", None)
        vtype_str = str(vtype) if vtype else None
        customer = ""
        if leg.reservation and leg.reservation.customer:
            customer = leg.reservation.customer.get_full_name()

        # Check direct feasibility against every inhouse driver
        best_direct = None
        for driver in inhouse_drivers:
            dvtype = driver_vtypes.get(driver.id)
            # Driver's vehicle must be able to handle the leg's required type
            if vtype_str and vtype_str not in get_compatible_vehicle_types(dvtype or ""):
                continue
            sched = schedules.get(driver.id)
            if not sched:
                continue
            feas = check_feasibility(sched, leg, selected_date, cfg.inter_job_buffer)
            if feas.feasible:
                if best_direct is None or feas.buffer_minutes > best_direct["buffer"]:
                    best_direct = {
                        "driver_id": driver.id,
                        "driver_name": str(driver),
                        "buffer": feas.buffer_minutes,
                    }

        affiliate_takeback.append({
            "id": leg.id,
            "pickup_time": leg.pickup_time.strftime("%I:%M %p").lstrip("0") if leg.pickup_time else "",
            "pickup_location": leg.pickup_location,
            "dropoff_location": leg.dropoff_location,
            "trip_type": trip_type,
            "vehicle_type": vtype_str or "",
            "customer": customer,
            "revenue": float(leg.revenue_share or 0),
            "current_driver": str(leg.driver),
            "direct_takeback": best_direct,
        })

    # Build timeline data for each driver (driver_vtypes already loaded above)
    timeline_drivers = []
    for driver in inhouse_drivers:
        sched = schedules.get(driver.id)
        if not sched:
            continue
        slots_data = []
        for slot in sched.slots:
            slots_data.append({
                "leg_id": slot.leg_id,
                "pickup_time": slot.pickup_time.strftime("%I:%M %p").lstrip("0"),
                "pickup_minutes": slot.pickup_time.hour * 60 + slot.pickup_time.minute,
                "end_time": slot.estimated_end_time.strftime("%I:%M %p").lstrip("0") if slot.estimated_end_time else "",
                "end_minutes": int(slot.estimated_end_time.hour * 60 + slot.estimated_end_time.minute) if slot.estimated_end_time else 0,
                "pickup_location": slot.pickup_location[:35],
                "dropoff_location": slot.dropoff_location[:35],
                "trip_type": slot.trip_type,
                "customer_name": slot.customer_name,
                "revenue": float(slot.revenue or 0),
                "vehicle_type": slot.vehicle_type or "",
            })
        vtype = driver_vtypes.get(driver.id, "")
        timeline_drivers.append({
            "id": driver.id,
            "name": str(driver),
            "vehicle_type": vtype,
            "slots": slots_data,
            "total_legs": len(slots_data),
        })

    context = {
        "selected_date": selected_date,
        "nofit_legs": json.dumps(nofit_legs),
        "timeline_drivers": json.dumps(timeline_drivers),
        "affiliate_takeback": json.dumps(affiliate_takeback),
        "nofit_count": len(nofit_legs),
        "total_legs": len(legs),
        "inhouse_count": sum(1 for l in legs if l.driver and l.driver.driver_type == "inhouse"),
        "unassigned_count": len(unassigned_legs),
        "affiliate_count": len(affiliate_takeback),
    }
    return render(request, "dispatching/swap_tester.html", context)
