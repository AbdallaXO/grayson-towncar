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
from rates.models import Vehicle, Rate, Location
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

    # PERF TEMP START — dispatching index checkpoints
    import time as _time; _t0 = _time.monotonic()
    import logging as _logging; _perf = _logging.getLogger('perf')
    # PERF TEMP END

    selected_date = request.GET.get("date")
    driver_filter = request.GET.get("driver")
    trip_type_filter = request.GET.get("trip_type")
    vehicle_filter = request.GET.get("vehicle")

    try:
        selected_date = (
            datetime.strptime(selected_date, "%Y-%m-%d").date()
            if selected_date
            else timezone.localdate()
        )
    except ValueError:
        selected_date = timezone.localdate()

    # Base queryset: all legs for the selected date (shared by dashboard + timeline)
    _base_legs_qs = (
        Leg.objects.filter(pickup_date=selected_date)
        .exclude(reservation__status='cancelled').exclude(status='cancelled')
        .select_related(
            "reservation",
            "reservation__customer",
            "reservation__vehicle",
            "reservation__travel_agent",
            "reservation__travel_agent__user",
            "driver",
            "driver__profile",
            "driver_assigned_by",
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

    # Evaluate once — all legs for the day (used by timeline + gap suggestions + vehicle counts)
    # PERF TEMP START
    _t_qs = _time.monotonic()
    # PERF TEMP END
    _all_day_legs = list(_base_legs_qs)
    # PERF TEMP START
    _perf.info("DASHBOARD queryset eval: %.0fms (%d legs)", (_time.monotonic()-_t_qs)*1000, len(_all_day_legs))
    # PERF TEMP END

    # Apply filters in Python to avoid a second DB query
    legs = _all_day_legs
    if driver_filter:
        if driver_filter == "unassigned":
            legs = [l for l in legs if not l.driver]
        else:
            try:
                _df = int(driver_filter)
                legs = [l for l in legs if l.driver_id == _df]
            except (ValueError, TypeError):
                pass
    if vehicle_filter:
        legs = [l for l in legs if l.reservation and l.reservation.vehicle and getattr(l.reservation.vehicle, 'vehicle_type', None) == vehicle_filter]
    
    # Apply trip type filter if specified (filter in Python since it's a computed property)
    if trip_type_filter:
        filtered_legs = []
        for leg in legs:
            if leg.get_trip_type() == trip_type_filter:
                filtered_legs.append(leg)
        legs = filtered_legs

    # Vehicle type counts for the day (from already-fetched legs, no extra query)
    _vtype_counter = {}
    _vtype_labels = {
        'towncar': 'Town Car', 'mini_van': 'Mini Van', 'suv': 'SUV',
        'van': 'Van', 'Van(14 Pax)': 'Van 14',
    }
    for _leg in _all_day_legs:
        _vt = getattr(_leg.reservation.vehicle, 'vehicle_type', None) if _leg.reservation and _leg.reservation.vehicle else None
        if _vt:
            _vtype_counter[_vt] = _vtype_counter.get(_vt, 0) + 1
    vehicle_type_counts = [
        {'type': vt, 'label': _vtype_labels.get(vt, vt), 'count': cnt}
        for vt, cnt in _vtype_counter.items()
    ]
    vehicle_type_counts.sort(key=lambda x: -x['count'])

    # Get all drivers (single query) for assignment dropdown + inhouse vehicle cards
    drivers = list(
        Driver.objects.select_related("profile")
        .prefetch_related("weekly_schedule")
        .all()
    )
    inhouse_drivers = sorted(
        [d for d in drivers if d.driver_type == "inhouse"],
        key=lambda d: (d.profile.first_name, d.profile.last_name, d.profile.username),
    )
    inhouse_assignments = DriverVehicleAssignment.objects.filter(
        date=selected_date, driver__in=inhouse_drivers
    ).select_related("vehicle", "vehicle__vehicle_type")
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

    # Count legs per driver on the selected date (from already-fetched legs, no extra query)
    _all_leg_counts = {}
    for _leg in _all_day_legs:
        if _leg.driver_id:
            _all_leg_counts[_leg.driver_id] = _all_leg_counts.get(_leg.driver_id, 0) + 1
    for row in inhouse_driver_rows:
        row["leg_count"] = _all_leg_counts.get(row["driver"].id, 0)

    inhouse_assigned_count = sum(
        1 for row in inhouse_driver_rows if row["assignment"] and row["assignment"].vehicle
    )

    # Build map: vehicle_id → driver name (for "taken by X" in dropdowns)
    vehicle_taken_map = {}
    for row in inhouse_driver_rows:
        a = row.get("assignment")
        if a and a.vehicle_id:
            vehicle_taken_map[a.vehicle_id] = str(row["driver"])

    inhouse_drivers_list = []
    affiliate_drivers_list = []
    for driver in drivers:
        driver.day_leg_count = _all_leg_counts.get(driver.id, 0)
        display_name = str(driver)
        if driver.driver_type == "inhouse":
            assignment = assignment_map.get(driver.id)
            if assignment and assignment.vehicle and assignment.vehicle.vehicle_number:
                vehicle_number = assignment.vehicle.vehicle_number
                vehicle_number = vehicle_number.lstrip("#").strip()
                display_name = f"{display_name} - #{vehicle_number}"
            inhouse_drivers_list.append(driver)
        else:
            affiliate_drivers_list.append(driver)
        driver.dashboard_display_name = display_name

    # Calculate total revenue from legs on this day (only for admins)
    # Use per-leg revenue share (reservation price / number of legs) for accuracy
    _can_view_rev = can_view_revenue(request.user)
    if _can_view_rev:
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
        FleetVehicle.objects.select_related("vehicle_type").all(), key=_vehicle_sort_key
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

    # Pre-load timing cache BEFORE any estimate_job_end_time calls (avoids per-leg DB hits)
    from dispatching.scheduler import estimate_job_end_time, build_driver_schedules, preload_timing_cache as _preload_cache
    _preload_cache()

    # Pre-compute _estimated_end_dt for ALL legs (reused by build_driver_schedules)
    for leg in _all_day_legs:
        try:
            leg._estimated_end_dt = estimate_job_end_time(leg, selected_date)
        except Exception:
            leg._estimated_end_dt = None

    # Annotate displayed legs with cleared time + duration strings
    for leg in legs:
        end_dt = leg._estimated_end_dt
        if end_dt:
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
        else:
            leg.cleared_time = None
            leg.duration_display = None

        # Actual cleared time from status history (if completed)
        leg.actual_cleared_time = None
        leg.actual_duration_display = None
        if leg.status == 'completed':
            pickup_dt = datetime.combine(selected_date, leg.pickup_time)
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

    # Annotate en-route legs with live GPS ETA for dispatchers
    if selected_date == today:
        from drivers.views import _annotate_legs_with_live_eta
        _annotate_legs_with_live_eta(list(legs) if not isinstance(legs, list) else legs)

    # Compute turnaround warnings: flag legs where gap from previous leg is < 20 min
    _legs_list = list(legs) if not isinstance(legs, list) else legs
    _driver_legs = {}
    for leg in _legs_list:
        leg.turnaround_warning = None
        if leg.driver_id:
            _driver_legs.setdefault(leg.driver_id, []).append(leg)
    for _d_id, _d_legs in _driver_legs.items():
        _d_legs.sort(key=lambda l: l.pickup_time)
        for i in range(1, len(_d_legs)):
            prev_leg = _d_legs[i - 1]
            cur_leg = _d_legs[i]
            if prev_leg._estimated_end_dt:
                cur_pickup_dt = datetime.combine(selected_date, cur_leg.pickup_time)
                gap_min = int((cur_pickup_dt - prev_leg._estimated_end_dt).total_seconds() / 60)
                if gap_min < 0:
                    cur_leg.turnaround_warning = f"Overlap: conflicts by {abs(gap_min)} min"
                elif gap_min < 10:
                    cur_leg.turnaround_warning = f"Critical: only {gap_min} min gap"
                elif gap_min < 20:
                    cur_leg.turnaround_warning = f"Tight turnaround: {gap_min} min gap"

    # Build compact driver timeline for in-house drivers with assignments
    # Reuse _all_day_legs (already fetched with all select_related + prefetch) — no extra query
    _all_legs_for_timeline = _all_day_legs
    _all_inhouse = [row["driver"] for row in inhouse_driver_rows if not row.get("is_off_today")]
    # PERF TEMP START
    _t_sched = _time.monotonic()
    # PERF TEMP END
    _driver_schedules = build_driver_schedules(_all_legs_for_timeline, _all_inhouse, selected_date)
    # PERF TEMP START
    _perf.info("DASHBOARD build_driver_schedules: %.0fms", (_time.monotonic()-_t_sched)*1000)
    # PERF TEMP END

    # Build leg-id → latest status info map for timeline popup
    _leg_status_map = {}
    _now = timezone.now()
    for _tleg in _all_legs_for_timeline:
        _sh_list = list(_tleg.status_history.all())  # already prefetched, ordered -timestamp
        if _sh_list:
            _latest = _sh_list[0]
            _local_ts = timezone.localtime(_latest.timestamp)
            _ago_secs = int((_now - _latest.timestamp).total_seconds())
            if _ago_secs < 60:
                _ago_str = "just now"
            elif _ago_secs < 3600:
                _ago_str = f"{_ago_secs // 60} min ago"
            else:
                _hrs = _ago_secs // 3600
                _mins = (_ago_secs % 3600) // 60
                _ago_str = f"{_hrs}h {_mins}m ago" if _mins else f"{_hrs}h ago"
            _status_label = dict(LegStatus.STATUS_CHOICES).get(_latest.status, _latest.status).title()
            _leg_status_map[_tleg.id] = {
                'status_label': _status_label,
                'status_time': _local_ts.strftime('%I:%M %p').lstrip('0'),
                'status_ago': _ago_str,
            }

    # Collect unassigned + affiliate legs for gap suggestions
    _gap_candidates = []
    for _tleg in _all_legs_for_timeline:
        _is_unassigned = not _tleg.driver
        _is_affiliate = (
            _tleg.driver and hasattr(_tleg.driver, 'driver_type')
            and _tleg.driver.driver_type == 'affiliate'
        )
        if _is_unassigned or _is_affiliate:
            _pickup_dt = datetime.combine(selected_date, _tleg.pickup_time)
            _trip = _tleg.get_trip_type() if hasattr(_tleg, 'get_trip_type') else 'other'
            _flight_str = ''
            if _tleg.flight_information:
                _fi = _tleg.flight_information
                _flight_str = f"{_fi.airline or ''} {_fi.flight_number or ''}".strip()
            _pax = _tleg.reservation.passenger_count if _tleg.reservation else ''
            _dropoff = _tleg.dropoff_location or ''
            _gap_candidates.append({
                'leg_id': _tleg.id,
                'pickup_time': _tleg.pickup_time,
                'pickup_dt': _pickup_dt,
                'pickup_display': _tleg.pickup_time.strftime('%I:%M %p').lstrip('0'),
                'customer': str(_tleg.reservation.customer) if _tleg.reservation else '',
                'pickup_location': _tleg.pickup_location or '',
                'dropoff_location': _dropoff,
                'trip_type': _trip,
                'source': 'affiliate' if _is_affiliate else 'unassigned',
                'driver_name': str(_tleg.driver) if _is_affiliate else '',
                'vehicle_type': getattr(_tleg.reservation.vehicle, 'vehicle_type', '') if _tleg.reservation and _tleg.reservation.vehicle else '',
                'flight_info': _flight_str,
                'passengers': str(_pax) if _pax else '',
                'status': _tleg.status or '',
            })

    # Timeline display range
    _hours_with_legs = set()
    for _leg in _all_legs_for_timeline:
        _hours_with_legs.add(_leg.pickup_time.hour)
    display_start = min(_hours_with_legs) if _hours_with_legs else 6
    display_end = max(_hours_with_legs) + 1 if _hours_with_legs else 22
    display_start = min(display_start, 6)
    display_end = max(display_end, 22)
    timeline_hours = list(range(display_start, display_end + 1))
    total_display_minutes = (display_end - display_start + 1) * 60

    # Map driver ID → vehicle number + type for timeline display
    _driver_vehicle_map = {}
    _driver_vehicle_type_map = {}
    for _row in inhouse_driver_rows:
        _a = _row.get("assignment")
        if _a and _a.vehicle:
            if _a.vehicle.vehicle_number:
                _driver_vehicle_map[_row["driver"].id] = _a.vehicle.vehicle_number
            if _a.vehicle.vehicle_type:
                _driver_vehicle_type_map[_row["driver"].id] = str(_a.vehicle.vehicle_type)

    # Get previous day's last leg per driver (for overnight turnaround display)
    _prev_day = selected_date - timedelta(days=1)
    _prev_day_last = {}
    _prev_legs = (
        Leg.objects.filter(pickup_date=_prev_day, driver__in=_all_inhouse)
        .exclude(status="cancelled")
        .select_related("driver")
        .order_by("driver_id", "-pickup_time")
    )
    for _pl in _prev_legs:
        if _pl.driver_id not in _prev_day_last:
            try:
                _end = estimate_job_end_time(_pl, _prev_day)
                _prev_day_last[_pl.driver_id] = _end.strftime('%I:%M %p').lstrip('0')
            except Exception:
                _prev_day_last[_pl.driver_id] = _pl.pickup_time.strftime('%I:%M %p').lstrip('0') + '?'

    # Get previous day's vehicle assignments (for showing which vehicle driver used)
    _prev_day_vehicle = {}
    _prev_day_assigns = DriverVehicleAssignment.objects.filter(
        date=_prev_day, driver__in=_all_inhouse
    ).select_related('vehicle', 'vehicle__vehicle_type')
    for _pda in _prev_day_assigns:
        if _pda.vehicle:
            _vn = _pda.vehicle.vehicle_number or ''
            _vt = str(_pda.vehicle.vehicle_type) if _pda.vehicle.vehicle_type else ''
            _prev_day_vehicle[_pda.driver_id] = f"#{_vn} {_vt}".strip() if _vn else _vt

    inhouse_timeline = []
    for _driver in _all_inhouse:
        _sched = _driver_schedules.get(_driver.id)
        if not _sched or not _sched.slots:
            continue
        # Position/width for each slot + status timestamps
        for _slot in _sched.slots:
            _slot_start_min = (_slot.pickup_time.hour - display_start) * 60 + _slot.pickup_time.minute
            _slot_end_min = (_slot.estimated_end_time.hour - display_start) * 60 + _slot.estimated_end_time.minute
            _duration = max(_slot_end_min - _slot_start_min, 15)
            _slot.position_pct = round(max(0, _slot_start_min / total_display_minutes * 100), 1)
            _slot.width_pct = round(min(_duration / total_display_minutes * 100, 100 - _slot.position_pct), 1)
            _slot.end_time_display = _slot.estimated_end_time.strftime('%I:%M').lstrip('0')
            # Annotate with status timestamp info
            _sinfo = _leg_status_map.get(_slot.leg_id)
            _slot.status_label = _sinfo['status_label'] if _sinfo else ''
            _slot.status_time = _sinfo['status_time'] if _sinfo else ''
            _slot.status_ago = _sinfo['status_ago'] if _sinfo else ''
        # Gaps
        _gaps = []
        for _i in range(len(_sched.slots) - 1):
            _cur_end = _sched.slots[_i].estimated_end_time
            _nxt_start = datetime.combine(selected_date, _sched.slots[_i + 1].pickup_time)
            _gap_min = int((_nxt_start - _cur_end).total_seconds() / 60)
            _end_min = (_cur_end.hour - display_start) * 60 + _cur_end.minute
            _start_min = (_sched.slots[_i + 1].pickup_time.hour - display_start) * 60 + _sched.slots[_i + 1].pickup_time.minute
            _gap_pos = round(max(0, _end_min / total_display_minutes * 100), 1)
            _gap_width = round(max(0, (_start_min - _end_min) / total_display_minutes * 100), 1)
            if _gap_min >= 60:
                _gh, _gm = divmod(_gap_min, 60)
                _gap_display = f"{_gh}h,{_gm}m" if _gm else f"{_gh}h"
            else:
                _gap_display = f"{_gap_min}m"
            _is_big = _gap_min >= 45
            # Find unassigned + affiliate legs that could fit in big gaps
            _fitting = []
            if _is_big:
                _gap_start_dt = _cur_end
                _gap_end_dt = _nxt_start
                for _ul in _gap_candidates:
                    if _ul['pickup_dt'] >= _gap_start_dt and _ul['pickup_dt'] < _gap_end_dt:
                        _fitting.append(_ul)
            _gaps.append({
                'gap_minutes': _gap_min,
                'gap_display': _gap_display,
                'is_tight': _gap_min < 20,
                'is_critical': _gap_min < 10,
                'is_big': _is_big,
                'fitting_unassigned': _fitting,
                'position_pct': _gap_pos,
                'width_pct': _gap_width,
            })
        inhouse_timeline.append({
            'driver': _driver,
            'schedule': _sched,
            'gaps': _gaps,
            'total_legs': _sched.total_legs,
            'vehicle_number': _driver_vehicle_map.get(_driver.id, ''),
            'vehicle_type_label': _driver_vehicle_type_map.get(_driver.id, ''),
            'prev_night_cleared': _prev_day_last.get(_driver.id, ''),
            'prev_night_vehicle': _prev_day_vehicle.get(_driver.id, ''),
        })

    # Build unassigned timeline slots for drag-and-drop
    _leg_by_id = {_l.id: _l for _l in _all_legs_for_timeline}  # O(1) lookup
    _unassigned_timeline_slots = []
    for _gc in _gap_candidates:
        if _gc['source'] != 'unassigned':
            continue
        _pt = _gc['pickup_time']
        _slot_start_min = (_pt.hour - display_start) * 60 + _pt.minute
        _uleg = _leg_by_id.get(_gc['leg_id'])
        _end_dt = getattr(_uleg, '_estimated_end_dt', None) if _uleg else None
        if _end_dt:
            _slot_end_min = (_end_dt.hour - display_start) * 60 + _end_dt.minute
        else:
            _slot_end_min = _slot_start_min + 45  # default 45 min estimate
        _duration = max(_slot_end_min - _slot_start_min, 15)
        _pos = round(max(0, _slot_start_min / total_display_minutes * 100), 1)
        _wid = round(min(_duration / total_display_minutes * 100, 100 - _pos), 1)
        _sinfo = _leg_status_map.get(_gc['leg_id'])
        _unassigned_timeline_slots.append({
            'leg_id': _gc['leg_id'],
            'trip_type': _gc['trip_type'],
            'pickup_display': _gc['pickup_display'],
            'pickup_time_raw': _gc['pickup_time'].strftime('%I:%M').lstrip('0') if _gc['pickup_time'] else '',
            'customer': _gc['customer'],
            'pickup_location': _gc['pickup_location'],
            'dropoff_location': _gc['dropoff_location'],
            'vehicle_type': _gc['vehicle_type'],
            'flight_info': _gc['flight_info'],
            'status': _gc['status'],
            'position_pct': _pos,
            'width_pct': _wid,
            'end_time_display': _end_dt.strftime('%I:%M').lstrip('0') if _end_dt else '',
            'status_label': _sinfo['status_label'] if _sinfo else '',
            'status_time': _sinfo['status_time'] if _sinfo else '',
            'status_ago': _sinfo['status_ago'] if _sinfo else '',
        })

    prev_date = selected_date - timedelta(days=1)
    next_date = selected_date + timedelta(days=1)

    # Oldest flight refresh timestamp for arrival legs on this date
    # Computed from already-fetched _all_day_legs (avoids extra DB query)
    oldest_flight_refresh = None
    for _tleg in _all_day_legs:
        if (
            _tleg.flight_information
            and _tleg.flight_information.last_updated
            and _tleg.status not in ("completed", "cancelled")
        ):
            if oldest_flight_refresh is None or _tleg.flight_information.last_updated < oldest_flight_refresh:
                oldest_flight_refresh = _tleg.flight_information.last_updated

    context = {
        "legs": legs,
        "selected_date": selected_date,
        "prev_date": prev_date,
        "next_date": next_date,
        "oldest_flight_refresh": oldest_flight_refresh,
        "driver_filter": driver_filter,
        "trip_type_filter": trip_type_filter,
        "vehicle_filter": vehicle_filter,
        "vehicle_type_counts": vehicle_type_counts,
        "total_legs": len(legs),
        "total_revenue": total_revenue,
        "driver_coverage": driver_coverage,
        "can_view_revenue": _can_view_rev,
        "drivers": drivers,
        "inhouse_drivers_list": inhouse_drivers_list,
        "affiliate_drivers_list": affiliate_drivers_list,
        "inhouse_driver_rows": inhouse_driver_rows,
        "inhouse_vehicles": inhouse_vehicles,
        "inhouse_assigned_count": inhouse_assigned_count,
        "vehicle_taken_map": vehicle_taken_map,
        "inhouse_timeline": inhouse_timeline,
        "timeline_hours": timeline_hours,
        "unassigned_timeline_slots": _unassigned_timeline_slots,
    }

    # PERF TEMP START
    _t1 = _time.monotonic()
    _response = render(request, "dispatching/legs_filter.html", context)
    _t2 = _time.monotonic()
    _perf.info(
        "DASHBOARD total: view=%.0fms template=%.0fms total=%.0fms",
        (_t1-_t0)*1000, (_t2-_t1)*1000, (_t2-_t0)*1000,
    )
    # PERF TEMP END
    return _response


@login_required(login_url="login")
def schedule_board(request):
    """
    Lightweight Schedule Board: drag-and-drop driver timeline only.
    No legs table, no mobile cards — just the timeline for fast reshuffling.
    """
    if not request.user.is_staff:
        return redirect("home")

    from dispatching.scheduler import (
        build_driver_schedules, estimate_job_end_time,
        preload_timing_cache as _preload_cache,
    )
    from drivers.models import DriverVehicleAssignment

    selected_date = request.GET.get("date")
    try:
        selected_date = (
            datetime.strptime(selected_date, "%Y-%m-%d").date()
            if selected_date
            else timezone.localdate()
        )
    except ValueError:
        selected_date = timezone.localdate()

    prev_date = selected_date - timedelta(days=1)
    next_date = selected_date + timedelta(days=1)

    # Fetch all legs for the date (single query)
    all_legs = list(
        Leg.objects.filter(pickup_date=selected_date)
        .exclude(reservation__status="cancelled")
        .exclude(status="cancelled")
        .select_related(
            "driver", "reservation", "reservation__customer",
            "reservation__vehicle", "flight_information", "cruise_information",
        )
        .prefetch_related(
            Prefetch("status_history", queryset=LegStatus.objects.select_related("updated_by").order_by("-timestamp"))
        )
        .order_by("pickup_time")
    )

    # Pre-compute end times
    _preload_cache()
    for leg in all_legs:
        try:
            leg._estimated_end_dt = estimate_job_end_time(leg, selected_date)
        except Exception:
            leg._estimated_end_dt = None

    # Get inhouse drivers with vehicle assignments, sorted by vehicle number
    inhouse_drivers = list(
        Driver.objects.filter(driver_type="inhouse")
        .select_related("profile")
        .order_by("profile__first_name")
    )
    assignments = {
        a.driver_id: a
        for a in DriverVehicleAssignment.objects.filter(
            driver__in=inhouse_drivers, date=selected_date
        ).select_related("vehicle", "vehicle__vehicle_type")
    }
    # Sort drivers: those with vehicle assignments first (by vehicle number), then unassigned
    inhouse_drivers.sort(
        key=lambda d: (
            0 if d.id in assignments else 1,
            assignments[d.id].vehicle.vehicle_number if d.id in assignments else 999,
        )
    )

    # Build schedules
    _driver_schedules = build_driver_schedules(all_legs, inhouse_drivers, selected_date)

    # Timeline hours range
    _hours_with_legs = set()
    for leg in all_legs:
        _hours_with_legs.add(leg.pickup_time.hour)
    display_start = min(_hours_with_legs) if _hours_with_legs else 6
    display_end = max(_hours_with_legs) + 1 if _hours_with_legs else 22
    display_start = min(display_start, 6)
    display_end = max(display_end, 22)
    timeline_hours = list(range(display_start, display_end + 1))
    total_display_minutes = (display_end - display_start + 1) * 60

    # Build half-hour ticks for the schedule board grid
    _timeline_ticks = []
    for h in range(display_start, display_end + 1):
        # Full hour tick
        _min_offset = (h - display_start) * 60
        _pct = round(_min_offset / total_display_minutes * 100, 2)
        if h == 0:
            _lbl = '12a'
        elif h < 12:
            _lbl = f'{h}a'
        elif h == 12:
            _lbl = '12p'
        else:
            _lbl = f'{h-12}p'
        _timeline_ticks.append({'pct': _pct, 'label': _lbl, 'is_hour': True})
        # Half-hour tick
        _half_min = _min_offset + 30
        if _half_min < total_display_minutes:
            _half_pct = round(_half_min / total_display_minutes * 100, 2)
            _timeline_ticks.append({'pct': _half_pct, 'label': '', 'is_hour': False})

    # Status map
    _now = timezone.now()
    _leg_status_map = {}
    for leg in all_legs:
        _sh_list = list(leg.status_history.all())
        if _sh_list:
            _latest = _sh_list[0]
            _local_ts = timezone.localtime(_latest.timestamp)
            _ago_secs = int((_now - _latest.timestamp).total_seconds())
            if _ago_secs < 60:
                _ago_str = "just now"
            elif _ago_secs < 3600:
                _ago_str = f"{_ago_secs // 60} min ago"
            else:
                _hrs = _ago_secs // 3600
                _mins = (_ago_secs % 3600) // 60
                _ago_str = f"{_hrs}h {_mins}m ago" if _mins else f"{_hrs}h ago"
            _status_label = dict(LegStatus.STATUS_CHOICES).get(_latest.status, _latest.status).title()
            _leg_status_map[leg.id] = {
                'status_label': _status_label,
                'status_time': _local_ts.strftime('%I:%M %p').lstrip('0'),
                'status_ago': _ago_str,
            }

    # Get previous day's last leg per driver (for overnight turnaround display)
    prev_day = selected_date - timedelta(days=1)
    _prev_day_last = {}
    _prev_legs = (
        Leg.objects.filter(pickup_date=prev_day, driver__in=inhouse_drivers)
        .exclude(status="cancelled")
        .select_related("driver")
        .order_by("driver_id", "-pickup_time")
    )
    for _pl in _prev_legs:
        if _pl.driver_id not in _prev_day_last:
            try:
                _end = estimate_job_end_time(_pl, prev_day)
                _prev_day_last[_pl.driver_id] = _end.strftime('%I:%M %p').lstrip('0')
            except Exception:
                _prev_day_last[_pl.driver_id] = _pl.pickup_time.strftime('%I:%M %p').lstrip('0') + '?'

    # Get previous day's vehicle assignments
    _sb_prev_day_vehicle = {}
    _sb_prev_assigns = DriverVehicleAssignment.objects.filter(
        date=prev_day, driver__in=inhouse_drivers
    ).select_related('vehicle', 'vehicle__vehicle_type')
    for _sbpda in _sb_prev_assigns:
        if _sbpda.vehicle:
            _vn = _sbpda.vehicle.vehicle_number or ''
            _vt = str(_sbpda.vehicle.vehicle_type) if _sbpda.vehicle.vehicle_type else ''
            _sb_prev_day_vehicle[_sbpda.driver_id] = f"#{_vn} {_vt}".strip() if _vn else _vt

    # Build inhouse timeline
    inhouse_timeline = []
    for driver in inhouse_drivers:
        sched = _driver_schedules.get(driver.id)
        if not sched or not sched.slots:
            continue
        assignment = assignments.get(driver.id)
        vehicle_number = ''
        vehicle_type_label = ''
        if assignment and assignment.vehicle:
            vehicle_number = assignment.vehicle.vehicle_number or ''
            if assignment.vehicle.vehicle_type:
                vehicle_type_label = str(assignment.vehicle.vehicle_type)
        for slot in sched.slots:
            _start_min = (slot.pickup_time.hour - display_start) * 60 + slot.pickup_time.minute
            _end_min = (slot.estimated_end_time.hour - display_start) * 60 + slot.estimated_end_time.minute
            _dur = max(_end_min - _start_min, 15)
            slot.position_pct = round(max(0, _start_min / total_display_minutes * 100), 1)
            slot.width_pct = round(min(_dur / total_display_minutes * 100, 100 - slot.position_pct), 1)
            slot.end_time_display = slot.estimated_end_time.strftime('%I:%M').lstrip('0')
            _sinfo = _leg_status_map.get(slot.leg_id)
            slot.status_label = _sinfo['status_label'] if _sinfo else ''
            slot.status_time = _sinfo['status_time'] if _sinfo else ''
            slot.status_ago = _sinfo['status_ago'] if _sinfo else ''
        inhouse_timeline.append({
            'driver': driver,
            'schedule': sched,
            'total_legs': sched.total_legs,
            'vehicle_number': vehicle_number,
            'vehicle_type_label': vehicle_type_label,
            'prev_night_cleared': _prev_day_last.get(driver.id, ''),
            'prev_night_vehicle': _sb_prev_day_vehicle.get(driver.id, ''),
        })

    # Build unassigned timeline slots
    _leg_by_id = {l.id: l for l in all_legs}
    unassigned_timeline_slots = []
    for leg in all_legs:
        if leg.driver is not None:
            continue
        pt = leg.pickup_time
        _start_min = (pt.hour - display_start) * 60 + pt.minute
        _end_dt = getattr(leg, '_estimated_end_dt', None)
        if _end_dt:
            _end_min = (_end_dt.hour - display_start) * 60 + _end_dt.minute
        else:
            _end_min = _start_min + 45
        _dur = max(_end_min - _start_min, 15)
        _pos = round(max(0, _start_min / total_display_minutes * 100), 1)
        _wid = round(min(_dur / total_display_minutes * 100, 100 - _pos), 1)
        _sinfo = _leg_status_map.get(leg.id)
        _trip = leg.get_trip_type() if hasattr(leg, 'get_trip_type') else 'other'
        _customer = str(leg.reservation.customer) if leg.reservation and leg.reservation.customer else ''
        _flight_str = ''
        if leg.flight_information:
            _fi = leg.flight_information
            _flight_str = f"{_fi.airline or ''} {_fi.flight_number or ''}".strip()
        _vtype = getattr(leg.reservation.vehicle, 'vehicle_type', '') if leg.reservation and leg.reservation.vehicle else ''
        _vabbr_map = {'towncar': 'TC', 'suv': 'SUV', 'mini_van': 'MV', 'van': 'VAN', 'Van(14 Pax)': 'V14'}
        _vabbr = _vabbr_map.get(str(_vtype), '') if _vtype else ''
        unassigned_timeline_slots.append({
            'leg_id': leg.id,
            'trip_type': _trip,
            'pickup_display': pt.strftime('%I:%M %p').lstrip('0'),
            'customer': _customer,
            'pickup_location': leg.pickup_location or '',
            'dropoff_location': leg.dropoff_location or '',
            'vehicle_type': str(_vtype) if _vtype else '',
            'vehicle_abbr': _vabbr,
            'flight_info': _flight_str,
            'status': leg.status or '',
            'position_pct': _pos,
            'width_pct': _wid,
            'end_time_display': _end_dt.strftime('%I:%M').lstrip('0') if _end_dt else '',
            'status_label': _sinfo['status_label'] if _sinfo else '',
            'status_time': _sinfo['status_time'] if _sinfo else '',
            'status_ago': _sinfo['status_ago'] if _sinfo else '',
        })

    # Sort unassigned slots: bigger vehicles first, then by pickup time
    _vehicle_sort_order = {'Van(14 Pax)': 0, 'van': 1, 'suv': 2, 'mini_van': 3, 'towncar': 4, '': 5}
    unassigned_timeline_slots.sort(key=lambda s: (_vehicle_sort_order.get(s['vehicle_type'], 5), s['position_pct']))

    # Assign lanes to unassigned slots so overlapping jobs stack vertically
    _lane_slot_height = 18  # px per lane row (matches CSS)
    _lane_gap = 2
    _lane_ends = []  # tracks end position_pct of each lane
    for _us in unassigned_timeline_slots:
        _left = _us['position_pct']
        _right = _left + _us['width_pct']
        # Find first lane where this slot doesn't overlap
        placed = False
        for i, lane_end in enumerate(_lane_ends):
            if _left >= lane_end:
                _us['lane'] = i
                _lane_ends[i] = _right
                placed = True
                break
        if not placed:
            _us['lane'] = len(_lane_ends)
            _lane_ends.append(_right)
        _us['lane_top'] = _us['lane'] * (_lane_slot_height + _lane_gap) + 2
    _num_lanes = max(len(_lane_ends), 1)
    _unassigned_lane_height = _num_lanes * (_lane_slot_height + _lane_gap) + 4

    # Summary counts
    total_legs = len(all_legs)
    assigned_count = sum(1 for l in all_legs if l.driver)
    unassigned_count = total_legs - assigned_count

    context = {
        "selected_date": selected_date,
        "prev_date": prev_date,
        "next_date": next_date,
        "inhouse_timeline": inhouse_timeline,
        "timeline_hours": timeline_hours,
        "timeline_ticks": _timeline_ticks,
        "unassigned_timeline_slots": unassigned_timeline_slots,
        "unassigned_lane_height": _unassigned_lane_height,
        "total_legs": total_legs,
        "assigned_count": assigned_count,
        "unassigned_count": unassigned_count,
    }
    return render(request, "dispatching/schedule_board.html", context)


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
            elif status_filter == "card_saved":
                queryset = queryset.filter(payments__status="card_saved").distinct()
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
            card_saved_count=Count("id", filter=Q(payments__status="card_saved"), distinct=True),
        )

        # Only calculate revenue for admins
        if can_view_revenue(self.request.user):
            revenue_stats = queryset.aggregate(
                total_revenue=Sum("total_price", filter=Q(payments__status="paid")),
                card_saved_total=Sum("total_price", filter=Q(payments__status="card_saved")),
            )
            total_revenue = revenue_stats["total_revenue"] or 0
            card_saved_total = revenue_stats["card_saved_total"] or 0
        else:
            total_revenue = None
            card_saved_total = None

        # Add statistics to context
        context.update(
            {
                "total_reservations": stats["total_count"],
                "pending_reservations": stats["pending_count"],
                "confirmed_reservations": stats["confirmed_count"],
                "need_payment_count": stats["need_payment_count"],
                "card_saved_count": stats["card_saved_count"],
                "card_saved_total": card_saved_total,
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
                    "card_saved_count": context["card_saved_count"],
                    "card_saved_total": context["card_saved_total"],
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

    # Payment reminder emails sent for this reservation
    from ops.models import EmailLog
    payment_reminders_sent = list(
        EmailLog.objects.filter(
            reservation=reservation,
            email_type="payment_reminder",
        )
        .select_related("sent_by")
        .order_by("-sent_at")
    )

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
        "payment_reminders_sent": payment_reminders_sent,
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
            leg_forms = [
                LegForm(request.POST, instance=leg, prefix=f"leg_{i + 1}")
                for i, leg in enumerate(reservation.legs.all())
            ]
            if not leg_forms:
                leg_forms.append(LegForm(request.POST, prefix="leg_1"))
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

    # Annotate en-route legs with live GPS ETA
    try:
        from drivers.views import _annotate_legs_with_live_eta
        _annotate_legs_with_live_eta(list(page_obj))
    except Exception:
        pass

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
                    # Single save: Leg.save() auto-fills pay when driver changes
                    leg.save(update_fields=['driver', 'driver_assigned_by', 'driver_assigned_at'])
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
                leg.save(update_fields=['driver', 'driver_assigned_by', 'driver_assigned_at'])
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
                    leg.save(update_fields=['status', 'status_changed_by', 'status_changed_at'])
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
        elif field == "private_notes":
            leg.private_notes = value or ""
            leg.save(update_fields=["private_notes"])
            logger.info(f"Updated leg {leg_id} private_notes by {request.user.username}")
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
@require_http_methods(["GET"])
def check_driver_feasibility(request):
    """
    AJAX endpoint: Check if assigning a driver to a leg creates scheduling conflicts.
    Uses the scheduler's check_feasibility() for accurate gap/overlap detection.
    """
    if not request.user.is_staff:
        return JsonResponse({"error": "Permission denied"}, status=403)

    leg_id = request.GET.get("leg_id")
    driver_id = request.GET.get("driver_id")

    if not leg_id or not driver_id:
        return JsonResponse({"error": "leg_id and driver_id required"}, status=400)

    try:
        from dispatching.scheduler import (
            build_driver_schedules, check_feasibility, preload_timing_cache,
            estimate_job_end_time,
        )
        from drivers.models import Driver

        leg = Leg.objects.select_related(
            "reservation", "reservation__vehicle", "flight_information", "cruise_information"
        ).get(id=leg_id)
        driver = Driver.objects.get(id=driver_id)
        target_date = leg.pickup_date

        # Check vehicle type match
        vehicle_match = True
        vehicle_mismatch_detail = ""
        required_type = getattr(leg.reservation.vehicle, 'vehicle_type', None) if leg.reservation.vehicle else None
        if required_type and driver.driver_type == "inhouse":
            from dispatching.scheduler import get_compatible_vehicle_types
            try:
                assignment = DriverVehicleAssignment.objects.select_related(
                    "vehicle", "vehicle__vehicle_type"
                ).get(driver=driver, date=target_date)
                if assignment.vehicle and assignment.vehicle.vehicle_type:
                    assigned_type = assignment.vehicle.vehicle_type.vehicle_type
                    compatible_types = get_compatible_vehicle_types(assigned_type)
                    if str(required_type) not in compatible_types:
                        vehicle_match = False
                        vehicle_mismatch_detail = f"Driver's vehicle is {assigned_type}, reservation requires {required_type}"
                else:
                    vehicle_match = False
                    vehicle_mismatch_detail = "Driver has no vehicle assigned today"
            except DriverVehicleAssignment.DoesNotExist:
                vehicle_match = False
                vehicle_mismatch_detail = "Driver has no vehicle assigned today"

        preload_timing_cache()

        # Build driver's current schedule (excluding this leg in case of reassignment)
        existing_legs = list(
            Leg.objects.select_related(
                "reservation", "flight_information", "cruise_information"
            ).filter(
                driver=driver,
                pickup_date=target_date,
                status__in=["confirmed", "in-progress", "on-the-way", "picked-up", "on-location"],
            ).exclude(id=leg.id).order_by("pickup_time")
        )

        schedules = build_driver_schedules(existing_legs, [driver], target_date)
        driver_schedule = schedules.get(driver.id)

        if not driver_schedule:
            # No existing schedule — always feasible
            end_time = estimate_job_end_time(leg, target_date)
            warnings = []
            if not vehicle_match and vehicle_mismatch_detail:
                warnings.append(vehicle_mismatch_detail)
            return JsonResponse({
                "feasible": True,
                "buffer_minutes": 999,
                "warnings": warnings,
                "reason": "No other trips — fully available",
                "estimated_end": end_time.strftime("%I:%M %p").lstrip("0") if end_time else None,
                "existing_trips": 0,
                "vehicle_match": vehicle_match,
                "vehicle_mismatch_detail": vehicle_mismatch_detail,
            })

        from dispatching.models import SchedulerSettings
        cfg = SchedulerSettings.get_settings()
        result = check_feasibility(driver_schedule, leg, target_date, arrival_grace=cfg.arrival_grace_minutes)
        end_time = estimate_job_end_time(leg, target_date)

        warnings = list(result.warnings) if result.warnings else []
        if not vehicle_match and vehicle_mismatch_detail:
            warnings.append(vehicle_mismatch_detail)

        return JsonResponse({
            "feasible": result.feasible,
            "buffer_minutes": result.buffer_minutes,
            "warnings": warnings,
            "reason": result.reason,
            "estimated_end": end_time.strftime("%I:%M %p").lstrip("0") if end_time else None,
            "existing_trips": len(driver_schedule.slots),
            "vehicle_match": vehicle_match,
            "vehicle_mismatch_detail": vehicle_mismatch_detail,
        })

    except Leg.DoesNotExist:
        return JsonResponse({"error": "Leg not found"}, status=404)
    except Exception as e:
        logger.error(f"Feasibility check error: {str(e)}")
        return JsonResponse({"error": str(e)}, status=500)


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
    """
    Copy vehicle assignments from the most recent previous date to a target date.

    Two modes:
    - preview=true: returns what WOULD be copied, with off-day flags, for review modal
    - preview=false (default): performs the copy, respecting exclude_driver_ids
    """
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

    is_preview = data.get("preview", False)

    # Find the most recent previous date with assignments
    prev = (
        DriverVehicleAssignment.objects.filter(date__lt=target_date)
        .order_by("-date")
        .values_list("date", flat=True)
        .first()
    )
    if not prev:
        return JsonResponse({"success": False, "error": "No previous assignments found"})

    prev_assignments = (
        DriverVehicleAssignment.objects.filter(date=prev)
        .select_related("driver", "driver__profile", "vehicle")
        .prefetch_related("driver__weekly_schedule")
    )

    # Build driver list with off-day status
    target_dow = target_date.weekday()
    drivers_list = []
    for a in prev_assignments:
        is_off = False
        for entry in a.driver.weekly_schedule.all():
            if entry.day_of_week == target_dow:
                is_off = not entry.is_available
                break
        vnum = a.vehicle.vehicle_number if a.vehicle else ''
        vtype = str(a.vehicle.vehicle_type) if a.vehicle and a.vehicle.vehicle_type else ''
        drivers_list.append({
            "driver_id": a.driver_id,
            "driver_name": (a.driver.profile.first_name or str(a.driver)) if a.driver.profile else str(a.driver),
            "vehicle_number": vnum,
            "vehicle_type": vtype,
            "is_off_today": is_off,
        })

    if is_preview:
        return JsonResponse({
            "success": True,
            "source_date": prev.strftime("%Y-%m-%d"),
            "drivers": drivers_list,
        })

    # Perform the copy — respect exclude list
    exclude_ids = set(data.get("exclude_driver_ids", []))
    copied = 0
    result_map = {}
    for a in prev_assignments:
        if a.driver_id in exclude_ids:
            continue
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
                        _run_in_background(send_reservation_confirmation, reservation, sent_by=request.user)
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

            # Clear ALL stale data from the old flight so nothing lingers
            if flight_data.get('status') in ('not_found', 'not_orlando'):
                flight.flight_iata = ''
                flight.origin = ''
                flight.destination = ''
                flight.status = 'Not Found'
                flight.scheduled_arrival_local = None
                flight.estimated_arrival_local = None
                flight.actual_arrival_local = None
                flight.scheduled_gate_arrival_local = None
                flight.estimated_gate_arrival_local = None
                flight.actual_gate_arrival_local = None
                flight.terminal = ''
                flight.gate = ''
                flight.baggage_claim = ''
                flight.last_updated = timezone.now()
                flight.save()
                logger.info(f"Cleared stale flight data for leg {leg.id} (flight {flight_ident} not found)")

                # Create a flight verification task if one doesn't already exist
                from ops.models import OperationalTask
                existing_task = OperationalTask.objects.filter(
                    leg=leg,
                    task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
                    status__in=list(OperationalTask.OPEN_STATUSES),
                ).first()
                if not existing_task:
                    customer = leg.reservation.customer if leg.reservation else None
                    pickup_date_fmt = leg.pickup_date.strftime('%m/%d/%Y') if leg.pickup_date else 'N/A'
                    pickup_time_fmt = leg.pickup_time.strftime('%I:%M %p').lstrip('0') if leg.pickup_time else 'N/A'
                    from datetime import timedelta as _td
                    OperationalTask.objects.create(
                        task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
                        priority=OperationalTask.Priority.HIGH,
                        title=f"⚠️ Flight not found: {flight_ident}",
                        description=(
                            f"Flight {flight_ident} does not exist. "
                            f"Please verify and correct the flight number.\n"
                            f"Pickup: {pickup_date_fmt} at {pickup_time_fmt}."
                        ),
                        leg=leg,
                        reservation=leg.reservation,
                        due_at=timezone.now() + _td(hours=4),
                    )
                    logger.info(f"Created flight verification task for leg {leg.id}")

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
        
        # Handle datetime fields — always set scheduled times (even to None to clear stale data)
        scheduled_arrival = flight_data.get('scheduled_arrival_local')
        flight.scheduled_arrival_local = scheduled_arrival

        scheduled_gate_arrival = flight_data.get('scheduled_gate_arrival_local')
        flight.scheduled_gate_arrival_local = scheduled_gate_arrival

        # Check if flight is scheduled for the future
        # Compare in Eastern time since flight times are Eastern-local
        from zoneinfo import ZoneInfo
        eastern = ZoneInfo('America/New_York')
        now = timezone.now()
        now_eastern = now.astimezone(eastern)
        is_future_flight = False
        if scheduled_arrival and scheduled_arrival > now:
            is_future_flight = True
        elif scheduled_gate_arrival and scheduled_gate_arrival > now:
            is_future_flight = True

        # Check if scheduled on a different day (truly future, not just later today)
        # Must compare in Eastern time — UTC date can differ from local date at night
        ref_dt = scheduled_gate_arrival or scheduled_arrival
        if ref_dt:
            ref_date_eastern = ref_dt.astimezone(eastern).date() if ref_dt.tzinfo else ref_dt.date()
            is_different_day = ref_date_eastern != now_eastern.date()
        else:
            is_different_day = False

        if is_future_flight:
            # Future flight (same-day or different-day): clear actuals, keep estimates
            # AeroAPI provides predictions up to ~48hrs out
            estimated_arrival = flight_data.get('estimated_arrival_local')
            if estimated_arrival is not None:
                flight.estimated_arrival_local = estimated_arrival

            estimated_gate_arrival = flight_data.get('estimated_gate_arrival_local')
            if estimated_gate_arrival is not None:
                flight.estimated_gate_arrival_local = estimated_gate_arrival

            flight.actual_arrival_local = None
            flight.actual_gate_arrival_local = None
        else:
            # For past/current flights, update estimated and actual times if provided
            estimated_arrival = flight_data.get('estimated_arrival_local')
            if estimated_arrival is not None:
                flight.estimated_arrival_local = estimated_arrival

            estimated_gate_arrival = flight_data.get('estimated_gate_arrival_local')
            if estimated_gate_arrival is not None:
                flight.estimated_gate_arrival_local = estimated_gate_arrival

            actual_arrival = flight_data.get('actual_runway_arrival_local')
            if actual_arrival is not None:
                flight.actual_arrival_local = actual_arrival
            actual_gate_arrival = flight_data.get('actual_gate_arrival_local')
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
                "last_updated": timezone.localtime(flight.last_updated).strftime('%Y-%m-%d %I:%M %p') if flight.last_updated else "",
            }
        })

    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)
    except Exception as e:
        logger.error(f"Error refreshing flight data: {e}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


def _best_flight_arrival_time(flight):
    """
    Pick the best arrival time for matching pickup to flight.

    - Landed: use actual gate/runway arrival
    - En route or delayed (has estimate): use estimated time
    - Scheduled with no estimate yet: fall back to scheduled time
    """
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
        old_time = leg.pickup_time
        Leg.objects.filter(id=leg.id).update(pickup_time=new_time)

        # Auto-resolve open tasks for this leg
        from ops.models import OperationalTask, StaffActivity
        from ops.services import close_task
        match_note = (
            f"Flight matched: pickup updated "
            f"{old_time.strftime('%I:%M %p').lstrip('0')} → "
            f"{new_time.strftime('%I:%M %p').lstrip('0')}"
        )

        # Always close flight_verify tasks — the mismatch is resolved
        fv_tasks = OperationalTask.objects.filter(
            leg=leg,
            task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
            status__in=list(OperationalTask.OPEN_STATUSES),
        )
        for task in fv_tasks:
            close_task(task, resolved_by=request.user, resolution_notes=match_note)
            StaffActivity.objects.create(
                user=request.user,
                action_type=StaffActivity.ActionType.TASK_COMPLETED,
                task=task,
            )

        # For driver_conflict tasks: only close if the conflict is actually resolved
        # Refresh leg from DB to get the updated pickup_time
        leg.refresh_from_db()
        dc_tasks = OperationalTask.objects.filter(
            leg=leg,
            task_type=OperationalTask.TaskType.DRIVER_CONFLICT,
            status__in=list(OperationalTask.OPEN_STATUSES),
        )
        for task in dc_tasks:
            from ops.tasks import detect_driver_conflicts
            remaining = detect_driver_conflicts(leg, leg.pickup_date)
            if not remaining:
                close_task(task, resolved_by=request.user, resolution_notes=match_note)
                StaffActivity.objects.create(
                    user=request.user,
                    action_type=StaffActivity.ActionType.TASK_COMPLETED,
                    task=task,
                )
            else:
                # Conflict persists — update the task description but keep it open
                worst = max(remaining, key=lambda c: c["conflict_minutes"])
                task.description = (
                    f"Flight matched but conflict remains: driver will be "
                    f"{worst['conflict_minutes']} min late — reassign or adjust times."
                )
                task.save(update_fields=["description", "updated_at"])

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

    # Soft flight verification warning — find legs with open flight_verify tasks
    flight_unverified_leg_ids = set()
    try:
        from ops.models import OperationalTask
        flight_unverified_leg_ids = set(
            OperationalTask.objects.filter(
                task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
                status__in=list(OperationalTask.OPEN_STATUSES),
                leg_id__in=[l.id for l in legs],
            ).values_list("leg_id", flat=True)
        )
    except Exception:
        pass

    # Check which reservations are unpaid
    unpaid_reservation_ids = set()
    reservation_ids = {leg.reservation_id for leg in legs if leg.reservation_id}
    if reservation_ids:
        from reservations.models import Reservation
        reservations = Reservation.objects.filter(id__in=reservation_ids).prefetch_related("payments")
        for res in reservations:
            if res.payment_status == "unpaid":
                unpaid_reservation_ids.add(res.id)

    rows = []
    for leg in legs:
        row = leg_to_row(leg)
        row["message_preview"] = get_confirmation_message(leg, row)
        row["leg"] = leg
        row["already_sent"] = bool(getattr(leg, "confirmation_sms_sent_at", None))
        row["flight_unverified"] = leg.id in flight_unverified_leg_ids
        row["unpaid"] = leg.reservation_id in unpaid_reservation_ids
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

            # Clear ALL stale data so nothing lingers after flight number change
            if flight_data.get("status") in ("not_found", "not_orlando"):
                flight.flight_iata = ""
                flight.origin = ""
                flight.destination = ""
                flight.status = "Not Found"
                flight.scheduled_arrival_local = None
                flight.estimated_arrival_local = None
                flight.actual_arrival_local = None
                flight.scheduled_gate_arrival_local = None
                flight.estimated_gate_arrival_local = None
                flight.actual_gate_arrival_local = None
                flight.terminal = ""
                flight.gate = ""
                flight.baggage_claim = ""
                flight.last_updated = timezone.now()
                flight.save()

                # Create a flight verification task if one doesn't already exist
                from ops.models import OperationalTask
                existing_task = OperationalTask.objects.filter(
                    leg=leg,
                    task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
                    status__in=list(OperationalTask.OPEN_STATUSES),
                ).first()
                if not existing_task:
                    flight_ident = flight.get_flight_ident() or "Unknown"
                    pickup_date_fmt = leg.pickup_date.strftime('%m/%d/%Y') if leg.pickup_date else 'N/A'
                    pickup_time_fmt = leg.pickup_time.strftime('%I:%M %p').lstrip('0') if leg.pickup_time else 'N/A'
                    from datetime import timedelta as _td
                    OperationalTask.objects.create(
                        task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
                        priority=OperationalTask.Priority.HIGH,
                        title=f"⚠️ Flight not found: {flight_ident}",
                        description=(
                            f"Flight {flight_ident} does not exist. "
                            f"Please verify and correct the flight number.\n"
                            f"Pickup: {pickup_date_fmt} at {pickup_time_fmt}."
                        ),
                        leg=leg,
                        reservation=leg.reservation,
                        due_at=timezone.now() + _td(hours=4),
                    )

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

        # Handle datetime fields - always update scheduled times
        flight.scheduled_arrival_local = flight_data.get("scheduled_arrival_local")
        flight.scheduled_gate_arrival_local = flight_data.get("scheduled_gate_arrival_local")

        # Handle actual/estimated arrival times based on flight timing
        # Compare in Eastern time since flight times are Eastern-local
        from zoneinfo import ZoneInfo
        eastern = ZoneInfo('America/New_York')
        now = timezone.now()
        now_eastern = now.astimezone(eastern)
        scheduled_arrival = flight.scheduled_arrival_local
        scheduled_gate_arrival = flight.scheduled_gate_arrival_local

        is_future_flight = False
        if scheduled_arrival and scheduled_arrival > now:
            is_future_flight = True
        elif scheduled_gate_arrival and scheduled_gate_arrival > now:
            is_future_flight = True

        # Check if scheduled on a different day (truly future, not just later today)
        # Must compare in Eastern time — UTC date can differ from local date at night
        is_different_day = False
        ref_dt = scheduled_gate_arrival or scheduled_arrival
        if ref_dt:
            ref_date_eastern = ref_dt.astimezone(eastern).date() if ref_dt.tzinfo else ref_dt.date()
            is_different_day = ref_date_eastern != now_eastern.date()

        if is_future_flight:
            # Future flight (same-day or different-day): clear actuals, keep estimates
            # AeroAPI provides predictions up to ~48hrs out
            flight.actual_arrival_local = None
            flight.actual_gate_arrival_local = None
            if flight_data.get("estimated_arrival_local") is not None:
                flight.estimated_arrival_local = flight_data["estimated_arrival_local"]
            if flight_data.get("estimated_gate_arrival_local") is not None:
                flight.estimated_gate_arrival_local = flight_data["estimated_gate_arrival_local"]
        else:
            # Past/current flights: update actuals from AeroAPI data
            flight.actual_arrival_local = flight_data.get("actual_runway_arrival_local")
            flight.actual_gate_arrival_local = flight_data.get("actual_gate_arrival_local")
            # Only update estimated if AeroAPI returns a value — don't wipe
            # existing estimates (e.g. landed/taxiing: runway actual exists but
            # gate estimate may still be useful until actual gate arrival)
            if flight_data.get("estimated_arrival_local"):
                flight.estimated_arrival_local = flight_data["estimated_arrival_local"]
            if flight_data.get("estimated_gate_arrival_local"):
                flight.estimated_gate_arrival_local = flight_data["estimated_gate_arrival_local"]

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
                "last_updated": timezone.localtime(flight.last_updated).strftime("%Y-%m-%d %I:%M %p")
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

    # --- Suggested rate lookup ---
    suggested_rate = None
    suggested_price = None
    suggested_route_label = None
    if vehicle and booking_data.get('legs_data'):
        first_leg = booking_data['legs_data'][0]
        pickup_text = (first_leg.get('pickup_location') or '').lower()
        dropoff_text = (first_leg.get('dropoff_location') or '').lower()
        if pickup_text and dropoff_text:
            locations = Location.objects.all()
            pickup_match = None
            dropoff_match = None
            for loc in locations:
                keywords = [loc.name.lower()]
                if loc.aliases:
                    keywords += [a.strip().lower() for a in loc.aliases.split(',')]
                for kw in keywords:
                    if kw and kw in pickup_text:
                        pickup_match = loc
                        break
                for kw in keywords:
                    if kw and kw in dropoff_text:
                        dropoff_match = loc
                        break
            if pickup_match and dropoff_match:
                trip_type = booking_data.get('trip_type', 'one_way')
                rate = Rate.objects.filter(
                    vehicle=vehicle,
                    route__origin=pickup_match,
                    route__destination=dropoff_match,
                ).select_related('route', 'route__origin', 'route__destination').first()
                # Try reverse direction if not found
                if not rate:
                    rate = Rate.objects.filter(
                        vehicle=vehicle,
                        route__origin=dropoff_match,
                        route__destination=pickup_match,
                    ).select_related('route', 'route__origin', 'route__destination').first()
                if rate:
                    suggested_rate = rate
                    if trip_type == 'round_trip':
                        suggested_price = rate.round_trip_price
                    else:
                        suggested_price = rate.oneway_price
                    suggested_route_label = f"{rate.route.origin.name} to {rate.route.destination.name}"

    context = {
        'form': form,
        'customer': customer,
        'vehicle': vehicle,
        'legs_data': booking_data.get('legs_data', []),
        'flights_data': booking_data.get('flights_data', []),
        'step': 5,
        'step_title': 'Pricing & Notes',
        'step_description': 'Set pricing and add any final notes',
        'booking_data': booking_data,
        'suggested_price': suggested_price,
        'suggested_route_label': suggested_route_label,
        'suggested_trip_type': 'Round Trip' if booking_data.get('trip_type') == 'round_trip' else 'One Way',
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


def _match_rate_from_legs(vehicle, legs_data):
    """Find the best Rate for a vehicle by matching the first leg's locations.

    Uses the same alias-based substring matching as Leg._match_location()
    to identify the route from pickup/dropoff text, then returns the
    Rate for that vehicle+route.  Falls back to any Rate for the vehicle
    if no location match is found.
    """
    from rates.models import Location, Route, Rate

    first_leg = legs_data[0] if legs_data else {}
    pickup = first_leg.get('pickup_location', '')
    dropoff = first_leg.get('dropoff_location', '')

    if pickup and dropoff:
        locations = list(Location.objects.all())

        def _match(text):
            text_lower = text.lower()
            best, best_len = None, 0
            for loc in locations:
                candidates = []
                if loc.name:
                    candidates.append(loc.name)
                if loc.aliases:
                    candidates.extend(a.strip() for a in loc.aliases.split(",") if a.strip())
                for c in candidates:
                    cl = c.lower()
                    if cl in text_lower and len(cl) > best_len:
                        best, best_len = loc, len(cl)
            return best

        origin = _match(pickup)
        destination = _match(dropoff)

        if origin and destination:
            route = Route.objects.filter(origin=origin, destination=destination).first()
            if not route:
                route = Route.objects.filter(origin=destination, destination=origin).first()
            if route:
                rate = Rate.objects.filter(vehicle=vehicle, route=route).first()
                if rate:
                    return rate

    # Fallback: any rate for this vehicle
    return Rate.objects.filter(vehicle=vehicle).first()


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
    
    # Try to find a matching rate for this vehicle based on actual leg locations
    rate = _match_rate_from_legs(vehicle, legs_data)
    
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
    Driver Payment Management Dashboard.
    Overview mode (no driver param): shows all drivers with unpaid legs,
    split by inhouse / affiliate, with key metrics.
    Detail mode (?driver=ID): shows legs for selected driver with pay editing.
    """
    from django.db.models import Min, Max, Count, Sum, Q, Value, DecimalField
    from django.db.models.functions import Coalesce
    from drivers.models import DriverPayment

    if not request.user.is_staff:
        return redirect("home")

    selected_driver_id = request.GET.get("driver")
    date_from = request.GET.get("date_from", "")
    date_to = request.GET.get("date_to", "")
    selected_driver = None
    legs = []
    total_pay = 0
    total_pay_completed = 0
    completed_leg_count = 0
    zero_pay_count = 0
    review_count = 0

    # ── Build annotated driver list (used for both overview + dropdown) ──
    today = timezone.localdate()
    from django.db.models import Case, When

    # Exclude cancelled legs from all unpaid counts/sums
    _unpaid_not_cancelled = Q(legs__payment_status='unpaid') & ~Q(legs__status='cancelled')

    drivers_with_unpaid = (
        Driver.objects.filter(legs__payment_status='unpaid')
        .exclude(legs__status='cancelled', legs__payment_status='unpaid')
        .select_related('profile')
        .annotate(
            unpaid_count=Count('legs', filter=_unpaid_not_cancelled),
            completed_unpaid_count=Count(
                'legs',
                filter=Q(legs__payment_status='unpaid', legs__status='completed'),
            ),
            # Match Leg.total_driver_pay logic: use base+gratuity+additional when
            # driver_base_pay is set, otherwise fall back to driver_pay_amount
            total_owed=Sum(
                Case(
                    When(
                        legs__driver_base_pay__isnull=False,
                        then=(
                            Coalesce('legs__driver_base_pay', Value(0, output_field=DecimalField()))
                            + Coalesce('legs__driver_gratuity', Value(0, output_field=DecimalField()))
                            + Coalesce('legs__driver_additional', Value(0, output_field=DecimalField()))
                        ),
                    ),
                    default=Coalesce('legs__driver_pay_amount', Value(0, output_field=DecimalField())),
                    output_field=DecimalField(),
                ),
                filter=_unpaid_not_cancelled,
            ),
            oldest_unpaid_date=Min(
                'legs__pickup_date', filter=_unpaid_not_cancelled,
            ),
            last_payment_date=Max('payments__payment_date'),
        )
        .distinct()
        .order_by('profile__first_name', 'profile__last_name')
    )

    # Compute derived fields in Python
    overdue_count = 0
    for d in drivers_with_unpaid:
        d.total_owed = d.total_owed or 0
        if d.last_payment_date:
            d.days_since_last_paid = (today - d.last_payment_date.date()).days
        else:
            d.days_since_last_paid = None  # never paid
        # Days since oldest unpaid leg
        if d.oldest_unpaid_date:
            d.days_since_oldest = (today - d.oldest_unpaid_date).days
        else:
            d.days_since_oldest = None
        # Overdue if oldest unpaid leg is >14 days old
        if d.days_since_oldest is not None and d.days_since_oldest > 14:
            d.is_overdue = True
            overdue_count += 1
        else:
            d.is_overdue = False

    inhouse_drivers = [d for d in drivers_with_unpaid if d.driver_type == 'inhouse']
    affiliate_drivers = [d for d in drivers_with_unpaid if d.driver_type == 'affiliate']
    total_inhouse_owed = sum(d.total_owed for d in inhouse_drivers)
    total_affiliate_owed = sum(d.total_owed for d in affiliate_drivers)

    # ── Detail mode: load legs for selected driver ──
    last_payment_info = None
    if selected_driver_id:
        try:
            selected_driver = get_object_or_404(Driver.objects.select_related('profile'), id=selected_driver_id)

            # Last payment info for detail header
            last_pmt = DriverPayment.objects.filter(driver=selected_driver).order_by('-payment_date').first()
            if last_pmt:
                last_payment_info = {
                    'date': last_pmt.payment_date,
                    'amount': last_pmt.amount,
                }

            # Get only unpaid legs for the driver with optimized queries
            legs_qs = (
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
            )

            # Apply date range filter if provided
            if date_from:
                legs_qs = legs_qs.filter(pickup_date__gte=date_from)
            if date_to:
                legs_qs = legs_qs.filter(pickup_date__lte=date_to)

            legs = legs_qs.order_by("pickup_date", "pickup_time")

            # Calculate total pay amounts (use new fields if available)
            total_pay = sum(leg.total_driver_pay for leg in legs)
            # Calculate total pay for completed legs only
            completed_legs = [leg for leg in legs if leg.status == 'completed']
            total_pay_completed = sum(leg.total_driver_pay for leg in completed_legs)
            completed_leg_count = len(completed_legs)

            # Flag legs that need rate review
            SANFORD_KEYWORDS = ["sfb", "sanford", "orlando sanford"]
            CRUISE_PORT_KEYWORDS = ["port canaveral", "canaveral", "cruise port", "cruise terminal", "cruise ship"]
            for leg in legs:
                loc = f"{leg.pickup_location} {leg.dropoff_location}".lower()
                leg.is_cruise = bool(leg.cruise_information_id) or any(kw in loc for kw in CRUISE_PORT_KEYWORDS)
                leg.is_sanford = any(kw in loc for kw in SANFORD_KEYWORDS)
                leg.is_zero_pay = not leg.driver_base_pay or leg.driver_base_pay == 0
                leg.needs_review = leg.is_zero_pay or leg.is_cruise or leg.is_sanford
                if leg.is_zero_pay:
                    zero_pay_count += 1
                if leg.needs_review:
                    review_count += 1

        except (ValueError, Driver.DoesNotExist):
            messages.error(request, "Invalid driver selected")
            selected_driver = None

    context = {
        # Overview data (always available)
        "inhouse_drivers": inhouse_drivers,
        "affiliate_drivers": affiliate_drivers,
        "total_inhouse_owed": total_inhouse_owed,
        "total_affiliate_owed": total_affiliate_owed,
        "overdue_count": overdue_count,
        # Legacy "drivers" for dropdown (keep compat) — combined list
        "drivers": drivers_with_unpaid,
        # Detail data
        "selected_driver": selected_driver,
        "selected_driver_id": selected_driver_id,
        "last_payment_info": last_payment_info,
        "date_from": date_from,
        "date_to": date_to,
        "legs": legs,
        "total_pay": total_pay,
        "total_pay_completed": total_pay_completed,
        "leg_count": len(legs),
        "completed_leg_count": completed_leg_count,
        "zero_pay_count": zero_pay_count,
        "review_count": review_count,
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
def recalculate_driver_pay(request):
    """Recalculate auto-fill pay for legs.

    Accepts JSON body:
      - driver_id (int, optional): recalc all zero-pay unpaid legs for this driver
      - leg_ids (list[int], optional): recalc specific legs
      - force (bool, optional): if true, recalculate even when pay is already set

    By default only touches legs where all pay fields are null/zero.
    With force=true + leg_ids, overwrites existing values.
    """
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    driver_id = data.get("driver_id")
    leg_ids = data.get("leg_ids")
    force = data.get("force", False)

    if not driver_id and not leg_ids:
        return JsonResponse({"success": False, "error": "driver_id or leg_ids required"}, status=400)

    from reservations.models import Leg
    from django.db.models import Q

    # Build queryset
    if leg_ids:
        legs_qs = Leg.objects.filter(id__in=leg_ids, driver__isnull=False)
        if not force:
            # Only zero-pay legs
            zero_pay_q = Q(
                Q(driver_base_pay__isnull=True) | Q(driver_base_pay=0),
                Q(driver_gratuity__isnull=True) | Q(driver_gratuity=0),
                Q(driver_additional__isnull=True) | Q(driver_additional=0),
                Q(driver_pay_amount__isnull=True) | Q(driver_pay_amount=0),
            )
            legs_qs = legs_qs.filter(zero_pay_q)
    else:
        # driver_id mode: always only zero-pay
        zero_pay_q = Q(
            Q(driver_base_pay__isnull=True) | Q(driver_base_pay=0),
            Q(driver_gratuity__isnull=True) | Q(driver_gratuity=0),
            Q(driver_additional__isnull=True) | Q(driver_additional=0),
            Q(driver_pay_amount__isnull=True) | Q(driver_pay_amount=0),
            driver__isnull=False,
        )
        legs_qs = Leg.objects.filter(
            driver_id=driver_id,
            payment_status='unpaid',
        ).filter(zero_pay_q)

    legs = list(
        legs_qs
        .select_related('driver', 'reservation', 'reservation__vehicle', 'route')
        .only(
            'id', 'driver', 'driver_id', 'route', 'route_id',
            'pickup_location', 'dropoff_location', 'pickup_time',
            'driver_base_pay', 'driver_gratuity', 'driver_additional',
            'driver_pay_amount', 'profit_estimate', 'revenue_share',
            'reservation__vehicle_id', 'reservation__gratuity_amount',
            'reservation__gratuity_percentage', 'reservation__base_price',
        )[:200]  # cap batch size
    )

    recalculated = 0
    filled = 0
    for leg in legs:
        # Clear route so it re-matches from pickup/dropoff text
        leg.route = None
        # Clear all pay fields to trigger auto-fill in save()
        leg.driver_base_pay = None
        leg.driver_gratuity = None
        leg.driver_additional = None
        leg.driver_pay_amount = None
        leg.save(update_fields=[
            'route', 'driver_base_pay', 'driver_gratuity', 'driver_additional',
            'driver_pay_amount', 'profit_estimate',
        ])
        recalculated += 1
        if leg.driver_base_pay and leg.driver_base_pay > 0:
            filled += 1

    return JsonResponse({
        "success": True,
        "recalculated": recalculated,
        "filled": filled,
        "still_zero": recalculated - filled,
        "message": (
            f"Recalculated {recalculated} legs: {filled} got pay values, "
            f"{recalculated - filled} still need manual entry (no matching rate)."
        ),
    })


@login_required(login_url="login")
@require_http_methods(["POST"])
def bulk_update_leg_status(request):
    """Bulk-update the status of multiple legs (e.g. mark as completed)."""
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

    try:
        data = json.loads(request.body)
        leg_ids = data.get("leg_ids", [])
        new_status = data.get("status", "")

        valid_statuses = [
            "confirmed", "in-progress", "on-the-way",
            "on-location", "picked-up", "completed", "cancelled",
        ]
        if new_status not in valid_statuses:
            return JsonResponse({"success": False, "error": f"Invalid status: {new_status}"}, status=400)
        if not leg_ids:
            return JsonResponse({"success": False, "error": "No legs selected"}, status=400)

        from reservations.models import LegStatus

        now = timezone.now()
        updated = 0
        for leg in Leg.objects.filter(id__in=leg_ids):
            if leg.status != new_status:
                leg.status = new_status
                leg.status_changed_by = request.user
                leg.status_changed_at = now
                leg.save(update_fields=['status', 'status_changed_by', 'status_changed_at'])
                LegStatus.objects.create(
                    leg=leg, status=new_status,
                    updated_by=request.user,
                    notes=f"Bulk status update from driver payment page",
                )
                updated += 1

        return JsonResponse({
            "success": True,
            "updated": updated,
            "message": f"{updated} leg{'s' if updated != 1 else ''} marked as {new_status}.",
        })
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)
    except Exception as e:
        logger.error(f"Error in bulk_update_leg_status: {e}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


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

        # Apply date range filter if provided
        date_from = data.get("date_from")
        date_to = data.get("date_to")
        if date_from:
            unpaid_legs = unpaid_legs.filter(pickup_date__gte=date_from)
        if date_to:
            unpaid_legs = unpaid_legs.filter(pickup_date__lte=date_to)

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
                    sent_by=request.user,
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

        # Validate refund amount — cap suggestion at what was actually paid
        max_refund = reservation.total_paid if reservation.total_paid > 0 else reservation.total_price
        suggested_amount = min(suggestion['total_suggested'], max_refund)
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
                leg.save(update_fields=['status', 'payment_status', 'driver'])

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
                    leg.save(update_fields=['status', 'payment_status', 'driver'])

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

        # Cap suggested amount at what was actually paid
        max_refundable = reservation.total_paid if reservation.total_paid > 0 else reservation.total_price
        capped_total = min(suggestion['total_suggested'], max_refundable)

        return JsonResponse({
            'success': True,
            'total_suggested': str(capped_total),
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
        suggest_assignments_clustered,
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
    # Sort by vehicle number (same order as legs dashboard)
    def _cp_vehicle_sort_key(d):
        a = assignment_map.get(d.id)
        if a and a.vehicle and a.vehicle.vehicle_number:
            vn = a.vehicle.vehicle_number.lstrip('#').strip()
            try:
                return (0, int(vn))
            except ValueError:
                return (0, vn)
        return (1, str(d))
    eligible_drivers.sort(key=_cp_vehicle_sort_key)

    # Fleet vehicles for quick-assign panel
    inhouse_vehicles = FleetVehicle.objects.select_related("vehicle_type").all().order_by("vehicle_number")

    # Build vehicle_assign_rows with off-today and leg count info
    _planner_dow = selected_date.weekday()
    _planner_leg_counts = {}
    for _leg in legs_list:
        if _leg.driver_id:
            _planner_leg_counts[_leg.driver_id] = _planner_leg_counts.get(_leg.driver_id, 0) + 1

    vehicle_assign_rows = []
    for d in inhouse_drivers:
        _is_off = False
        for _entry in d.weekly_schedule.all():
            if _entry.day_of_week == _planner_dow:
                _is_off = not _entry.is_available
                break
        _assignment = assignment_map.get(d.id)
        if _is_off and _assignment and _assignment.vehicle_id:
            _is_off = False
        vehicle_assign_rows.append({
            "driver": d,
            "assignment": _assignment,
            "is_off_today": _is_off,
            "leg_count": _planner_leg_counts.get(d.id, 0),
        })

    # Sort: assigned drivers first (by vehicle number), then unassigned, off last
    vehicle_assign_rows.sort(
        key=lambda r: (
            2 if r["is_off_today"] else (1 if r["assignment"] is None else 0),
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
        suggestions = suggest_assignments_clustered(_unassigned_legs, _inhouse_for_suggestions, selected_date)
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

    # Build leg-id → latest status info map for timeline popup
    _cp_leg_status_map = {}
    _cp_now = timezone.now()
    for _cpleg in legs_list:
        _sh_list = list(_cpleg.status_history.all())  # already prefetched, ordered -timestamp
        if _sh_list:
            _latest = _sh_list[0]
            _local_ts = timezone.localtime(_latest.timestamp)
            _ago_secs = int((_cp_now - _latest.timestamp).total_seconds())
            if _ago_secs < 60:
                _ago_str = "just now"
            elif _ago_secs < 3600:
                _ago_str = f"{_ago_secs // 60} min ago"
            else:
                _hrs = _ago_secs // 3600
                _mins = (_ago_secs % 3600) // 60
                _ago_str = f"{_hrs}h {_mins}m ago" if _mins else f"{_hrs}h ago"
            _status_label = dict(LegStatus.STATUS_CHOICES).get(_latest.status, _latest.status).title()
            _cp_leg_status_map[_cpleg.id] = {
                'status_label': _status_label,
                'status_time': _local_ts.strftime('%I:%M %p').lstrip('0'),
                'status_ago': _ago_str,
            }

    # Get previous day's last leg per driver (for overnight turnaround display)
    _cp_prev_day = selected_date - timedelta(days=1)
    _cp_prev_day_last = {}
    _cp_prev_legs = (
        Leg.objects.filter(pickup_date=_cp_prev_day, driver__in=eligible_drivers)
        .exclude(status="cancelled")
        .select_related("driver")
        .order_by("driver_id", "-pickup_time")
    )
    for _cpl in _cp_prev_legs:
        if _cpl.driver_id not in _cp_prev_day_last:
            try:
                _cp_end = estimate_job_end_time(_cpl, _cp_prev_day)
                _cp_prev_day_last[_cpl.driver_id] = _cp_end.strftime('%I:%M %p').lstrip('0')
            except Exception:
                _cp_prev_day_last[_cpl.driver_id] = _cpl.pickup_time.strftime('%I:%M %p').lstrip('0') + '?'

    # Get previous day's vehicle assignments
    _cp_prev_day_vehicle = {}
    _cp_prev_assigns = DriverVehicleAssignment.objects.filter(
        date=_cp_prev_day, driver__in=eligible_drivers
    ).select_related('vehicle', 'vehicle__vehicle_type')
    for _cpda in _cp_prev_assigns:
        if _cpda.vehicle:
            _vn = _cpda.vehicle.vehicle_number or ''
            _vt = str(_cpda.vehicle.vehicle_type) if _cpda.vehicle.vehicle_type else ''
            _cp_prev_day_vehicle[_cpda.driver_id] = f"#{_vn} {_vt}".strip() if _vn else _vt

    # Build in-house timeline data — only drivers with vehicles assigned for the day
    inhouse_timeline = []
    for driver in eligible_drivers:
        sched = driver_schedules.get(driver.id)
        if not sched:
            continue

        # Calculate position/width percentages for each slot + status timestamps
        for slot in sched.slots:
            slot_start_min = (slot.pickup_time.hour - display_start) * 60 + slot.pickup_time.minute
            slot_end_min = (slot.estimated_end_time.hour - display_start) * 60 + slot.estimated_end_time.minute
            duration = max(slot_end_min - slot_start_min, 15)

            slot.position_pct = round(max(0, slot_start_min / total_display_minutes * 100), 1)
            slot.width_pct = round(min(duration / total_display_minutes * 100, 100 - slot.position_pct), 1)
            # Annotate with status timestamp info
            _sinfo = _cp_leg_status_map.get(slot.leg_id)
            slot.status_label = _sinfo['status_label'] if _sinfo else ''
            slot.status_time = _sinfo['status_time'] if _sinfo else ''
            slot.status_ago = _sinfo['status_ago'] if _sinfo else ''

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

        _cp_assign = assignment_map.get(driver.id)
        _cp_vnum = ''
        _cp_vtype = ''
        if _cp_assign and _cp_assign.vehicle:
            _cp_vnum = _cp_assign.vehicle.vehicle_number or ''
            if _cp_assign.vehicle.vehicle_type:
                _cp_vtype = str(_cp_assign.vehicle.vehicle_type)
        inhouse_timeline.append({
            'driver': driver,
            'schedule': sched,
            'gaps': gaps,
            'total_legs': sched.total_legs,
            'total_revenue': sched.total_revenue,
            'vehicle_number': _cp_vnum,
            'vehicle_type_label': _cp_vtype,
            'prev_night_cleared': _cp_prev_day_last.get(driver.id, ''),
            'prev_night_vehicle': _cp_prev_day_vehicle.get(driver.id, ''),
        })

    # Build per-driver availability for the selected date (for auto-assign modal defaults)
    driver_availability = {}
    for d in eligible_drivers:
        is_avail, start_h, end_h, pref, flex = d.get_availability_for_date(selected_date)
        driver_availability[d.id] = {
            "is_available": is_avail,
            "start_hour": start_h,
            "end_hour": end_h,
            "preference": pref,
            "flexible": flex,
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
    apply_driver_ids = data.get("apply_driver_ids", None)  # optional: only apply for these drivers

    from datetime import datetime as dt
    from dispatching.scheduler import (
        build_driver_schedules, suggest_assignments_clustered,
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
    # Start with driver availability defaults, then apply frontend overrides
    driver_hours = {}
    flexible_drivers = set()
    for d in inhouse_drivers:
        avail = d.get_availability_for_date(target_date)
        is_avail, sh, eh, pref, flex = avail
        if is_avail:
            driver_hours[d.id] = (sh, eh)
            if flex:
                flexible_drivers.add(d.id)

    # Frontend overrides take precedence
    for did_str, hours in raw_driver_hours.items():
        try:
            driver_hours[int(did_str)] = (int(hours["start"]), int(hours["end"]))
        except (ValueError, KeyError, TypeError):
            continue

    # Parse per-driver trip preferences: {driver_id: "prefer_arrival"}
    # Start with driver availability defaults, then apply frontend overrides
    driver_preferences = {}
    for d in inhouse_drivers:
        avail = d.get_availability_for_date(target_date)
        if avail[3]:  # preference
            driver_preferences[d.id] = avail[3]

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
    suggestions = suggest_assignments_clustered(auto_unassigned, schedules, target_date,
                                                driver_hours=driver_hours or None,
                                                driver_preferences=driver_preferences or None,
                                                flexible_drivers=flexible_drivers or None) if auto_unassigned else []

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

        # Filter to selected drivers only if specified
        if apply_driver_ids is not None:
            selected_dids = set(int(d) for d in apply_driver_ids)
            final_assignments = {lid: did for lid, did in final_assignments.items() if did in selected_dids}

        # PERF TEMP START
        import time as _time; _t_assign = _time.monotonic()
        import logging as _logging; _perf = _logging.getLogger('perf')
        # PERF TEMP END
        now = timezone.now()
        saved = 0
        for lid, did in final_assignments.items():
            leg = legs_by_id[lid]
            driver = drivers_by_id[did]
            try:
                leg.driver = driver
                leg.driver_assigned_by = request.user
                leg.driver_assigned_at = now
                # Single save: Leg.save() auto-fills pay when driver changes
                leg.save(update_fields=[
                    'driver', 'driver_assigned_by', 'driver_assigned_at',
                ])
                saved += 1
            except Exception:
                continue

        # PERF TEMP START
        _perf.info("AUTO-ASSIGN apply: %d legs saved in %.0fms", saved, (_time.monotonic()-_t_assign)*1000)
        # PERF TEMP END
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
    - start_hour / end_hour: availability window
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
            'store_stop': res.store_stop if res else False,
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
                    leg.save(update_fields=['driver', 'driver_assigned_by', 'driver_assigned_at'])
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

    from dispatching.scheduler import DRIVE_TIME_ESTIMATES, DEFAULT_DRIVE_TIME
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
    buckets = defaultdict(lambda: {'dwell': [], 'drive': [], 'total': [], 'leg_ids': [], 'total_legs': 0})

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
            # Categorize the leg so we can always add it to leg_ids
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
            # Always add to leg_ids so modal shows ALL legs for this bucket
            buckets[key]['leg_ids'].append(leg.id)
            # Fallback: for arrivals, try gate → completed total time
            gate_total = calculate_gate_to_completed_time(leg)
            if gate_total is not None:
                buckets[key]['total'].append(gate_total)
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

        total_leg_count = len(vals['leg_ids'])
        metrics_list.append({
            'pickup_cat': pickup_cat,
            'dropoff_cat': dropoff_cat,
            'trip_type': trip_type,
            'time_cat': time_cat,
            'day_cat': day_cat,
            'time_label': TIME_LABELS.get(time_cat, time_cat),
            'day_label': DAY_LABELS.get(day_cat, day_cat),
            'sample_count': sample_count,
            'total_leg_count': total_leg_count,
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

    # --- P0 enhancements: heatmap, deltas, gaps, insights ---

    # 1. Heatmap matrix: overall P75 drive per (pickup, dropoff)
    heatmap_data = {}  # {(pickup, dropoff): {'p75': X, 'samples': N, 'confidence': str}}
    for g in grouped.values():
        all_drive = []
        total_samp = 0
        for r in g['rows']:
            total_samp += r['sample_count']
            if r['drive'].get('p75'):
                all_drive.extend([r['drive']['p75']] * r['sample_count'])
            elif r['drive'].get('avg'):
                all_drive.extend([r['drive']['avg']] * r['sample_count'])
        overall_p75 = round(statistics.quantiles(all_drive, n=4)[2]) if len(all_drive) >= 4 else (round(statistics.median(all_drive)) if len(all_drive) >= 2 else (round(all_drive[0]) if all_drive else None))
        conf = 'high' if total_samp >= 20 else ('medium' if total_samp >= 10 else ('low' if total_samp >= 5 else 'none'))
        heatmap_data[(g['pickup_cat'], g['dropoff_cat'])] = {
            'p75': overall_p75, 'samples': total_samp, 'confidence': conf,
        }

    # All location categories that appear in the data
    heatmap_cats = sorted(set(
        [k[0] for k in heatmap_data] + [k[1] for k in heatmap_data]
    ))
    # Build matrix rows for template
    heatmap_matrix = []
    for pickup in heatmap_cats:
        row_cells = []
        for dropoff in heatmap_cats:
            cell = heatmap_data.get((pickup, dropoff))
            row_cells.append({
                'pickup': pickup, 'dropoff': dropoff,
                'p75': cell['p75'] if cell else None,
                'samples': cell['samples'] if cell else 0,
                'confidence': cell['confidence'] if cell else 'none',
                'hardcoded': DRIVE_TIME_ESTIMATES.get((pickup, dropoff)),
            })
        heatmap_matrix.append({'label': pickup, 'cells': row_cells})

    # 2. P75 vs fallback deltas on each route group
    for g in route_groups:
        all_p75 = [r['drive']['p75'] for r in g['rows'] if r['drive'].get('p75')]
        if all_p75:
            # Weighted by sample count
            weighted = []
            for r in g['rows']:
                if r['drive'].get('p75'):
                    weighted.extend([r['drive']['p75']] * r['sample_count'])
            g['best_p75'] = round(statistics.median(weighted)) if weighted else None
        else:
            g['best_p75'] = None
        hc = g.get('hardcoded_drive_time')
        if g['best_p75'] is not None and hc is not None:
            g['delta'] = g['best_p75'] - hc
        else:
            g['delta'] = None

    # 3. Data gaps: routes with low confidence
    data_gaps = []
    for g in route_groups:
        total_samp = sum(r['sample_count'] for r in g['rows'])
        if total_samp < 10:
            data_gaps.append({
                'route': f"{g['pickup_cat']} → {g['dropoff_cat']}",
                'samples': total_samp,
            })
    # Also add hardcoded routes that have NO data at all
    routes_with_data = set((g['pickup_cat'], g['dropoff_cat']) for g in route_groups)
    for route_pair, mins in DRIVE_TIME_ESTIMATES.items():
        if route_pair not in routes_with_data and (route_pair[1], route_pair[0]) not in routes_with_data:
            data_gaps.append({
                'route': f"{route_pair[0]} → {route_pair[1]}",
                'samples': 0,
            })
    data_gaps.sort(key=lambda x: x['samples'])
    data_gap_count = len(data_gaps)

    # 4. Auto-generated insights (short, scannable text)
    insights = []
    # a) Fallback too generous or too tight
    for g in route_groups:
        if g['delta'] is not None:
            route = f"{g['pickup_cat']} → {g['dropoff_cat']}"
            if g['delta'] >= 8:
                insights.append({
                    'icon': 'bi-exclamation-triangle',
                    'severity': 'danger',
                    'text': f"{route} — P75 is {g['best_p75']}m, fallback says {g['hardcoded_drive_time']}m (+{g['delta']}m off)",
                })
            elif g['delta'] <= -8:
                insights.append({
                    'icon': 'bi-graph-down-arrow',
                    'severity': 'success',
                    'text': f"{route} — P75 is {g['best_p75']}m, fallback says {g['hardcoded_drive_time']}m (could tighten by {abs(g['delta'])}m)",
                })
    # b) Rush hour impact: routes where max P75 - min P75 > 8 min
    for g in route_groups:
        p75s = [(r['time_label'], r['drive']['p75']) for r in g['rows'] if r['drive'].get('p75')]
        if len(p75s) >= 2:
            slowest = max(p75s, key=lambda x: x[1])
            fastest = min(p75s, key=lambda x: x[1])
            diff = slowest[1] - fastest[1]
            if diff >= 8:
                # Shorten time label: "Morning Rush (7-10 AM)" → "Morning Rush"
                slow_short = slowest[0].split('(')[0].strip()
                fast_short = fastest[0].split('(')[0].strip()
                insights.append({
                    'icon': 'bi-clock-history',
                    'severity': 'warning',
                    'text': f"{g['pickup_cat']} → {g['dropoff_cat']} — {slow_short} +{diff}m vs {fast_short}",
                })
    # c) Routes with no fallback defined
    no_fallback = [g for g in route_groups if g['hardcoded_drive_time'] is None]
    if no_fallback:
        names = ', '.join(f"{g['pickup_cat']} → {g['dropoff_cat']}" for g in no_fallback[:3])
        suffix = f" +{len(no_fallback) - 3} more" if len(no_fallback) > 3 else ""
        insights.append({
            'icon': 'bi-question-circle',
            'severity': 'info',
            'text': f"{len(no_fallback)} route{'s' if len(no_fallback) != 1 else ''} using {DEFAULT_DRIVE_TIME}m default — {names}{suffix}",
        })

    # 5. Chart data per route group (JSON for Chart.js)
    import json as _json
    for g in route_groups:
        chart_rows = []
        for r in g['rows']:
            chart_rows.append({
                'time': r['time_label'], 'day': r['day_label'],
                'p75_drive': r['drive'].get('p75'),
                'med_drive': r['drive'].get('median'),
                'avg_drive': r['drive'].get('avg'),
                'p75_dwell': r['dwell'].get('p75'),
                'samples': r['sample_count'],
                'trip_type': r['trip_type'],
            })
        g['chart_data_json'] = _json.dumps(chart_rows)

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
        # P0 enhancements
        'heatmap_matrix': heatmap_matrix,
        'heatmap_cats': heatmap_cats,
        'data_gaps': data_gaps[:8],
        'data_gap_count': data_gap_count,
        'insights': insights,
    }

    return render(request, 'dispatching/route_timing_reference.html', context)


@login_required
def route_timing_leg_details(request):
    """AJAX endpoint: return leg details for a comma-separated list of leg IDs."""
    if not request.user.is_staff:
        return JsonResponse({"error": "Permission denied"}, status=403)

    from dispatching.analytics import (
        calculate_airport_dwell_time, calculate_drive_time,
        has_valid_status_chain,
    )
    from django.utils import timezone as tz

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

        # Determine status reason
        if leg.exclude_from_analytics:
            data_status = 'excluded'
        elif has_valid_status_chain(leg):
            data_status = 'valid'
        elif dwell is None and drive is None:
            data_status = 'incomplete'
        else:
            data_status = 'partial'

        # Get raw status timestamps for diagnostics
        status_times = {}
        for s in leg.status_history.all():
            if s.status in ('on-the-way', 'picked-up', 'completed') and s.status not in status_times:
                ts = s.timestamp
                if tz.is_aware(ts):
                    ts = tz.localtime(ts)
                status_times[s.status] = ts.strftime('%I:%M %p').lstrip('0')

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
            'data_status': data_status,
            'otw_time': status_times.get('on-the-way'),
            'pickup_actual': status_times.get('picked-up'),
            'completed_time': status_times.get('completed'),
        })

    # Sort by pickup_date descending (most recent first)
    results.sort(key=lambda r: r['pickup_date'], reverse=True)

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

        # Recalculate the affected bucket synchronously
        try:
            from dispatching.analytics import update_single_route_timing_metric
            update_single_route_timing_metric(leg)
        except Exception:
            pass

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
                "flexible": entry.flexible,
                "preference": entry.preference,
            }
        result.append({
            "id": d.id,
            "name": str(d),
            "default_start_hour": d.default_start_hour,
            "default_end_hour": d.default_end_hour,
            "default_flexible": d.default_flexible,
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
        driver.default_flexible = d_data.get("default_flexible", True)
        driver.default_preference = d_data.get("default_preference", "")
        if "notes" in d_data:
            driver.notes = d_data["notes"].strip() or None
        driver.save(update_fields=["default_start_hour", "default_end_hour", "default_flexible", "default_preference", "notes"])

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
                    "flexible": entry.get("flexible", True),
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
        """Return a short human-readable hour label like 2a, 12p, 11p."""
        if h == 0:  return "12a"
        if h < 12:  return f"{h}a"
        if h == 12: return "12p"
        return f"{h - 12}p"

    def _fmt_hour_long(h):
        """Return a full select-option label like '12 AM', '2 AM', '5 PM'."""
        if h == 0:  return "12 AM"
        if h < 12:  return f"{h} AM"
        if h == 12: return "12 PM"
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
        "preference_choices": DriverWeeklySchedule.PREFERENCE_CHOICES,
        "today": today,
        "today_legs_url": f"/dispatching/?date={today.strftime('%Y-%m-%d')}",
    }
    return render(request, "dispatching/inhouse_schedule.html", context)


@login_required(login_url="login")
def driver_schedules_dashboard(request):
    """
    Read-only driver schedule dashboard showing weekly coverage overview,
    timeline bars, hourly heatmap, and coverage alerts.
    """
    if not request.user.is_staff:
        messages.error(request, "Permission denied.")
        return redirect("dashboard")

    DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    DAY_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    def _fmt_hour(h):
        if h == 0:  return "12 AM"
        if h < 12:  return f"{h} AM"
        if h == 12: return "12 PM"
        return f"{h - 12} PM"

    def _fmt_hour_short(h):
        """Compact label for small cells: 12a, 1a, 12p, 1p"""
        if h == 0:  return "12a"
        if h < 12:  return f"{h}a"
        if h == 12: return "12p"
        return f"{h - 12}p"

    import calendar as _cal
    import json as _json
    from datetime import date as _date_type

    today = timezone.localdate()

    # ── Determine selected date ──
    # Support ?date=YYYY-MM-DD (month view clicks) or ?day=N (week view clicks)
    selected_date = None
    date_param = request.GET.get("date")
    if date_param:
        try:
            selected_date = _date_type.fromisoformat(date_param)
        except (ValueError, TypeError):
            pass

    if selected_date is None:
        try:
            day_param = int(request.GET.get("day", today.weekday()))
            if day_param < 0 or day_param > 6:
                day_param = today.weekday()
        except (ValueError, TypeError):
            day_param = today.weekday()
        monday = today - timedelta(days=today.weekday())
        selected_date = monday + timedelta(days=day_param)

    # Compute the week containing the selected date
    monday = selected_date - timedelta(days=selected_date.weekday())
    week_dates = [monday + timedelta(days=i) for i in range(7)]
    selected_day = selected_date.weekday()  # 0-6

    # Which view mode
    view_mode = request.GET.get("view", "week")

    inhouse_drivers = (
        Driver.objects.filter(driver_type="inhouse")
        .select_related("profile")
        .prefetch_related("weekly_schedule")
        .order_by("profile__first_name", "profile__last_name")
    )

    # Build per-driver, per-day availability matrix
    driver_schedules = []
    for driver in inhouse_drivers:
        days = []
        for i, date in enumerate(week_dates):
            is_avail, sh, eh, pref, _flex = driver.get_availability_for_date(date)
            hours = (eh - sh) if is_avail and eh > sh else 0
            days.append({
                "day_idx": i,
                "is_available": is_avail,
                "start_hour": sh,
                "end_hour": eh,
                "hours": hours,
                "preference": pref,
                "start_label": _fmt_hour(sh),
                "end_label": _fmt_hour(eh),
            })
        driver_schedules.append({
            "driver": driver,
            "days": days,
            "selected": days[selected_day],
        })

    # Weekly totals for heatmap
    weekly_totals = []
    for i in range(7):
        total_hours = sum(ds["days"][i]["hours"] for ds in driver_schedules)
        active_count = sum(1 for ds in driver_schedules if ds["days"][i]["is_available"])
        weekly_totals.append({
            "day_idx": i,
            "day_name": DAY_NAMES[i],
            "date": week_dates[i],
            "total_hours": total_hours,
            "active_count": active_count,
            "is_selected": i == selected_day,
            "is_today": week_dates[i] == today,
        })

    # Compute bar_pct and is_weakest for weekly sparkline
    max_hours = max((wt["total_hours"] for wt in weekly_totals), default=1) or 1
    min_hours = min((wt["total_hours"] for wt in weekly_totals), default=0)
    for wt in weekly_totals:
        wt["bar_pct"] = round((wt["total_hours"] / max_hours) * 100, 1)
        wt["is_weakest"] = wt["total_hours"] == min_hours and wt["total_hours"] < max_hours

    # Timeline data for selected day
    timeline_rows = []
    for ds in driver_schedules:
        sel = ds["selected"]
        if sel["is_available"]:
            left_pct = round((sel["start_hour"] / 24) * 100, 2)
            width_pct = round(((sel["end_hour"] - sel["start_hour"]) / 24) * 100, 2)
            color = "teal" if sel["hours"] >= 12 else "gold"
        else:
            left_pct = 0
            width_pct = 0
            color = "off"
        timeline_rows.append({
            "driver": ds["driver"],
            "is_available": sel["is_available"],
            "start_hour": sel["start_hour"],
            "end_hour": sel["end_hour"],
            "hours": sel["hours"],
            "preference": sel["preference"],
            "start_label": sel["start_label"],
            "end_label": sel["end_label"],
            "left_pct": left_pct,
            "width_pct": width_pct,
            "color": color,
        })

    # Sort: active drivers first (by start hour), OFF drivers at bottom
    timeline_rows.sort(key=lambda r: (0 if r["is_available"] else 1, r["start_hour"]))

    # Hourly coverage
    hourly_coverage = []
    for hour in range(24):
        count = sum(
            1 for ds in driver_schedules
            if ds["selected"]["is_available"]
            and ds["selected"]["start_hour"] <= hour < ds["selected"]["end_hour"]
        )
        if count == 0:
            color_class = "cov-red"
        elif count == 1:
            color_class = "cov-orange"
        elif count <= 3:
            color_class = "cov-yellow"
        else:
            color_class = "cov-green"
        hourly_coverage.append({
            "hour": hour,
            "label": _fmt_hour(hour),
            "short_label": _fmt_hour_short(hour),
            "count": count,
            "color_class": color_class,
        })

    # Coverage alerts
    alerts = []
    for hc in hourly_coverage:
        h = hc["hour"]
        if hc["count"] == 0 and 4 <= h <= 23:
            alerts.append({"severity": "critical", "icon": "bi-x-octagon-fill", "message": f"No drivers available at {hc['label']}"})
        elif hc["count"] == 1 and 6 <= h <= 22:
            alerts.append({"severity": "warning", "icon": "bi-exclamation-triangle-fill", "message": f"Only 1 driver available at {hc['label']}"})
        elif (h >= 22 or h <= 4) and 0 < hc["count"] < 2:
            alerts.append({"severity": "info", "icon": "bi-moon-fill", "message": f"Late-night {hc['label']}: only {hc['count']} driver available"})

    # Early outs
    for ds in driver_schedules:
        sel = ds["selected"]
        if sel["is_available"] and sel["end_hour"] < 18:
            alerts.append({
                "severity": "notice",
                "icon": "bi-clock",
                "message": f"{ds['driver']} ends at {sel['end_label']} (early out)",
            })

    # Summary stats
    active_drivers = [ds for ds in driver_schedules if ds["selected"]["is_available"]]
    total_active = len(active_drivers)
    total_driver_hours = sum(ds["selected"]["hours"] for ds in active_drivers)
    peak_hour = max(hourly_coverage, key=lambda h: h["count"]) if hourly_coverage else None
    gap_hours = sum(1 for hc in hourly_coverage if hc["count"] == 0 and 4 <= hc["hour"] <= 23)
    earliest_start = min((ds["selected"]["start_hour"] for ds in active_drivers), default=None)
    latest_end = max((ds["selected"]["end_hour"] for ds in active_drivers), default=None)

    # Farm risk: any hour 6-22 with fewer than 3 drivers
    farm_risk_hours = sum(1 for hc in hourly_coverage if 6 <= hc["hour"] <= 22 and hc["count"] < 3)

    # ── Job demand data for all 7 days ──
    active_statuses = ["in-progress", "confirmed", "on-the-way", "on-location", "picked-up"]
    week_legs = list(
        Leg.objects.filter(
            pickup_date__gte=monday,
            pickup_date__lte=week_dates[6],
            status__in=active_statuses,
        ).select_related("driver").only(
            "pickup_date", "pickup_time", "status",
            "driver__driver_type", "driver__id",
        )
    )

    # Bucket jobs by (day_index, hour) for all 7 days
    job_demand_by_day = {i: {h: 0 for h in range(24)} for i in range(7)}
    job_totals_by_day = {i: 0 for i in range(7)}
    for leg in week_legs:
        day_idx = (leg.pickup_date - monday).days
        if 0 <= day_idx <= 6:
            job_demand_by_day[day_idx][leg.pickup_time.hour] += 1
            job_totals_by_day[day_idx] += 1

    selected_date = week_dates[selected_day]
    selected_demand = job_demand_by_day[selected_day]

    # First / last job pickup times for selected day
    day_legs_qs = [l for l in week_legs if l.pickup_date == selected_date]
    day_legs_qs.sort(key=lambda l: l.pickup_time)

    first_job = day_legs_qs[0] if day_legs_qs else None
    last_job = day_legs_qs[-1] if day_legs_qs else None
    first_job_time = first_job.pickup_time if first_job else None
    last_job_time = last_job.pickup_time if last_job else None

    # Compute percentage positions for demand window markers
    if first_job_time:
        first_job_hour = first_job_time.hour + first_job_time.minute / 60
        first_job_pct = round((first_job_hour / 24) * 100, 2)
        first_job_label = first_job_time.strftime("%I:%M %p").lstrip("0")
    else:
        first_job_pct = None
        first_job_label = None

    if last_job_time:
        last_job_hour = last_job_time.hour + last_job_time.minute / 60
        last_job_pct = round((last_job_hour / 24) * 100, 2)
        last_job_label = last_job_time.strftime("%I:%M %p").lstrip("0")
    else:
        last_job_pct = None
        last_job_label = None

    total_jobs = job_totals_by_day[selected_day]

    # ── Enhance hourly coverage with job demand ──
    max_jobs_hour = max(selected_demand.values()) if selected_demand else 0
    at_risk_hours = 0
    for hc in hourly_coverage:
        h = hc["hour"]
        jobs = selected_demand.get(h, 0)
        drivers = hc["count"]
        hc["jobs"] = jobs
        # New color logic: demand-aware
        if jobs > drivers:
            hc["color_class"] = "cov-red"
            at_risk_hours += 1
        elif jobs > 0 and jobs == drivers:
            hc["color_class"] = "cov-orange"
        elif drivers > 0 and jobs >= (drivers * 0.75):
            hc["color_class"] = "cov-yellow"
        elif drivers > 0:
            hc["color_class"] = "cov-green"
        else:
            if jobs == 0:
                hc["color_class"] = "cov-dark"
            else:
                hc["color_class"] = "cov-red"
        # Bar height for job demand histogram (0-100%)
        hc["job_bar_pct"] = round((jobs / max_jobs_hour) * 100) if max_jobs_hour > 0 else 0

    # Utilization %
    util_pct = round((total_jobs / total_driver_hours) * 100) if total_driver_hours > 0 else 0

    # ── Add job totals to weekly_totals ──
    for wt in weekly_totals:
        d_idx = wt["day_idx"]
        wt["job_count"] = job_totals_by_day[d_idx]
        # Check if any hour has jobs > drivers for this day
        day_driver_avail = {}
        for h in range(24):
            day_driver_avail[h] = sum(
                1 for ds in driver_schedules
                if ds["days"][d_idx]["is_available"]
                and ds["days"][d_idx]["start_hour"] <= h < ds["days"][d_idx]["end_hour"]
            )
        wt["has_risk"] = any(
            job_demand_by_day[d_idx][h] > day_driver_avail[h]
            for h in range(24) if job_demand_by_day[d_idx][h] > 0
        )

    # ── Demand-aware alerts ──
    # Jobs > drivers alerts
    for hc in hourly_coverage:
        h = hc["hour"]
        jobs = hc["jobs"]
        drivers = hc["count"]
        if jobs > drivers and jobs > 0:
            alerts.append({
                "severity": "critical",
                "icon": "bi-exclamation-diamond-fill",
                "message": f"{jobs} job{'s' if jobs > 1 else ''} at {hc['label']} — only {drivers} driver{'s' if drivers != 1 else ''} available",
            })
        elif jobs > 0 and jobs == drivers:
            alerts.append({
                "severity": "warning",
                "icon": "bi-exclamation-triangle-fill",
                "message": f"Peak demand at {hc['label']} — {jobs} job{'s' if jobs > 1 else ''}, {drivers} available (no buffer)",
            })

    # First/last job info alert
    if first_job_label and last_job_label:
        alerts.insert(0, {
            "severity": "info",
            "icon": "bi-clock-history",
            "message": f"First job: {first_job_label} — Last job: {last_job_label} ({total_jobs} total)",
        })

    # Dead hours: find contiguous blocks with 0 jobs during operational window
    if first_job_time and last_job_time:
        op_start = first_job_time.hour
        op_end = last_job_time.hour
        dead_start = None
        for h in range(op_start, op_end + 1):
            if selected_demand.get(h, 0) == 0:
                if dead_start is None:
                    dead_start = h
            else:
                if dead_start is not None:
                    alerts.append({
                        "severity": "info",
                        "icon": "bi-pause-circle",
                        "message": f"Dead hours: {_fmt_hour(dead_start)}–{_fmt_hour(h)} — no jobs scheduled",
                    })
                    dead_start = None
        if dead_start is not None:
            alerts.append({
                "severity": "info",
                "icon": "bi-pause-circle",
                "message": f"Dead hours: {_fmt_hour(dead_start)}–{_fmt_hour(op_end + 1)} — no jobs scheduled",
            })

    # ── Schedule Insights (all 7 days) ──
    insights = []
    for d_idx in range(7):
        day_name = DAY_FULL[d_idx]
        demand = job_demand_by_day[d_idx]
        day_job_count = job_totals_by_day[d_idx]
        if day_job_count == 0:
            continue

        # Compute per-hour driver availability for this day
        day_avail = {}
        for h in range(24):
            day_avail[h] = sum(
                1 for ds in driver_schedules
                if ds["days"][d_idx]["is_available"]
                and ds["days"][d_idx]["start_hour"] <= h < ds["days"][d_idx]["end_hour"]
            )

        # Find first/last job hours for this day
        job_hours_with_demand = [h for h in range(24) if demand[h] > 0]
        if not job_hours_with_demand:
            continue
        day_first_h = min(job_hours_with_demand)
        day_last_h = max(job_hours_with_demand)

        # 1. Early start needed
        if day_first_h < 6:
            insights.append({
                "day": day_name,
                "day_idx": d_idx,
                "severity": "warning",
                "message": f"Early start needed — {demand[day_first_h]} job{'s' if demand[day_first_h] > 1 else ''} at {_fmt_hour(day_first_h)}, consider shifting a driver to {_fmt_hour(max(0, day_first_h - 1))} start",
            })

        # 2. Night coverage gap
        if day_last_h >= 21:
            night_drivers = day_avail.get(day_last_h, 0)
            if night_drivers == 0:
                insights.append({
                    "day": day_name,
                    "day_idx": d_idx,
                    "severity": "critical",
                    "message": f"Night coverage gap — {demand[day_last_h]} job{'s' if demand[day_last_h] > 1 else ''} at {_fmt_hour(day_last_h)}, no in-house driver available",
                })
            elif night_drivers < demand[day_last_h]:
                insights.append({
                    "day": day_name,
                    "day_idx": d_idx,
                    "severity": "warning",
                    "message": f"Night coverage tight — {demand[day_last_h]} job{'s' if demand[day_last_h] > 1 else ''} at {_fmt_hour(day_last_h)}, only {night_drivers} available",
                })

        # 3. Bottleneck: 3+ jobs in one hour with fewer drivers
        for h in range(24):
            if demand[h] >= 3 and demand[h] > day_avail.get(h, 0):
                insights.append({
                    "day": day_name,
                    "day_idx": d_idx,
                    "severity": "critical",
                    "message": f"Bottleneck at {_fmt_hour(h)} — {demand[h]} jobs, only {day_avail.get(h, 0)} available",
                })

        # 4. Oversupply: drivers start before first job
        earliest_driver = min(
            (ds["days"][d_idx]["start_hour"] for ds in driver_schedules if ds["days"][d_idx]["is_available"]),
            default=None
        )
        if earliest_driver is not None and day_first_h - earliest_driver >= 2:
            idle_drivers = day_avail.get(earliest_driver, 0)
            if idle_drivers >= 2:
                insights.append({
                    "day": day_name,
                    "day_idx": d_idx,
                    "severity": "opportunity",
                    "message": f"Oversupply {_fmt_hour(earliest_driver)}–{_fmt_hour(day_first_h)} — {idle_drivers} available, first job not until {_fmt_hour(day_first_h)}. Consider staggered starts",
                })

    # Current hour for "now" indicator
    current_hour = timezone.localtime().hour if selected_date == today else None

    # ── Month calendar data ──
    cal_month = selected_date.month
    cal_year = selected_date.year
    cal_first_day = _date_type(cal_year, cal_month, 1)
    cal_days_in_month = _cal.monthrange(cal_year, cal_month)[1]
    cal_last_day = _date_type(cal_year, cal_month, cal_days_in_month)

    # Grid starts on Monday of the week containing the 1st
    grid_start = cal_first_day - timedelta(days=cal_first_day.weekday())
    # Grid ends on Sunday of the week containing the last day
    grid_end = cal_last_day + timedelta(days=(6 - cal_last_day.weekday()))

    # Query jobs for the full grid range
    month_legs = list(
        Leg.objects.filter(
            pickup_date__gte=grid_start,
            pickup_date__lte=grid_end,
            status__in=active_statuses,
        ).values_list("pickup_date", "pickup_time")
    )
    # Bucket jobs by date and hour
    month_jobs_by_date = {}
    for pd, pt in month_legs:
        if pd not in month_jobs_by_date:
            month_jobs_by_date[pd] = {h: 0 for h in range(24)}
        month_jobs_by_date[pd][pt.hour] += 1

    # Build month_data JSON: {date_str: {drivers, jobs, has_risk}}
    month_data = {}
    current = grid_start
    while current <= grid_end:
        # Driver availability for this date
        avail_count = 0
        avail_by_hour = {h: 0 for h in range(24)}
        for driver in inhouse_drivers:
            is_avail, sh, eh, _, _flex = driver.get_availability_for_date(current)
            if is_avail:
                avail_count += 1
                for h in range(sh, eh):
                    avail_by_hour[h] += 1

        day_jobs = month_jobs_by_date.get(current, {})
        job_total = sum(day_jobs.values())
        has_risk = any(
            day_jobs.get(h, 0) > avail_by_hour[h]
            for h in range(24) if day_jobs.get(h, 0) > 0
        )

        month_data[current.isoformat()] = {
            "drivers": avail_count,
            "jobs": job_total,
            "has_risk": has_risk,
        }
        current += timedelta(days=1)

    # Build calendar weeks for template rendering
    cal_weeks = []
    current = grid_start
    while current <= grid_end:
        week_row = []
        for _ in range(7):
            d = month_data.get(current.isoformat(), {})
            week_row.append({
                "date": current,
                "date_str": current.isoformat(),
                "day_num": current.day,
                "in_month": current.month == cal_month,
                "is_today": current == today,
                "is_selected": current == selected_date,
                "is_past": current < today,
                "drivers": d.get("drivers", 0),
                "jobs": d.get("jobs", 0),
                "has_risk": d.get("has_risk", False),
            })
            current += timedelta(days=1)
        cal_weeks.append(week_row)

    # Prev/next month dates for navigation
    if cal_month == 1:
        prev_month_date = _date_type(cal_year - 1, 12, 1)
    else:
        prev_month_date = _date_type(cal_year, cal_month - 1, 1)
    if cal_month == 12:
        next_month_date = _date_type(cal_year + 1, 1, 1)
    else:
        next_month_date = _date_type(cal_year, cal_month + 1, 1)

    context = {
        "driver_schedules": driver_schedules,
        "weekly_totals": weekly_totals,
        "selected_day": selected_day,
        "selected_day_name": DAY_FULL[selected_day],
        "selected_date": selected_date,
        "timeline_rows": timeline_rows,
        "hourly_coverage": hourly_coverage,
        "alerts": alerts,
        "total_active": total_active,
        "total_driver_hours": total_driver_hours,
        "peak_hour": peak_hour,
        "gap_hours": gap_hours,
        "earliest_start": _fmt_hour(earliest_start) if earliest_start is not None else "—",
        "latest_end": _fmt_hour(latest_end) if latest_end is not None else "—",
        "farm_risk_hours": farm_risk_hours,
        "current_hour": current_hour,
        "hour_markers": [{"hour": h, "label": _fmt_hour(h)} for h in range(24)],
        "first_job_pct": first_job_pct,
        "first_job_label": first_job_label,
        "last_job_pct": last_job_pct,
        "last_job_label": last_job_label,
        "total_jobs": total_jobs,
        "at_risk_hours": at_risk_hours,
        "util_pct": util_pct,
        "insights": insights,
        "selected_demand": selected_demand,
        # Month calendar
        "view_mode": view_mode,
        "cal_weeks": cal_weeks,
        "cal_month_label": f"{_cal.month_name[cal_month].upper()} {cal_year}",
        "prev_month_date": prev_month_date.isoformat(),
        "next_month_date": next_month_date.isoformat(),
        "today_iso": today.isoformat(),
    }
    return render(request, "dispatching/driver_schedules_dashboard.html", context)


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
                leg.save(update_fields=['driver', 'driver_assigned_by', 'driver_assigned_at'])
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
            leg.save(update_fields=['driver', 'driver_assigned_by', 'driver_assigned_at'])
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
        build_driver_schedules, suggest_assignments_clustered,
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
    suggestions = suggest_assignments_clustered(
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
            feas = check_feasibility(sched, leg, selected_date, cfg.inter_job_buffer, arrival_grace=cfg.arrival_grace_minutes)
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


@login_required(login_url="login")
def lead_analytics(request):
    """
    Lead analytics dashboard showing conversion funnel, follow-up
    effectiveness, revenue attribution, and pipeline health.
    Restricted to superusers only — dispatchers should not see revenue data.
    """
    if not request.user.is_superuser:
        return redirect("dashboard")
    from django.db.models import Count, Sum, Q, Avg, F, ExpressionWrapper, DurationField, Case, When
    from django.db.models.functions import Coalesce, Greatest
    from reservations.models import Lead
    from ghl_integration.models import FollowUpTask, LeadActivity

    # ── Date range ──
    days_param = request.GET.get("days", "30")
    end_date = timezone.now()
    if days_param == "all":
        days_back = "all"
        start_date = None
    else:
        days_back = int(days_param)
        start_date = end_date - timedelta(days=days_back)

    # ── Base queryset ──
    if start_date:
        leads_qs = Lead.objects.filter(created_at__gte=start_date, created_at__lte=end_date)
    else:
        leads_qs = Lead.objects.all()
    all_leads = leads_qs.count()

    # ── Status funnel ──
    status_counts = dict(
        leads_qs.values_list("status").annotate(c=Count("id")).values_list("status", "c")
    )
    new_count = status_counts.get("new", 0)
    contacted_count = status_counts.get("contacted", 0)
    interested_count = status_counts.get("interested", 0)
    converted_count = status_counts.get("converted", 0)
    lost_count = status_counts.get("lost", 0)
    cold_count = status_counts.get("cold", 0)
    future_count = status_counts.get("future_contact", 0)

    conversion_rate = round(converted_count / all_leads * 100, 1) if all_leads else 0
    reply_rate_val = leads_qs.filter(has_replied=True).count()
    reply_rate = round(reply_rate_val / all_leads * 100, 1) if all_leads else 0

    # ── Priority breakdown ──
    priority_counts = dict(
        leads_qs.values_list("priority").annotate(c=Count("id")).values_list("priority", "c")
    )

    # ── Source attribution (UTM) — normalize historical variants ──
    from django.db.models import Value
    from django.db.models.functions import Lower
    normalized_source = Case(
        When(utm_source__in=["facebook", "fb", "ig", "instagram", "Facebook", "Meta"], then=Value("meta")),
        default=Lower("utm_source"),
    )
    source_data = list(
        leads_qs.exclude(utm_source__isnull=True)
        .exclude(utm_source="")
        .annotate(norm_source=normalized_source)
        .values("norm_source")
        .annotate(
            total=Count("id"),
            converted=Count("id", filter=Q(converted=True)),
        )
        .order_by("-total")[:10]
    )
    for src in source_data:
        src["utm_source"] = src["norm_source"]
        src["conv_pct"] = round(src["converted"] / src["total"] * 100, 1) if src["total"] else 0

    # Ad click attribution
    google_leads = leads_qs.exclude(gclid__isnull=True).exclude(gclid="").count()
    meta_leads = leads_qs.exclude(fbclid__isnull=True).exclude(fbclid="").count()
    organic_leads = leads_qs.filter(
        Q(gclid__isnull=True) | Q(gclid=""),
        Q(fbclid__isnull=True) | Q(fbclid=""),
    ).exclude(utm_source__isnull=True).exclude(utm_source="").count()
    direct_leads = all_leads - google_leads - meta_leads - organic_leads

    # ── Revenue attribution ──
    revenue_data = (
        leads_qs.filter(converted=True, converted_reservation__isnull=False)
        .aggregate(
            total_revenue=Sum("converted_reservation__total_price"),
            avg_revenue=Avg("converted_reservation__total_price"),
            count=Count("id"),
        )
    )
    total_lead_revenue = revenue_data["total_revenue"] or Decimal("0.00")
    avg_lead_revenue = revenue_data["avg_revenue"] or Decimal("0.00")

    # Revenue by source (normalized)
    revenue_by_source = list(
        leads_qs.filter(converted=True, converted_reservation__isnull=False)
        .exclude(utm_source__isnull=True)
        .exclude(utm_source="")
        .annotate(norm_source=normalized_source)
        .values("norm_source")
        .annotate(revenue=Sum("converted_reservation__total_price"), count=Count("id"))
        .order_by("-revenue")[:10]
    )
    for rev in revenue_by_source:
        rev["utm_source"] = rev["norm_source"]

    # ── Time to conversion (lead created → converted) ──
    converted_leads_qs = leads_qs.filter(converted=True, converted_at__isnull=False)
    avg_conversion_time = None
    if converted_leads_qs.exists():
        conv_times = converted_leads_qs.annotate(
            conv_delta=ExpressionWrapper(
                F("converted_at") - F("created_at"),
                output_field=DurationField(),
            )
        ).filter(conv_delta__isnull=False, conv_delta__gt=timedelta(0))
        if conv_times.exists():
            avg_td = conv_times.aggregate(avg=Avg("conv_delta"))["avg"]
            if avg_td:
                avg_days = avg_td.total_seconds() / 86400
                avg_conversion_time = round(avg_days, 1)

    # ── Follow-up engine metrics ──
    tasks_qs = FollowUpTask.objects.filter(created_at__gte=start_date) if start_date else FollowUpTask.objects.all()
    task_status_counts = dict(
        tasks_qs.values_list("status").annotate(c=Count("id")).values_list("status", "c")
    )
    tasks_sent = task_status_counts.get("sent", 0)
    tasks_pending = task_status_counts.get("pending", 0)
    tasks_cancelled = task_status_counts.get("cancelled", 0)
    tasks_failed = task_status_counts.get("failed", 0)
    tasks_skipped = task_status_counts.get("skipped", 0)
    tasks_total = sum(task_status_counts.values())

    # Step-by-step effectiveness
    step_data = list(
        tasks_qs.filter(status="sent")
        .values("step_number")
        .annotate(
            sent_count=Count("id"),
        )
        .order_by("step_number")
    )

    # Cancellation reasons
    cancel_reasons = list(
        tasks_qs.filter(status="cancelled")
        .values("cancel_reason")
        .annotate(count=Count("id"))
        .order_by("-count")
    )

    # ── Sequence stats ──
    sequences_active = leads_qs.filter(sequence_active=True).count()
    sequences_completed = leads_qs.filter(sequence_completed_at__isnull=False).count()
    needs_follow_up = leads_qs.filter(needs_human_follow_up=True).count()

    # ── Response time (first contact → first reply) ──
    # Use whichever contact method actually reached the lead (SMS or email fallback)
    replied_leads = leads_qs.filter(has_replied=True, last_reply_at__isnull=False)
    avg_response_time = None
    if replied_leads.exists():
        response_times = replied_leads.annotate(
            first_contact_at=Case(
                When(initial_sms_sent_at__isnull=False, initial_email_sent_at__isnull=False,
                     then=Greatest(F("initial_sms_sent_at"), F("initial_email_sent_at"))),
                When(initial_email_sent_at__isnull=False, then=F("initial_email_sent_at")),
                When(initial_sms_sent_at__isnull=False, then=F("initial_sms_sent_at")),
            ),
        ).exclude(first_contact_at__isnull=True).annotate(
            response_delta=ExpressionWrapper(
                F("last_reply_at") - F("first_contact_at"),
                output_field=DurationField(),
            )
        ).filter(response_delta__isnull=False, response_delta__gt=timedelta(0))
        if response_times.exists():
            avg_td = response_times.aggregate(avg=Avg("response_delta"))["avg"]
            if avg_td:
                avg_response_hours = avg_td.total_seconds() / 3600
                avg_response_time = round(avg_response_hours, 1)

    # ── Daily trend (last 14 days) ──
    trend_start = end_date - timedelta(days=13)
    from django.db.models.functions import TruncDate
    daily_trend = list(
        Lead.objects.filter(created_at__gte=trend_start)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(
            created=Count("id"),
            converted=Count("id", filter=Q(converted=True)),
            replied=Count("id", filter=Q(has_replied=True)),
        )
        .order_by("day")
    )

    # ── Pipeline health (leads needing attention) ──
    stale_leads = leads_qs.filter(
        status__in=["new", "contacted"],
        sequence_active=False,
        converted=False,
        has_replied=False,
    ).count()

    from ghl_integration.models import GHLSyncLog
    failed_syncs = GHLSyncLog.objects.filter(
        status=GHLSyncLog.StatusChoices.DEAD_LETTER,
    ).count()

    # ── Recent activity ──
    activity_qs = LeadActivity.objects.select_related("lead").order_by("-created_at")
    if start_date:
        activity_qs = activity_qs.filter(created_at__gte=start_date)
    recent_activity = list(activity_qs[:20])

    context = {
        "days_back": days_back,
        "start_date": start_date,
        "end_date": end_date,
        # Top-line metrics
        "all_leads": all_leads,
        "conversion_rate": conversion_rate,
        "reply_rate": reply_rate,
        "reply_count": reply_rate_val,
        "converted_count": converted_count,
        "total_lead_revenue": total_lead_revenue,
        "avg_lead_revenue": avg_lead_revenue,
        "avg_response_time": avg_response_time,
        # Funnel
        "new_count": new_count,
        "contacted_count": contacted_count,
        "interested_count": interested_count,
        "converted_count": converted_count,
        "lost_count": lost_count,
        "cold_count": cold_count,
        "future_count": future_count,
        # Priority
        "priority_counts": priority_counts,
        # Source
        "source_data": source_data,
        "google_leads": google_leads,
        "meta_leads": meta_leads,
        "organic_leads": organic_leads,
        "direct_leads": direct_leads,
        "revenue_by_source": revenue_by_source,
        "avg_conversion_time": avg_conversion_time,
        # Follow-up engine
        "tasks_sent": tasks_sent,
        "tasks_pending": tasks_pending,
        "tasks_cancelled": tasks_cancelled,
        "tasks_failed": tasks_failed,
        "tasks_skipped": tasks_skipped,
        "tasks_total": tasks_total,
        "step_data": step_data,
        "cancel_reasons": cancel_reasons,
        # Sequences
        "sequences_active": sequences_active,
        "sequences_completed": sequences_completed,
        "needs_follow_up": needs_follow_up,
        # Trend
        "daily_trend": daily_trend,
        "daily_trend_json": json.dumps(
            [{"day": str(d["day"]), "created": d["created"], "converted": d["converted"], "replied": d["replied"]}
             for d in daily_trend]
        ),
        # Pipeline health
        "stale_leads": stale_leads,
        "failed_syncs": failed_syncs,
        # Activity
        "recent_activity": recent_activity,
    }
    return render(request, "dispatching/lead_analytics.html", context)


# =============================================
# AFFILIATE PAYMENT DASHBOARD
# =============================================

from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Min
from users.models import TravelAgent, Agency, CommissionPayout, AgencyCommissionPayout
from users.services import (
    process_agent_payout as svc_process_agent_payout,
    process_agency_payout as svc_process_agency_payout,
    preview_agent_payout as svc_preview_agent_payout,
    preview_agency_payout as svc_preview_agency_payout,
)


@staff_member_required
def affiliate_payments(request):
    """Main affiliate payments dashboard."""
    search = request.GET.get("q", "").strip()
    show = request.GET.get("show", "owing")  # owing | all
    sort = request.GET.get("sort", "amount")  # amount | name | date
    pay_method = request.GET.get("pay_method", "").strip()  # filter by payment method

    # --- Agencies with unpaid balances ---
    from django.db.models import F, ExpressionWrapper, DecimalField, Subquery, OuterRef, Value
    from django.db.models.functions import Coalesce as CoalesceFunc

    agencies_qs = Agency.objects.filter(is_active=True).prefetch_related("heads")

    # Annotate agencies with live-calculated unpaid totals from reservations
    from reservations.models import Reservation as AgencyRes
    agency_unpaid_subquery = AgencyRes.objects.filter(
        commission_paid=False, status="completed",
        travel_agent__agency=OuterRef("pk"),
        travel_agent__agency_handles_payment=True,
    ).annotate(
        calc_commission=ExpressionWrapper(
            F("base_price") * F("travel_agent__commission_rate") / Value(Decimal("100.00")),
            output_field=DecimalField(max_digits=12, decimal_places=4),
        )
    ).values("travel_agent__agency").annotate(
        total=Sum("calc_commission")
    ).values("total")

    agencies_qs = agencies_qs.annotate(
        unpaid_total=CoalesceFunc(
            Subquery(agency_unpaid_subquery),
            Decimal("0"),
        ),
        owing_agent_count=Count(
            "agents",
            filter=Q(
                agents__agency_handles_payment=True,
                agents__reservations__commission_paid=False,
                agents__reservations__status="completed",
            ),
            distinct=True,
        ),
    )

    if show == "owing":
        agencies_qs = agencies_qs.filter(unpaid_total__gt=0)

    if search:
        agencies_qs = agencies_qs.filter(
            Q(name__icontains=search)
            | Q(agents__agent_name__icontains=search)
            | Q(agents__user__email__icontains=search)
        ).distinct()

    if sort == "amount":
        agencies_qs = agencies_qs.order_by("-unpaid_total")
    elif sort == "name":
        agencies_qs = agencies_qs.order_by("name")

    # --- Individual agents (no agency, or agency_handles_payment=False) ---
    agents_qs = TravelAgent.objects.filter(is_active=True).select_related("user", "agency")

    # Only agents who are paid directly (not through agency)
    agents_qs = agents_qs.filter(
        Q(agency__isnull=True) | Q(agency_handles_payment=False)
    )

    # Annotate with live-calculated unpaid commissions (for filtering/sorting)
    agents_qs = agents_qs.annotate(
        live_unpaid=CoalesceFunc(
            Sum(
                ExpressionWrapper(
                    F("reservations__base_price") * F("commission_rate") / Value(Decimal("100.00")),
                    output_field=DecimalField(max_digits=12, decimal_places=4),
                ),
                filter=Q(reservations__commission_paid=False, reservations__status="completed"),
            ),
            Decimal("0"),
            output_field=DecimalField(max_digits=10, decimal_places=2),
        ),
    )

    if show == "owing":
        agents_qs = agents_qs.filter(live_unpaid__gt=0)

    if search:
        agents_qs = agents_qs.filter(
            Q(agent_name__icontains=search)
            | Q(user__email__icontains=search)
            | Q(agency__name__icontains=search)
        )

    # Payment method filter
    if pay_method == "no_agency":
        agents_qs = agents_qs.exclude(payment_method="agency")
    elif pay_method == "none":
        agents_qs = agents_qs.filter(Q(payment_method__isnull=True) | Q(payment_method=""))
    elif pay_method:
        agents_qs = agents_qs.filter(payment_method=pay_method)

    if sort == "amount":
        agents_qs = agents_qs.order_by("-live_unpaid")
    elif sort == "name":
        agents_qs = agents_qs.order_by("agent_name")
    elif sort == "date":
        agents_qs = agents_qs.order_by("last_payment_date")

    # Count unpaid reservations per agent
    from reservations.models import Reservation

    agent_unpaid_counts = dict(
        Reservation.objects.filter(
            commission_paid=False, status="completed",
            travel_agent__in=agents_qs,
        ).values_list("travel_agent").annotate(cnt=Count("id")).values_list("travel_agent", "cnt")
    )

    # Pagination for agents
    agents_paginator = Paginator(agents_qs, 25)
    agents_page = request.GET.get("agents_page", 1)
    try:
        agents_page_obj = agents_paginator.page(agents_page)
    except (PageNotAnInteger, EmptyPage):
        agents_page_obj = agents_paginator.page(1)

    # Attach unpaid res counts and recalculate live_unpaid in Python for precision
    from reservations.models import Reservation as UnpaidRes
    for agent in agents_page_obj:
        agent.unpaid_res_count = agent_unpaid_counts.get(agent.id, 0)
        # Python Decimal calculation — no SQL rounding issues
        unpaid_qs = UnpaidRes.objects.filter(
            travel_agent=agent, commission_paid=False, status="completed"
        )
        rate = agent.commission_rate / Decimal("100")
        agent.live_unpaid = sum(
            (r.base_price or Decimal("0")) * rate for r in unpaid_qs
        ).quantize(Decimal("0.01"))

    # Agency unpaid reservation counts
    agency_res_counts = {}
    agency_ids = [a.id for a in agencies_qs]
    if agency_ids:
        rows = (
            Reservation.objects.filter(
                commission_paid=False, status="completed",
                travel_agent__agency_id__in=agency_ids,
                travel_agent__agency_handles_payment=True,
            )
            .values("travel_agent__agency_id")
            .annotate(cnt=Count("id"))
        )
        for row in rows:
            agency_res_counts[row["travel_agent__agency_id"]] = row["cnt"]

    for agency in agencies_qs:
        agency.unpaid_res_count = agency_res_counts.get(agency.id, 0)

    # Summary stats — use live calculation from reservations, not stored field
    from reservations.models import Reservation as SummaryRes

    direct_agent_unpaid = SummaryRes.objects.filter(
        commission_paid=False, status="completed",
        travel_agent__is_active=True,
    ).filter(
        Q(travel_agent__agency__isnull=True) | Q(travel_agent__agency_handles_payment=False)
    ).annotate(
        calc_commission=ExpressionWrapper(
            F("base_price") * F("travel_agent__commission_rate") / Value(Decimal("100.00")),
            output_field=DecimalField(max_digits=12, decimal_places=4),
        )
    ).aggregate(total=Sum("calc_commission"), count=Count("travel_agent", distinct=True))

    total_owing_agents = {
        "total": direct_agent_unpaid["total"] or Decimal("0"),
        "count": direct_agent_unpaid["count"] or 0,
    }

    agency_handled_unpaid = SummaryRes.objects.filter(
        commission_paid=False, status="completed",
        travel_agent__is_active=True,
        travel_agent__agency_handles_payment=True,
        travel_agent__agency__isnull=False,
    ).annotate(
        calc_commission=ExpressionWrapper(
            F("base_price") * F("travel_agent__commission_rate") / Value(Decimal("100.00")),
            output_field=DecimalField(max_digits=12, decimal_places=4),
        )
    )

    total_owing_agencies = agency_handled_unpaid.values(
        "travel_agent__agency"
    ).distinct().count()

    agency_owing_amount = agency_handled_unpaid.aggregate(
        total=Sum("calc_commission")
    )["total"] or Decimal("0")

    total_owing = total_owing_agents["total"] + agency_owing_amount

    # --- Payout history ---
    history_tab = request.GET.get("history_tab", "agency")  # agency | agent

    agency_payouts = AgencyCommissionPayout.objects.select_related(
        "agency"
    ).prefetch_related(
        "agent_payouts__agent__user"
    ).order_by("-paid_at")

    agency_payouts_paginator = Paginator(agency_payouts, 15)
    agency_payouts_page = request.GET.get("ap_page", 1)
    try:
        agency_payouts_page_obj = agency_payouts_paginator.page(agency_payouts_page)
    except (PageNotAnInteger, EmptyPage):
        agency_payouts_page_obj = agency_payouts_paginator.page(1)

    agent_payouts = CommissionPayout.objects.select_related(
        "agent", "agent__user", "agency"
    ).order_by("-paid_at")

    agent_payouts_paginator = Paginator(agent_payouts, 15)
    agent_payouts_page = request.GET.get("cp_page", 1)
    try:
        agent_payouts_page_obj = agent_payouts_paginator.page(agent_payouts_page)
    except (PageNotAnInteger, EmptyPage):
        agent_payouts_page_obj = agent_payouts_paginator.page(1)

    # Reuse paginator counts instead of separate COUNT queries
    total_payouts = agent_payouts_paginator.count + agency_payouts_paginator.count

    context = {
        "agencies": agencies_qs,
        "agents_page_obj": agents_page_obj,
        "search": search,
        "show": show,
        "sort": sort,
        "pay_method": pay_method,
        "total_owing": total_owing,
        "total_owing_agencies": total_owing_agencies,
        "total_owing_agents": total_owing_agents["count"] or 0,
        "total_payouts": total_payouts,
        "history_tab": history_tab,
        "agency_payouts_page_obj": agency_payouts_page_obj,
        "agent_payouts_page_obj": agent_payouts_page_obj,
    }
    return render(request, "dispatching/affiliate_payments.html", context)


@staff_member_required
def agency_payouts_report(request):
    """Affiliate Explorer — agencies, agents, analytics, payouts."""
    from reservations.models import Reservation
    from django.db.models import Max, Avg
    from django.db.models.functions import Coalesce

    section = request.GET.get("section", "overview")  # overview | agencies | agents
    search = request.GET.get("q", "").strip()

    # ---- Global stats (used by all sections) ----
    all_agents_qs = TravelAgent.objects.filter(is_active=True).select_related("user", "agency")
    total_agents_count = all_agents_qs.count()
    total_agencies_count = Agency.objects.filter(is_active=True).count()

    global_unpaid = all_agents_qs.aggregate(t=Sum("unpaid_commissions"))["t"] or Decimal("0")
    global_paid = CommissionPayout.objects.aggregate(t=Sum("total_amount"))["t"] or Decimal("0")
    global_revenue = Reservation.objects.filter(
        travel_agent__isnull=False, status="completed"
    ).aggregate(t=Sum("base_price"))["t"] or Decimal("0")

    context = {
        "section": section,
        "search": search,
        "total_agents_count": total_agents_count,
        "total_agencies_count": total_agencies_count,
        "global_unpaid": global_unpaid,
        "global_paid": global_paid,
        "global_revenue": global_revenue,
    }

    if section == "overview":
        # Top 10 agents by revenue
        top_agents_revenue = all_agents_qs.annotate(
            total_revenue=Coalesce(Sum(
                "reservations__base_price",
                filter=Q(reservations__status="completed"),
            ), Decimal("0")),
            booking_count=Count(
                "reservations",
                filter=Q(reservations__status="completed"),
            ),
        ).filter(total_revenue__gt=0).order_by("-total_revenue")[:10]

        # Top 10 agents with most unpaid
        top_agents_unpaid = all_agents_qs.filter(
            unpaid_commissions__gt=0
        ).order_by("-unpaid_commissions")[:10]

        # Top agencies by revenue
        top_agencies = Agency.objects.filter(is_active=True).annotate(
            total_revenue=Coalesce(Sum(
                "agents__reservations__base_price",
                filter=Q(agents__reservations__status="completed"),
            ), Decimal("0")),
            agent_count=Count("agents", filter=Q(agents__is_active=True), distinct=True),
            unpaid_total=Coalesce(Sum(
                "agents__unpaid_commissions",
                filter=Q(agents__is_active=True),
            ), Decimal("0")),
            total_paid=Coalesce(Sum("commission_payouts__total_amount"), Decimal("0")),
            booking_count=Count(
                "agents__reservations",
                filter=Q(agents__reservations__status="completed"),
            ),
        ).filter(total_revenue__gt=0).order_by("-total_revenue")[:10]

        # Recent payouts (last 10)
        recent_payouts = CommissionPayout.objects.select_related(
            "agent", "agent__user", "agency"
        ).order_by("-paid_at")[:10]

        # Agents needing payment (longest overdue)
        overdue_agents = all_agents_qs.filter(
            unpaid_commissions__gt=0
        ).annotate(
            oldest_unpaid=Min(
                "reservations__created_at",
                filter=Q(reservations__commission_paid=False, reservations__status="completed"),
            ),
        ).filter(oldest_unpaid__isnull=False).order_by("oldest_unpaid")[:10]

        context.update({
            "top_agents_revenue": top_agents_revenue,
            "top_agents_unpaid": top_agents_unpaid,
            "top_agencies": top_agencies,
            "recent_payouts": recent_payouts,
            "overdue_agents": overdue_agents,
        })

    elif section == "agencies":
        agency_id = request.GET.get("agency")
        tab = request.GET.get("tab", "agents")

        agencies = Agency.objects.filter(is_active=True).annotate(
            total_paid=Coalesce(Sum("commission_payouts__total_amount"), Decimal("0")),
            payout_count=Count("commission_payouts"),
            agent_count=Count("agents", filter=Q(agents__is_active=True), distinct=True),
            unpaid_total=Coalesce(Sum(
                "agents__unpaid_commissions",
                filter=Q(agents__is_active=True),
            ), Decimal("0")),
            total_revenue=Coalesce(Sum(
                "agents__reservations__base_price",
                filter=Q(agents__reservations__status="completed"),
            ), Decimal("0")),
        ).order_by("name")

        if search:
            agencies = agencies.filter(
                Q(name__icontains=search)
                | Q(agents__agent_name__icontains=search)
                | Q(agents__user__email__icontains=search)
            ).distinct()

        selected_agency = None
        agents_list = None
        agency_payouts_page_obj = None

        if agency_id:
            try:
                selected_agency = Agency.objects.get(id=agency_id)

                agents_list = TravelAgent.objects.filter(
                    agency=selected_agency, is_active=True
                ).select_related("user").annotate(
                    total_revenue=Coalesce(Sum(
                        "reservations__base_price",
                        filter=Q(reservations__status="completed"),
                    ), Decimal("0")),
                    completed_res_count=Count(
                        "reservations", filter=Q(reservations__status="completed"),
                    ),
                    unpaid_res_count=Count(
                        "reservations",
                        filter=Q(reservations__status="completed", reservations__commission_paid=False),
                    ),
                    paid_res_count=Count(
                        "reservations", filter=Q(reservations__commission_paid=True),
                    ),
                    last_booking=Max("reservations__created_at"),
                ).order_by("-total_revenue")

                payouts_qs = AgencyCommissionPayout.objects.filter(
                    agency=selected_agency
                ).prefetch_related(
                    "agent_payouts__agent__user",
                    "agent_payouts__reservations",
                ).order_by("-paid_at")

                payouts_paginator = Paginator(payouts_qs, 10)
                page = request.GET.get("page", 1)
                try:
                    agency_payouts_page_obj = payouts_paginator.page(page)
                except (PageNotAnInteger, EmptyPage):
                    agency_payouts_page_obj = payouts_paginator.page(1)

            except Agency.DoesNotExist:
                pass

        context.update({
            "agencies": agencies,
            "selected_agency": selected_agency,
            "tab": tab,
            "agents_list": agents_list,
            "agency_payouts_page_obj": agency_payouts_page_obj,
        })

    elif section == "agents":
        sort = request.GET.get("sort", "revenue")  # revenue | unpaid | bookings | name | rate
        agent_filter = request.GET.get("filter", "all")  # all | owing | independent | agency

        agents_qs = all_agents_qs.annotate(
            total_revenue=Coalesce(Sum(
                "reservations__base_price",
                filter=Q(reservations__status="completed"),
            ), Decimal("0")),
            booking_count=Count(
                "reservations", filter=Q(reservations__status="completed"),
            ),
            unpaid_res_count=Count(
                "reservations",
                filter=Q(reservations__status="completed", reservations__commission_paid=False),
            ),
            last_booking=Max("reservations__created_at"),
        )

        if search:
            agents_qs = agents_qs.filter(
                Q(agent_name__icontains=search)
                | Q(user__email__icontains=search)
                | Q(agency__name__icontains=search)
            )

        if agent_filter == "owing":
            agents_qs = agents_qs.filter(unpaid_commissions__gt=0)
        elif agent_filter == "independent":
            agents_qs = agents_qs.filter(agency__isnull=True)
        elif agent_filter == "agency":
            agents_qs = agents_qs.filter(agency__isnull=False)

        sort_map = {
            "revenue": "-total_revenue",
            "unpaid": "-unpaid_commissions",
            "bookings": "-booking_count",
            "name": "agent_name",
            "rate": "-commission_rate",
        }
        agents_qs = agents_qs.order_by(sort_map.get(sort, "-total_revenue"))

        agents_paginator = Paginator(agents_qs, 25)
        page = request.GET.get("page", 1)
        try:
            agents_page_obj = agents_paginator.page(page)
        except (PageNotAnInteger, EmptyPage):
            agents_page_obj = agents_paginator.page(1)

        context.update({
            "agents_page_obj": agents_page_obj,
            "sort": sort,
            "agent_filter": agent_filter,
        })

    return render(request, "dispatching/agency_payouts_report.html", context)


@staff_member_required
@require_POST
def process_agent_payout_view(request):
    """AJAX endpoint to process agent payout."""
    try:
        data = json.loads(request.body)
        agent_id = data.get("id")
        send_email = data.get("send_email", False)

        agent = TravelAgent.objects.select_related("user", "agency").get(id=agent_id)

        live_unpaid = agent.calculate_unpaid_commissions()
        if live_unpaid <= 0:
            return JsonResponse({"success": False, "error": "No unpaid commissions for this agent."})

        payout, amount, agency_payout = svc_process_agent_payout(
            agent, send_email=send_email, recipient_email=agent.user.email, sent_by=request.user
        )

        if not payout:
            return JsonResponse({"success": False, "error": "No completed unpaid reservations found."})

        result = {
            "success": True,
            "payout_id": payout.id,
            "amount": str(amount),
            "email_sent_to": agent.user.email if send_email else None,
            "payout_url": reverse("admin_agent_payout_detail", args=[payout.pk]),
        }

        if agency_payout:
            result["agency_payout_id"] = agency_payout.id
            result["agency_payout_url"] = reverse("admin_agency_payout_detail", args=[agency_payout.id])

        return JsonResponse(result)

    except TravelAgent.DoesNotExist:
        return JsonResponse({"success": False, "error": "Agent not found."}, status=404)
    except Exception as e:
        logger.exception(f"Error processing agent payout: {e}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
@require_POST
def process_agency_payout_view(request):
    """AJAX endpoint to process agency payout."""
    try:
        data = json.loads(request.body)
        agency_id = data.get("id")
        send_email = data.get("send_email", False)

        agency = Agency.objects.get(id=agency_id)

        # Check if there are agents with unpaid commissions
        owing_agents = agency.agents.filter(
            unpaid_commissions__gt=0, agency_handles_payment=True
        ).count()

        if owing_agents == 0:
            return JsonResponse({"success": False, "error": "No unpaid commissions for this agency."})

        # Determine recipient email
        recipient_email = None
        if send_email:
            first_head = agency.heads.first()
            recipient_email = first_head.email if first_head else None

        payout, amount = svc_process_agency_payout(
            agency, send_email=send_email, recipient_email=recipient_email, sent_by=request.user
        )

        if not payout:
            return JsonResponse({"success": False, "error": "No completed unpaid reservations found."})

        return JsonResponse({
            "success": True,
            "payout_id": payout.id,
            "amount": str(amount),
            "agents_count": payout.agent_payouts.count(),
            "email_sent_to": recipient_email if send_email else None,
            "payout_url": reverse("admin_agency_payout_detail", args=[payout.id]),
        })

    except Agency.DoesNotExist:
        return JsonResponse({"success": False, "error": "Agency not found."}, status=404)
    except Exception as e:
        logger.exception(f"Error processing agency payout: {e}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@staff_member_required
def preview_agent_payout_view(request):
    """AJAX GET endpoint to preview agent payout."""
    agent_id = request.GET.get("id")
    if not agent_id:
        return JsonResponse({"error": "Missing agent id."}, status=400)

    try:
        agent = TravelAgent.objects.select_related("user").get(id=agent_id)
        preview = svc_preview_agent_payout(agent)
        preview["agent_name"] = agent.agent_name or agent.user.username
        preview["commission_rate"] = str(agent.commission_rate)
        return JsonResponse(preview)
    except TravelAgent.DoesNotExist:
        return JsonResponse({"error": "Agent not found."}, status=404)


@staff_member_required
def preview_agency_payout_view(request):
    """AJAX GET endpoint to preview agency payout."""
    agency_id = request.GET.get("id")
    if not agency_id:
        return JsonResponse({"error": "Missing agency id."}, status=400)

    try:
        agency = Agency.objects.get(id=agency_id)
        preview = svc_preview_agency_payout(agency)
        preview["agency_name"] = agency.name
        return JsonResponse(preview)
    except Agency.DoesNotExist:
        return JsonResponse({"error": "Agency not found."}, status=404)


@staff_member_required
def admin_agency_payout_detail(request, payout_id):
    """Admin-facing detail view for an agency commission payout."""
    payout = get_object_or_404(
        AgencyCommissionPayout.objects.select_related("agency").prefetch_related(
            "agency__heads",
            Prefetch(
                "agent_payouts",
                queryset=CommissionPayout.objects.select_related(
                    "agent", "agent__user"
                ).prefetch_related(
                    Prefetch(
                        "reservations",
                        queryset=Reservation.objects.select_related(
                            "customer", "rate__route__origin", "rate__route__destination"
                        ),
                    )
                ),
            ),
        ),
        id=payout_id,
    )
    agency = payout.agency

    # Use prefetched data — no extra queries
    agent_payouts = list(payout.agent_payouts.all())
    for ap in agent_payouts:
        ap.res_count = len(ap.reservations.all())
    total_reservations = sum(ap.res_count for ap in agent_payouts)
    agent_count = len(agent_payouts)
    average_commission = payout.total_amount / agent_count if agent_count else 0

    context = {
        "agency": agency,
        "payout": payout,
        "agent_payouts": agent_payouts,
        "total_reservations": total_reservations,
        "average_commission": average_commission,
        "agent_count": agent_count,
    }
    return render(request, "dispatching/admin_agency_payout_detail.html", context)


@staff_member_required
def admin_agent_payout_detail(request, pk):
    """Admin-facing detail view for an agent commission payout."""
    payout = get_object_or_404(
        CommissionPayout.objects.select_related("agent", "agent__user", "agency"),
        pk=pk,
    )
    agent = payout.agent
    reservations = payout.reservations.select_related(
        "customer", "rate__route__origin", "rate__route__destination"
    ).order_by("-created_at")

    context = {
        "payout": payout,
        "agent": agent,
        "reservations": reservations,
    }
    return render(request, "dispatching/admin_agent_payout_detail.html", context)


# ── Duplicate Reservation Cleanup ──────────────────────────────────────


@login_required(login_url="login")
def duplicate_reservations(request):
    """Show duplicate reservations: same customer + same pickup date, one paid one unpaid."""
    if not request.user.is_superuser:
        messages.error(request, "You don't have permission to access this page.")
        return redirect("dashboard")

    from collections import defaultdict

    # Scan range: past 90 days + future
    cutoff = timezone.now().date() - timedelta(days=90)

    reservations = (
        Reservation.objects.filter(
            legs__pickup_date__gte=cutoff,
        )
        .exclude(status="cancelled")
        .select_related("customer", "vehicle")
        .prefetch_related(
            Prefetch("payments", queryset=Payment.objects.all()),
            Prefetch(
                "legs",
                queryset=Leg.objects.select_related(
                    "flight_information", "cruise_information"
                ).order_by("pickup_date", "pickup_time"),
            ),
        )
        .distinct()
    )

    # Group by (customer_id, pickup_date)
    groups = defaultdict(list)
    for res in reservations:
        if not res.customer_id:
            continue
        first_leg = res.legs.all().first()
        if first_leg:
            key = (res.customer_id, first_leg.pickup_date)
            groups[key].append(res)

    # Find groups where at least one paid + one unpaid
    duplicate_groups = []
    total_unpaid = 0
    for (customer_id, pickup_date), res_list in groups.items():
        seen_ids = set()
        unique = []
        for r in res_list:
            if r.id not in seen_ids:
                seen_ids.add(r.id)
                unique.append(r)
        if len(unique) < 2:
            continue

        paid = [r for r in unique if r.payment_status in ("paid", "card_saved")]
        unpaid = [r for r in unique if r.payment_status not in ("paid", "card_saved")]

        if not paid or not unpaid:
            continue

        total_unpaid += len(unpaid)
        customer = unique[0].customer
        duplicate_groups.append(
            {
                "customer": customer,
                "pickup_date": pickup_date,
                "paid": paid,
                "unpaid": unpaid,
            }
        )

    # Sort by upcoming dates first (ascending), then past dates after
    today = timezone.now().date()
    duplicate_groups.sort(
        key=lambda g: (0 if g["pickup_date"] >= today else 1, g["pickup_date"]),
    )

    context = {
        "duplicate_groups": duplicate_groups,
        "total_unpaid": total_unpaid,
        "total_groups": len(duplicate_groups),
    }
    return render(request, "dispatching/duplicate_reservations.html", context)


@require_POST
@login_required(login_url="login")
def cancel_duplicate_reservation(request):
    """Delete an unpaid duplicate reservation via AJAX."""
    if not request.user.is_superuser:
        return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

    try:
        data = json.loads(request.body)
        reservation_uuid = data.get("reservation_uuid")

        if not reservation_uuid:
            return JsonResponse(
                {"success": False, "error": "Missing reservation UUID"}, status=400
            )

        reservation = get_object_or_404(Reservation, uuid=reservation_uuid)

        # Safety: don't delete paid reservations
        if reservation.payment_status == "paid":
            return JsonResponse(
                {"success": False, "error": "Cannot delete a paid reservation from this page. Use the refund workflow instead."},
                status=400,
            )

        res_id = reservation.id
        res_name = reservation.customer.get_full_name()
        reservation.delete()

        logger.info(
            f"Deleted duplicate reservation #{res_id} "
            f"({res_name}) by {request.user.username}"
        )

        return JsonResponse(
            {
                "success": True,
                "message": f"Reservation #{res_id} deleted.",
            }
        )

    except json.JSONDecodeError:
        return JsonResponse(
            {"success": False, "error": "Invalid JSON"}, status=400
        )
    except Exception as e:
        logger.error(f"Error cancelling duplicate reservation: {e}")
        return JsonResponse(
            {"success": False, "error": str(e)}, status=500
        )


# ── Quote Calculator ────────────────────────────────────────────────
# Formula constants per vehicle type: (base_fee, per_mile_rate, rt_multiplier)
# These are CUSTOM/RESIDENTIAL rates (higher than standard hotel zone rates
# because the driver has a dead leg back). Prices rounded to nearest $5.
QUOTE_FORMULA = {
    "towncar":      (Decimal("55"),  Decimal("3.35"), Decimal("1.90")),
    "mini_van":     (Decimal("60"),  Decimal("3.55"), Decimal("1.85")),
    "suv":          (Decimal("65"),  Decimal("3.85"), Decimal("1.90")),
    "van":          (Decimal("70"),  Decimal("4.25"), Decimal("1.93")),
    "Van(14 Pax)":  (Decimal("85"),  Decimal("5.85"), Decimal("1.95")),
}
# Minimum one-way price per vehicle — short trips can't go below this
QUOTE_MINIMUMS = {
    "towncar":      Decimal("135"),
    "mini_van":     Decimal("135"),
    "suv":          Decimal("170"),
    "van":          Decimal("175"),
    "Van(14 Pax)":  Decimal("220"),
}
# Display/iteration order: cheapest to most expensive
VEHICLE_TIER_ORDER = ["towncar", "mini_van", "suv", "van", "Van(14 Pax)"]
VEHICLE_LABELS = {
    "towncar": "Towncar",
    "suv": "SUV",
    "mini_van": "Mini Van",
    "van": "Van",
    "Van(14 Pax)": "Van (14 Pax)",
}


def _round_to_5(price):
    """Round a Decimal price to the nearest $5."""
    return Decimal(5) * round(price / Decimal(5))


@login_required(login_url="login")
def quote_calculator(request):
    """Quote calculator page — admin only (under review)."""
    if not request.user.is_superuser:
        return redirect("dashboard")
    vehicles = Vehicle.objects.all()
    formula_display = [
        (vt, VEHICLE_LABELS.get(vt, vt), str(base), str(pm))
        for vt, (base, pm, _rt) in QUOTE_FORMULA.items()
    ]
    context = {
        "vehicles": vehicles,
        "formula_display": formula_display,
        "google_maps_key": getattr(settings, "GOOGLE_MAPS_API_KEY", ""),
    }
    return render(request, "dispatching/quote_calculator.html", context)


@login_required(login_url="login")
@require_POST
def quote_calculator_api(request):
    """AJAX endpoint: calculate quote from pickup/dropoff addresses."""
    if not request.user.is_superuser:
        return JsonResponse({"error": "Unauthorized"}, status=403)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid request"}, status=400)

    pickup = data.get("pickup", "").strip()
    dropoff = data.get("dropoff", "").strip()
    vehicle_type = data.get("vehicle", "towncar")
    trip_type = data.get("trip_type", "oneway")

    if not pickup or not dropoff:
        return JsonResponse({"error": "Both addresses are required."})

    # Get distance from Google Distance Matrix
    from drivers.utils import get_drive_time
    drive_info = get_drive_time(pickup, dropoff)
    if not drive_info:
        return JsonResponse({
            "error": "Could not calculate distance. Check the addresses and try again."
        })

    # Parse miles from distance_text (e.g. "45.2 mi")
    distance_text = drive_info.get("distance_text", "0 mi")
    try:
        miles = Decimal(distance_text.replace(",", "").split()[0])
    except Exception:
        return JsonResponse({"error": "Could not parse distance."})

    # Calculate for selected vehicle
    base_fee, per_mile, rt_mult = QUOTE_FORMULA.get(vehicle_type, QUOTE_FORMULA["towncar"])
    min_ow = QUOTE_MINIMUMS.get(vehicle_type, Decimal("100"))
    oneway_price = max(_round_to_5(base_fee + per_mile * miles), min_ow)
    roundtrip_price = _round_to_5(oneway_price * rt_mult)

    if trip_type == "roundtrip":
        suggested_price = roundtrip_price
    else:
        suggested_price = oneway_price

    mileage_fee = _round_to_5(per_mile * miles)

    # Calculate for ALL vehicles
    all_vehicles = []
    for vt in VEHICLE_TIER_ORDER:
        vb, vpm, vrt = QUOTE_FORMULA[vt]
        v_min = QUOTE_MINIMUMS.get(vt, Decimal("100"))
        v_ow = max(_round_to_5(vb + vpm * miles), v_min)
        v_rt = _round_to_5(v_ow * vrt)
        all_vehicles.append({
            "vehicle_type": vt,
            "label": VEHICLE_LABELS.get(vt, vt),
            "oneway": str(v_ow),
            "roundtrip": str(v_rt),
        })

    # Check if an existing Rate matches this route
    existing_rate = None
    pickup_lower = pickup.lower()
    dropoff_lower = dropoff.lower()
    locations = Location.objects.all()
    pickup_match = None
    dropoff_match = None
    for loc in locations:
        keywords = [loc.name.lower()]
        if loc.aliases:
            keywords += [a.strip().lower() for a in loc.aliases.split(",")]
        for kw in keywords:
            if kw and kw in pickup_lower:
                pickup_match = loc
                break
        for kw in keywords:
            if kw and kw in dropoff_lower:
                dropoff_match = loc
                break

    if pickup_match and dropoff_match:
        rate = Rate.objects.filter(
            vehicle__vehicle_type=vehicle_type,
            route__origin=pickup_match,
            route__destination=dropoff_match,
        ).select_related("route__origin", "route__destination").first()
        if not rate:
            rate = Rate.objects.filter(
                vehicle__vehicle_type=vehicle_type,
                route__origin=dropoff_match,
                route__destination=pickup_match,
            ).select_related("route__origin", "route__destination").first()
        if rate:
            existing_rate = {
                "route": f"{rate.route.origin.name} → {rate.route.destination.name}",
                "oneway": str(rate.oneway_price),
                "roundtrip": str(rate.round_trip_price),
            }

    return JsonResponse({
        "suggested_price": str(suggested_price),
        "trip_type_label": "Round Trip" if trip_type == "roundtrip" else "One Way",
        "vehicle_type": vehicle_type,
        "vehicle_label": VEHICLE_LABELS.get(vehicle_type, vehicle_type),
        "distance_text": distance_text,
        "duration_text": drive_info.get("duration_text", "N/A"),
        "base_fee": str(base_fee),
        "mileage_fee": str(mileage_fee),
        "all_vehicles": all_vehicles,
        "existing_rate": existing_rate,
    })
