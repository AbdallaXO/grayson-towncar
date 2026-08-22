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
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
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

from reservations.models import Reservation, Leg, Customer, Flight, LegStatus, LegKeoi, RefundRequest, LegStop, LegFlight
from reservations.utils import _run_in_background
from payment.models import Payment
from reservations.forms import ReservationAdminForm, CustomerForm, LegForm
from .confirmation_sms import leg_to_row
from . import quote_engine
from . import pickup_policy
from . import store_stop
from django.templatetags.static import static
from drivers.models import (
    Driver,
    DriverPayment,
    LegPayment,
    DriverVehicleAssignment,
    FleetVehicle,
    DriverWeeklySchedule,
    DriverDateOverride,
)
from drivers.availability import format_exception_badge, availability_block_bands, format_shift_preference
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
# Bound every Stripe call (e.g. refunds) so a Stripe slowdown can't hang a
# dispatcher request. Default SDK timeout is ~80s with retries (incident 2026-07-18).
stripe.max_network_retries = 1
stripe.default_http_client = stripe.RequestsClient(timeout=20)


# Permission helpers
def can_view_revenue(user):
    """Check if user can view revenue information (admins only)"""
    return user.is_superuser


def can_view_statistics(user):
    """Check if user can view statistics page (admins only)"""
    return user.is_superuser


# Sandbox write-side core lives in dispatching/assignment.py (the front door).
# Imported under the same names this module used before the extraction.
from dispatching.assignment import (
    can_use_sandbox,
    set_leg_driver,
    sanctioned_live_write,
    _active_draft_for_date,
    _log_draft_event,
    _upsert_draft_assignment,
)


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
    keoi_filter = request.GET.get("keoi")
    highlight_leg_id = request.GET.get("highlight")

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
            "reservation__vehicle", "vehicle",
            "reservation__travel_agent",
            "reservation__travel_agent__user",
            "reservation__travel_agent__agency",  # for Leg.is_vip agency-keyword check (no N+1)
            "driver",
            "driver__profile",
            "driver_assigned_by",
            "flight_information",
            "cruise_information",
        )
        .prefetch_related(
            "reservation__legs",
            "legstop_set",
            "legflight_set__flight",
            Prefetch("reservation__payments", queryset=Payment.objects.order_by('-created_at')),
            Prefetch(
                "status_history",
                queryset=LegStatus.objects.order_by('-timestamp').select_related('updated_by')
            ),
            Prefetch(
                "keoi_flags",
                queryset=LegKeoi.objects.filter(closed_at__isnull=True).select_related("created_by"),
                to_attr="active_keoi_list",
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

    # ── Sandbox draft overlay (shared with board + planner) ──
    # When the day is held, re-point each leg's in-memory driver to its proposed
    # draft value so the table/counts/coverage show the PROPOSED world. The overlay
    # carries its own proposed_driver objects, so no driver list is needed here.
    # Live Leg.driver in the DB is untouched.
    _draft_ctx = _draft_view_context(request, selected_date)
    _apply_draft_overlay(_draft_ctx["draft"], _all_day_legs, None)

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
        legs = [l for l in legs if l.effective_vehicle_type == vehicle_filter]
    
    # Apply trip type filter if specified (filter in Python since it's a computed property)
    if trip_type_filter:
        filtered_legs = []
        for leg in legs:
            if leg.get_trip_type() == trip_type_filter:
                filtered_legs.append(leg)
        legs = filtered_legs

    # KEOI ("Keep Eye On It") filter — count basis is the whole day (matches
    # vehicle_type_counts semantics); reads leg.active_keoi (prefetched, no query).
    # keoi_legs feeds the "Watching today" strip above the table: whole-day basis
    # too, so the strip never shrinks just because a driver/vehicle filter is on —
    # the strip's job is "everything flagged today", not "flagged within this view".
    keoi_legs = [l for l in _all_day_legs if l.active_keoi]
    keoi_count = len(keoi_legs)
    if keoi_filter == "active":
        legs = [l for l in legs if l.active_keoi]

    # Vehicle type counts for the day (from already-fetched legs, no extra query)
    _vtype_counter = {}
    _vtype_labels = {
        'towncar': 'Town Car', 'mini_van': 'Mini Van', 'suv': 'SUV',
        'van': 'Van', 'Van(14 Pax)': 'Van 14',
    }
    for _leg in _all_day_legs:
        _vt = _leg.effective_vehicle_type
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
        .prefetch_related(
            "weekly_schedule", "date_overrides",
            "certified_vehicle_types", "preferred_vehicle_types", "preferred_vehicles",
        )
        .all()
    )
    # Inactive drivers (departed / on leave) are excluded from assignment + timeline;
    # they remain visible only in the driver directory.
    inhouse_drivers = sorted(
        [d for d in drivers if d.driver_type == "inhouse" and d.is_active],
        key=lambda d: (d.profile.first_name, d.profile.last_name, d.profile.username),
    )
    inhouse_assignments = DriverVehicleAssignment.objects.filter(
        date=selected_date, driver__in=inhouse_drivers
    ).select_related("vehicle", "vehicle__vehicle_type")
    assignment_map = {
        assignment.driver_id: assignment for assignment in inhouse_assignments
    }
    inhouse_driver_rows = []
    for _driver in inhouse_drivers:
        _assignment = assignment_map.get(_driver.id)
        # Driver availability for this date — combines weekly + date overrides
        _ld_eff = _driver.get_effective_availability(selected_date)
        # Underlying schedule state — preserved so the UI can flag override-working drivers
        _was_scheduled_off = not _ld_eff["is_available"]
        _is_off = _was_scheduled_off
        # If driver has a vehicle assigned today, treat them as working regardless of schedule.
        # _is_off flips to False, but _was_scheduled_off retains the original signal so the
        # card can warn dispatch that this person was supposed to be off.
        if _is_off and _assignment and _assignment.vehicle_id:
            _is_off = False
        _ld_is_avail = _ld_eff["is_available"]
        _ld_sh = _ld_eff["start_hour"]
        _ld_eh = _ld_eff["end_hour"]
        _ld_pref = _ld_eff["preference"]
        _ld_flex = _ld_eff["flexible"]

        _LD_PREF_SHORT = {
            "prefer_arrival": "Pref Arrivals", "prefer_return": "Pref Returns",
            "prefer_cruise": "Pref Cruises", "heavy_arrival": "Heavy Arrivals",
            "heavy_return": "Heavy Returns", "heavy_cruise": "Heavy Cruises",
            "only_arrival": "Only Arrivals", "only_return": "Only Returns",
            "only_cruise": "Only Cruises",
        }
        _ld_vnotes = ''
        if _assignment and _assignment.vehicle:
            _ld_vnotes = _assignment.vehicle.notes or ''

        _ld_stype = _ld_eff.get("shift_type", "full_day")
        _ld_mhrs = _ld_eff.get("max_hours")
        _ld_shift_disp = _ld_eff["display_label"] if _ld_is_avail else ''
        if _ld_is_avail and _ld_mhrs and _ld_eff["status"] != "limited":
            _ld_shift_disp += f" ({int(_ld_mhrs)}h)"

        inhouse_driver_rows.append({
            "driver": _driver,
            "assignment": _assignment,
            # A unit can be marked down AFTER it was assigned, so the chip on the
            # driver's card has to carry the state too — not just the pool.
            "vehicle_oos_label": (
                _assignment.vehicle.out_of_service_label(selected_date)
                if _assignment and _assignment.vehicle else ""
            ),
            "is_off_today": _is_off,
            "was_scheduled_off": _was_scheduled_off,
            "shift_display": _ld_shift_disp,
            "shift_type": _ld_stype,
            "shift_start": _ld_sh,
            "shift_end": _ld_eh,
            "flexible": _ld_flex,
            "max_hours": float(_ld_mhrs) if _ld_mhrs else None,
            "preference": _ld_pref,
            "pref_short": _LD_PREF_SHORT.get(_ld_pref, ''),
            "driver_notes": _driver.notes or '',
            "driver_phone": _driver.phone_number or '',
            "vehicle_notes": _ld_vnotes,
            "preferred_shift": _ld_eff.get("preferred_shift", ""),
            "scheduling_notes": _ld_eff.get("scheduling_notes", ""),
            "avail_status": _ld_eff["status"],
            "avail_tooltip": _ld_eff["tooltip"],
            "exception_notes": _ld_eff["exception_notes"],
            "has_exception": _ld_eff["has_exception"],
            "exc_badge": format_exception_badge(_ld_eff),
            "cert_labels": _driver.cert_labels(),
            "sprinter_ok": bool(_driver.cert_labels()),
            "pref_vehicle": _driver.preferred_vehicle_label(),
            "shift_pref_label": format_shift_preference(_ld_eff),
        })
    def _inhouse_vehicle_sort_key(row):
        # Off-today drivers sink to bottom; within each group: numeric vehicle#s
        # first (sorted numerically), then non-numeric (sorted lexicographically),
        # then unassigned (sorted by driver name).
        # All tuple positions must use comparable types — mixing int and str
        # in the same slot blows up Python's tuple comparison.
        off_bucket = 2 if row.get("is_off_today") else 0
        assignment = row.get("assignment")
        vehicle_number = None
        if assignment and assignment.vehicle and assignment.vehicle.vehicle_number:
            vehicle_number = assignment.vehicle.vehicle_number.lstrip("#").strip()
        if vehicle_number:
            try:
                return (off_bucket, 0, int(vehicle_number), "")
            except ValueError:
                return (off_bucket, 1, 0, vehicle_number)
        return (off_bucket + 1, 2, 0, str(row["driver"]))

    inhouse_driver_rows.sort(key=_inhouse_vehicle_sort_key)

    # Count legs per driver on the selected date (from already-fetched legs, no extra query)
    # Also capture each driver's "next stop" leg — the one the Samsara ETA sweep flagged
    # with a fresh dispatch_risk_status — so the driver card can show the live ETA/badge.
    _all_leg_counts = {}
    _live_leg_by_driver = {}
    for _leg in _all_day_legs:
        if _leg.driver_id:
            _all_leg_counts[_leg.driver_id] = _all_leg_counts.get(_leg.driver_id, 0) + 1
            if _leg.dispatch_risk_status and _leg.dispatch_eta_is_fresh:
                _live_leg_by_driver[_leg.driver_id] = _leg
    for row in inhouse_driver_rows:
        row["leg_count"] = _all_leg_counts.get(row["driver"].id, 0)
        row["live_leg"] = _live_leg_by_driver.get(row["driver"].id)

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

    inhouse_vehicles = _annotate_vehicle_status(
        sorted(
            FleetVehicle.objects.filter(is_active=True).select_related("vehicle_type"),
            key=_vehicle_sort_key,
        ),
        selected_date,
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

    # After-hours fee owed (delay-aware) per leg + a dashboard count, so a delay
    # past 10 PM shows an unmissable inline flag the dispatcher can charge in one
    # click. afterhours_fee_outstanding() returns 0 for already-charged legs
    # (incl. trips booked late), so those never flag.
    afterhours_owed_count = 0
    for leg in legs:
        leg.afterhours_owed = leg.afterhours_fee_outstanding() > 0
        if leg.afterhours_owed:
            afterhours_owed_count += 1

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

    _legs_list = list(legs) if not isinstance(legs, list) else legs

    # (Removed) A per-leg turnaround_warning pass used to run here, banding the raw
    # clock gap at 0/10/20 with no repositioning drive and no deplaning grace. No
    # template ever rendered it, and its numbers contradicted the feasibility engine,
    # so it was pure cost and a trap for anyone who later wired it up. The board's
    # turnaround signal is the timeline gap chip, now engine-backed via
    # _gap_turn_slack(); the per-row signal is the conflict-task badge just below.

    # Open driver-conflict / tight-turn tasks → red "Conflict → task" badge on
    # the row so the board itself points at the task queue (one query).
    _conflict_task_by_leg = {}
    try:
        from ops.models import OperationalTask
        for _leg_id, _task_id in OperationalTask.objects.filter(
            leg_id__in=[l.id for l in _legs_list],
            status__in=list(OperationalTask.OPEN_STATUSES),
            task_type__in=[
                OperationalTask.TaskType.DRIVER_CONFLICT,
                OperationalTask.TaskType.TIGHT_TURN,
            ],
        ).values_list('leg_id', 'id'):
            _conflict_task_by_leg.setdefault(_leg_id, _task_id)
    except Exception:
        pass
    for leg in _legs_list:
        leg.open_conflict_task_id = _conflict_task_by_leg.get(leg.id)

    # Build compact driver timeline for in-house drivers with assignments
    # Reuse _all_day_legs (already fetched with all select_related + prefetch) — no extra query
    _all_legs_for_timeline = _all_day_legs
    # O(1) leg lookup, hoisted here so the timeline slot loop can attach KEOI data
    # (also reused for the unassigned-slot build below).
    _leg_by_id = {_l.id: _l for _l in _all_legs_for_timeline}
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
            # Earliest 'picked-up' tap, naive local to match the gap math. _sh_list is
            # newest-first, so overwriting keeps the EARLIEST — the true start. The gap
            # chips re-anchor a turn on this fact (see _gap_turn_slack).
            _picked_up_local = None
            for _sh in _sh_list:
                if _sh.status == 'picked-up':
                    _picked_up_local = timezone.localtime(_sh.timestamp).replace(tzinfo=None)
            _leg_status_map[_tleg.id] = {
                'status_label': _status_label,
                'status_time': _local_ts.strftime('%I:%M %p').lstrip('0'),
                'status_ago': _ago_str,
                'picked_up_dt': _picked_up_local,
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
            _pax = _tleg.effective_passenger_count if _tleg.reservation else ''
            _dropoff = _tleg.dropoff_location or ''
            try:
                _stop_count = len(_tleg.legstop_set.all())
            except Exception:
                _stop_count = 0
            try:
                _legflight_total = len(_tleg.legflight_set.all())
            except Exception:
                _legflight_total = 0
            _gap_candidates.append({
                'leg_id': _tleg.id,
                'pickup_time': _tleg.pickup_time,
                'pickup_dt': _pickup_dt,
                'pickup_display': _tleg.pickup_time.strftime('%I:%M %p').lstrip('0'),
                'customer': str(_tleg.reservation.customer) if _tleg.reservation else '',
                'pickup_location': _tleg.pickup_location or '',
                'dropoff_location': _dropoff,
                'trip_type': _trip,
                'pending_refund': bool(_tleg.reservation.has_pending_refund) if _tleg.reservation else False,
                'source': 'affiliate' if _is_affiliate else 'unassigned',
                'driver_name': str(_tleg.driver) if _is_affiliate else '',
                'vehicle_type': _tleg.effective_vehicle_type or '',
                'flight_info': _flight_str,
                'passengers': str(_pax) if _pax else '',
                'status': _tleg.status or '',
                'extra_stop_count': _stop_count,
                'secondary_flight_count': max(_legflight_total - 1, 0),
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
        # reservation + flight_information are read by estimate_job_end_time
        # (store_stop, flight arrival) — pull them in to avoid a .get() per leg.
        .select_related("driver", "reservation", "flight_information")
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
        # Effective availability for the day (drives the limited badge + on-grid block band)
        _tl_eff = _driver.get_effective_availability(selected_date)
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
            # KEOI watch flag (prefetched — no query)
            _kd = _slot_keoi(_leg_by_id.get(_slot.leg_id))
            _slot.keoi_category = _kd['keoi_category']
            _slot.keoi_category_label = _kd['keoi_category_label']
            _slot.keoi_status_label = _kd['keoi_status_label']
            _slot.keoi_desc = _kd['keoi_desc']
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
            # Tight/critical come from the feasibility engine, not the raw clock gap,
            # so the chip and the assignment check can never describe the same turn
            # differently. _gap_min stays the raw gap — that's what the label shows.
            # A recorded pickup on the previous leg re-anchors its clear time, so a
            # driver running ahead of the plan stops showing a stale amber.
            _prev_sinfo = _leg_status_map.get(_sched.slots[_i].leg_id)
            _turn_band = pickup_policy.turn_band(_gap_turn_slack(
                _sched.slots[_i], _sched.slots[_i + 1], selected_date,
                prev_leg=_leg_by_id.get(_sched.slots[_i].leg_id),
                prev_picked_up_dt=(_prev_sinfo.get('picked_up_dt') if _prev_sinfo else None)))
            _gaps.append({
                'gap_minutes': _gap_min,
                'gap_display': _gap_display,
                'is_tight': _turn_band == 'tight',
                'is_critical': _turn_band == 'critical',
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
            'avail_status': _tl_eff["status"],
            'shift_display': _tl_eff["display_label"] if _tl_eff["is_available"] else "Off",
            'avail_tooltip': _tl_eff["tooltip"],
            'exception_notes': _tl_eff["exception_notes"],
            'exc_badge': format_exception_badge(_tl_eff),
            'avail_blocks': availability_block_bands(_tl_eff, display_start, total_display_minutes),
            'cert_labels': _driver.cert_labels(),
            'sprinter_ok': bool(_driver.cert_labels()),
            'pref_vehicle': _driver.preferred_vehicle_label(),
            'shift_pref_label': format_shift_preference(_tl_eff),
        })

    # Build unassigned timeline slots for drag-and-drop
    # (_leg_by_id was hoisted above so the timeline slot loop could attach KEOI data)
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
            'pending_refund': _gc.get('pending_refund', False),
            'is_vip': bool(_uleg.is_vip) if _uleg else False,
            'extra_stop_count': _gc.get('extra_stop_count', 0),
            'secondary_flight_count': _gc.get('secondary_flight_count', 0),
        })

    prev_date = selected_date - timedelta(days=1)
    next_date = selected_date + timedelta(days=1)

    # ── Overnight tail: tomorrow's after-midnight pickups, shown at the end of
    # TONIGHT's board. Dispatch thinks in shift-nights — a 12:30 AM Jul 2 pickup
    # belongs to the Jul 1 night crew. Replaces the old manual workaround of
    # moving such jobs to 11:59 PM (which corrupted the real pickup date and
    # broke flight tracking): the leg keeps its true date and simply APPEARS
    # here too, read-only, with a jump link to its real board.
    from datetime import time as _dtime
    from .overnight_arrival import NIGHT_TAIL_END_HOUR as _tail_end
    # Founder rule: only 12 AM-2 AM jobs belong to the previous night's crew.
    # Anything later (red-eyes landing 3-5 AM, early departure runs) stays on
    # its own day's board only.
    overnight_tail_legs = list(
        Leg.objects.filter(
            pickup_date=next_date,
            pickup_time__lt=_dtime(_tail_end, 0),
        )
        .exclude(reservation__status='cancelled').exclude(status='cancelled')
        .select_related(
            "reservation", "reservation__customer", "reservation__vehicle",
            "vehicle", "driver", "driver__profile", "flight_information",
        )
        .order_by("pickup_time")
    )

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
        "keoi_filter": keoi_filter,
        "keoi_count": keoi_count,
        "keoi_legs": keoi_legs,
        "vehicle_type_counts": vehicle_type_counts,
        "total_legs": len(legs),
        "afterhours_owed_count": afterhours_owed_count,
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
        "highlight_leg_id": int(highlight_leg_id) if highlight_leg_id and highlight_leg_id.isdigit() else None,
        "overnight_tail_legs": overnight_tail_legs,
        # ── Sandbox draft context (banner, review modal, controls) ──
        **_draft_ctx,
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

    # ── Board mode ──────────────────────────────────────────────────────────
    # Two SEPARATE boards over the same day, never mixed: the in-house board
    # (rows = inhouse drivers, each with a fleet vehicle) and the affiliate
    # board (rows = every active affiliate, no vehicles — we don't own them).
    # Both share the unassigned lane, so a job is farmed out by dragging it
    # from Unassigned onto an affiliate row.
    board_view = request.GET.get("view", "inhouse")
    if board_view not in ("inhouse", "affiliate"):
        board_view = "inhouse"
    is_affiliate_board = board_view == "affiliate"

    # ── Driver focus filter ─────────────────────────────────────────────────
    # Narrow the board to ONE driver's lane. Read here, validated further down
    # against the rows that actually got built — a driver who is off, or who
    # belongs to the other board, has no lane to focus on.
    driver_filter = (request.GET.get("driver") or "").strip()

    # Fetch all legs for the date (single query)
    all_legs = list(
        Leg.objects.filter(pickup_date=selected_date)
        .exclude(reservation__status="cancelled")
        .exclude(status="cancelled")
        .select_related(
            "driver", "reservation", "reservation__customer",
            "reservation__vehicle", "vehicle", "flight_information", "cruise_information",
            "reservation__travel_agent",
            "reservation__travel_agent__agency",  # for Leg.is_vip agency-keyword check (no N+1)
        )
        .prefetch_related(
            "legstop_set",
            "legflight_set",
            Prefetch("status_history", queryset=LegStatus.objects.select_related("updated_by").order_by("-timestamp")),
            Prefetch("reservation__payments", queryset=Payment.objects.order_by('-created_at')),
            Prefetch(
                "keoi_flags",
                queryset=LegKeoi.objects.filter(closed_at__isnull=True).select_related("created_by"),
                to_attr="active_keoi_list",
            ),
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

    # Roster for the active board. Inactive drivers are excluded from both
    # boards (directory-only).
    board_drivers = list(
        Driver.objects.filter(
            driver_type="affiliate" if is_affiliate_board else "inhouse",
            is_active=True,
        )
        .select_related("profile")
        .prefetch_related(
            "weekly_schedule", "date_overrides",
            "certified_vehicle_types", "preferred_vehicle_types", "preferred_vehicles",
        )
        .order_by("profile__first_name")
    )

    # Affiliates never get a fleet vehicle (Day Setup is inhouse-only and treats
    # any affiliate-held DriverVehicleAssignment as stale), so the vehicle
    # lookup + vehicle-number sort are skipped entirely on that board.
    assignments = {}
    _aff_profiles = {}
    _aff_carded_ids = set()
    if is_affiliate_board:
        from drivers.models import AffiliateProfile, DriverPayRate
        _aff_profiles = {
            p.driver_id: p
            for p in AffiliateProfile.objects.filter(driver__in=board_drivers)
        }
        # "Carded" = has >=1 DriverPayRate row. Uncarded affiliates are still
        # shown (the founder wants the whole bench visible) but flagged, since
        # farm-out pricing can't quote them.
        _aff_carded_ids = set(
            DriverPayRate.objects.filter(driver__in=board_drivers)
            .values_list("driver_id", flat=True)
        )
    else:
        assignments = {
            a.driver_id: a
            for a in DriverVehicleAssignment.objects.filter(
                driver__in=board_drivers, date=selected_date
            ).select_related("vehicle", "vehicle__vehicle_type")
        }
        # Sort: vehicle-assigned drivers first by vehicle number, everyone else after.
        # The number is a CharField, so sort it naturally — plain string order puts
        # "10" before "9". Drivers without a vehicle keep the queryset's first-name
        # ordering (Python's sort is stable), i.e. alphabetical.
        def _vehicle_sort_key(d):
            a = assignments.get(d.id)
            vnum = (a.vehicle.vehicle_number or '') if (a and a.vehicle) else ''
            if not vnum:
                return (1, 0, '')
            digits = ''.join(ch for ch in vnum if ch.isdigit())
            return (0, int(digits) if digits else 0, vnum)

        board_drivers.sort(key=_vehicle_sort_key)

    # ── Sandbox draft overlay (shared with dashboard + planner) ──
    # If this date is held by an active draft, render the PROPOSED world: re-point
    # each leg's IN-MEMORY driver to its effective draft driver (never saved) so the
    # existing pipeline buckets legs into the proposed lanes. Live Leg.driver untouched.
    _draft_ctx = _draft_view_context(request, selected_date)
    is_held = _draft_ctx["is_held"]
    _leg_by_id_overlay = {l.id: l for l in all_legs}
    _apply_draft_overlay(_draft_ctx["draft"], all_legs, board_drivers)

    # Build schedules
    _driver_schedules = build_driver_schedules(all_legs, board_drivers, selected_date)

    # ── Timeline hours range ────────────────────────────────────────────────
    # Fit the axis to the DAY'S ACTUAL SPAN, padded by an hour each side. The old
    # rule forced a 6am-10pm floor on every date, so a light day (6 jobs, all at
    # 9 AM) got 17 hours of canvas and rendered as an unreadable 2%-wide cluster
    # in an ocean of white. A busy day naturally spans 5am-11pm and is unaffected;
    # only sparse days change, and they change a lot.
    _hours_with_legs = set()
    for leg in all_legs:
        _hours_with_legs.add(leg.pickup_time.hour)
        _e = getattr(leg, '_estimated_end_dt', None)
        if _e is not None:
            # A job clearing after midnight shouldn't drag the axis back to 0.
            _hours_with_legs.add(_e.hour if _e.date() == selected_date else 23)
    if _hours_with_legs:
        display_start = max(min(_hours_with_legs) - 1, 0)
        display_end = min(max(_hours_with_legs) + 1, 23)
    else:
        display_start, display_end = 6, 22
    # Keep a minimum window so one job doesn't stretch to a 30-minute axis; grow
    # symmetrically around what's there, then push inward off either boundary.
    # Grow ALTERNATELY rather than always leftward, or a single cluster ends up
    # pinned against the right edge with all the dead space in front of it.
    _MIN_SPAN_HOURS = 5
    _grow_end = True
    while (display_end - display_start) < _MIN_SPAN_HOURS:
        if _grow_end and display_end < 23:
            display_end += 1
        elif not _grow_end and display_start > 0:
            display_start -= 1
        elif display_end < 23:
            display_end += 1
        elif display_start > 0:
            display_start -= 1
        else:
            break
        _grow_end = not _grow_end
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
    # Store stop as RECORDED, resolved off the same prefetched trail (no extra
    # queries). Feeds the pill's clearing read-out: once a driver taps his way
    # through a Publix stop, the board stops guessing at a flat 25 minutes.
    _store_state_map = {}
    for leg in all_legs:
        _sh_list = list(leg.status_history.all())
        _store_state_map[leg.id] = store_stop.resolve_store_state(
            leg, status_rows=_sh_list)
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
            # Actual pickup + clear timestamps (naive local, to match the pill's
            # datetime.combine(day, pickup_time) math). _sh_list is newest-first, so
            # overwriting keeps the EARLIEST occurrence — the true start / first clear.
            _picked_up_local = None
            _completed_local = None
            for _sh in _sh_list:
                if _sh.status == 'picked-up':
                    _picked_up_local = timezone.localtime(_sh.timestamp).replace(tzinfo=None)
                elif _sh.status == 'completed':
                    _completed_local = timezone.localtime(_sh.timestamp).replace(tzinfo=None)
            _leg_status_map[leg.id] = {
                'status_label': _status_label,
                'status_time': _local_ts.strftime('%I:%M %p').lstrip('0'),
                'status_ago': _ago_str,
                'picked_up_dt': _picked_up_local,
                'completed_dt': _completed_local,
            }

    # ── "Now" + left-edge anchors for truthful pill geometry ──────────────────
    # Naive local, to match the datetime.combine(selected_date, pickup_time) math the
    # pills already use. Reality (actual pickup / clear / now) is applied in local time.
    _now_local_naive = timezone.localtime(_now).replace(tzinfo=None)
    _board_is_today_geom = (selected_date == _now_local_naive.date())
    _day_left_dt = datetime.combine(selected_date, datetime.min.time()) + timedelta(hours=display_start)

    def _mins_from_left(_dt):
        """Minutes from the board's left edge for a naive-local datetime (rollover-safe)."""
        return (_dt - _day_left_dt).total_seconds() / 60.0

    # Get previous day's last leg per driver (for overnight turnaround display)
    prev_day = selected_date - timedelta(days=1)
    _prev_day_last = {}
    _prev_legs = (
        Leg.objects.filter(pickup_date=prev_day, driver__in=board_drivers)
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

    # Get previous day's vehicle assignments (in-house only — affiliates never hold one,
    # so on that board this would always be an empty round-trip).
    _sb_prev_day_vehicle = {}
    if not is_affiliate_board:
        _sb_prev_assigns = DriverVehicleAssignment.objects.filter(
            date=prev_day, driver__in=board_drivers
        ).select_related('vehicle', 'vehicle__vehicle_type')
        for _sbpda in _sb_prev_assigns:
            if _sbpda.vehicle:
                _vn = _sbpda.vehicle.vehicle_number or ''
                _vt = str(_sbpda.vehicle.vehicle_type) if _sbpda.vehicle.vehicle_type else ''
                _sb_prev_day_vehicle[_sbpda.driver_id] = f"#{_vn} {_vt}".strip() if _vn else _vt

    # Compact preference labels for schedule board badges
    _PREF_SHORT = {
        "prefer_arrival": "Pref Arrivals",
        "prefer_return": "Pref Returns",
        "prefer_cruise": "Pref Cruises",
        "heavy_arrival": "Heavy Arrivals",
        "heavy_return": "Heavy Returns",
        "heavy_cruise": "Heavy Cruises",
        "only_arrival": "Only Arrivals",
        "only_return": "Only Returns",
        "only_cruise": "Only Cruises",
    }

    def _fmt_hour(h):
        """Format hour as compact string: 0→12a, 6→6a, 12→12p, 15→3p, 23→11p"""
        if h == 0:
            return '12a'
        if h < 12:
            return f'{h}a'
        if h == 12:
            return '12p'
        return f'{h - 12}p'

    # Build the board timeline.
    # In-house: everyone WORKING that day gets a row — with or without a vehicle, with
    # or without jobs. Drivers who are off are hidden entirely (they aren't dispatchable,
    # so they're just noise). Previously the rule was "has a vehicle OR has a leg", which
    # dropped everyone else into `available_no_jobs` — a context key the template never
    # rendered — so any date before Day Setup ran had NO drop targets at all.
    # Affiliate: include EVERY active affiliate. We hold no shift data for contractors,
    # so there's no "off" to filter on, and an affiliate with no jobs today is exactly
    # who you want to farm to.
    inhouse_timeline = []
    _drivers_in_timeline = set()
    for driver in board_drivers:
        sched = _driver_schedules.get(driver.id)
        if not sched:
            continue
        has_vehicle = driver.id in assignments

        # Driver availability for selected date (effective: weekly + any active exception)
        _eff = driver.get_effective_availability(selected_date)

        # An off driver who ALREADY holds jobs still gets a row. Hiding them would take
        # their assigned legs off the board with them — the work would silently vanish
        # rather than showing up as the scheduling conflict it actually is.
        if not is_affiliate_board and not _eff["is_available"] and not sched.slots:
            continue

        _drivers_in_timeline.add(driver.id)
        assignment = assignments.get(driver.id)
        vehicle_number = ''
        vehicle_type_label = ''
        vehicle_notes = ''
        vehicle_oos_label = ''
        if assignment and assignment.vehicle:
            vehicle_number = assignment.vehicle.vehicle_number or ''
            vehicle_notes = assignment.vehicle.notes or ''
            # The board is where a dispatcher watches the day run. A driver still
            # holding a unit that's been marked down has to read as a problem here
            # too, not only on the pages where cars get assigned.
            vehicle_oos_label = assignment.vehicle.out_of_service_label(selected_date)
            if assignment.vehicle.vehicle_type:
                vehicle_type_label = str(assignment.vehicle.vehicle_type)
        _is_avail, _sh, _eh, _pref, _flex = (
            _eff["is_available"], _eff["start_hour"], _eff["end_hour"],
            _eff["preference"], _eff["flexible"],
        )
        _stype = _eff.get("shift_type", "full_day")
        _mhrs = _eff.get("max_hours")
        _pshift = _eff.get("preferred_shift", "")
        _snotes = _eff.get("scheduling_notes", "")
        _shift_display = _eff["display_label"] if _is_avail else "Off"
        if _is_avail and _mhrs and _eff["status"] != "limited":
            _shift_display += f" ({int(_mhrs)}h)"
        _avail_status = _eff["status"]
        _avail_tooltip = _eff["tooltip"]
        _exception_notes = _eff["exception_notes"]
        _has_exception = _eff["has_exception"]
        _pref_short = _PREF_SHORT.get(_pref, '')

        for slot in sched.slots:
            _sinfo = _leg_status_map.get(slot.leg_id)

            # ── Truthful pill geometry ───────────────────────────────────────
            # Resolve the pill's effective start/end from REALITY wherever it's known
            # (actual pickup / actual clear / still-running-now), estimate otherwise.
            # When this pickup should actually have HAPPENED. For a flight-tracked
            # arrival (including an airport->cruise-port transfer, whose trip_type
            # reads 'cruise') that's gate arrival + the real airport dwell, so a
            # delayed flight moves the bar out instead of reporting the driver late
            # for a plane still in the air, and a driver waiting at baggage claim on
            # schedule isn't called overdue.
            _risk_leg_src = _leg_by_id_overlay.get(slot.leg_id)
            _expected_dt, _deadline_basis = (None, '')
            _flight_gated = None
            if _risk_leg_src is not None:
                _expected_dt, _deadline_basis = pickup_policy.pickup_expected_dt(
                    _risk_leg_src, aware=False)
                _flight_gated = _risk_leg_src.is_flight_tracked_arrival()
            # ── Live clearing (the "second clearing time") ───────────────────
            # Until the guest is aboard, the clear time is a forecast off the
            # flight. After the pickup tap it is arithmetic off what the driver
            # actually did — and every store tap sharpens it further. Drawing the
            # forecast instead is what let a van that had been rolling since 1:27
            # occupy the board until 2:55, and what raised CRITICAL conflict tasks
            # about turns the driver was comfortably going to make.
            _picked_dt = (_sinfo.get('picked_up_dt') if _sinfo else None)
            _store = _store_state_map.get(slot.leg_id)
            _est_end = slot.estimated_end_time
            if (_risk_leg_src is not None and _picked_dt is not None
                    and slot.status not in ('completed', 'cancelled')):
                try:
                    from dispatching.scheduler import chain_clear_dt_from_actual
                    _est_end = chain_clear_dt_from_actual(
                        _risk_leg_src, _picked_dt, store_state=_store)
                except Exception:
                    _est_end = slot.estimated_end_time
            slot.store_phase = _store.phase if _store else 'none'
            slot.store_note = store_stop.describe(_store) or '' if _store else ''
            slot.store_adhoc = bool(_store and _store.adhoc)
            slot.clearing_is_live = _est_end is not slot.estimated_end_time

            _span = _truthful_pill_span(
                sched_start_dt=datetime.combine(selected_date, slot.pickup_time),
                est_end_dt=_est_end,
                status=slot.status,
                trip_type=slot.trip_type,
                picked_up_dt=(_sinfo.get('picked_up_dt') if _sinfo else None),
                completed_dt=(_sinfo.get('completed_dt') if _sinfo else None),
                now_dt=_now_local_naive,
                is_today=_board_is_today_geom,
                expected_pickup_dt=_expected_dt,
                is_flight_gated=_flight_gated,
            )
            slot.late_start = _span['late_start']
            slot.late_start_mins = _span['late_start_mins']
            slot.actual_pickup_display = (
                _span['actual_pickup_dt'].strftime('%I:%M %p').lstrip('0')
                if _span['actual_pickup_dt'] else '')
            slot.overrunning = _span['overrunning']
            slot.overrun_mins = _span['overrun_mins']
            slot.cleared_is_actual = _span['cleared_is_actual']
            slot.pickup_overdue = _span['pickup_overdue']
            slot.pickup_overdue_mins = _span['pickup_overdue_mins']
            slot.pickup_stalled = _span['pickup_stalled']
            slot.overrun_from_pct = None

            # Live GPS "will he make the pickup?" band (Samsara sweep), when fresh, folded
            # together with the clock flags into one escalating risk cue. The dispatch_*
            # columns are already loaded on the leg; we only READ them (the sweep owns
            # writing). Only a PICKUP-deadline target with fresh telematics counts.
            _risk_leg = _leg_by_id_overlay.get(slot.leg_id)
            _gps_status, _gps_eta, _gps_reason = '', None, ''
            if (_board_is_today_geom and _risk_leg is not None
                    and getattr(_risk_leg, 'dispatch_eta_is_fresh', False)
                    and (_risk_leg.dispatch_eta_target or '') in _GPS_PICKUP_TARGETS):
                _gps_status = _risk_leg.dispatch_risk_status or ''
                _gps_eta = _risk_leg.dispatch_eta_minutes
                _gps_reason = _risk_leg.dispatch_risk_reason or ''
            _risk = _pickup_risk(
                pickup_overdue=slot.pickup_overdue, pickup_stalled=slot.pickup_stalled,
                overdue_mins=slot.pickup_overdue_mins,
                gps_status=_gps_status, gps_eta_mins=_gps_eta, gps_reason=_gps_reason)
            slot.risk_tier = _risk['tier']
            slot.risk_source = _risk['source']
            slot.risk_label = _risk['label']
            # Name the rule that fired. A dispatcher who can see "flight gated 10:42 ·
            # meet by 10:52" can VERIFY the flag instead of having to trust it — which
            # is the difference between a signal and a decoration.
            slot.risk_reason = (
                f"{_risk['reason']} · {_deadline_basis}"
                if _risk['tier'] and _deadline_basis else _risk['reason'])
            slot.gps_eta_mins = _gps_eta if _gps_status else None

            _start_min = _mins_from_left(_span['eff_start'])
            _dur = max(_mins_from_left(_span['eff_end']) - _start_min, _SLOT_FLOOR_MIN)
            slot.position_pct = round(max(0, _start_min / total_display_minutes * 100), 1)
            slot.width_pct = round(min(_dur / total_display_minutes * 100, 100 - slot.position_pct), 1)

            # Where the over-schedule hatch begins WITHIN the pill (est end → now), as a
            # % of the pill's own width, so only the overrun portion is marked.
            if slot.overrunning and _span['est_end'] is not None:
                _frac = (_mins_from_left(_span['est_end']) - _start_min) / _dur
                slot.overrun_from_pct = round(min(max(_frac, 0.0), 1.0) * 100, 1)

            # Clearing read-out for the popup: the ACTUAL time (no tilde) once complete,
            # the estimate (tilde added in the template) while still projected.
            if slot.cleared_is_actual:
                slot.end_time_display = _span['eff_end'].strftime('%I:%M %p').lstrip('0')
            else:
                slot.end_time_display = (_span['est_end'].strftime('%I:%M %p').lstrip('0')
                                         if _span['est_end'] is not None else '')

            slot.status_label = _sinfo['status_label'] if _sinfo else ''
            slot.status_time = _sinfo['status_time'] if _sinfo else ''
            slot.status_ago = _sinfo['status_ago'] if _sinfo else ''
            # Draft overlay flags (held days only)
            _sleg = _leg_by_id_overlay.get(slot.leg_id)
            slot.is_proposed = bool(getattr(_sleg, 'draft_proposed', False)) if _sleg else False
            slot.live_driver_label = _driver_label(getattr(_sleg, 'draft_live_driver', None)) if _sleg else None
            slot.live_conflict = bool(getattr(_sleg, 'draft_live_conflict', False)) if _sleg else False
            slot.staged_label = getattr(_sleg, 'draft_staged_label', None) if _sleg else None
            slot.live_by_label = getattr(_sleg, 'draft_live_by_label', None) if _sleg else None
            slot.time_changed = bool(getattr(_sleg, 'draft_time_changed', False)) if _sleg else False
            slot.old_time = getattr(_sleg, 'draft_old_time', None) if _sleg else None
            for _k, _v in _slot_notes(_sleg).items():
                setattr(slot, _k, _v)
            for _k, _v in _slot_keoi(_sleg).items():
                setattr(slot, _k, _v)

        # Affiliate capacity read-out (replaces the vehicle column on that board).
        # Mirrors AffiliateProfile's capacity model: a single-chain affiliate is one
        # vehicle end-to-end, a count_cap/fleet affiliate sells N seats that day.
        _aff_cap_label = ''
        _aff_cap_used = len(sched.slots)
        _aff_cap_max = None
        _aff_cap_full = False
        _aff_tier = ''
        _aff_no_rate = False
        _aff_no_port = False
        if is_affiliate_board:
            _prof = _aff_profiles.get(driver.id)
            _aff_no_rate = driver.id not in _aff_carded_ids
            if _prof:
                _aff_tier = _prof.get_max_vehicle_tier_display() if _prof.max_vehicle_tier else ''
                _aff_no_port = _prof.no_pickup_at_port_sanford
                if _prof.capacity_mode == 'single_chain':
                    _aff_cap_label = 'Single vehicle'
                else:
                    # Same default the farm-out gate applies when daily_cap is unset,
                    # so the badge can't promise room the drop check will refuse.
                    from dispatching.farmout_optimizer import ANTHONY_MAX_LEGS_PER_DAY
                    _aff_cap_max = _prof.daily_cap
                    if _aff_cap_max is None and _prof.capacity_mode == 'count_cap':
                        _aff_cap_max = ANTHONY_MAX_LEGS_PER_DAY
                    _mode_word = 'Fleet' if _prof.capacity_mode == 'fleet' else 'Cap'
                    if _aff_cap_max:
                        _aff_cap_label = f'{_mode_word} {_aff_cap_used}/{_aff_cap_max}'
                        _aff_cap_full = _aff_cap_used >= _aff_cap_max
                    else:
                        _aff_cap_label = _mode_word
            else:
                _aff_cap_label = 'No profile'

        # Lane-pack this driver's bars. Without it every slot sat at the same
        # top:2px, so concurrent jobs painted over each other in start order and
        # the EARLIER job was buried — invisible, unhoverable and undraggable,
        # while still counting toward "N jobs". A double-booked driver looked fine.
        _row_lanes = _pack_lanes(sched.slots,
                                 lane_height=_DRIVER_LANE_H, gap=_DRIVER_LANE_GAP)
        _row_bar_height = _row_lanes * (_DRIVER_LANE_H + _DRIVER_LANE_GAP) + 2

        inhouse_timeline.append({
            'driver': driver,
            'schedule': sched,
            'total_legs': sched.total_legs,
            'row_lanes': _row_lanes,
            'row_bar_height': _row_bar_height,
            'has_overlap': _row_lanes > 1,
            'has_vehicle': has_vehicle,
            'aff_cap_label': _aff_cap_label,
            'aff_cap_used': _aff_cap_used,
            'aff_cap_max': _aff_cap_max,
            'aff_cap_full': _aff_cap_full,
            'aff_tier': _aff_tier,
            'aff_no_rate': _aff_no_rate,
            'aff_no_port': _aff_no_port,
            'affiliate_vehicle': driver.vehicle or '' if is_affiliate_board else '',
            'vehicle_number': vehicle_number,
            'vehicle_oos_label': vehicle_oos_label,
            'vehicle_type_label': vehicle_type_label,
            'prev_night_cleared': _prev_day_last.get(driver.id, ''),
            'prev_night_vehicle': _sb_prev_day_vehicle.get(driver.id, ''),
            'shift_display': _shift_display,
            'shift_type': _stype,
            'shift_start': _sh,
            'shift_end': _eh,
            'flexible': _flex,
            'max_hours': float(_mhrs) if _mhrs else None,
            'preference': _pref,
            'pref_short': _pref_short,
            'driver_notes': driver.notes or '',
            'driver_phone': driver.phone_number or '',
            'vehicle_notes': vehicle_notes,
            'preferred_shift': _pshift,
            'scheduling_notes': _snotes,
            'avail_status': _avail_status,
            'avail_tooltip': _avail_tooltip,
            'exception_notes': _exception_notes,
            'has_exception': _has_exception,
            'exc_badge': format_exception_badge(_eff),
            'avail_blocks': availability_block_bands(_eff, display_start, total_display_minutes),
            'cert_labels': driver.cert_labels(),
            'sprinter_ok': bool(driver.cert_labels()),
            'pref_vehicle': driver.preferred_vehicle_label(),
            'shift_pref_label': format_shift_preference(_eff),
        })

    # Mark where the DEPLOYED drivers end and the AVAILABLE-but-no-vehicle ones begin.
    # The sort already groups them; this flags the FIRST of the second group so the
    # board draws one divider instead of the template guessing at the transition.
    # In-house only — affiliates never hold a fleet vehicle, so the split is
    # meaningless there and would put a divider above every row.
    if not is_affiliate_board:
        for _row in inhouse_timeline:
            if not _row['has_vehicle']:
                _row['starts_no_vehicle_group'] = True
                break

    # `available_no_jobs` used to be built here: ~45 lines and a per-driver availability
    # call for a context key the template never rendered. Every working driver now gets
    # a real row above, so it is retired — kept as an empty list only because the context
    # key is still passed.
    available_no_jobs = []

    # Build unassigned timeline slots
    _leg_by_id = {l.id: l for l in all_legs}
    unassigned_timeline_slots = []
    for leg in all_legs:
        if leg.driver is not None:
            continue
        pt = leg.pickup_time
        _start_min = (pt.hour - display_start) * 60 + pt.minute
        _end_dt = getattr(leg, '_estimated_end_dt', None)
        # Same rollover-safe measurement as the driver slots (see _slot_duration_minutes).
        _dur = (_slot_duration_minutes(selected_date, pt, _end_dt) if _end_dt else 45)
        _pos = round(max(0, _start_min / total_display_minutes * 100), 1)
        _wid = round(min(_dur / total_display_minutes * 100, 100 - _pos), 1)
        _sinfo = _leg_status_map.get(leg.id)
        _trip = leg.get_trip_type() if hasattr(leg, 'get_trip_type') else 'other'
        _customer = str(leg.reservation.customer) if leg.reservation and leg.reservation.customer else ''
        _flight_str = ''
        if leg.flight_information:
            _fi = leg.flight_information
            _flight_str = f"{_fi.airline or ''} {_fi.flight_number or ''}".strip()
        _vtype = leg.effective_vehicle_type or ''
        _vabbr_map = {'towncar': 'TC', 'suv': 'SUV', 'mini_van': 'MV', 'van': 'VAN', 'Van(14 Pax)': 'V14'}
        _vabbr = _vabbr_map.get(str(_vtype), '') if _vtype else ''
        # Compact car-seat string (e.g. "1 rf, 2 ff, 1 b")
        _us_carseat_parts = []
        try:
            if leg.effective_need_carseats:
                if leg.effective_rf_carseats:
                    _us_carseat_parts.append(f"{leg.effective_rf_carseats} rf")
                if leg.effective_ff_carseats:
                    _us_carseat_parts.append(f"{leg.effective_ff_carseats} ff")
                if leg.effective_booster_seats:
                    _us_carseat_parts.append(f"{leg.effective_booster_seats} b")
        except Exception:
            pass
        _us_carseats = ", ".join(_us_carseat_parts)

        unassigned_timeline_slots.append({
            'leg_id': leg.id,
            'trip_type': _trip,
            'pickup_display': pt.strftime('%I:%M %p').lstrip('0'),
            # Short form for the CHIP LABEL. The lane rendered the full "6:45 AM"
            # into the same width driver bars use for "6:45", so unassigned chips
            # truncated ~3 characters earlier for no information gain — the meridiem
            # is already implied by the position on the axis. Full form stays on the
            # hover popup via `pickup_display`.
            'pickup_short': pt.strftime('%I:%M').lstrip('0'),
            **_slot_notes(leg),
            **_slot_keoi(leg),
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
            'is_paid': (leg.reservation.payment_status == 'paid') if leg.reservation else True,
            'passengers': int(leg.effective_passenger_count or 1),
            'luggage': int(leg.effective_luggage_count or 0),
            'luggage_type': leg.effective_luggage_type or '',
            'carseats_short': _us_carseats,
            # Only the grocery leg (arrival / airport-origin cruise) shows Publix.
            'store_stop': leg.shows_store_stop,
            'pending_refund': bool(leg.reservation.has_pending_refund) if leg.reservation else False,
            'is_vip': leg.is_vip,
            'reservation_id': leg.reservation_id,
            'extra_stop_count': len(getattr(leg, 'legstop_set').all()) if leg.pk else 0,
            'secondary_flight_count': max(len(getattr(leg, 'legflight_set').all()) - 1, 0) if leg.pk else 0,
            # Draft overlay flags (held days only)
            'is_proposed': bool(getattr(leg, 'draft_proposed', False)),
            'is_new_attention': bool(getattr(leg, 'draft_new_attention', False)),
            'live_driver_label': _driver_label(getattr(leg, 'draft_live_driver', None)),
            'live_conflict': bool(getattr(leg, 'draft_live_conflict', False)),
            'staged_label': getattr(leg, 'draft_staged_label', None),
            'live_by_label': getattr(leg, 'draft_live_by_label', None),
            'time_changed': bool(getattr(leg, 'draft_time_changed', False)),
            'old_time': getattr(leg, 'draft_old_time', None),
        })

    # Lane-pack the unassigned backlog so overlapping jobs stack instead of hiding
    # each other. Display order stays "bigger vehicles first" (that's the scan order
    # dispatchers want), but PACKING runs start-sorted — see _pack_lanes.
    _num_lanes = _pack_lanes(unassigned_timeline_slots,
                             lane_height=_UNASSIGNED_LANE_H, gap=_UNASSIGNED_LANE_GAP)
    _vehicle_sort_order = {'Van(14 Pax)': 0, 'van': 1, 'suv': 2, 'mini_van': 3, 'towncar': 4, '': 5}
    unassigned_timeline_slots.sort(
        key=lambda s: (_vehicle_sort_order.get(s['vehicle_type'], 5), s['position_pct']))
    _unassigned_lane_height = _num_lanes * (_UNASSIGNED_LANE_H + _UNASSIGNED_LANE_GAP) + 4

    # Live-clock seed (server local time; see the context block for why).
    _board_local_now = timezone.localtime()
    _board_is_today = selected_date == _board_local_now.date()
    _board_now_secs = (
        _board_local_now.hour * 3600 + _board_local_now.minute * 60 + _board_local_now.second
    )

    # Summary counts. `assigned_count` stays whole-day (both boards show the same
    # day), while the farmed/inhouse split tells you where the day actually sits.
    total_legs = len(all_legs)
    assigned_count = sum(1 for l in all_legs if l.driver)
    unassigned_count = total_legs - assigned_count
    farmed_count = sum(
        1 for l in all_legs if l.driver and l.driver.driver_type == "affiliate"
    )
    inhouse_count = assigned_count - farmed_count
    # Affiliate-board header: how much of the bench is actually working today.
    affiliate_roster_count = len(inhouse_timeline) if is_affiliate_board else 0
    affiliate_working_count = (
        sum(1 for r in inhouse_timeline if r['total_legs']) if is_affiliate_board else 0
    )

    # ── Driver focus filter, applied ────────────────────────────────────────
    # Applied LAST, on the finished rows, so nothing upstream changes shape: the
    # axis, the day's counts and the Unassigned lane all stay whole-day. That is
    # deliberate — you focus on a driver mostly to hand him something off the
    # backlog, and a filtered board that also hid the backlog (or quietly reported
    # "3 legs" for a 27-leg day) would be lying about the day.
    #
    # Options come from the rows we actually built, so the dropdown can never
    # offer a driver the board wouldn't draw. Anything else — a stale link, a
    # driver who is off today, a hand-typed id — falls back to the whole board
    # and says so, rather than rendering an empty one.
    board_driver_options = sorted(
        (
            {
                "id": _r["driver"].id,
                "label": str(_r["driver"]),
                "total_legs": _r["total_legs"],
                "vehicle_number": _r["vehicle_number"],
            }
            for _r in inhouse_timeline
        ),
        key=lambda o: o["label"].lower(),
    )
    driver_filter_dropped = ""
    filtered_driver_name = ""
    filtered_driver_legs = 0
    if driver_filter:
        if driver_filter in {str(o["id"]) for o in board_driver_options}:
            inhouse_timeline = [
                r for r in inhouse_timeline if str(r["driver"].id) == driver_filter
            ]
            # A single row has no group above it, so the "Available — no vehicle
            # assigned" divider would be a header over nothing.
            inhouse_timeline[0].pop("starts_no_vehicle_group", None)
            filtered_driver_name = str(inhouse_timeline[0]["driver"])
            filtered_driver_legs = inhouse_timeline[0]["total_legs"]
        else:
            _req = next((d for d in board_drivers if str(d.id) == driver_filter), None)
            driver_filter_dropped = str(_req) if _req else "That driver"
            driver_filter = ""

    # Overnight tail (same night-crew rule as the dashboard): tomorrow's
    # 12-2 AM jobs shown as a read-only strip at the end of TONIGHT's board.
    # Deliberately NOT merged into the drag/assign timeline — drivers watch
    # this board live, and cross-date lanes would scramble the hour math.
    from datetime import time as _dtime
    from .overnight_arrival import NIGHT_TAIL_END_HOUR as _tail_end
    overnight_tail_legs = list(
        Leg.objects.filter(
            pickup_date=next_date,
            pickup_time__lt=_dtime(_tail_end, 0),
        )
        .exclude(reservation__status='cancelled').exclude(status='cancelled')
        .select_related(
            "reservation", "reservation__customer", "driver", "driver__profile",
            "flight_information", "reservation__vehicle", "vehicle",
        )
        .order_by("pickup_time")
    )

    context = {
        "selected_date": selected_date,
        "prev_date": prev_date,
        "next_date": next_date,
        "board_view": board_view,
        "is_affiliate_board": is_affiliate_board,
        # ── Driver focus filter ──
        "driver_filter": driver_filter,
        "board_driver_options": board_driver_options,
        "filtered_driver_name": filtered_driver_name,
        "filtered_driver_legs": filtered_driver_legs,
        "driver_filter_dropped": driver_filter_dropped,
        # ── Live clock + "now" marker ──
        # Seeded from the SERVER's local time, not the browser's: dispatchers
        # reviewing the board from another timezone must still see Orlando time,
        # and the now-line must sit where the timeline math puts it. The client
        # ticks forward from this seed using elapsed time only, never its own
        # wall clock. The marker is meaningless on any day but today.
        "board_is_today": _board_is_today,
        "board_now_secs": _board_now_secs,
        "board_display_start": display_start,
        "board_total_minutes": total_display_minutes,
        "farmed_count": farmed_count,
        "inhouse_count": inhouse_count,
        "affiliate_roster_count": affiliate_roster_count,
        "affiliate_working_count": affiliate_working_count,
        "inhouse_timeline": inhouse_timeline,
        "timeline_hours": timeline_hours,
        "timeline_ticks": _timeline_ticks,
        "unassigned_timeline_slots": unassigned_timeline_slots,
        "unassigned_lane_height": _unassigned_lane_height,
        "total_legs": total_legs,
        "assigned_count": assigned_count,
        "unassigned_count": unassigned_count,
        "available_no_jobs": available_no_jobs,
        "overnight_tail_legs": overnight_tail_legs,
        # ── Sandbox draft context (banner, review modal, controls) ──
        **_draft_ctx,
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
            "reservation__vehicle", "vehicle",
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
        # Default to a 90-day window. An unbounded "all time" load scanned the whole
        # Reservation table on every request -- including the 5 filtered counts + 2
        # revenue sums in get_context_data -- which is what made this view take
        # ~130s and hold its DB connection that long (incident 2026-07-18). "all"
        # opts into the full scan explicitly; an active search always spans all time
        # so staff can find any customer regardless of date.
        if time_filter == "week":
            queryset = queryset.filter(
                created_at__gte=timezone.now() - timedelta(days=7)
            )
        elif time_filter == "month":
            queryset = queryset.filter(
                created_at__gte=timezone.now() - timedelta(days=30)
            )
        elif time_filter != "all" and not search_query:
            queryset = queryset.filter(
                created_at__gte=timezone.now() - timedelta(days=90)
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
                # Empty -> the default 90-day window (see _get_filtered_queryset).
                "time_filter": self.request.GET.get("time_filter", ""),
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
                    ),
                    # The journey block renders every stop (with its Map button) and
                    # every flight's tracker links — pull both in rather than firing
                    # a pair of queries per leg at template time.
                    "legstop_set__location",
                    "legflight_set__flight",
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

    # Vehicles for per-leg override dropdown in the inline edit form
    vehicles = Vehicle.objects.order_by("capacity")

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

    # Attach each leg's live Samsara vehicle snapshot (read-only; Phase 1).
    # The physical car a leg's driver is in comes from the per-day
    # DriverVehicleAssignment; resolve them all in one batched query and pin the
    # FleetVehicle onto each (prefetched) leg as `leg.samsara_vehicle`. The
    # template reads it only when vehicle.samsara_enabled, so un-onboarded /
    # affiliate vehicles render exactly as before.
    _res_legs = list(reservation.legs.all())
    _assign_keys = {
        (leg.driver_id, leg.pickup_date)
        for leg in _res_legs
        if leg.driver_id and leg.pickup_date
    }
    _assign_lookup = {}
    if _assign_keys:
        for a in DriverVehicleAssignment.objects.filter(
            driver_id__in={k[0] for k in _assign_keys},
            date__in={k[1] for k in _assign_keys},
        ).select_related("vehicle"):
            _assign_lookup[(a.driver_id, a.date)] = a.vehicle
    for leg in _res_legs:
        leg.samsara_vehicle = _assign_lookup.get((leg.driver_id, leg.pickup_date))

    context = {
        "reservation": reservation,
        "total_legs": len(reservation.legs.all()),
        "drivers": drivers,
        "vehicles": vehicles,
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


def _history_actor(record):
    """
    Who made this change, in dispatcher language.

    simple_history only records a user when the save happened inside a request
    from a signed-in user. Customer bookings and background tasks (reminders,
    Stripe webhooks, GHL sync) legitimately have no user; returns None so the
    template can label those "System" rather than showing a bare dash that
    reads like missing data.
    """
    user = record.history_user
    if user:
        return user.get_full_name() or user.username
    return None


def _reservation_creator(reservation):
    """
    Who placed this booking, and in what capacity.

    ``created_by`` is only filled in when a dispatcher books through the back
    office, so on its own it leaves every online booking attributed to nobody.
    A reservation always knows who it belongs to though — fall through to the
    travel agent, then to the customer who booked it themselves.

    Returns (name, capacity); either may be None if the reservation is too
    sparse to name anyone.
    """
    if reservation.created_by:
        user = reservation.created_by
        return (user.get_full_name() or user.username), "Dispatcher"

    agent = getattr(reservation, "travel_agent", None)
    if agent:
        name = agent.agency_name
        if not name and agent.user:
            name = agent.user.get_full_name() or agent.user.username
        if name:
            return name, "Travel agent"

    customer = getattr(reservation, "customer", None)
    if customer:
        from reservations.attribution import channel_label

        capacity = "Customer — booked online"
        label = channel_label(reservation.booking_source)
        if label and label != "—":
            capacity = f"Customer — booked online via {label}"
        return customer.get_full_name(), capacity

    return None, None


RESERVATION_TIMELINE_LIMIT = 300


def _reservation_timeline(reservation, limit=RESERVATION_TIMELINE_LIMIT):
    """
    One chronological story for a reservation: its own field changes plus every
    leg's, newest first.

    The reservation row itself barely ever changes — nearly all dispatcher work
    lands on the legs (driver, times, status, pay). A reservation-only audit log
    therefore looks empty even on a booking that has been touched twenty times,
    which is exactly what "View reservation history" used to show. Merging the
    legs in is what makes the panel worth opening.

    Returns (entries, truncated) where each entry is a plain dict so the same
    template renders reservation and leg rows side by side.
    """
    legs = list(reservation.legs.all().order_by("pickup_date", "pickup_time", "id"))
    leg_labels = {
        leg.id: (
            f"Leg {index}",
            f"{leg.pickup_location} → {leg.dropoff_location}",
        )
        for index, leg in enumerate(legs, start=1)
    }

    entries = []

    res_history = list(
        get_history_manager_for_model(Reservation)
        .filter(uuid=reservation.uuid)
        .select_related("history_user")
        .order_by("-history_date")
    )
    _build_history_with_deltas(Reservation, res_history)
    for record in res_history:
        entries.append(
            {
                "when": record.history_date,
                "scope": "Reservation",
                "scope_detail": "",
                "action": record.get_history_type_display(),
                "actor": _history_actor(record),
                "actor_note": None,
                "changes": getattr(record, "history_delta_changes", None) or [],
            }
        )

    if legs:
        leg_history = list(
            get_history_manager_for_model(Leg)
            .filter(id__in=[leg.id for leg in legs])
            .select_related("history_user")
            .order_by("id", "-history_date")
        )
        # diff_against() only makes sense between two snapshots of the same leg,
        # so build the deltas per leg before merging everything together.
        from collections import defaultdict

        by_leg = defaultdict(list)
        for record in leg_history:
            by_leg[record.id].append(record)
        for leg_id, records in by_leg.items():
            _build_history_with_deltas(Leg, records)
            scope, scope_detail = leg_labels.get(leg_id, (f"Leg #{leg_id}", ""))
            for record in records:
                entries.append(
                    {
                        "when": record.history_date,
                        "scope": scope,
                        "scope_detail": scope_detail,
                        "action": record.get_history_type_display(),
                        "actor": _history_actor(record),
                        "actor_note": None,
                        "changes": getattr(record, "history_delta_changes", None) or [],
                    }
                )

    # "Created, when, by who" is the one thing the panel must always answer, and
    # history alone can't: tracking only started in March 2026, and the insert
    # fires from a signal that has no request user on an online booking or a
    # back-office script. The reservation itself knows — so seed the entry when
    # it's missing, and name the creator when the history row left it blank.
    creator_name, creator_capacity = _reservation_creator(reservation)
    created_entry = next(
        (e for e in entries if e["scope"] == "Reservation" and e["action"] == "Created"),
        None,
    )
    if created_entry is None:
        created_entry = {
            "when": reservation.created_at,
            "scope": "Reservation",
            "scope_detail": "",
            "action": "Created",
            "actor": None,
            "actor_note": None,
            "changes": [],
        }
        entries.append(created_entry)
    if not created_entry["actor"]:
        created_entry["actor"] = creator_name
        created_entry["actor_note"] = creator_capacity
    elif creator_capacity == "Dispatcher":
        # History caught the signed-in dispatcher; still say what they were.
        created_entry["actor_note"] = creator_capacity

    entries.sort(key=lambda e: (e["when"] is not None, e["when"]), reverse=True)
    truncated = len(entries) - limit if len(entries) > limit else 0
    return entries[:limit], truncated


def _reservation_history_context(reservation):
    entries, truncated = _reservation_timeline(reservation)
    creator_name, creator_capacity = _reservation_creator(reservation)
    return {
        "reservation": reservation,
        "timeline_entries": entries,
        "timeline_truncated": truncated,
        # Repeated in the header so the answer survives the 300-entry cap.
        "creator_name": creator_name,
        "creator_capacity": creator_capacity,
    }


def _reservation_for_history(id):
    return get_object_or_404(Reservation.objects.select_related("created_by"), uuid=id)


@login_required(login_url="login")
def reservation_history(request, id):
    """
    Full audit log for a reservation (same data as admin History, in app view).
    """
    if not request.user.is_staff:
        return redirect("home")

    reservation = _reservation_for_history(id)
    context = _reservation_history_context(reservation)
    context["page_title"] = f"Reservation history — {reservation}"
    return render(request, "dispatching/reservation_history.html", context)


@login_required(login_url="login")
def reservation_history_partial(request, id):
    """
    Same timeline as reservation_history, as a fragment for the modal on the
    reservation page. Loaded on open so the (already query-heavy) reservation
    page doesn't pay for history nobody asked to see.
    """
    if not request.user.is_staff:
        return HttpResponse(status=403)

    reservation = _reservation_for_history(id)
    return render(
        request,
        "dispatching/reservation_history_partial.html",
        _reservation_history_context(reservation),
    )


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
    from .leg_timeline import build_leg_timeline, timeline_summary

    events = build_leg_timeline(leg)
    context = {
        "leg": leg,
        "reservation": leg.reservation,
        "events": events,
        "summary": timeline_summary(events),
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
    from .leg_timeline import build_leg_timeline, timeline_summary

    events = build_leg_timeline(leg)
    context = {
        "leg": leg,
        "reservation": leg.reservation,
        "events": events,
        "summary": timeline_summary(events),
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

            # Process leg forms. The template renders one tab per existing leg, so
            # process EVERY leg (not just the first two) and never report success when
            # a submitted leg failed validation — a silent "updated successfully" while
            # an edit is dropped sends a driver to the old address/time.
            leg_errors = []
            leg_count = reservation.legs.count()
            for i in range(1, leg_count + 1):
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

                if not has_data:
                    continue

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
                else:
                    # Surface the specific failure instead of silently dropping the edit.
                    for field, errs in leg_form.errors.items():
                        field_label = "form" if field == "__all__" else field
                        leg_errors.append(
                            f"Leg {i} — {field_label}: {'; '.join(errs)}"
                        )

            if leg_errors:
                for err in leg_errors:
                    messages.error(request, err)
                messages.error(
                    request,
                    "Those leg changes were NOT saved. Fix the errors above and "
                    "resubmit (the reservation and customer details were saved).",
                )
                # Re-bind leg forms with the submitted data and fall through to the
                # re-render below so nothing the dispatcher typed is lost.
                leg_forms = [
                    LegForm(request.POST, instance=leg, prefix=f"leg_{i + 1}")
                    for i, leg in enumerate(reservation.legs.all())
                ]
                if not leg_forms:
                    leg_forms.append(LegForm(request.POST, prefix="leg_1"))
            else:
                messages.success(
                    request,
                    f"Reservation {updated_reservation.uuid} updated successfully.",
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

    # Apply vehicle filter (check leg-level override first, fall back to reservation)
    if vehicle_filter:
        from django.db.models.functions import Coalesce
        legs_query = legs_query.filter(
            **{
                'pk__in': legs_query.annotate(
                    _eff_vtype=Coalesce('vehicle__vehicle_type', 'reservation__vehicle__vehicle_type')
                ).filter(_eff_vtype=vehicle_filter).values('pk')
            }
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

    # get_trip_type() inspects the reservation + leg position and is otherwise
    # recomputed for every leg in each of the three loops below (DISP-05). Cache
    # the result once per leg instance and reuse it across all passes.
    def _leg_trip_type(leg):
        if not hasattr(leg, "_cached_trip_type"):
            leg._cached_trip_type = leg.get_trip_type()
        return leg._cached_trip_type

    # Apply trip type filter if specified (filter in Python since it's a computed property)
    if trip_type_filter:
        filtered_legs = []
        for leg in page_obj:
            if _leg_trip_type(leg) == trip_type_filter:
                filtered_legs.append(leg)
        page_obj.object_list = filtered_legs
        page_obj._object_list = filtered_legs

    # Calculate statistics using utils - reuse the already fetched data
    vehicle_stats = calculate_vehicle_statistics(page_obj)
    
    # Calculate trip type statistics
    trip_type_stats = {"arrival": 0, "return": 0, "cruise": 0, "other": 0}
    for leg in page_obj:
        trip_type = _leg_trip_type(leg)
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
        trip_type = _leg_trip_type(leg)
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
            # All routing (held day -> draft overlay; otherwise live; emergency
            # live_override -> live + overlay mirror) lives in set_leg_driver —
            # the single front door in dispatching/assignment.py.
            live_override = bool(data.get("live_override"))
            if value:
                try:
                    driver = Driver.objects.get(id=value)
                except Driver.DoesNotExist:
                    logger.warning(f"Driver with ID {value} not found")
                    return JsonResponse(
                        {"success": False, "error": "Driver not found"}, status=404
                    )
                try:
                    mode, draft = set_leg_driver(
                        leg, driver, request.user,
                        live_override=live_override, source="manual_assign",
                    )
                    if mode == "staged":
                        logger.info(
                            f"Staged leg {leg_id} -> driver {driver.id} in draft {draft.id} by {request.user.username}"
                        )
                    else:
                        cache.delete(f"capacity_planner_{leg.pickup_date.isoformat()}")
                        logger.info(
                            f"Updated leg {leg_id} with driver {driver.profile.username if hasattr(driver, 'profile') else driver.id} by {request.user.username}"
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
                mode, draft = set_leg_driver(
                    leg, None, request.user,
                    live_override=live_override, source="manual_unassign",
                )
                if mode == "staged":
                    logger.info(f"Staged leg {leg_id} unassign in draft {draft.id} by {request.user.username}")
                else:
                    logger.info(f"Removed driver from leg {leg_id} by {request.user.username}")
                    cache.delete(f"capacity_planner_{leg.pickup_date.isoformat()}")
            use_overlay = (mode == "staged")
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
        if field == "driver":
            # Tell the caller whether this edit was staged in a draft (held day,
            # granted user) vs written live, so the UI can badge it accordingly.
            response_data["held"] = use_overlay
        return JsonResponse(response_data)

    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return JsonResponse(
            {"success": False, "error": f"Server error: {str(e)}"}, status=500
        )


# Lane geometry, shared by the packer and the CSS that renders the lanes.
_UNASSIGNED_LANE_H = 18   # px per stacked chip in the unassigned backlog
_UNASSIGNED_LANE_GAP = 2
_DRIVER_LANE_H = 30       # px per stacked bar in a driver row (matches .timeline-slot)
_DRIVER_LANE_GAP = 2

# Timeline pill geometry (schedule board). A pill is drawn from REALITY wherever
# reality is known and from the estimate only for what hasn't happened yet.
_SLOT_FLOOR_MIN = 15      # a pill always spans at least this many min (pixel/hover visibility)
_LATE_START_MIN = 10      # a departure picked up >= this many min after its scheduled
                          # pickup is flagged as a late start (earliest downstream-risk signal)
_PICKUP_OVERDUE_MIN = 3   # the pickup time has passed by >= this many min with NO pickup
                          # recorded -> flag "pickup overdue" (a still-open job at risk)
# ...but stop shouting once it's clearly just an unpressed status button. The GPS
# engine has always capped its own past-pickup badge at 45 min ("a pickup overdue by
# hours that nobody marked done is noise, not a live-ETA signal"); the clock flags
# never did, which is what produced the 70m/108m-late pills that crowded out real
# reds. Same judgement, now applied on both sides (pickup_policy.OVERDUE_STALE_MIN).
_PICKUP_OVERDUE_STALE_MIN = pickup_policy.OVERDUE_STALE_MIN
# Statuses that mean the driver is actively working toward this pickup. Their ABSENCE
# once the pickup is overdue is the difference between "late but en route" and "stalled".
_MOVING_STATUSES = ('on-the-way', 'on-location')
# Samsara-sweep ETA targets that represent a PICKUP deadline (so the will-he-make-it
# band is worth surfacing). 'dropoff' has no deadline; a driver mid-trip isn't at risk.
_GPS_PICKUP_TARGETS = ('pickup', 'next_pickup')


def _gap_turn_slack(prev_slot, next_slot, target_date, prev_leg=None,
                    prev_picked_up_dt=None):
    """Thin delegate — promoted to ``board_validation.turn_slack_minutes`` so the
    Recovery Advisor runs the exact formula the board renders (full docstring
    there). Kept under this name for the board's call sites and existing tests."""
    from dispatching.board_validation import turn_slack_minutes
    return turn_slack_minutes(prev_slot, next_slot, target_date,
                              prev_leg=prev_leg,
                              prev_picked_up_dt=prev_picked_up_dt)


def _annotate_vehicle_status(vehicles, on_date):
    """Stamp each unit with its out-of-service label and permit rows for a date.

    Every surface that renders a vehicle pool goes through here — the legs
    dashboard, the planner, and anything added later. A car marked down has to
    look the same wherever it appears; the bug this exists to prevent is exactly
    the one that shipped first, where the planner greyed #001 out and the legs
    dashboard drew it as a perfectly normal card.

    Resolved per DATE, not "now": these pages are date-scoped, and a unit in the
    shop this week is a normal unit on next week's board.
    """
    for vehicle in vehicles:
        vehicle.oos_label = vehicle.out_of_service_label(on_date)
        vehicle.permit_rows = vehicle.permits(day=on_date)
    return vehicles


def _pack_lanes(slots, *, lane_height, gap, top_pad=2):
    """Greedy interval packing: give every slot a `lane` + `lane_top` so overlapping
    jobs stack vertically instead of painting over each other. Returns the lane count.

    Accepts dicts (unassigned chips) or ScheduleSlot objects (driver bars).

    CRITICAL: greedy packing is only near-optimal when the input is processed in
    START order. The unassigned lane used to pack in its *display* order (vehicle
    class first, start second), which left every lane cursor parked at a late
    position after each vehicle group — so the next group's early-morning job could
    not reuse any lane and appended a new one. Lane count became roughly the SUM of
    each vehicle group's peak concurrency instead of the day's actual peak: an 85-leg
    board packed to 17 lanes (344px) where the true answer is ~8, and the CSS then
    clipped the overflow out of sight. We pack start-sorted here and let the caller
    re-sort for display afterwards; lane assignments are per-slot so they survive it.
    """
    def _get(s, key, default=0.0):
        return s.get(key, default) if isinstance(s, dict) else getattr(s, key, default)

    def _set(s, key, value):
        if isinstance(s, dict):
            s[key] = value
        else:
            setattr(s, key, value)

    lane_ends = []  # right-edge % of the last slot placed in each lane
    for s in sorted(slots, key=lambda s: _get(s, 'position_pct')):
        left = _get(s, 'position_pct')
        right = left + _get(s, 'width_pct')
        placed = False
        for i, lane_end in enumerate(lane_ends):
            if left >= lane_end:
                _set(s, 'lane', i)
                lane_ends[i] = right
                placed = True
                break
        if not placed:
            _set(s, 'lane', len(lane_ends))
            lane_ends.append(right)
        _set(s, 'lane_top', _get(s, 'lane') * (lane_height + gap) + top_pad)
    return max(len(lane_ends), 1)


def _slot_notes(leg):
    """Free-text notes worth surfacing on a board hover, newest-concern first.

    Four separate fields exist and they mean genuinely different things, so they are
    kept distinct rather than concatenated:
      * leg private_notes   — dispatcher's own note about THIS leg (internal)
      * reservation private_notes — internal note spanning the whole booking
      * reservation special_requests — what the GUEST asked for (customer-facing)
      * leg driver_notes    — what the driver wrote back about the trip

    Returns '' for anything absent so the popup can omit the row entirely.
    """
    _EMPTY = {'note_leg': '', 'note_res': '', 'note_guest': '',
              'note_driver': '', 'note_stops': ''}
    if leg is None:
        return dict(_EMPTY)
    res = leg.reservation

    def _clean(v):
        return (v or '').strip()

    # Per-stop instructions. legstop_set is already prefetched by the board query,
    # so read the cache rather than re-querying per slot.
    stops = []
    try:
        for s in leg.legstop_set.all():
            n = _clean(s.notes)
            if n:
                loc = _clean(getattr(s, 'display_location', '')) or _clean(
                    getattr(s, 'location', ''))
                stops.append(f"{loc}: {n}" if loc else n)
    except Exception:
        pass

    out = {
        'note_leg': _clean(leg.private_notes),
        'note_res': _clean(getattr(res, 'private_notes', '')) if res else '',
        'note_guest': _clean(getattr(res, 'special_requests', '')) if res else '',
        'note_driver': _clean(leg.driver_notes),
        'note_stops': ' · '.join(stops),
    }
    # Drives a folded-corner marker on the bar so a job carrying instructions is
    # identifiable WITHOUT hovering it — otherwise notes are invisible until you
    # happen to point at the right 40px bar.
    out['has_notes'] = any(out.values())
    return out


def _slot_keoi(leg):
    """KEOI ('Keep Eye On It') fields for a board slot/chip.

    Kept separate from _slot_notes so the KEOI watch marker stays distinct from
    the folded-corner note marker. Returns empty strings when the leg carries no
    active flag. Reads leg.active_keoi (prefetched by the board query — no N+1).
    """
    _EMPTY = {'keoi_category': '', 'keoi_category_label': '',
              'keoi_status_label': '', 'keoi_desc': ''}
    if leg is None:
        return dict(_EMPTY)
    k = leg.active_keoi
    if not k:
        return dict(_EMPTY)
    return {
        'keoi_category': k.category,
        'keoi_category_label': k.get_category_display(),
        'keoi_status_label': k.get_operational_status_display(),
        'keoi_desc': k.description,
    }


def _slot_duration_minutes(day, pickup_time, end_dt, *, floor=15):
    """Minutes a timeline slot should span, measured as a real elapsed duration.

    `end_dt` is a datetime from ``estimate_job_end_time`` (pickup + drive/dwell), so a
    late job legitimately rolls past midnight. Deriving the end offset from ``end_dt.hour``
    discards that date: a 11:30 PM pickup clearing 12:45 AM reads hour 0, giving a NEGATIVE
    offset that floors to the 15-minute minimum — every night-crew job drew as a stub
    instead of a 75-minute bar. Subtracting the datetimes keeps the rollover intact.
    """
    if end_dt is None:
        return floor
    pickup_dt = datetime.combine(day, pickup_time)
    end_naive = end_dt
    if timezone.is_aware(end_naive):
        end_naive = timezone.make_naive(end_naive)
    return max(int((end_naive - pickup_dt).total_seconds() // 60), floor)


def _truthful_pill_span(*, sched_start_dt, est_end_dt, status, trip_type,
                        picked_up_dt, completed_dt, now_dt, is_today,
                        late_min=_LATE_START_MIN, overdue_min=_PICKUP_OVERDUE_MIN,
                        expected_pickup_dt=None, is_flight_gated=None,
                        stale_min=_PICKUP_OVERDUE_STALE_MIN):
    """Resolve a timeline pill's effective start/end + risk flags from REALITY.

    The board draws a pill from the plan (scheduled pickup -> estimated clear) until
    reality is available, then lets reality bend the geometry and raise flags:

      * completed          -> clamp the RIGHT edge to the ACTUAL cleared time
                              (shrinks if it cleared early, grows if it ran late);
      * still open & past its estimate on TODAY's board
                           -> extend the RIGHT edge to NOW so a pill never ends in
                              the past while the driver is still on the leg (overrun);
      * departure picked up late (>= ``late_min`` after schedule)
                           -> shift the LEFT edge to the ACTUAL pickup. Arrivals are
                              flight-gated, so a "late" pickup there is the flight, not
                              the driver, and is never flagged;
      * PICKUP OVERDUE: the EXPECTED pickup time has passed by >= ``overdue_min``
                        and NO pickup is recorded -> flag it. This catches the ABSENCE
                        of progress, which no status badge can show. "Stalled" (the
                        loud one) means the driver isn't even reporting movement yet.
                        Expires after ``stale_min``: past that nobody has acted on it,
                        so it's an unpressed button, not a live risk.

    ``expected_pickup_dt`` is when the job should have STARTED — guest in the car —
    which is not the booked slot for a flight-tracked arrival: there it is gate
    arrival + the real airport dwell (~45 min of deplaning, walking and bags), via
    dispatching/pickup_policy.pickup_expected_dt. Two things this fixes: a 90-minute
    flight delay used to paint a critical "90m late" while the plane was still
    airborne, and a perfectly on-schedule airport pickup used to read "35 min
    overdue" the moment it passed the driver's own 10-minute meet deadline.
    Note this is deliberately NOT pickup_deadline() — that answers "must the driver
    be there yet", which is the live-ETA question, not this one.
    Defaults to ``sched_start_dt`` so existing callers and tests are unchanged.

    ``is_flight_gated`` says the pickup waits on a plane, so a "late" pickup is the
    flight's doing and never the driver's. Defaults to ``trip_type == 'arrival'``, but
    callers should pass ``leg.is_flight_tracked_arrival()`` — that also covers the
    airport->cruise-port transfer, whose trip_type reads 'cruise' while its pickup is
    every bit as flight-gated as a plain arrival.

    All datetimes are naive local, matching ``datetime.combine(day, pickup_time)``.
    Returns a dict; callers turn ``eff_start``/``eff_end`` into on-screen percentages.
    This is a pure function (no DB, no wall clock) so the rules can be unit-tested.
    """
    if expected_pickup_dt is None:
        expected_pickup_dt = sched_start_dt
    elif timezone.is_aware(expected_pickup_dt):
        expected_pickup_dt = timezone.make_naive(expected_pickup_dt)
    if is_flight_gated is None:
        is_flight_gated = (trip_type == 'arrival')
    if est_end_dt is not None and timezone.is_aware(est_end_dt):
        est_end_dt = timezone.make_naive(est_end_dt)

    out = {
        'eff_start': sched_start_dt,
        'eff_end': est_end_dt,
        'est_end': est_end_dt,
        'late_start': False,
        'late_start_mins': 0,
        'actual_pickup_dt': None,
        'overrunning': False,
        'overrun_mins': 0,
        'cleared_is_actual': False,
        'pickup_overdue': False,
        'pickup_overdue_mins': 0,
        'pickup_stalled': False,
    }

    # Has the passenger actually been collected? Trust the recorded pickup, and also
    # the 'picked-up' status even if its timestamp wasn't captured.
    _picked = (picked_up_dt is not None) or (status == 'picked-up')
    _active = status not in ('completed', 'cancelled')

    # LEFT edge — late-start for departures only (never for a flight-gated pickup).
    if not is_flight_gated and picked_up_dt is not None:
        _late = (picked_up_dt - sched_start_dt).total_seconds() / 60.0
        if _late >= late_min:
            out['late_start'] = True
            out['late_start_mins'] = int(round(_late))
            out['actual_pickup_dt'] = picked_up_dt
            out['eff_start'] = picked_up_dt

    # START risk — the pickup should have happened by now and nothing is recorded.
    # Earliest sign a job is slipping, and the one thing a status badge can't show:
    # its own absence. Measured against expected_pickup_dt, so a delayed flight moves
    # the bar out rather than reporting a driver late for a plane that hasn't landed,
    # and an airport pickup isn't called overdue while the guest is still deplaning.
    _never_started = False
    if (is_today and _active and not _picked
            and now_dt > expected_pickup_dt + timedelta(minutes=overdue_min)):
        _never_started = status not in _MOVING_STATUSES
        _overdue_mins = int(round((now_dt - expected_pickup_dt).total_seconds() / 60.0))
        # Expire the FLAG once it ages out: past stale_min nobody has acted on it, so
        # it's an unpressed status button, not a live risk, and leaving it up buries
        # the flags that still mean something. The geometry below still knows the job
        # never started (_never_started), so an aged-out leg doesn't suddenly start
        # drawing as a long busy bar.
        if _overdue_mins <= stale_min:
            out['pickup_overdue'] = True
            out['pickup_overdue_mins'] = _overdue_mins
            # Stalled = not even an en-route / on-location report. That's the red one; a
            # driver who IS moving but a little past pickup is only amber.
            out['pickup_stalled'] = _never_started

    # RIGHT edge — actual clear once complete; extend to now while overrunning. A job the
    # driver never started isn't "running long" — don't draw it as a busy bar; the
    # pickup-overdue flag (while it's still live) carries that case.
    if status == 'completed' and completed_dt is not None:
        out['eff_end'] = completed_dt
        out['cleared_is_actual'] = True
    elif (is_today and _active and not _never_started
          and est_end_dt is not None and now_dt > est_end_dt):
        out['eff_end'] = now_dt
        out['overrunning'] = True
        out['overrun_mins'] = int(round((now_dt - est_end_dt).total_seconds() / 60.0))

    # A very-late pickup can land after the (stale) estimate — keep the span
    # non-negative so the pct math and the visibility floor stay well-defined.
    if out['eff_end'] is None or out['eff_end'] < out['eff_start']:
        out['eff_end'] = out['eff_start']

    return out


def _pickup_risk(*, pickup_overdue, pickup_stalled, overdue_mins,
                 gps_status, gps_eta_mins, gps_reason):
    """Thin delegate — promoted to ``pickup_policy.pickup_risk`` (the GPS-over-clock
    precedence ladder; full docstring there) so the Recovery Advisor and the board
    read the identical fold. Kept under this name for existing call sites/tests."""
    return pickup_policy.pickup_risk(
        pickup_overdue=pickup_overdue, pickup_stalled=pickup_stalled,
        overdue_mins=overdue_mins, gps_status=gps_status,
        gps_eta_mins=gps_eta_mins, gps_reason=gps_reason)


def _affiliate_feasibility(leg, driver, target_date):
    """Feasibility for an AFFILIATE drop target (the affiliate board).

    Affiliates are contractors, not shifts. We hold no weekly schedule for them and
    assign them no fleet vehicle, so the in-house checks would either invent limits
    (the weekly fallback paints a default 'shift') or fail every row (no
    DriverVehicleAssignment => 'no vehicle assigned today'). What actually gates a
    farm-out is the AffiliateProfile capability/permit config plus a real rate card —
    the same gates the Farm-Out Optimizer applies in ``_gate_affiliate`` — so this
    mirrors those rather than the driver-shift model.

    Capacity is mode-dependent, and that distinction is the whole point:
      * single_chain (or no profile) — ONE vehicle end to end, so ordinary
        turnaround/overlap detection is exactly right.
      * count_cap / fleet — N seats a day across parallel vehicles, so overlapping
        jobs are NORMAL. Running the chain check here would flag phantom conflicts on
        every second job; only the daily count is a real limit.
    """
    from dispatching.scheduler import (
        build_driver_schedules, check_feasibility, preload_timing_cache,
        estimate_job_end_time, get_vehicle_tier,
    )
    from dispatching.farmout_optimizer import (
        ANTHONY_MAX_LEGS_PER_DAY, is_port_or_sanford,
    )
    from dispatching.models import SchedulerSettings
    from drivers.models import AffiliateProfile, DriverPayRate

    prof = AffiliateProfile.objects.filter(driver=driver).first()
    warnings = []
    hard_blocks = []
    required_type = str(leg.effective_vehicle_type or "")

    # 1. CAPABILITY — max vehicle class on file. Blank tier = no cap (the rate card
    #    alone gates), matching the optimizer's reading of a profile-less affiliate.
    if prof and prof.max_vehicle_tier and required_type:
        ptier = get_vehicle_tier(prof.max_vehicle_tier)
        ltier = get_vehicle_tier(required_type)
        if ltier == -1 or ptier == -1 or ltier > ptier:
            hard_blocks.append(
                f"Tops out at {prof.get_max_vehicle_tier_display()} — this job needs {required_type}"
            )

    # 2. PERMIT — drop-off-only affiliates never originate at Port Canaveral / Sanford.
    if prof and prof.no_pickup_at_port_sanford and is_port_or_sanford(leg.pickup_location):
        hard_blocks.append("No pickup permit at Port Canaveral / Sanford")

    # 3. RATE CARD — soft. Assigning still works; driver pay just won't auto-fill,
    #    so warn loudly rather than block a dispatcher who knows the price.
    if not DriverPayRate.objects.filter(driver=driver).exists():
        warnings.append("No rate card on file — driver pay won't auto-fill")

    preload_timing_cache()
    # Their REAL day, counted exactly the way farmout_actions._resolve_affiliate counts it
    # (all non-cancelled legs, NOT the in-house "still active" status filter). A completed
    # leg consumed a seat, so it must still count against a daily cap — and this keeps the
    # drag-time check, the row badge, and the Farm-Out apply path from ever disagreeing.
    existing_legs = list(
        Leg.objects.select_related(
            "reservation", "flight_information", "cruise_information"
        ).filter(driver=driver, pickup_date=target_date)
        .exclude(status="cancelled").exclude(reservation__status="cancelled")
        .exclude(id=leg.id).order_by("pickup_time")
    )
    end_time = estimate_job_end_time(leg, target_date)
    mode = prof.capacity_mode if prof else AffiliateProfile.CAP_SINGLE_CHAIN
    parallel = mode in (AffiliateProfile.CAP_COUNT, AffiliateProfile.CAP_FLEET)

    if parallel:
        # Count-based: overlap is expected, the daily cap is the only real ceiling.
        used = len(existing_legs)
        cap = prof.daily_cap if (prof and prof.daily_cap is not None) else (
            ANTHONY_MAX_LEGS_PER_DAY if mode == AffiliateProfile.CAP_COUNT else None)
        if cap:
            if used >= cap:
                hard_blocks.append(f"At daily cap ({used}/{cap} legs)")
            elif used + 1 == cap:
                warnings.append(f"This fills their last seat ({used + 1}/{cap})")
            reason = f"{used}/{cap} legs used today"
        else:
            reason = f"{used} leg{'' if used == 1 else 's'} today — no cap on file"
        buffer_minutes = 999
        existing_trips = used
    else:
        # Single vehicle: real chain feasibility, same as an in-house driver.
        schedules = build_driver_schedules(existing_legs, [driver], target_date)
        driver_schedule = schedules.get(driver.id)
        if not driver_schedule or not driver_schedule.slots:
            reason = "No other trips — fully available"
            buffer_minutes = 999
            existing_trips = 0
        else:
            cfg = SchedulerSettings.get_settings()
            result = check_feasibility(
                driver_schedule, leg, target_date, arrival_grace=cfg.arrival_grace_minutes
            )
            reason = result.reason
            buffer_minutes = result.buffer_minutes
            existing_trips = len(driver_schedule.slots)
            if result.warnings:
                warnings.extend(result.warnings)
            if not result.feasible:
                hard_blocks.append(result.reason or "Conflicts with their existing chain")

    feasible = not hard_blocks
    return JsonResponse({
        "feasible": feasible,
        "buffer_minutes": buffer_minutes,
        "warnings": hard_blocks + warnings,
        "reason": hard_blocks[0] if hard_blocks else reason,
        "estimated_end": end_time.strftime("%I:%M %p").lstrip("0") if end_time else None,
        "existing_trips": existing_trips,
        # No fleet vehicle is ever assigned to an affiliate, so the in-house
        # vehicle-match concept doesn't apply; capability is checked above instead.
        "vehicle_match": True,
        "vehicle_mismatch_detail": "",
        "avail_status": "available",
        "is_affiliate": True,
        "capacity_mode": mode,
    })


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
        from dispatching import feasibility_guards as fg
        from drivers.models import Driver

        leg = Leg.objects.select_related(
            "reservation", "reservation__vehicle", "vehicle", "flight_information", "cruise_information"
        ).get(id=leg_id)
        driver = Driver.objects.get(id=driver_id)
        target_date = leg.pickup_date

        if driver.driver_type == "affiliate":
            return _affiliate_feasibility(leg, driver, target_date)

        # Check driver availability for this date (Off / partial-day / window)
        from drivers.availability import is_pickup_within_window
        eff = driver.get_effective_availability(target_date)
        availability_warnings = []
        availability_blocks = False
        if not eff["is_available"]:
            availability_blocks = True
            reason_pretty = (eff.get("exception_reason") or "").replace("_", " ").title()
            msg = f"Driver is OFF on {target_date.strftime('%b %d')}"
            if reason_pretty:
                msg += f" ({reason_pretty})"
            if eff.get("exception_notes"):
                msg += f" — {eff['exception_notes']}"
            availability_warnings.append(msg)
        else:
            ok, reason = is_pickup_within_window(eff, leg.pickup_time, dropoff_dt=None)
            if not ok:
                availability_warnings.append(reason)
                if eff.get("exception_notes"):
                    availability_warnings.append(f"Note: {eff['exception_notes']}")
            elif eff.get("exception_notes"):
                availability_warnings.append(f"Driver note: {eff['exception_notes']}")

        # The driver's unit for the date. Fetched once: both the vehicle-type
        # check and the pickup-permit check below need it.
        day_vehicle = None
        if driver.driver_type == "inhouse":
            _dva = (
                DriverVehicleAssignment.objects
                .select_related("vehicle", "vehicle__vehicle_type")
                .filter(driver=driver, date=target_date)
                .first()
            )
            day_vehicle = _dva.vehicle if _dva else None

        # Check vehicle type match
        vehicle_match = True
        vehicle_mismatch_detail = ""
        required_type = leg.effective_vehicle_type
        if required_type and driver.driver_type == "inhouse":
            from dispatching.scheduler import get_compatible_vehicle_types
            if day_vehicle is not None and day_vehicle.vehicle_type:
                assigned_type = day_vehicle.vehicle_type.vehicle_type
                compatible_types = get_compatible_vehicle_types(assigned_type)
                if str(required_type) not in compatible_types:
                    vehicle_match = False
                    vehicle_mismatch_detail = f"Driver's vehicle is {assigned_type}, reservation requires {required_type}"
            else:
                vehicle_match = False
                vehicle_mismatch_detail = "Driver has no vehicle assigned today"

        # Pickup permit — ADVISORY, never a block.
        # Central Florida permits the VEHICLE, not the company: MCO, Sanford and
        # Port Canaveral each need their own decal to PICK UP there. Dropping is
        # unrestricted, so only the pickup end is checked. This warns rather than
        # gates by explicit decision — pickup locations are free text matched by
        # categorize_location(), and MCO is most of the business, so a hard block
        # would misfire on the busiest lane. The dispatcher gets the unit number,
        # the permit and the reason, and makes the call.
        permit_warning = ""
        if day_vehicle is not None:
            _missing = day_vehicle.missing_permit_for_pickup(
                leg.pickup_location, day=target_date)
            if _missing:
                _unit = f"#{day_vehicle.vehicle_number}"
                if _missing["expired"]:
                    permit_warning = (
                        f"{_unit}'s {_missing['label']} permit expired "
                        f"{_missing['expires_on']} — this trip collects at "
                        f"{_missing['label']}.")
                else:
                    permit_warning = (
                        f"{_unit} has no {_missing['label']} pickup permit — "
                        f"this trip collects at {_missing['label']}.")

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
            # No existing schedule — always feasible (modulo availability)
            end_time = estimate_job_end_time(leg, target_date)
            warnings = list(availability_warnings)
            if not vehicle_match and vehicle_mismatch_detail:
                warnings.append(vehicle_mismatch_detail)
            if permit_warning:
                warnings.append(permit_warning)
            feasible = not availability_blocks
            reason = "Driver is off this date" if availability_blocks else "No other trips — fully available"
            return JsonResponse({
                "feasible": feasible,
                "buffer_minutes": 999,
                "warnings": warnings,
                "reason": reason,
                "estimated_end": end_time.strftime("%I:%M %p").lstrip("0") if end_time else None,
                "existing_trips": 0,
                "vehicle_match": vehicle_match,
                "vehicle_mismatch_detail": vehicle_mismatch_detail,
                "permit_warning": permit_warning,
                "avail_status": eff["status"],
            })

        # Guard C (duty-span cap / clear-by / night rule) used to be skipped entirely on
        # this path: no driver_window meant the manual dropdown and drag-and-drop were
        # checked more loosely than auto-assign, so the board would happily accept an
        # assignment the engine would have refused. enforce_cap=False keeps the founder
        # rule "flag but do it" — a dispatcher's deliberate long day is surfaced as a
        # warning, never hard-blocked.
        #
        # The arrival grace also comes from pickup_policy now, not
        # SchedulerSettings.arrival_grace_minutes (15) — that field is the PASSENGER-ready
        # time, and using it here quietly judged manual assignments against a looser
        # deadline than the 10-minute meet rule auto-assign enforces.
        _mw_eff = driver.get_effective_availability(target_date)
        _mw_max = _mw_eff.get("max_hours")
        manual_window = fg.get_effective_window(
            driver.id,
            configured={"start": _mw_eff.get("start_hour"), "end": _mw_eff.get("end_hour"),
                        "max_hours": (float(_mw_max) if _mw_max else None),
                        "flexible": bool(_mw_eff.get("flexible"))},
            enforce_cap=False,
        )
        result = check_feasibility(
            driver_schedule, leg, target_date,
            arrival_grace=pickup_policy.ARRIVAL_MEET_GRACE_MIN,
            driver_window=manual_window,
        )
        end_time = estimate_job_end_time(leg, target_date)

        warnings = list(result.warnings) if result.warnings else []
        warnings = availability_warnings + warnings
        if not vehicle_match and vehicle_mismatch_detail:
            warnings.append(vehicle_mismatch_detail)
        if permit_warning:
            warnings.append(permit_warning)

        feasible = result.feasible and not availability_blocks
        reason = "Driver is off this date" if availability_blocks else result.reason

        return JsonResponse({
            "feasible": feasible,
            "buffer_minutes": result.buffer_minutes,
            "warnings": warnings,
            "reason": reason,
            "estimated_end": end_time.strftime("%I:%M %p").lstrip("0") if end_time else None,
            "existing_trips": len(driver_schedule.slots),
            "vehicle_match": vehicle_match,
            "vehicle_mismatch_detail": vehicle_mismatch_detail,
            "permit_warning": permit_warning,
            "avail_status": eff["status"],
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
        cache.delete(f"capacity_planner_{assignment_date.isoformat()}")
        return JsonResponse({"success": True, "cleared": True})

    try:
        vehicle = FleetVehicle.objects.select_related("vehicle_type").get(id=vehicle_id)
    except FleetVehicle.DoesNotExist:
        return JsonResponse(
            {"success": False, "error": "Vehicle not found"}, status=404
        )

    # Out of service: refuse, but let a human overrule.
    #
    # This is the ONE per-vehicle state allowed to block an assignment, and it is
    # allowed precisely because a person set it by hand — unlike Guard A, which
    # was pulled for inferring blocks from stale telematics
    # (docs/fleet-management.md, feasibility_guards.py:140-144).
    # The override exists so a wrong or forgotten flag can never strand a car
    # that came back early: the dispatcher is told what's on record, and decides.
    if vehicle.is_out_of_service_on(assignment_date) and not data.get("override_oos"):
        return JsonResponse(
            {
                "success": False,
                "error": (f"#{vehicle.vehicle_number} is out of service — "
                          f"{vehicle.out_of_service_label(assignment_date)}."),
                "out_of_service": True,
                "can_override": True,
                "vehicle_number": vehicle.vehicle_number,
                "reason": vehicle.out_of_service_label(assignment_date),
            },
            status=409,
        )

    # Hard block: a vehicle type requiring certification (e.g. the Sprinter / 14-pax)
    # may only be assigned to a driver explicitly cleared for it.
    if not driver.can_drive(vehicle.vehicle_type):
        vtype = vehicle.vehicle_type
        type_label = "Sprinter (14-pax)" if getattr(vtype, "vehicle_type", "") == "Van(14 Pax)" else str(vtype)
        return JsonResponse(
            {"success": False, "error": f"{driver} isn't cleared to drive the {type_label}."},
            status=400,
        )

    assignment, _ = DriverVehicleAssignment.objects.get_or_create(
        driver=driver, date=assignment_date
    )
    assignment.vehicle = vehicle
    assignment.save()
    cache.delete(f"capacity_planner_{assignment_date.isoformat()}")

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
        .prefetch_related("driver__weekly_schedule", "driver__date_overrides")
    )

    # Build driver list with off-day status.
    # IMPORTANT: use get_effective_availability so date_overrides (time-off
    # requests, vacation, sick, etc.) are honored — not just the recurring
    # weekly_schedule. Otherwise drivers who requested today off get checked
    # by default in the copy modal and silently inherit a vehicle.
    drivers_list = []
    for a in prev_assignments:
        eff = a.driver.get_effective_availability(target_date)
        is_off = not eff["is_available"]
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
    skipped_oos = []
    result_map = {}
    for a in prev_assignments:
        if a.driver_id in exclude_ids:
            continue
        # A unit that has gone into the shop since the source date must not be
        # copied forward. Skipped, not refused: the rest of the day's plan is
        # still worth having, and the driver simply lands with no vehicle —
        # which the planner already renders as "needs a vehicle".
        if a.vehicle and a.vehicle.is_out_of_service_on(target_date):
            skipped_oos.append(f"#{a.vehicle.vehicle_number}")
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
        "skipped_out_of_service": skipped_oos,
    })


@login_required
@require_POST
def reset_vehicle_assignments(request):
    """Clear EVERY vehicle assignment for a date — the "start the day over" button.

    Two modes, same as the copy path:
    - preview=true: what WOULD be cleared, so the confirm modal can name it. Each
      driver carries his job count for the day, because that is the one thing a
      reset does NOT touch: clearing the car does not cancel the work. A driver
      with 4 jobs and no vehicle is a real problem on the board, so the modal has
      to say his name before you press the button, not after.
    - preview=false: deletes the rows. Vehicle assignments are a plan for the day,
      not a record of it — the trip history lives on the legs — so a full delete
      is the honest reset. Planned AM/PM share windows go with them, which is
      correct: they describe an assignment that no longer exists.
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

    rows = (
        DriverVehicleAssignment.objects.filter(date=target_date)
        .select_related("driver", "driver__profile", "vehicle")
        .order_by("driver__profile__first_name")
    )

    if data.get("preview", False):
        # Jobs still on each driver's plate for the day. Cancelled legs don't
        # count — they aren't work anyone has to cover.
        leg_counts = dict(
            Leg.objects.filter(pickup_date=target_date, driver__isnull=False)
            .exclude(reservation__status="cancelled")
            .exclude(status="cancelled")
            .values_list("driver_id")
            .annotate(n=Count("id"))
        )
        drivers_list = [
            {
                "driver_id": a.driver_id,
                "driver_name": (
                    (a.driver.profile.first_name or str(a.driver))
                    if a.driver.profile else str(a.driver)
                ),
                "vehicle_number": a.vehicle.vehicle_number if a.vehicle else "",
                "vehicle_type": (
                    str(a.vehicle.vehicle_type)
                    if a.vehicle and a.vehicle.vehicle_type else ""
                ),
                "leg_count": leg_counts.get(a.driver_id, 0),
            }
            for a in rows
        ]
        return JsonResponse({
            "success": True,
            "date": target_date.strftime("%Y-%m-%d"),
            "drivers": drivers_list,
            "total": len(drivers_list),
            "with_jobs": sum(1 for d in drivers_list if d["leg_count"]),
        })

    cleared = rows.count()
    if not cleared:
        return JsonResponse({"success": True, "cleared": 0})
    rows.delete()
    cache.delete(f"capacity_planner_{target_date.isoformat()}")
    return JsonResponse({"success": True, "cleared": cleared})


@login_required
@require_POST
def suggest_day_setup_view(request):
    """Day Setup preview: propose today's roster + vehicle plan. STRICTLY read-only —
    nothing persists until apply_day_setup. See dispatching/day_setup.py."""
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    try:
        data = json.loads(request.body)
        target_date = datetime.strptime(data.get("date", ""), "%Y-%m-%d").date()
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"success": False, "error": "Invalid request"}, status=400)
    from dispatching.day_setup import suggest_day_setup, DAY_SETUP_ENABLED
    if not DAY_SETUP_ENABLED:
        return JsonResponse({"success": False, "error": "Day Setup is disabled"}, status=400)
    # Optional A/B overrides (harness + console use); omitted -> module flags decide.
    _solo = data.get("solo_first")
    _peak = data.get("peak_sizing")
    try:
        _raw_inc = data.get("force_include") or []
        _raw_exc = data.get("force_exclude") or []
        # Reject non-list payloads: iterating a string like "123" would silently
        # yield driver ids [1, 2, 3].
        if isinstance(_raw_inc, str) or isinstance(_raw_exc, str):
            raise ValueError
        _finc = [int(x) for x in _raw_inc]
        _fexc = [int(x) for x in _raw_exc]
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "error": "Bad force_include/exclude ids"},
                            status=400)
    proposal = suggest_day_setup(target_date,
                                 solo_first=(None if _solo is None else bool(_solo)),
                                 peak_sizing=(None if _peak is None else bool(_peak)),
                                 force_include=_finc, force_exclude=_fexc)
    proposal["success"] = True
    return JsonResponse(proposal)


@login_required
@require_POST
def apply_day_setup(request):
    """Create the accepted Day Setup vehicle assignments — ONE atomic, validated write.

    Payload: {date, pairs: [{driver_id, vehicle_id}], snapshot: {driver_id: vehicle_id|null}}
    - cert hard-block per pair (server-side, same rule as update_inhouse_vehicle_assignment);
    - two pairs naming the same unit -> 400 before any write;
    - a payload unit already held by a driver OUTSIDE the payload -> 400 naming the holder.
      Scoped to payload-touched vehicles ONLY: the founder's pre-existing hand-built AM/PM
      shares (~24% of dates) must never make an unrelated Apply fail;
    - snapshot drift (a row changed between preview and Apply) -> 409 naming the row;
    - idempotent (re-applying the same plan is a no-op); never deletes rows.
    """
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    try:
        data = json.loads(request.body)
        target_date = datetime.strptime(data.get("date", ""), "%Y-%m-%d").date()
        pairs = data.get("pairs", [])
        snapshot = data.get("snapshot", {}) or {}
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"success": False, "error": "Invalid request"}, status=400)
    if not pairs:
        return JsonResponse({"success": False, "error": "Nothing to apply"}, status=400)

    from django.db import transaction
    from dispatching.day_setup import _is_excluded

    try:
        # allow_share: a DELIBERATE two-drivers-one-car pair — either an advisor freed-unit
        # accept or a Day Setup planned AM/PM share (both pairs in the same payload, with
        # partitioned planned windows). planned_start/end_hour: the planned working window
        # persisted on the row so the auto-assign modal prefills the split as a HARD window.
        def _hour(p, key):
            v = p.get(key)
            return int(v) if v is not None and str(v) != "" else None
        clean = [(int(p["driver_id"]), int(p["vehicle_id"]), bool(p.get("allow_share")),
                  _hour(p, "planned_start_hour"), _hour(p, "planned_end_hour"))
                 for p in pairs]
    except (KeyError, TypeError, ValueError):
        return JsonResponse({"success": False, "error": "Malformed pairs"}, status=400)

    vehicle_ids = [vid for _, vid, _, _, _ in clean]
    _by_vid = {}
    for did, vid, share, _ps, _pe in clean:
        _by_vid.setdefault(vid, []).append(share)
    for vid, shares in _by_vid.items():
        # The same unit twice in one payload is legal ONLY as an explicit share (every pair
        # flagged) — an accidental double-pick still fails loudly.
        if len(shares) > 1 and not all(shares):
            return JsonResponse({"success": False,
                                 "error": "Two drivers were given the same vehicle — fix and retry."},
                                status=400)

    # A share row whose PARTNER was unchecked in the modal arrives as the only pair for its
    # unit. Declining a proposed split must mean "one driver keeps the car all day" — so
    # strip the orphaned PARTITIONED WINDOW, otherwise the remaining driver would be capped
    # at the handoff hour for a handoff that no longer exists. The allow_share flag itself
    # SURVIVES for a single pair: a deliberate one-pair share is exactly what a Second-Shift
    # Advisor freed-unit accept sends (the unit's holder keeps his row and is NOT in the
    # payload) — stripping the flag made the holder cross-check below reject every advisor
    # accept. The shared-car occupancy gate, not this flag, is what keeps a real share
    # physically safe; for a declined proposal no outside holder exists, so the surviving
    # flag is inert.
    clean = [(did, vid, share,
              ps if (share and len(_by_vid[vid]) > 1) else None,
              pe if (share and len(_by_vid[vid]) > 1) else None)
             for did, vid, share, ps, pe in clean]

    drivers = {d.id: d for d in Driver.objects.filter(
        id__in=[did for did, _, _, _, _ in clean]).select_related("profile")
        .prefetch_related("certified_vehicle_types")}
    vehicles = {v.id: v for v in FleetVehicle.objects.filter(
        id__in=vehicle_ids).select_related("vehicle_type")}

    for did, vid, _share, _ps, _pe in clean:
        d, v = drivers.get(did), vehicles.get(vid)
        if d is None or v is None:
            return JsonResponse({"success": False, "error": "Unknown driver or vehicle"}, status=400)
        if d.driver_type != "inhouse" or not d.is_active or _is_excluded(d):
            return JsonResponse({"success": False,
                                 "error": f"{d} can't be scheduled (inactive/excluded)."}, status=400)
        if not d.can_drive(v.vehicle_type):
            return JsonResponse({"success": False,
                                 "error": f"{d} isn't cleared to drive #{v.vehicle_number}."},
                                status=400)
        # The suggester never proposes an out-of-service unit, so reaching here
        # means the flag was set between preview and Apply, or the payload was
        # hand-edited. Either way this is a bulk write — refuse the whole batch
        # and send them back to a fresh preview rather than offer an override
        # that would silently apply to every pair in it.
        if v.is_out_of_service_on(target_date):
            return JsonResponse(
                {"success": False,
                 "error": (f"#{v.vehicle_number} is out of service — "
                           f"{v.out_of_service_label(target_date)}. "
                           f"Re-open Suggest Day Setup.")},
                status=409)

    payload_driver_ids = {did for did, _, _, _, _ in clean}
    with transaction.atomic():
        existing = {a.driver_id: a for a in
                    DriverVehicleAssignment.objects.filter(date=target_date)
                    .select_related("driver", "vehicle")}
        # Drift check: any payload driver whose row changed since the preview snapshot.
        for did, _, _share, _ps, _pe in clean:
            snap_vid = snapshot.get(str(did), None)
            cur = existing.get(did)
            cur_vid = cur.vehicle_id if cur else None
            if cur_vid != snap_vid:
                return JsonResponse({"success": False, "stale": True,
                                     "error": f"{drivers[did]}'s vehicle changed since this preview "
                                              f"was opened — re-open Suggest Day Setup."}, status=409)
        # Cross-check, scoped to payload vehicles only: a unit we are assigning must not be
        # held by a driver OUTSIDE the payload (pre-existing shares among untouched rows are
        # the founder's business, never a failure).
        for did, vid, allow_share, _ps, _pe in clean:
            if allow_share:
                continue   # advisor freed-unit share: holder keeps his row, both are real
            for other_id, a in existing.items():
                if other_id not in payload_driver_ids and a.vehicle_id == vid:
                    if not a.driver.is_active or a.driver.driver_type != "inhouse":
                        continue   # stale row held by a deactivated driver — not a real claim
                    return JsonResponse(
                        {"success": False,
                         "error": f"#{vehicles[vid].vehicle_number} is already assigned to "
                                  f"{a.driver} — include or clear them first."}, status=400)
        created = updated = 0
        for did, vid, _share, _ps, _pe in clean:
            obj, was_created = DriverVehicleAssignment.objects.get_or_create(
                driver=drivers[did], date=target_date)
            changed = (obj.vehicle_id != vid or obj.planned_start_hour != _ps
                       or obj.planned_end_hour != _pe)
            if changed:
                obj.vehicle = vehicles[vid]
                obj.planned_start_hour = _ps
                obj.planned_end_hour = _pe
                obj.save()
            if was_created:
                created += 1
            elif changed:
                updated += 1

    cache.delete(f"capacity_planner_{target_date.isoformat()}")
    return JsonResponse({"success": True, "created": created, "updated": updated,
                         "total": len(clean)})


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





def _add_leg_gratuity(leg, portion):
    """Add a gratuity portion to a single leg's driver_gratuity, recompute its
    driver_pay_amount, and record a per-leg note (same '$X.XX Gratuity Included'
    convention as booking-time extra_charges). Setting driver_gratuity keeps the
    Leg.save() equal-split from later overwriting this attribution."""
    portion = Decimal(str(portion)).quantize(Decimal("0.01"))
    leg.driver_gratuity = (leg.driver_gratuity or Decimal("0.00")) + portion
    leg.driver_pay_amount = (
        (leg.driver_base_pay or Decimal("0.00"))
        + leg.driver_gratuity
        + (leg.driver_additional or Decimal("0.00"))
    )
    update_fields = ["driver_gratuity", "driver_pay_amount"]
    if portion > 0:
        note = f"${portion:.2f} Gratuity Included"
        leg.private_notes = f"{leg.private_notes}\n{note}" if leg.private_notes else note
        update_fields.append("private_notes")
    leg.save(update_fields=update_fields)


def _apply_gratuity_to_legs(reservation, amount, target):
    """Attribute a charged customer gratuity to per-leg driver_gratuity so payroll
    pays the right driver. target='whole' splits evenly across legs (rounding
    remainder on the last leg); otherwise `target` is a leg id and the whole tip
    goes to that one leg's driver."""
    legs = list(reservation.legs.all())
    if not legs:
        return
    amount = Decimal(str(amount)).quantize(Decimal("0.01"))

    if target and target != "whole":
        try:
            target_id = int(target)
        except (TypeError, ValueError):
            target_id = None
        chosen = next((l for l in legs if l.id == target_id), None)
        if chosen is not None:
            _add_leg_gratuity(chosen, amount)
            return
        # Unknown leg id → fall through to an even split (never drop the money).

    # Whole reservation → split evenly; put any rounding remainder on the last leg.
    share = (amount / Decimal(len(legs))).quantize(Decimal("0.01"))
    distributed = Decimal("0.00")
    for i, leg in enumerate(legs):
        portion = share if i < len(legs) - 1 else (amount - distributed)
        _add_leg_gratuity(leg, portion)
        distributed += portion


@login_required(login_url="login")
def dispatcher_payment_portal(request, reservation_id):
    """
    A portal for dispatchers to process payments or save cards for reservations.

    Args:
        request: The HTTP request
        reservation_id: The UUID of the reservation

    Returns:
        Rendered form or redirect to Stripe checkout
    """
    # Staff-only: this console exposes saved cards and can fire off-session charges.
    # The reservation UUID is not a secret (it ships in customer payment-reminder
    # emails as /process-payment/<uuid>/), so login + is_staff are the real gate.
    if not request.user.is_staff:
        return redirect("home")

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
        # One-time nonce stamped by the form on render — makes the saved-card charge
        # idempotent so a double-click (likely on the single sync worker's hung page)
        # can't bill the customer twice.
        payment_nonce = request.POST.get("payment_nonce", "")

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
                    amount_decimal = Decimal(amount_str).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
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
                    amount_decimal = Decimal(amount_str).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
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

                    # Idempotency: same form submission (double-click / retry) reuses the
                    # nonce -> Stripe returns the SAME charge instead of billing twice.
                    _idem = (
                        {"idempotency_key": f"portal-{reservation.uuid}-{payment_nonce}"}
                        if payment_nonce
                        else {}
                    )
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
                        **_idem,
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

                        # Charge-type-aware bookkeeping.
                        charge_type = request.POST.get("charge_type", "additional")
                        gratuity_target = request.POST.get("gratuity_target", "whole")

                        # Update reservation status
                        reservation.status = "confirmed"

                        if charge_type == "gratuity":
                            # A tip is always NEW money: record it as gratuity AND
                            # attribute it to the right leg/driver so payroll is correct
                            # (a single-leg tip is NOT split across all legs). This
                            # removes the old two-step "charge then hand-edit gratuity".
                            reservation.gratuity_amount = (
                                reservation.gratuity_amount or Decimal("0.00")
                            ) + final_amount
                            reservation.total_price = (
                                reservation.total_price or Decimal("0.00")
                            ) + final_amount
                            # Reservation-level customer note (same convention as
                            # booking-time extra_charges' special_requests note).
                            _grat_note = f"${final_amount:.2f} Gratuity Included"
                            reservation.special_requests = (
                                f"{reservation.special_requests}\n{_grat_note}"
                                if reservation.special_requests
                                else _grat_note
                            )
                            with transaction.atomic():
                                reservation.save(
                                    update_fields=["status", "total_price", "gratuity_amount", "special_requests"]
                                )
                                payment.save()
                                _apply_gratuity_to_legs(
                                    reservation, final_amount, gratuity_target
                                )
                            logger.info(
                                f"Recorded ${final_amount} gratuity on reservation "
                                f"{reservation.uuid} (target={gratuity_target})"
                            )
                        else:
                            # Additional charge (existing behavior): add to total only
                            # if nothing was owed (else it's a payment toward a balance).
                            should_add_to_total = amount_owed_before <= Decimal("0.01")
                            if should_add_to_total:
                                reservation.total_price += final_amount
                                logger.info(
                                    f"Auto-added ${final_amount} to reservation total (was ${reservation.total_price - final_amount}, "
                                    f"now ${reservation.total_price}) - detected as new charge"
                                )
                            with transaction.atomic():
                                if should_add_to_total:
                                    reservation.save(update_fields=["status", "total_price"])
                                else:
                                    reservation.save(update_fields=["status"])
                                payment.save()

                        # Send confirmation email after successful payment (non-blocking)
                        _run_in_background(send_reservation_confirmation, reservation, sent_by=request.user)
                        logger.info(f"Confirmation email queued for dispatcher payment on reservation {reservation.uuid}")

                        # Send purchase event to Meta in background (matches webhook.py pattern).
                        # Stable event_id (payment-intent id, no timestamp) so a retried charge
                        # or a payment_intent.succeeded webhook for the same intent dedupes.
                        event_id = str(payment_intent.id)
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

        # Create a payment intent. Deterministic idempotency key (reservation + amount):
        # charging the same balance twice within Stripe's idempotency window is always an
        # accidental double-charge for a "pay the balance" action, so Stripe returns the
        # first charge instead of billing again.
        _amount_cents = int(reservation.total_price * 100)
        payment_intent = stripe.PaymentIntent.create(
            amount=_amount_cents,
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
            idempotency_key=f"charge-saved-{reservation.uuid}-{_amount_cents}",
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


def _refresh_one_flight(flight, leg, aeroapi):
    """
    Fetch fresh AeroAPI data for one Flight and persist it. Returns
    {success, error, flight_data, not_found}. `flight` is mutated and saved on success
    OR when the API reports the flight as not_found/not_orlando (we clear stale fields then).
    """
    flight_ident = flight.get_flight_ident()
    if not flight_ident:
        return {"success": False, "error": "Could not determine flight identifier", "flight_data": None, "not_found": False}

    flight_date = leg.pickup_date.strftime('%Y-%m-%d') if leg.pickup_date else None
    trip_type = leg.flight_tracking_trip_type()
    api_data = aeroapi.get_flight_data(flight_ident, flight_date=flight_date, trip_type=trip_type)

    if api_data.get('status') != 'success':
        error_msg = api_data.get('error', 'Unknown error')
        not_found = api_data.get('status') in ('not_found', 'not_orlando')
        if not_found:
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
            logger.info(f"Cleared stale data on flight {flight.id} ({flight_ident} not found)")
        return {"success": False, "error": error_msg, "flight_data": None, "not_found": not_found}

    if api_data.get('flight_iata'):
        flight.flight_iata = api_data.get('flight_iata')
    if api_data.get('origin'):
        flight.origin = api_data.get('origin')
    if api_data.get('destination'):
        flight.destination = api_data.get('destination')
    flight_status = api_data.get('flight_status') or api_data.get('status', '')
    if flight_status:
        flight.status = flight_status

    scheduled_arrival = api_data.get('scheduled_arrival_local')
    flight.scheduled_arrival_local = scheduled_arrival
    scheduled_gate_arrival = api_data.get('scheduled_gate_arrival_local')
    flight.scheduled_gate_arrival_local = scheduled_gate_arrival

    now = timezone.now()
    is_future_flight = (
        (scheduled_arrival and scheduled_arrival > now)
        or (scheduled_gate_arrival and scheduled_gate_arrival > now)
    )

    estimated_arrival = api_data.get('estimated_arrival_local')
    if estimated_arrival is not None:
        flight.estimated_arrival_local = estimated_arrival
    estimated_gate_arrival = api_data.get('estimated_gate_arrival_local')
    if estimated_gate_arrival is not None:
        flight.estimated_gate_arrival_local = estimated_gate_arrival

    if is_future_flight:
        flight.actual_arrival_local = None
        flight.actual_gate_arrival_local = None
    else:
        actual_arrival = api_data.get('actual_runway_arrival_local')
        if actual_arrival is not None:
            flight.actual_arrival_local = actual_arrival
        actual_gate_arrival = api_data.get('actual_gate_arrival_local')
        if actual_gate_arrival is not None:
            flight.actual_gate_arrival_local = actual_gate_arrival

    if api_data.get('terminal'):
        flight.terminal = api_data.get('terminal')
    if api_data.get('gate'):
        flight.gate = api_data.get('gate')
    if api_data.get('baggage_claim'):
        flight.baggage_claim = api_data.get('baggage_claim')

    flight.last_updated = api_data.get('last_updated', timezone.now())

    try:
        flight.save()
    except Exception as e:
        logger.error(f"Error saving flight {flight.id}: {e}")
        return {"success": False, "error": f"Error saving flight data: {str(e)}", "flight_data": None, "not_found": False}

    return {
        "success": True,
        "error": None,
        "not_found": False,
        "flight_data": {
            "flight_id": flight.id,
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
        },
    }


@login_required
@require_POST
def refresh_flight_data(request):
    """
    Refresh AeroAPI data for every flight linked to a leg (the legacy
    `flight_information` FK plus every LegFlight row). Returns the controlling
    flight's data under `flight_data` for backward compatibility, plus
    `all_flight_data` covering every flight refreshed and `multi_flight` so
    the UI can decide whether to live-update one card or reload to refresh all.
    """
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    try:
        data = json.loads(request.body)
        leg_id = data.get("leg_id")
        if not leg_id:
            return JsonResponse({"success": False, "error": "Missing leg ID"}, status=400)

        leg = get_object_or_404(Leg, id=leg_id)

        # Build the de-duped list of flights to refresh: controlling first,
        # then any other LegFlight rows.
        flights_to_refresh = []
        seen = set()
        if leg.flight_information_id:
            flights_to_refresh.append(leg.flight_information)
            seen.add(leg.flight_information_id)
        for lf in leg.legflight_set.select_related('flight').all():
            if lf.flight_id and lf.flight_id not in seen:
                flights_to_refresh.append(lf.flight)
                seen.add(lf.flight_id)

        if not flights_to_refresh:
            return JsonResponse({"success": False, "error": "Leg does not have flight information"}, status=400)

        aeroapi = AeroAPIService()
        all_flight_data = []
        errors = []
        any_not_found = False

        for idx, f in enumerate(flights_to_refresh):
            logger.info(f"Refreshing flight {f.get_flight_ident()} for leg {leg.id} ({idx + 1}/{len(flights_to_refresh)})")
            try:
                res = _refresh_one_flight(f, leg, aeroapi)
            except Exception as e:
                logger.error(f"Exception refreshing flight {f.id} ({f.get_flight_ident()}): {e}", exc_info=True)
                errors.append({"flight_id": f.id, "error": str(e)})
                continue
            if res["success"]:
                all_flight_data.append(res["flight_data"])
            else:
                errors.append({"flight_id": f.id, "error": res["error"]})
                if res["not_found"]:
                    any_not_found = True

        # If the controlling flight came back not_found, surface a flight-verification task.
        # (Same behavior as before — only fired for the controlling flight, not secondaries.)
        if any_not_found and not all_flight_data:
            from ops.models import OperationalTask
            ctl_ident = (leg.flight_information.get_flight_ident() if leg.flight_information else "") or ""
            existing_task = OperationalTask.objects.filter(
                leg=leg,
                task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
                status__in=list(OperationalTask.OPEN_STATUSES),
            ).first()
            if not existing_task:
                pickup_date_fmt = leg.pickup_date.strftime('%m/%d/%Y') if leg.pickup_date else 'N/A'
                pickup_time_fmt = leg.pickup_time.strftime('%I:%M %p').lstrip('0') if leg.pickup_time else 'N/A'
                from datetime import timedelta as _td
                OperationalTask.objects.create(
                    task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
                    priority=OperationalTask.Priority.HIGH,
                    title=f"⚠️ Flight not found: {ctl_ident}",
                    description=(
                        f"Flight {ctl_ident} does not exist. "
                        f"Please verify and correct the flight number.\n"
                        f"Pickup: {pickup_date_fmt} at {pickup_time_fmt}."
                    ),
                    leg=leg,
                    reservation=leg.reservation,
                    due_at=timezone.now() + _td(hours=4),
                )
                logger.info(f"Created flight verification task for leg {leg.id}")

        if not all_flight_data:
            # `verifiable=True` tells the frontend it makes sense to offer the
            # "Verify with Guest" email here — the flight is either missing or
            # arriving at the wrong airport, both of which the guest can correct.
            # Transient errors (rate limits, network issues) get verifiable=False
            # so we don't spam the guest for something a retry would fix.
            first_err_text = errors[0]["error"] if errors else ""
            any_not_found_or_wrong_airport = (
                any_not_found
                or "orlando" in first_err_text.lower()
                or "not found" in first_err_text.lower()
            )
            return JsonResponse({
                "success": False,
                "error": first_err_text or "Refresh failed",
                "errors": errors,
                "verifiable": any_not_found_or_wrong_airport,
            }, status=400)

        return JsonResponse({
            "success": True,
            "message": "Flight data refreshed",
            "flight_data": all_flight_data[0],
            "all_flight_data": all_flight_data,
            "multi_flight": len(flights_to_refresh) > 1,
            "errors": errors,
        })

    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)
    except Exception as e:
        logger.error(f"Error refreshing flight data: {e}", exc_info=True)
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


def _flight_match_skip_reason(leg):
    """
    Reason a leg must NOT be auto-matched to its flight time, or None if it's
    safe. Cancelled / diverted / wrong-day flights have no arrival time worth
    matching a pickup to — a cancelled flight still carries its old scheduled
    time, and both match endpoints move only the pickup TIME (never the date),
    so a past-midnight arrival would land the pickup on the wrong day. These are
    held for the dispatcher to handle by hand.
    """
    flight = getattr(leg, "flight_information", None)
    if not flight:
        return None
    status_l = (flight.status or "").lower()
    if "cancel" in status_l:
        return "cancelled"
    if "divert" in status_l:
        return "diverted"
    arr = _best_flight_arrival_time(flight)
    if arr is not None and leg.pickup_date:
        try:
            arr_local = timezone.localtime(arr) if timezone.is_aware(arr) else arr
            if arr_local.date() != leg.pickup_date:
                return "wrong_day"
        except Exception:
            pass
    return None


def _apply_matched_pickup(leg, new_time, user, new_date=None):
    """
    Dispatcher-facing wrapper for a flight-matched pickup (used by both match
    endpoints). The stamped write + AuditLog + history rows live in the shared
    apply_pickup_time_move() helper (also used by the guest flight-verify
    auto-adjust); this adds the StaffActivity FLIGHT_MATCHED row, which is
    dispatcher-context only. No-op when nothing moves. Returns True if the
    pickup actually moved.

    ``new_date`` is only ever passed when a dispatcher explicitly confirmed a
    day move — see match_leg_time_to_flight.
    """
    from ops.models import StaffActivity
    from .pickup_moves import apply_pickup_time_move

    old_time = leg.pickup_time
    old_date = leg.pickup_date
    if not apply_pickup_time_move(
        leg, new_time, user=user, note="Flight match", new_date=new_date
    ):
        return False

    # Unconditional activity row — the task-scoped FLIGHT_MATCHED rows created
    # by the single-leg endpoint only exist when a conflict task is open.
    if user is not None:
        try:
            StaffActivity.objects.create(
                user=user,
                action_type=StaffActivity.ActionType.FLIGHT_MATCHED,
                metadata={
                    "leg_id": leg.id,
                    "reservation_id": leg.reservation_id,
                    "old_time": old_time.strftime("%H:%M") if old_time else "",
                    "new_time": new_time.strftime("%H:%M"),
                    # Dates recorded too: a match that crossed the calendar day
                    # is a different (and far more serious) event than a retime,
                    # and the old metadata could not tell them apart.
                    "old_date": old_date.isoformat() if old_date else "",
                    "new_date": (new_date or old_date).isoformat() if (new_date or old_date) else "",
                    "day_moved": bool(new_date and old_date and new_date != old_date),
                },
            )
        except Exception as e:
            logger.warning(f"Flight-match activity log failed for leg {leg.id}: {e}")

    return True


def _serialize_match_conflicts(leg, raw_conflicts):
    """
    Turn detect_driver_conflicts() results into the JSON-ready conflict dicts
    the post-match summary modal renders (dispatcher-facing only — task
    creation stays with _scan_driver_overlaps). Worst first.
    """
    from ops.tasks import TIGHT_TURN_RED_AFTER_MIN

    conflicts = []
    for c in raw_conflicts or []:
        other = c["conflicting_leg"]
        try:
            customer = other.reservation.customer
            guest_name = (
                f"{(customer.first_name or '').title()} {(customer.last_name or '').title()}".strip()
                or "Guest"
            )
        except Exception:
            guest_name = "Guest"
        minutes = c["conflict_minutes"]
        conflicts.append({
            "reservation_id": other.reservation_id,
            "guest_name": guest_name,
            "driver": str(leg.driver) if leg.driver else "",
            "conflict_minutes": minutes,
            "tier": "red" if minutes >= TIGHT_TURN_RED_AFTER_MIN else "amber",
            "conflicting_pickup_time": (
                other.pickup_time.strftime("%I:%M %p").lstrip("0")
                if other.pickup_time else ""
            ),
        })
    conflicts.sort(key=lambda c: -(c["conflict_minutes"] or 0))
    return conflicts


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
        if not leg.is_flight_tracked_arrival():
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
        flight_date = flight_dt.date()
        old_time = leg.pickup_time
        old_date = leg.pickup_date

        # ── Wrong-day guard ────────────────────────────────────────────────
        # _flight_match_skip_reason already encodes exactly this rule, and the
        # BULK endpoint has honoured it since it was written. The single-leg
        # button never called it. That gap is how an 11:25 PM arrival gets
        # stamped onto the NEXT day's pickup: the time matches the flight, the
        # date silently doesn't, and the trip sits ~23h out of position until a
        # guest is standing at the curb with no car.
        from .pickup_moves import (
            describe_pickup_move,
            humanize_shift_minutes,
            pickup_shift_minutes,
        )

        skip_reason = _flight_match_skip_reason(leg)
        confirmed = (data.get("confirm") or "").strip()  # "" | "move_date" | "keep_date"

        if skip_reason in ("cancelled", "diverted"):
            return JsonResponse(
                {
                    "success": False,
                    "error": (
                        f"This flight is {skip_reason}. Its arrival time is stale, so "
                        f"there is nothing safe to match — call the guest and set the "
                        f"pickup by hand."
                    ),
                },
                status=409,
            )

        if skip_reason == "wrong_day" and not confirmed:
            # Stop and make the dispatcher choose. Both options are spelled out
            # in wall-clock terms because "matching the time" sounds harmless
            # and is the option that breaks the trip.
            time_only_shift = pickup_shift_minutes(old_date, old_time, old_date, new_time)
            full_move_shift = pickup_shift_minutes(old_date, old_time, flight_date, new_time)
            flight_label = (
                f"{flight.airline or ''}{flight.flight_number or ''}".strip() or "This flight"
            )
            return JsonResponse(
                {
                    "success": False,
                    "needs_confirmation": "wrong_day",
                    "leg_id": leg.id,
                    "flight_label": flight_label,
                    "flight_arrives": f"{flight_date.strftime('%a %b %-d')}, {new_time.strftime('%I:%M %p').lstrip('0')}",
                    "pickup_currently": f"{old_date.strftime('%a %b %-d')}, {old_time.strftime('%I:%M %p').lstrip('0')}" if old_date and old_time else "",
                    "move_date_summary": describe_pickup_move(old_date, old_time, flight_date, new_time),
                    "move_date_shift": humanize_shift_minutes(full_move_shift),
                    "keep_date_summary": describe_pickup_move(old_date, old_time, old_date, new_time),
                    "keep_date_shift": humanize_shift_minutes(time_only_shift),
                    "message": (
                        f"{flight_label} lands on {flight_date.strftime('%a %b %-d')}, but this "
                        f"pickup is set for {old_date.strftime('%a %b %-d')}. Matching the time "
                        f"alone would move the pickup {humanize_shift_minutes(time_only_shift)} "
                        f"and leave it on the wrong day."
                    ),
                },
                status=409,
            )

        # Only a deliberate "move_date" confirmation is allowed to change the
        # calendar day — never a bare match.
        _apply_matched_pickup(
            leg,
            new_time,
            request.user,
            new_date=flight_date if confirmed == "move_date" else None,
        )

        # After-hours fee: the matched pickup time may now fall in the 10 PM-6 AM
        # window (flight delayed). Flag it for the dispatcher to review + charge.
        try:
            from ops.tasks import flag_afterhours_fee
            flag_afterhours_fee(leg, new_time)
        except Exception as e:
            logger.warning(f"After-hours flag failed for leg {leg.id}: {e}")

        # Auto-resolve open tasks for this leg
        from ops.models import OperationalTask, StaffActivity
        from ops.services import close_task
        match_note = (
            f"Flight matched: pickup updated "
            f"{old_time.strftime('%I:%M %p').lstrip('0')} → "
            f"{new_time.strftime('%I:%M %p').lstrip('0')}"
        )

        # Always record the match on the conflict/tight-turn tasks' activity feed —
        # even when the conflict still persists afterward (matching the booked time
        # doesn't free the committed driver), so the dispatcher sees their action.
        conflict_types = [
            OperationalTask.TaskType.DRIVER_CONFLICT,
            OperationalTask.TaskType.TIGHT_TURN,
        ]
        for t in OperationalTask.objects.filter(
            leg=leg,
            task_type__in=conflict_types,
            status__in=list(OperationalTask.OPEN_STATUSES),
        ):
            StaffActivity.objects.create(
                user=request.user,
                action_type=StaffActivity.ActionType.FLIGHT_MATCHED,
                task=t,
                metadata={
                    "old_time": old_time.strftime("%H:%M"),
                    "new_time": new_time.strftime("%H:%M"),
                    "note": match_note,
                },
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

        # Conflict re-check runs UNCONDITIONALLY — a sub-30-min move can create
        # a brand-new overlap that has no open task yet, and the old task-only
        # re-check silently missed those.
        # Refresh leg from DB to get the updated pickup_time
        leg.refresh_from_db()
        try:
            from ops.tasks import detect_driver_conflicts
            remaining = detect_driver_conflicts(leg, leg.pickup_date)
        except Exception as e:
            logger.warning(f"Post-match conflict check failed for leg {leg.id}: {e}")
            remaining = None  # unknown — keep any open conflict tasks open

        # For driver_conflict tasks: only close if the conflict is actually resolved
        dc_tasks = OperationalTask.objects.filter(
            leg=leg,
            task_type=OperationalTask.TaskType.DRIVER_CONFLICT,
            status__in=list(OperationalTask.OPEN_STATUSES),
        )
        for task in dc_tasks:
            if remaining is None:
                continue
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

        # Same-day moves also get the instant turn-risk pass so a fresh conflict
        # files its task now instead of on the next 30-min background cycle.
        if leg.pickup_date == timezone.localdate():
            try:
                from ops.tasks import _scan_driver_overlaps
                _scan_driver_overlaps()
            except Exception as e:
                logger.warning(f"Post-match turn-risk scan failed for leg {leg.id}: {e}")

        # Dispatcher-facing summary: what moved + any conflicts the move leaves.
        try:
            conflict_rows = _serialize_match_conflicts(leg, remaining)
        except Exception as e:
            logger.warning(f"Post-match conflict summary failed for leg {leg.id}: {e}")
            conflict_rows = []
        # Count the date on both sides. The old version combined new_time and
        # old_time against the SAME pickup_date, so a move that crossed a day
        # reported a small delta and looked harmless in the summary modal.
        day_moved = bool(old_date and leg.pickup_date and leg.pickup_date != old_date)
        delta_minutes = pickup_shift_minutes(
            old_date, old_time, leg.pickup_date, new_time
        ) or 0

        return JsonResponse({
            "success": True,
            "message": (
                f"Pickup moved to {leg.pickup_date.strftime('%a %b %-d')}, "
                f"{new_time.strftime('%I:%M %p').lstrip('0')} to match flight arrival"
                if day_moved
                else "Leg pickup time updated to match flight arrival"
            ),
            "pickup_time": new_time.strftime("%H:%M"),
            "pickup_date": leg.pickup_date.isoformat() if leg.pickup_date else "",
            "day_moved": day_moved,
            "summary": {
                "old_time": old_time.strftime("%I:%M %p").lstrip("0") if old_time else "",
                "new_time": new_time.strftime("%I:%M %p").lstrip("0"),
                "old_date": old_date.isoformat() if old_date else "",
                "new_date": leg.pickup_date.isoformat() if leg.pickup_date else "",
                "day_moved": day_moved,
                "moved": describe_pickup_move(old_date, old_time, leg.pickup_date, new_time),
                "delta_minutes": delta_minutes,
                "conflicts": conflict_rows,
            },
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
            ).select_related("flight_information", "reservation__customer", "driver")
        )
        arrival_legs = [leg for leg in legs if leg.is_flight_tracked_arrival()]
        updated = 0
        changes = []
        changed_legs = []
        skipped = []
        for leg in arrival_legs:
            flight = leg.flight_information
            # Hold cancelled / diverted / wrong-day flights out of the bulk match —
            # there is no arrival time worth matching a pickup to. The dispatcher
            # handles each by hand (call the guest); the UI lists them separately.
            skip_reason = _flight_match_skip_reason(leg)
            if skip_reason:
                try:
                    customer = leg.reservation.customer
                    sk_name = (
                        f"{(customer.first_name or '').title()} {(customer.last_name or '').title()}".strip()
                        or "Guest"
                    )
                except Exception:
                    sk_name = "Guest"
                skipped.append({
                    "leg_id": leg.id,
                    "reservation_id": leg.reservation_id,
                    "guest_name": sk_name,
                    "reason": skip_reason,
                })
                continue
            flight_dt = _best_flight_arrival_time(flight)
            if not flight_dt:
                continue
            if timezone.is_aware(flight_dt):
                flight_dt = timezone.make_naive(
                    flight_dt, timezone.get_current_timezone()
                )
            new_time = flight_dt.time()
            if leg.pickup_time != new_time:
                old_time = leg.pickup_time
                _apply_matched_pickup(leg, new_time, request.user)
                updated += 1
                changed_legs.append(leg)
                try:
                    customer = leg.reservation.customer
                    guest_name = (
                        f"{(customer.first_name or '').title()} {(customer.last_name or '').title()}".strip()
                        or "Guest"
                    )
                except Exception:
                    guest_name = "Guest"
                changes.append({
                    "leg_id": leg.id,
                    "reservation_id": leg.reservation_id,
                    "guest_name": guest_name,
                    "old_time": old_time.strftime("%I:%M %p").lstrip("0") if old_time else "",
                    "new_time": new_time.strftime("%I:%M %p").lstrip("0"),
                    "delta_minutes": int(
                        (
                            datetime.combine(target_date, new_time)
                            - datetime.combine(target_date, old_time)
                        ).total_seconds() // 60
                    ) if old_time else 0,
                })
                # After-hours fee: flag if the new pickup is in the late-night window.
                try:
                    from ops.tasks import flag_afterhours_fee
                    flag_afterhours_fee(leg, new_time)
                except Exception as e:
                    logger.warning(f"After-hours flag failed for leg {leg.id}: {e}")

        # Unconditional conflict sweep over the changed legs, deduped on the leg
        # pair (A→B and B→A report once, worst minutes wins).
        new_conflicts = []
        try:
            from ops.tasks import detect_driver_conflicts
            conflict_by_pair = {}
            for leg in changed_legs:
                for c in detect_driver_conflicts(leg, target_date):
                    pair = frozenset((leg.id, c["conflicting_leg"].id))
                    best = conflict_by_pair.get(pair)
                    if best and best[1]["conflict_minutes"] >= c["conflict_minutes"]:
                        continue
                    conflict_by_pair[pair] = (leg, c)
            for leg, c in conflict_by_pair.values():
                new_conflicts.extend(_serialize_match_conflicts(leg, [c]))
            new_conflicts.sort(key=lambda c: -(c["conflict_minutes"] or 0))
            # Same instant turn-risk pass as the single-leg match (today only).
            if target_date == timezone.localdate() and changed_legs:
                try:
                    from ops.tasks import _scan_driver_overlaps
                    _scan_driver_overlaps()
                except Exception as e:
                    logger.warning(f"Post-match-all turn-risk scan failed: {e}")
        except Exception as e:
            logger.warning(f"Post-match-all conflict sweep failed: {e}")

        return JsonResponse({
            "success": True,
            "message": f"Updated {updated} arrival leg(s) to match flight arrival time.",
            "updated_count": updated,
            "total_arrival_legs": len(arrival_legs),
            "summary": {
                "matched": len(arrival_legs),
                "changed": updated,
                "changes": changes,
                "new_conflicts": new_conflicts,
                "skipped": skipped,
                "skipped_count": len(skipped),
            },
        })
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)
    except Exception as e:
        logger.error(f"Error matching all leg times: {e}", exc_info=True)
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required
@require_POST
def charge_afterhours_fee(request, leg_id):
    """Charge the flat after-hours fee for one leg (dispatcher one-click). JSON."""
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    leg = get_object_or_404(
        Leg.objects.select_related("reservation", "reservation__customer"), id=leg_id
    )
    result = _charge_afterhours_fee_for_leg(leg, request.user)
    if result.get("success"):
        amount = result["amount"]
        return JsonResponse({
            "success": True,
            "message": f"Charged ${amount} after-hours fee and emailed the customer.",
            "amount": str(amount),
            "tasks_closed": result.get("tasks_closed", 0),
        })
    err = result.get("error", "Charge failed.")
    low = err.lower()
    status = 402 if "declined" in low else (502 if "look up" in low else 400)
    return JsonResponse({"success": False, "error": err}, status=status)


def _charge_afterhours_fee_for_leg(leg, user):
    """Charge the flat after-hours fee for one leg to the card on file, mark the
    leg, roll the fee into the reservation totals, note it, email the customer,
    and close the open after-hours task. Returns a result dict (no HttpResponse)
    so both the single-leg endpoint and the batch "charge all" action reuse it.

    NOTE: this is also the single charge+notify action the future flag-gated auto
    path (`reservations.utils.AFTERHOURS_AUTO_CHARGE`) would reuse on the day of pickup.
    """
    from reservations.utils import (
        AFTERHOURS_FEE_AMOUNT,
        adjust_reservation_for_stop_fee_delta,
    )
    from users.emails import send_afterhours_fee_notice
    from ops.models import OperationalTask
    from ops.services import close_task

    reservation = leg.reservation
    if reservation is None:
        return {"success": False, "error": "Leg has no reservation."}
    customer = reservation.customer
    amount = AFTERHOURS_FEE_AMOUNT

    # Re-verify the fee is genuinely owed at charge time (delay/flap-aware), so a
    # stale button or a flight that flapped back out of the window can't charge,
    # and an already-charged leg is never double-charged.
    if leg.afterhours_fee_outstanding() <= 0:
        return {"success": False, "error": "After-hours fee not owed (already charged or no longer in the 10 PM-6 AM window)."}

    try:
        if getattr(customer, "stripe_customer_id", None):
            stripe_customer_id = customer.stripe_customer_id
        else:
            stripe_customer_id = get_or_create_stripe_customer(reservation).id
    except Exception as e:
        logger.error(f"After-hours charge: customer profile resolve failed for res {reservation.id}: {e}")
        return {"success": False, "error": "Could not resolve the customer's payment profile."}

    try:
        methods = stripe.PaymentMethod.list(customer=stripe_customer_id)
    except stripe.error.StripeError as e:
        logger.error(f"After-hours charge: PM lookup failed for res {reservation.id}: {e}")
        return {"success": False, "error": "Could not look up saved cards."}
    if not methods.data:
        return {"success": False, "error": "No card on file to charge."}
    payment_method_id = methods.data[0].id

    desc = f"After-Hours Fee - Res #{reservation.id}"
    try:
        payment_intent = stripe.PaymentIntent.create(
            amount=int(amount * 100),
            currency="usd",
            customer=stripe_customer_id,
            payment_method=payment_method_id,
            off_session=True,
            confirm=True,
            description=desc,
            statement_descriptor_suffix="AfterHours Fee",
            metadata={
                "reservation_uuid": str(reservation.uuid),
                "reservation_id": reservation.id,
                "leg_id": leg.id,
                "charge_type": "afterhours_fee",
                "initiated_by": "dispatcher",
            },
            # One after-hours fee per leg: a retry (e.g. after a DB error between
            # the Stripe success and the local commit) returns the SAME charge
            # instead of billing the customer twice.
            idempotency_key=f"afterhours-fee-leg-{leg.id}",
        )
    except stripe.error.CardError as e:
        logger.warning(f"After-hours charge declined for res {reservation.id}: {e}")
        return {"success": False, "error": f"Card declined: {getattr(e, 'user_message', None) or str(e)}"}
    except stripe.error.StripeError as e:
        logger.error(f"After-hours charge error for res {reservation.id}: {e}")
        return {"success": False, "error": str(e)}

    if payment_intent.status != "succeeded":
        return {"success": False, "error": f"Payment not completed ({payment_intent.status})."}

    with transaction.atomic():
        Payment.objects.get_or_create(
            reservation=reservation,
            customer=customer,
            stripe_payment_intent_id=payment_intent.id,
            defaults={
                "amount": amount,
                "description": desc,
                "payment_type": "pay_now",
                "status": "paid",
                "stripe_customer_id": stripe_customer_id,
                "stripe_payment_method_id": payment_method_id,
            },
        )
        # Mark the leg, note it, and roll the fee into the reservation totals.
        delta = amount - (leg.afterhours_fee or Decimal("0.00"))
        leg.afterhours_fee = amount
        _note = f"${amount:.2f} After-Hours Fee charged"
        leg.private_notes = f"{leg.private_notes}\n{_note}" if leg.private_notes else _note
        leg.save(update_fields=["afterhours_fee", "private_notes"])
        adjust_reservation_for_stop_fee_delta(reservation, delta)

    _run_in_background(send_afterhours_fee_notice, reservation, leg, amount, sent_by=user)

    closed = 0
    for task in OperationalTask.objects.filter(
        leg=leg,
        task_type=OperationalTask.TaskType.AFTERHOURS_FEE,
        status__in=list(OperationalTask.OPEN_STATUSES),
    ):
        close_task(
            task,
            resolved_by=user,
            resolution_notes=f"Charged ${amount} after-hours fee + emailed customer.",
        )
        closed += 1

    return {"success": True, "amount": amount, "tasks_closed": closed}


@login_required
@require_POST
def charge_all_afterhours_fees(request):
    """Semi-auto "review then charge + email" action: charge the after-hours fee
    for ALL flagged (owed-but-not-charged) legs on a date. POST JSON:
    {"date": "YYYY-MM-DD"} or {"leg_ids": [...]}. Re-checks each leg server-side
    (only charges genuinely-owed legs) so already-charged trips are never touched.
    Returns a per-leg summary."""
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        data = {}

    legs_qs = Leg.objects.select_related(
        "reservation", "reservation__customer", "flight_information"
    ).exclude(status="cancelled").exclude(reservation__status="cancelled")

    if data.get("leg_ids"):
        legs_qs = legs_qs.filter(id__in=data["leg_ids"])
    elif data.get("date"):
        legs_qs = legs_qs.filter(pickup_date=data["date"])
    else:
        return JsonResponse({"success": False, "error": "Provide a date or leg_ids."}, status=400)

    owed = [leg for leg in legs_qs if leg.afterhours_fee_outstanding() > 0]

    # Cap the synchronous Stripe burst on the single worker (each leg = ~2 Stripe
    # calls; stay well under the 60s gunicorn timeout); report if capped.
    MAX_BATCH = 12
    batch, capped = owed[:MAX_BATCH], len(owed) > MAX_BATCH

    charged = 0
    failed = []
    for leg in batch:
        try:
            res = _charge_afterhours_fee_for_leg(leg, request.user)
        except Exception as e:
            # One leg's failure must never abort the whole batch.
            logger.error(f"After-hours batch: leg {leg.id} raised: {e}", exc_info=True)
            res = {"success": False, "error": "Unexpected error."}
        if res.get("success"):
            charged += 1
        else:
            cust = leg.reservation.customer if leg.reservation else None
            failed.append({
                "leg_id": leg.id,
                "customer": cust.get_full_name() if cust else "",
                "error": res.get("error", "Charge failed."),
            })

    return JsonResponse({
        "success": True,
        "charged": charged,
        "failed": failed,
        "total_owed": len(owed),
        "capped": capped,
        "message": f"Charged {charged} after-hours fee(s)"
                   + (f"; {len(failed)} failed" if failed else "")
                   + ("; more remain — run again" if capped else "") + ".",
    })


@login_required
@require_POST
def save_confirmation_override(request):
    """
    Save (or clear) a per-leg confirmation SMS override.

    POST JSON: { "leg_id": <int>, "body": <str>, "reset": <bool optional> }
    - reset=true clears the override (leg falls back to auto-generated body).
    - body is stripped; empty string is treated as a reset.
    Returns: { "success": bool, "has_custom": bool, "preview": <str>, "default": <str> }
    """
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    from .confirmation_sms import leg_to_row, get_confirmation_message

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    leg_id = data.get("leg_id")
    body = (data.get("body") or "").strip()
    reset = bool(data.get("reset"))

    if not leg_id:
        return JsonResponse({"success": False, "error": "Missing leg_id"}, status=400)

    try:
        leg = Leg.objects.select_related(
            "reservation", "reservation__customer", "reservation__vehicle",
            "flight_information", "cruise_information",
        ).get(id=int(leg_id))
    except (Leg.DoesNotExist, ValueError, TypeError):
        return JsonResponse({"success": False, "error": "Leg not found"}, status=404)

    if reset or not body:
        leg.confirmation_sms_override = ""
    else:
        leg.confirmation_sms_override = body
    leg.save(update_fields=["confirmation_sms_override"])

    # Recompute preview + default for the client
    row = leg_to_row(leg)
    preview = get_confirmation_message(leg, row)
    saved = leg.confirmation_sms_override
    leg.confirmation_sms_override = ""
    default_msg = get_confirmation_message(leg, row)
    leg.confirmation_sms_override = saved

    return JsonResponse({
        "success": True,
        "has_custom": bool(saved.strip()),
        "preview": preview,
        "default": default_msg,
    })


@login_required(login_url="login")
def confirmations_view(request):
    """
    Next-day confirmation SMS: preview legs for a date, export CSV, or send texts via Twilio.
    Intended to run after validating flights (Refresh Arrival Flights / Match All Flight Times).
    """
    # Staff-only: this page exposes customer PII and can send (paid) Twilio SMS.
    if not request.user.is_staff:
        return redirect("dashboard")

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
        # Effective message (honors override) + auto-generated default for "Reset"
        row["message_preview"] = get_confirmation_message(leg, row)
        custom = (leg.confirmation_sms_override or "").strip()
        if custom:
            saved_override = leg.confirmation_sms_override
            leg.confirmation_sms_override = ""
            row["default_message"] = get_confirmation_message(leg, row)
            leg.confirmation_sms_override = saved_override
        else:
            row["default_message"] = row["message_preview"]
        row["has_custom"] = bool(custom)
        row["leg"] = leg
        row["already_sent"] = bool(getattr(leg, "confirmation_sms_sent_at", None))
        row["flight_unverified"] = leg.id in flight_unverified_leg_ids
        row["unpaid"] = leg.reservation_id in unpaid_reservation_ids
        rows.append(row)

    legs_filter_url = reverse("dashboard") + f"?date={selected_date.isoformat()}"
    vip_count = sum(1 for r in rows if r.get("is_vip"))

    return render(
        request,
        "dispatching/confirmations.html",
        {
            "selected_date": selected_date,
            "rows": rows,
            "twilio_configured": twilio_configured(),
            "legs_filter_url": legs_filter_url,
            "vip_count": vip_count,
        },
    )


# Bulk-refresh progress is stored in the DB, not the cache: without REDIS_URL
# the cache is per-process LocMem, so with `gunicorn --workers 3` the status
# poll usually landed on a worker that had never seen the task and 404'd.
# See FlightRefreshTask.

def _flight_refresh_set(task_id, state):
    """Write (or overwrite) the state blob for one refresh task."""
    from .models import FlightRefreshTask

    FlightRefreshTask.objects.update_or_create(
        task_id=task_id, defaults={"state": state}
    )


def _flight_refresh_get(task_id):
    """Return the state blob for a refresh task, or None if unknown."""
    from .models import FlightRefreshTask

    row = FlightRefreshTask.objects.filter(task_id=task_id).only("state").first()
    return row.state if row else None


def _flight_refresh_prune(older_than_hours=24):
    """Drop finished task rows so the table doesn't grow without bound."""
    from .models import FlightRefreshTask

    cutoff = timezone.now() - timedelta(hours=older_than_hours)
    FlightRefreshTask.objects.filter(created_at__lt=cutoff).delete()


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
        trip_type = leg.flight_tracking_trip_type()

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
    from .flight_refresh_review import (
        snapshot_flight_state,
        classify_refresh_row,
        build_review_summary,
        auto_create_flight_verify_tasks,
    )

    refresh_started_dt = timezone.now()
    started_at = refresh_started_dt.isoformat()
    BATCH_SIZE = 5  # AeroAPI Standard: up to 5 queries/sec

    try:
        legs = list(
            Leg.objects.filter(id__in=leg_ids, flight_information__isnull=False).select_related(
                "flight_information"
            )
        )

        if not legs:
            _flight_refresh_set(
                task_id,
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
            )
            return

        # Snapshot each flight BEFORE refresh so we can detect what changed.
        # Stored in-memory only — no model, scoped to this worker thread.
        snapshots = {
            leg.id: snapshot_flight_state(leg.flight_information) for leg in legs
        }

        results = []
        success_count = 0
        failure_count = 0
        total_legs = len(legs)

        _flight_refresh_set(
            task_id,
            {
                "status": "running",
                "total": total_legs,
                "processed": 0,
                "success_count": 0,
                "failure_count": 0,
                "results": [],
                "started_at": started_at,
            },
        )

        # Each pool thread opens its own DB connection (flight.save()); close it
        # when its task finishes so a large refresh doesn't leave a pile of idle
        # connections behind (connection saturation 2026-07-18).
        def _refresh_and_close(_leg):
            try:
                return _refresh_single_flight(_leg)
            finally:
                from django.db import connection
                connection.close()

        # Process in batches of 5 (5/sec limit) so 45 flights ≈ 9 batches ≈ ~10 sec
        for offset in range(0, total_legs, BATCH_SIZE):
            batch = legs[offset : offset + BATCH_SIZE]
            with ThreadPoolExecutor(max_workers=BATCH_SIZE) as executor:
                batch_results = list(executor.map(_refresh_and_close, batch))
            results.extend(batch_results)
            success_count += sum(1 for r in batch_results if r.get("success"))
            failure_count += sum(1 for r in batch_results if not r.get("success"))
            processed = min(offset + BATCH_SIZE, total_legs)
            _flight_refresh_set(
                task_id,
                {
                    "status": "running",
                    "total": total_legs,
                    "processed": processed,
                    "success_count": success_count,
                    "failure_count": failure_count,
                    "results": results,
                    "started_at": started_at,
                },
            )

        message = (
            f"Refreshed {success_count} flight(s) successfully"
            + (f", {failure_count} failed" if failure_count > 0 else "")
        )

        # Classify each leg with fresh model state, build review summary, and
        # auto-file flight_verify ops tasks for risky legs (dedup is handled
        # inside ops.services.create_task — repeat refreshes don't spam).
        summary = None
        try:
            from django.conf import settings as _settings
            threshold = int(getattr(
                _settings, "FLIGHT_REVIEW_MISMATCH_THRESHOLD_MINUTES", 30
            ))
            minor_threshold = int(getattr(
                _settings, "FLIGHT_REVIEW_MINOR_CHANGE_MINUTES", 5
            ))
            fresh_legs = list(
                Leg.objects.filter(id__in=[l.id for l in legs])
                .select_related("flight_information", "reservation__customer", "driver")
            )
            result_by_leg = {r.get("leg_id"): r for r in results}
            rows = [
                classify_refresh_row(
                    leg,
                    snapshots.get(leg.id),
                    result_by_leg.get(leg.id),
                    threshold_minutes=threshold,
                    minor_threshold_minutes=minor_threshold,
                )
                for leg in fresh_legs
            ]
            summary = build_review_summary(
                rows,
                minor_threshold_minutes=minor_threshold,
                threshold_minutes=threshold,
            )
            try:
                auto_create_flight_verify_tasks(rows)
            except Exception as e:
                logger.error(f"auto_create_flight_verify_tasks failed: {e}")
            # Instant turn-risk pass: surface driver-conflict (red) and tight-turn
            # (amber) flags right away — the early-flight safety net — instead of
            # waiting for the next 30-min background cycle. Scoped to today's board
            # (the morning-refresh use case) and deduped, so it is safe to run
            # alongside the background scan.
            try:
                from ops.tasks import _scan_driver_overlaps
                _scan_driver_overlaps()
            except Exception as e:
                logger.error(f"Post-refresh turn-risk scan failed: {e}")
            # Conflicts CREATED by this refresh (tasks filed since it started, on
            # the refreshed day) — the review modal calls them out up top so a
            # sub-30-min move that just broke a driver's day is never missed.
            # Filter on the DATE, not the refreshed leg ids: _scan_driver_overlaps
            # anchors its task on the leg the driver is late TO (often a
            # departure that wasn't refreshed), so an id-scoped filter missed those.
            try:
                from ops.models import OperationalTask
                new_conflicts = []
                refreshed_dates = {l.pickup_date for l in fresh_legs}
                conflict_tasks = OperationalTask.objects.filter(
                    task_type__in=[
                        OperationalTask.TaskType.DRIVER_CONFLICT,
                        OperationalTask.TaskType.TIGHT_TURN,
                    ],
                    created_at__gte=refresh_started_dt,
                    leg__pickup_date__in=refreshed_dates,
                )
                for t in conflict_tasks:
                    md = t.metadata or {}
                    new_conflicts.append({
                        "task_id": t.id,
                        "title": t.title,
                        "driver_name": md.get("driver_name") or "",
                        "conflict_minutes": md.get("conflict_minutes") or md.get("late_minutes"),
                        "reservation_id": t.reservation_id,
                        "tier": "amber" if t.task_type == OperationalTask.TaskType.TIGHT_TURN else "red",
                    })
                summary["new_conflicts"] = new_conflicts
            except Exception as e:
                logger.error(f"Post-refresh conflict summary failed: {e}")
        except Exception as e:
            logger.error(f"Review summary build failed: {e}")

        _flight_refresh_set(
            task_id,
            {
                "status": "completed",
                "message": message,
                "total": total_legs,
                "processed": total_legs,
                "success_count": success_count,
                "failure_count": failure_count,
                "results": results,
                "summary": summary,
                "started_at": started_at,
                "finished_at": timezone.now().isoformat(),
            },
        )
    except Exception as e:
        logger.error(f"Error in bulk refresh thread: {e}")
        _flight_refresh_set(
            task_id,
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
        )
    finally:
        # This worker thread opened its own DB connection (Leg queries above);
        # release it so it isn't left idle (connection saturation 2026-07-18).
        from django.db import connection
        connection.close()


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
        
        # Filter to flight-tracked legs: airport arrivals AND airport→cruise transfers
        # (their inbound flight lands at the airport, so it's tracked like an arrival).
        # Filtered in Python since is_flight_tracked_arrival() is a computed property.
        arrival_legs = [leg for leg in legs if leg.is_flight_tracked_arrival()]
        legs = arrival_legs

        if not legs:
            return JsonResponse({
                "success": False,
                "error": "No arrival flights found to refresh. Only arrival trips are refreshed."
            }, status=400)
        
        task_id = uuid.uuid4().hex

        # Written before the task_id is handed to the client, so the first poll
        # always finds a row no matter which worker serves it.
        _flight_refresh_set(
            task_id,
            {
                "status": "queued",
                "total": len(legs),
                "processed": 0,
                "success_count": 0,
                "failure_count": 0,
                "results": [],
                "started_at": timezone.now().isoformat(),
            },
        )
        try:
            _flight_refresh_prune()
        except Exception as e:
            logger.error(f"Flight refresh task prune failed: {e}")

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
@require_POST
def dismiss_flight_review(request):
    """
    Close any open flight_verify task on a leg, called from the post-refresh
    review modal's "Mark Reviewed" button. Idempotent — succeeds even if no
    open task exists for the leg.
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
                {"success": False, "error": "leg_id is required"}, status=400
            )

        from ops.models import OperationalTask
        from ops.services import close_task

        task = (
            OperationalTask.objects.filter(
                leg_id=leg_id,
                task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
                status__in=list(OperationalTask.OPEN_STATUSES),
            )
            .order_by("-created_at")
            .first()
        )
        closed = False
        if task:
            close_task(
                task,
                resolved_by=request.user,
                resolution_notes=f"Reviewed via post-refresh summary by {request.user}",
            )
            closed = True
        return JsonResponse({"success": True, "closed": closed})

    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)
    except Exception as e:
        logger.error(f"Error dismissing flight review for leg: {e}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required
@require_POST
def acknowledge_time_change(request):
    """
    Clear the board's "time changed" badge for one leg (leg_id) or several
    (leg_ids) by stamping pickup_change_ack_at. Idempotent.
    """
    if not request.user.is_staff:
        return JsonResponse(
            {"success": False, "error": "Permission denied"}, status=403
        )

    try:
        data = json.loads(request.body)
        leg_ids = data.get("leg_ids") or []
        if leg_ids and not isinstance(leg_ids, list):
            # A string like "57" would char-iterate into legs 5 and 7 below.
            return JsonResponse(
                {"success": False, "error": "leg_ids must be a list"}, status=400
            )
        if data.get("leg_id"):
            leg_ids = list(leg_ids) + [data["leg_id"]]
        leg_ids = [int(i) for i in leg_ids]
        if not leg_ids:
            return JsonResponse(
                {"success": False, "error": "leg_id or leg_ids is required"}, status=400
            )
        acked = Leg.objects.filter(id__in=leg_ids).update(
            pickup_change_ack_at=timezone.now()
        )
        return JsonResponse({"success": True, "acked": acked})

    except (json.JSONDecodeError, ValueError, TypeError):
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)
    except Exception as e:
        logger.error(f"Error acknowledging time change: {e}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required
@require_http_methods(["GET"])
def refresh_all_flights_status(request, task_id):
    if not request.user.is_staff:
        return JsonResponse(
            {"success": False, "error": "Permission denied"}, status=403
        )

    data = _flight_refresh_get(task_id)
    if not data:
        return JsonResponse(
            {"success": False, "error": "Refresh task not found"}, status=404
        )

    return JsonResponse({"success": True, **data})


_LEG_OVERRIDE_INT_FIELDS = (
    "passenger_count", "luggage_count",
    "rf_carseats", "ff_carseats", "booster_seats",
    "extra_carseats", "extra_boosters",
)
_LEG_OVERRIDE_FIELDS = _LEG_OVERRIDE_INT_FIELDS + ("vehicle", "luggage_type", "need_carseats")


def _apply_leg_override_fields(leg, leg_data):
    """
    Apply per-leg trip-detail overrides from a payload dict to a Leg instance
    (mutates in place). Only inspects keys present in leg_data; missing keys
    are left untouched.

    Returns (modified_field_names, error_message_or_None).
    """
    modified = []

    if "vehicle" in leg_data:
        value = leg_data["vehicle"]
        if value == "" or value is None:
            if leg.vehicle_id is not None:
                leg.vehicle = None
                modified.append("vehicle")
        else:
            try:
                new_v = Vehicle.objects.get(id=int(value))
            except (Vehicle.DoesNotExist, ValueError, TypeError):
                return modified, "Invalid vehicle selection"
            if leg.vehicle_id != new_v.id:
                leg.vehicle = new_v
                modified.append("vehicle")

    for field in _LEG_OVERRIDE_INT_FIELDS:
        if field not in leg_data:
            continue
        value = leg_data[field]
        if value == "" or value is None:
            new_val = None
        else:
            try:
                new_val = int(value)
                if new_val < 0:
                    raise ValueError
            except (ValueError, TypeError):
                return modified, f"Invalid {field}"
        if getattr(leg, field) != new_val:
            setattr(leg, field, new_val)
            modified.append(field)

    if "luggage_type" in leg_data:
        value = leg_data["luggage_type"] or ""
        if value and value not in ("carry_on", "checked"):
            return modified, "Invalid luggage_type"
        if (leg.luggage_type or "") != value:
            leg.luggage_type = value
            modified.append("luggage_type")

    if "need_carseats" in leg_data:
        value = leg_data["need_carseats"]
        if value == "" or value is None:
            new_val = None
        elif str(value).lower() in ("true", "1", "yes"):
            new_val = True
        elif str(value).lower() in ("false", "0", "no"):
            new_val = False
        else:
            return modified, "Invalid need_carseats"
        if leg.need_carseats != new_val:
            leg.need_carseats = new_val
            modified.append("need_carseats")

    return modified, None


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

        # Update leg fields (non-override scalars)
        update_fields = []
        _needs_legflight_sync = False
        for field, value in leg_data.items():
            if field in _LEG_OVERRIDE_FIELDS:
                continue  # handled by _apply_leg_override_fields below
            if hasattr(leg, field) and value is not None:
                # Handle date and time fields properly
                if field == 'pickup_date' and value:
                    from datetime import datetime
                    try:
                        date_obj = datetime.strptime(value, '%Y-%m-%d').date()
                        setattr(leg, field, date_obj)
                        update_fields.append(field)
                    except ValueError:
                        return JsonResponse({"success": False, "error": "Invalid date format"}, status=400)
                elif field == 'pickup_time' and value:
                    from datetime import datetime
                    try:
                        time_obj = datetime.strptime(value, '%H:%M').time()
                        setattr(leg, field, time_obj)
                        update_fields.append(field)
                    except ValueError:
                        return JsonResponse({"success": False, "error": "Invalid time format"}, status=400)
                else:
                    setattr(leg, field, value)
                    update_fields.append(field)

        # Apply per-leg trip-detail overrides
        override_modified, override_error = _apply_leg_override_fields(leg, leg_data)
        if override_error:
            return JsonResponse({"success": False, "error": override_error}, status=400)
        update_fields.extend(override_modified)

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
                # Defer the LegFlight sync until after leg.save() runs below so
                # flight_information_id is committed first.
                _needs_legflight_sync = True

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

        # Keep LegFlight rows in sync with a freshly-set legacy flight_information.
        if _needs_legflight_sync:
            _sync_legacy_flight_information(leg)

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
# LEG EXTRA STOPS & MULTI-FLIGHT — inline CRUD on the reservation detail page
# ============================================================================

def _render_leg_extras_panel(leg):
    """Render the editor panel HTML for a single leg's stops + flights."""
    return render_to_string(
        "dispatching/includes/_leg_extras_panel.html",
        {"leg": leg},
    )


def _apply_stop_fee_delta(leg, fee_delta):
    """Adjust the reservation's total by exactly `fee_delta` (Decimal or 0).
    Leaves all other charge components untouched."""
    from reservations.utils import adjust_reservation_for_stop_fee_delta
    if leg.reservation_id and fee_delta:
        try:
            adjust_reservation_for_stop_fee_delta(leg.reservation, fee_delta)
        except Exception:
            logger.exception("adjust_reservation_for_stop_fee_delta failed for reservation %s", leg.reservation_id)


def _staff_only_or_403(request):
    if not request.user.is_authenticated or not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    return None


def _next_legstop_sequence(leg):
    last = leg.legstop_set.order_by("-sequence").first()
    return (last.sequence + 1) if last else 0


def _parse_time_or_none(raw):
    """Accept 'HH:MM' / 'HH:MM:SS' / empty / None and return a datetime.time or None."""
    if raw in (None, ""):
        return None
    s = str(raw).strip()
    if not s:
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    return None


def _next_legflight_sequence(leg):
    last = leg.legflight_set.order_by("-sequence", "-id").first()
    return (last.sequence + 1) if last else 0


def _sync_legacy_flight_information(leg):
    """Make sure leg.flight_information has a corresponding controlling LegFlight.

    Legacy edit/booking paths still write directly to Leg.flight_information.
    Without a matching LegFlight, the new flights panel hides the legacy flight
    once any other LegFlight is added. Call this after any code path that
    assigns or replaces leg.flight_information.
    """
    if not leg.flight_information_id:
        return
    existing = leg.legflight_set.filter(flight_id=leg.flight_information_id).first()
    if existing:
        if not existing.is_controlling:
            leg.legflight_set.filter(is_controlling=True).update(is_controlling=False)
            existing.is_controlling = True
            existing.save(update_fields=["is_controlling"])
        return
    leg.legflight_set.filter(is_controlling=True).update(is_controlling=False)
    LegFlight.objects.create(
        leg=leg,
        flight_id=leg.flight_information_id,
        is_controlling=True,
        sequence=_next_legflight_sequence(leg),
    )


@login_required
@require_POST
def add_leg_stop(request, leg_id):
    """Create a LegStop for a leg. Returns the refreshed editor panel HTML."""
    deny = _staff_only_or_403(request)
    if deny:
        return deny
    leg = get_object_or_404(Leg, id=leg_id)
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    location_text = (data.get("location_text") or "").strip()

    try:
        duration = int(data.get("duration_minutes") or 10)
    except (TypeError, ValueError):
        duration = 10
    stop_type = data.get("stop_type") or "dropoff"
    if stop_type not in dict(LegStop.STOP_TYPE_CHOICES):
        stop_type = "dropoff"

    # Charter stops can be open-ended ("take them anywhere") — location optional.
    if not location_text and stop_type != "charter":
        return JsonResponse({"success": False, "error": "Stop location is required"}, status=400)
    notes = (data.get("notes") or "").strip()

    extra_fee_raw = data.get("extra_fee")
    extra_fee = None
    if extra_fee_raw not in (None, ""):
        try:
            extra_fee = Decimal(str(extra_fee_raw))
        except Exception:
            return JsonResponse({"success": False, "error": "Invalid extra_fee"}, status=400)

    start_time = _parse_time_or_none(data.get("start_time"))

    LegStop.objects.create(
        leg=leg,
        sequence=_next_legstop_sequence(leg),
        location_text=location_text,
        stop_type=stop_type,
        duration_minutes=duration,
        start_time=start_time,
        notes=notes,
        extra_fee=extra_fee,
        requires_manual_review=bool(data.get("requires_manual_review")),
    )
    # Bump the reservation total by the new stop's fee (if any)
    if extra_fee:
        _apply_stop_fee_delta(leg, extra_fee)
    return JsonResponse({"success": True, "html": _render_leg_extras_panel(leg)})


@login_required
@require_POST
def update_leg_stop(request, leg_id, stop_id):
    """Update fields of an existing LegStop."""
    deny = _staff_only_or_403(request)
    if deny:
        return deny
    leg = get_object_or_404(Leg, id=leg_id)
    stop = get_object_or_404(LegStop, id=stop_id, leg=leg)

    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    # Snapshot the fee before mutating so we can compute the delta after save
    old_fee = stop.extra_fee or Decimal("0.00")

    # Apply stop_type first so we know whether location_text is required below.
    if "stop_type" in data and data["stop_type"] in dict(LegStop.STOP_TYPE_CHOICES):
        stop.stop_type = data["stop_type"]
    if "location_text" in data:
        text = (data.get("location_text") or "").strip()
        if not text and stop.stop_type != "charter":
            return JsonResponse({"success": False, "error": "Stop location is required"}, status=400)
        stop.location_text = text
    if "duration_minutes" in data:
        try:
            stop.duration_minutes = max(int(data["duration_minutes"]), 0)
        except (TypeError, ValueError):
            pass
    if "notes" in data:
        stop.notes = (data["notes"] or "").strip()
    if "extra_fee" in data:
        raw = data["extra_fee"]
        if raw in (None, ""):
            stop.extra_fee = None
        else:
            try:
                stop.extra_fee = Decimal(str(raw))
            except Exception:
                return JsonResponse({"success": False, "error": "Invalid extra_fee"}, status=400)
    if "requires_manual_review" in data:
        stop.requires_manual_review = bool(data["requires_manual_review"])
    if "start_time" in data:
        stop.start_time = _parse_time_or_none(data["start_time"])
    stop.save()

    new_fee = stop.extra_fee or Decimal("0.00")
    _apply_stop_fee_delta(leg, new_fee - old_fee)
    return JsonResponse({"success": True, "html": _render_leg_extras_panel(leg)})


@login_required
@require_POST
def delete_leg_stop(request, leg_id, stop_id):
    """Delete a LegStop and re-pack sequences so they stay 0-indexed."""
    deny = _staff_only_or_403(request)
    if deny:
        return deny
    leg = get_object_or_404(Leg, id=leg_id)
    stop = get_object_or_404(LegStop, id=stop_id, leg=leg)

    fee_to_remove = stop.extra_fee or Decimal("0.00")
    with transaction.atomic():
        stop.delete()
        # Re-pack sequences so subsequent inserts don't collide on unique_together(leg, sequence)
        for new_seq, remaining in enumerate(leg.legstop_set.order_by("sequence", "id")):
            if remaining.sequence != new_seq:
                remaining.sequence = new_seq
                remaining.save(update_fields=["sequence"])
    if fee_to_remove:
        _apply_stop_fee_delta(leg, -fee_to_remove)
    return JsonResponse({"success": True, "html": _render_leg_extras_panel(leg)})


@login_required
@require_POST
def add_leg_flight(request, leg_id):
    """Create a Flight + LegFlight in one call. If the leg has no controlling
    flight yet, the new flight becomes controlling."""
    deny = _staff_only_or_403(request)
    if deny:
        return deny
    leg = get_object_or_404(Leg, id=leg_id)

    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    airline = (data.get("airline") or "").strip()
    flight_number = (data.get("flight_number") or "").strip()
    if not (airline or flight_number):
        return JsonResponse({"success": False, "error": "Airline or flight number is required"}, status=400)

    flight_type = data.get("flight_type") or ""
    if flight_type not in ("", "arrival", "departure"):
        flight_type = ""

    with transaction.atomic():
        # Make sure any pre-existing legacy flight_information is wrapped in a
        # LegFlight first, otherwise it would be hidden once a second LegFlight
        # is added (the inline template only falls back to flight_information
        # when zero LegFlights exist).
        _sync_legacy_flight_information(leg)

        flight = Flight.objects.create(
            airline=airline,
            flight_number=flight_number,
            flight_type=flight_type,
        )
        has_controlling = leg.legflight_set.filter(is_controlling=True).exists() or bool(leg.flight_information_id)
        is_controlling = bool(data.get("is_controlling")) or not has_controlling

        if is_controlling:
            # Clear any existing controlling rows so the partial unique constraint holds
            leg.legflight_set.filter(is_controlling=True).update(is_controlling=False)

        LegFlight.objects.create(
            leg=leg,
            flight=flight,
            is_controlling=is_controlling,
            sequence=_next_legflight_sequence(leg),
        )
        # Mirror onto the legacy OneToOne if it's empty so existing readers see it.
        if is_controlling and not leg.flight_information_id:
            leg.flight_information = flight
            leg.save(update_fields=["flight_information"])

    return JsonResponse({"success": True, "html": _render_leg_extras_panel(leg)})


@login_required
@require_POST
def set_controlling_legflight(request, leg_id, legflight_id):
    """Mark one LegFlight as controlling; clear the flag on others for the same leg."""
    deny = _staff_only_or_403(request)
    if deny:
        return deny
    leg = get_object_or_404(Leg, id=leg_id)
    target = get_object_or_404(LegFlight, id=legflight_id, leg=leg)

    with transaction.atomic():
        leg.legflight_set.filter(is_controlling=True).update(is_controlling=False)
        target.is_controlling = True
        target.save(update_fields=["is_controlling"])
        # Keep the legacy OneToOne in sync with the controlling flight
        if leg.flight_information_id != target.flight_id:
            leg.flight_information = target.flight
            leg.save(update_fields=["flight_information"])

    return JsonResponse({"success": True, "html": _render_leg_extras_panel(leg)})


@login_required
@require_POST
def delete_leg_flight(request, leg_id, legflight_id):
    """Detach a flight from a leg. If it was the controlling row, promote
    another LegFlight to controlling (or clear flight_information if none left)."""
    deny = _staff_only_or_403(request)
    if deny:
        return deny
    leg = get_object_or_404(Leg, id=leg_id)
    target = get_object_or_404(LegFlight, id=legflight_id, leg=leg)
    flight = target.flight
    was_controlling = target.is_controlling

    with transaction.atomic():
        target.delete()
        if was_controlling:
            replacement = leg.legflight_set.order_by("sequence", "id").first()
            if replacement:
                replacement.is_controlling = True
                replacement.save(update_fields=["is_controlling"])
                if leg.flight_information_id != replacement.flight_id:
                    leg.flight_information = replacement.flight
                    leg.save(update_fields=["flight_information"])
            else:
                # No flights left — clear the legacy OneToOne
                if leg.flight_information_id:
                    leg.flight_information = None
                    leg.save(update_fields=["flight_information"])
        # If the flight is now orphaned (no other legs use it) and it was created
        # via this inline UI, garbage-collect it. We check both old and new sides.
        still_used = (
            Leg.objects.filter(flight_information=flight).exists()
            or LegFlight.objects.filter(flight=flight).exists()
        )
        if not still_used:
            try:
                flight.delete()
            except Exception:
                pass

    return JsonResponse({"success": True, "html": _render_leg_extras_panel(leg)})


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
    reservation_data = booking_data.get('reservation_data', {})

    # Resolve the reservation-level vehicle (used as override default placeholder)
    vehicle = None
    if reservation_data.get('manual_vehicle'):
        try:
            vehicle = Vehicle.objects.get(id=reservation_data['manual_vehicle'])
        except (Vehicle.DoesNotExist, ValueError, TypeError):
            vehicle = None
    vehicles = Vehicle.objects.all().order_by('vehicle_type')

    # Field names that can be overridden per-leg
    _OVR_FIELDS = (
        'vehicle', 'passenger_count', 'luggage_count', 'luggage_type',
        'need_carseats', 'rf_carseats', 'ff_carseats', 'booster_seats',
    )

    def _parse_overrides_from_post(post, leg_index):
        """Pull legs-N-override-<field> values from POST into a dict.
        Empty strings and None are dropped so we only persist real overrides."""
        out = {}
        for f in _OVR_FIELDS:
            key = f"legs-{leg_index}-override-{f}"
            val = post.get(key, '')
            if val is None:
                continue
            val = str(val).strip()
            if val == '':
                continue
            out[f] = val
        if out:
            out['has_any'] = True
            # Add a display name for vehicle for review screen
            if 'vehicle' in out:
                try:
                    v = Vehicle.objects.get(id=int(out['vehicle']))
                    out['vehicle_display'] = v.get_vehicle_type_display()
                except (Vehicle.DoesNotExist, ValueError, TypeError):
                    pass
        return out

    sanity_panel = None

    if request.method == "POST":
        leg_formset = DispatcherLegFormSet(request.POST, prefix='legs')
        flight_formset = DispatcherFlightFormSet(request.POST, prefix='flights')

        if leg_formset.is_valid() and flight_formset.is_valid():
            legs_data = []
            flights_data = []

            # Legs and flights are paired BY INDEX (flight form i belongs to leg
            # form i), so they must be collected together: collecting them in two
            # independent loops that skip blank forms shifts every later flight
            # onto the wrong leg (e.g. leg 1 without a flight steals leg 2's).
            # A leg without a flight keeps a {} placeholder to preserve alignment.
            flight_forms = list(flight_formset)
            for idx, form in enumerate(leg_formset):
                if form.cleaned_data and not form.cleaned_data.get('DELETE', False):
                    leg_data = {}
                    for field, value in form.cleaned_data.items():
                        if field != 'DELETE':
                            leg_data[field] = str(value) if value is not None else None
                    # Per-leg override fields (raw POST, not part of the formset)
                    overrides = _parse_overrides_from_post(request.POST, idx)
                    if overrides:
                        leg_data['overrides'] = overrides

                    flight_data = {}
                    if idx < len(flight_forms):
                        fform = flight_forms[idx]
                        if fform.cleaned_data and not fform.cleaned_data.get('DELETE', False):
                            for field, value in fform.cleaned_data.items():
                                if field != 'DELETE':
                                    flight_data[field] = str(value) if value is not None else None

                    legs_data.append(leg_data)
                    flights_data.append(flight_data)

            if not legs_data:
                messages.error(request, "At least one trip leg is required. Please add leg details.")
            else:
                # Sanity guards: wrong-date / AM-PM / flight-schedule checks.
                # Blocking warnings render once with an acknowledge checkbox;
                # the token pins the acknowledgment to THIS set of warnings, so
                # editing the form re-arms the gate if new issues appear.
                # Hard errors (Publix stop while the store is closed) have no
                # acknowledge path at all — the step can't proceed until fixed.
                from .booking_guards import (
                    check_publix_store_stop,
                    run_leg_sanity_checks,
                    warnings_token,
                )
                hard_errors = check_publix_store_stop(
                    legs_data,
                    store_stop=reservation_data.get('store_stop') == 'True',
                )
                sanity_warnings = hard_errors + run_leg_sanity_checks(legs_data, flights_data)
                blocking = [w for w in sanity_warnings if w['severity'] == 'warning']
                token = warnings_token(sanity_warnings)
                acknowledged = (
                    request.POST.get('sanity_ack') == '1'
                    and request.POST.get('sanity_ack_token') == token
                )

                if hard_errors or (blocking and not acknowledged):
                    sanity_panel = {
                        'warnings': sanity_warnings,
                        'token': token,
                        'blocking_count': len(blocking),
                        'error_count': len(hard_errors),
                        # All blockers are routine early-departure runs → the
                        # acknowledge box softens to a quick "AM, not PM" tick
                        # instead of "double-checked with the customer".
                        'light_ack': bool(blocking) and all(
                            w['code'] == 'early_morning_departure' for w in blocking
                        ),
                    }
                else:
                    booking_data['legs_data'] = legs_data
                    booking_data['flights_data'] = flights_data
                    # Carried to the review step so acknowledged warnings and
                    # verified-flight confirmations stay visible at confirm time.
                    booking_data['sanity_results'] = sanity_warnings
                    booking_data['sanity_acknowledged'] = bool(blocking)
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

    # Build per-leg override list for template (used to repopulate fields on
    # back-nav and to keep the override panel expanded when overrides are set).
    if request.method == "POST":
        leg_overrides = [_parse_overrides_from_post(request.POST, i) for i in range(num_legs)]
    else:
        saved_legs = booking_data.get('legs_data') or []
        leg_overrides = []
        for i in range(num_legs):
            if i < len(saved_legs) and isinstance(saved_legs[i], dict):
                leg_overrides.append(saved_legs[i].get('overrides') or {})
            else:
                leg_overrides.append({})

    context = {
        'leg_formset': leg_formset,
        'flight_formset': flight_formset,
        'customer': customer,
        'num_legs': num_legs,
        'step': 4,
        'step_title': 'Trip Details',
        'step_description': f'Enter details for {num_legs} trip leg(s)',
        'booking_data': booking_data,
        'reservation_data': reservation_data,
        'vehicle': vehicle,
        'vehicles': vehicles,
        'leg_overrides': leg_overrides,
        'sanity_panel': sanity_panel,
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
            additional_charges = form.cleaned_data.get('additional_charges') or Decimal('0.00')
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
    
    # Relative-day chip per leg ("TODAY" / "tomorrow" / "in 12 days") — gives
    # the dispatcher something to verify the date against at confirm time.
    today = timezone.localdate()
    for combined_leg in combined_legs:
        pd = combined_leg.get('pickup_date')
        if hasattr(pd, 'toordinal'):
            combined_leg['days_until'] = (pd - today).days

    sanity_results = booking_data.get('sanity_results') or []
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
        'booking_data': booking_data,
        'sanity_acknowledged_warnings': [w for w in sanity_results if w.get('severity') == 'warning'],
        'sanity_ok_results': [w for w in sanity_results if w.get('severity') == 'ok'],
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

    # Backstop for the legs-step hard block: a Publix grocery stop can't be
    # honored while the store is closed (9 PM-6 AM pickups). Normally caught
    # at the legs step; this covers stale session data / skipped re-submits.
    from .booking_guards import check_publix_store_stop
    if check_publix_store_stop(legs_data, store_stop=reservation_data.get('store_stop') == 'True'):
        raise ValueError(
            "This reservation includes a Publix grocery stop, but the pickup time "
            "is while the store is closed (9 PM-6 AM). Remove the grocery stop on "
            "the Details step or change the pickup time."
        )

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
            )
            if flight is not None:
                _sync_legacy_flight_information(leg)

            # Apply per-leg trip-detail overrides if any were captured in step 4
            overrides = leg_data.get('overrides') or {}
            if overrides:
                ovr_payload = {
                    k: v for k, v in overrides.items()
                    if k in ('vehicle', 'passenger_count', 'luggage_count', 'luggage_type',
                             'need_carseats', 'rf_carseats', 'ff_carseats', 'booster_seats')
                }
                if ovr_payload:
                    modified, ovr_err = _apply_leg_override_fields(leg, ovr_payload)
                    if ovr_err:
                        # Don't block creation for override issues — log via raise so the
                        # outer transaction rolls back and the dispatcher sees the error.
                        raise ValueError(f"Leg {i+1} override: {ovr_err}")
                    if modified:
                        leg.save(update_fields=modified)

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

        # Apply per-leg trip-detail overrides (vehicle, passenger_count, etc.)
        override_modified, override_error = _apply_leg_override_fields(leg, leg_data)
        if override_error:
            leg.delete()
            return JsonResponse({"success": False, "error": override_error}, status=400)
        if override_modified:
            leg.save(update_fields=override_modified)

        # Create flight information if provided
        if flight_data.get('airline') or flight_data.get('flight_number'):
            flight = Flight.objects.create(
                airline=flight_data.get('airline', ''),
                flight_number=flight_data.get('flight_number', '')
            )
            leg.flight_information = flight
            leg.save()
            _sync_legacy_flight_information(leg)

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
    active_tab = request.GET.get("tab", "all")
    if active_tab not in ("all", "inhouse", "affiliate"):
        active_tab = "all"
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
    from dispatching.models import SchedulerSettings
    settings = SchedulerSettings.get_settings()
    overdue_days = settings.driver_pay_overdue_days

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
        # Overdue if oldest unpaid leg exceeds configurable threshold
        if d.days_since_oldest is not None and d.days_since_oldest > overdue_days:
            d.is_overdue = True
            overdue_count += 1
        else:
            d.is_overdue = False

    inhouse_drivers = [d for d in drivers_with_unpaid if d.driver_type == 'inhouse']
    affiliate_drivers = [d for d in drivers_with_unpaid if d.driver_type == 'affiliate']
    combined_drivers = list(drivers_with_unpaid)
    total_inhouse_owed = sum(d.total_owed for d in inhouse_drivers)
    total_affiliate_owed = sum(d.total_owed for d in affiliate_drivers)
    total_owed_all = total_inhouse_owed + total_affiliate_owed

    if active_tab == "inhouse":
        tab_drivers = inhouse_drivers
        tab_total_owed = total_inhouse_owed
    elif active_tab == "affiliate":
        tab_drivers = affiliate_drivers
        tab_total_owed = total_affiliate_owed
    else:
        tab_drivers = combined_drivers
        tab_total_owed = total_owed_all

    # ── Detail mode: load legs for selected driver ──
    last_payment_info = None
    if selected_driver_id:
        try:
            selected_driver = get_object_or_404(Driver.objects.select_related('profile'), id=selected_driver_id)

            # Last payment info for detail header + recent payment history
            recent_payments = (
                DriverPayment.objects
                .filter(driver=selected_driver)
                .select_related('created_by')
                .order_by('-payment_date')[:10]
            )
            last_pmt = recent_payments[0] if recent_payments else None
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
                    "reservation__vehicle", "vehicle",
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

            # Detect stale legs: past pickup date but not completed (likely forgotten status update)
            stale_cutoff = today - timedelta(days=3)
            stale_legs_count = sum(
                1 for leg in legs
                if leg.status != 'completed' and leg.status != 'cancelled'
                and leg.pickup_date and leg.pickup_date <= stale_cutoff
            )

        except (ValueError, Driver.DoesNotExist):
            messages.error(request, "Invalid driver selected")
            selected_driver = None

    context = {
        # Overview data (always available)
        "inhouse_drivers": inhouse_drivers,
        "affiliate_drivers": affiliate_drivers,
        "combined_drivers": combined_drivers,
        "tab_drivers": tab_drivers,
        "tab_total_owed": tab_total_owed,
        "active_tab": active_tab,
        "all_count": len(combined_drivers),
        "inhouse_count": len(inhouse_drivers),
        "affiliate_count": len(affiliate_drivers),
        "total_inhouse_owed": total_inhouse_owed,
        "total_affiliate_owed": total_affiliate_owed,
        "total_owed_all": total_owed_all,
        "overdue_count": overdue_count,
        "overdue_days": overdue_days,
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
        "recent_payments": recent_payments if selected_driver_id and selected_driver else [],
        "stale_legs_count": stale_legs_count if selected_driver_id and selected_driver else 0,
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

        # Guard: this endpoint edits the leg's stored pay before a payment
        # is processed. Once the leg is on an active LegPayment line, edits
        # must go through the statement detail page so they create an
        # audit trail and recalculate the payment total.
        if leg.payment_status != "unpaid":
            return JsonResponse({
                "success": False,
                "error": (
                    "This leg has already been paid. Edit the amount on "
                    "the driver's payment statement instead — that path "
                    "keeps an audit trail."
                ),
            }, status=400)

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

                # Update reservation profit calculations
                try:
                    leg.reservation.update_profit_calculations()
                except Exception as e:
                    logger.warning(f"Could not update reservation profit calculations: {e}")

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

                # Update reservation profit calculations
                try:
                    leg.reservation.update_profit_calculations()
                except Exception as e:
                    logger.warning(f"Could not update reservation profit calculations: {e}")

                return JsonResponse({
                    "success": True,
                    "message": "Driver pay amount updated successfully",
                    "new_amount": float(leg.driver_pay_amount),
                })

        except (ValueError, TypeError) as e:
            return JsonResponse({"success": False, "error": f"Invalid amount format: {str(e)}"}, status=400)

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
      - driver_id (int, optional): recalc unpaid legs for this driver
      - leg_ids (list[int], optional): recalc specific legs
      - force (bool, optional): if true, recalculate even when pay is already set

    By default only touches legs where all pay fields are null/zero.
    With force=true, overwrites existing values (works in both modes).
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

    zero_pay_q = Q(
        Q(driver_base_pay__isnull=True) | Q(driver_base_pay=0),
        Q(driver_gratuity__isnull=True) | Q(driver_gratuity=0),
        Q(driver_additional__isnull=True) | Q(driver_additional=0),
        Q(driver_pay_amount__isnull=True) | Q(driver_pay_amount=0),
    )

    # Build queryset
    if leg_ids:
        legs_qs = Leg.objects.filter(id__in=leg_ids, driver__isnull=False)
        if not force:
            legs_qs = legs_qs.filter(zero_pay_q)
    else:
        legs_qs = Leg.objects.filter(
            driver_id=driver_id,
            payment_status='unpaid',
            driver__isnull=False,
        )
        if not force:
            legs_qs = legs_qs.filter(zero_pay_q)

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
        
        # Only process legs that have driver pay > 0
        # Use new fields (driver_base_pay + driver_gratuity + driver_additional)
        # with fallback to legacy driver_pay_amount for backward compatibility
        from django.db.models import Case, When, Value, DecimalField as DjDecimalField
        from django.db.models.functions import Coalesce

        unpaid_legs = unpaid_legs.annotate(
            _total_pay=Case(
                When(
                    driver_base_pay__isnull=False,
                    then=(
                        Coalesce('driver_base_pay', Value(0, output_field=DjDecimalField()))
                        + Coalesce('driver_gratuity', Value(0, output_field=DjDecimalField()))
                        + Coalesce('driver_additional', Value(0, output_field=DjDecimalField()))
                    ),
                ),
                default=Coalesce('driver_pay_amount', Value(0, output_field=DjDecimalField())),
                output_field=DjDecimalField(),
            )
        ).filter(_total_pay__gt=0)

        if not unpaid_legs.exists():
            return JsonResponse({
                "success": False,
                "error": "No completed unpaid legs with driver pay amount found for this driver"
            }, status=400)

        # Calculate total using the model property (handles field fallback correctly)
        payment_total = sum(leg.total_driver_pay for leg in unpaid_legs)

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
            leg_total = sum(leg.total_driver_pay for leg in legs)
            notes.append(
                f"\nReservation #{reservation.id} - {reservation.customer.get_full_name()}"
            )
            for leg in legs:
                notes.append(
                    f"  \u2022 {leg.pickup_date.strftime('%m/%d/%Y')} | "
                    f"{leg.pickup_location} \u2192 {leg.dropoff_location} | "
                    f"Payment: ${leg.total_driver_pay:.2f}"
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


# ── Gusto Smart Import CSV Export ────────────────────────────────────
#
# Final step AFTER the existing driver payment processing flow. Staff picks
# a payroll period (Mon–Sun by convention), reviews already-processed
# in-house DriverPayments for that period, ticks the rows they want, and
# downloads a CSV that uploads cleanly into Gusto's Smart Import.
#
# This view never touches calculation, never calls Gusto's API, and never
# marks anything as paid. It only reads finalized DriverPayments.


def _payroll_week_default(today=None):
    """Return (from_date, to_date) for the Mon–Sun week most recently completed.

    Payroll usually runs Sunday (or Monday). On any day of the week, the
    "current" payroll period is the Mon–Sun week ending on the most recent
    Sunday-or-today. We default to "last completed week" so the page is
    immediately useful on Monday morning.
    """
    from datetime import timedelta as _td
    if today is None:
        today = timezone.localdate()
    # Monday of the week containing `today`. weekday(): Mon=0 … Sun=6.
    this_monday = today - _td(days=today.weekday())
    last_monday = this_monday - _td(days=7)
    last_sunday = last_monday + _td(days=6)
    return last_monday, last_sunday


def _parse_period(request):
    """Pull from_date / to_date off the request. Returns (from_date, to_date, errors)."""
    from datetime import datetime as _dt
    errors = []
    raw_from = (request.GET.get("from_date") or request.POST.get("from_date") or "").strip()
    raw_to = (request.GET.get("to_date") or request.POST.get("to_date") or "").strip()
    if not raw_from or not raw_to:
        f, t = _payroll_week_default()
        return f, t, errors
    try:
        from_date = _dt.strptime(raw_from, "%Y-%m-%d").date()
        to_date = _dt.strptime(raw_to, "%Y-%m-%d").date()
    except ValueError:
        errors.append("Invalid date format. Use YYYY-MM-DD.")
        f, t = _payroll_week_default()
        return f, t, errors
    if from_date > to_date:
        errors.append("From date must be on or before To date.")
    return from_date, to_date, errors


@login_required(login_url="login")
def gusto_export_view(request):
    """Page + CSV download for Gusto Smart Import.

    GET  → render the eligibility table.
    POST → validate selected payment IDs and stream the CSV download.
    """
    if not request.user.is_staff:
        return redirect("home")

    from drivers.gusto_export import (
        build_rows_for_period,
        validate_selection,
        write_csv,
        csv_filename,
        GUSTO_CSV_HEADER,
    )
    from drivers.models import DriverPaymentExport

    from_date, to_date, period_errors = _parse_period(request)

    # ── POST: generate the CSV ──
    if request.method == "POST":
        for err in period_errors:
            messages.error(request, err)
        if period_errors:
            return redirect(f"{reverse('gusto_export')}?from_date={from_date}&to_date={to_date}")

        payment_ids = request.POST.getlist("payment_ids")
        if not payment_ids:
            messages.error(request, "Select at least one processed payment to export.")
            return redirect(f"{reverse('gusto_export')}?from_date={from_date}&to_date={to_date}")

        result = validate_selection(payment_ids, from_date, to_date)
        if result.errors and not result.valid_payments:
            for e in result.errors:
                messages.error(request, e)
            return redirect(f"{reverse('gusto_export')}?from_date={from_date}&to_date={to_date}")

        # Even with a mix of valid + blocked rows, we refuse the export rather
        # than silently dropping the bad ones. Staff must uncheck them.
        if result.errors:
            for e in result.errors:
                messages.error(request, e)
            messages.error(
                request,
                "Export cancelled — fix the blockers above (uncheck affiliate / zero / out-of-range "
                "rows) and try again."
            )
            return redirect(f"{reverse('gusto_export')}?from_date={from_date}&to_date={to_date}")

        # Build the CSV in memory (typical payroll has <50 rows — no streaming needed).
        buf = io.StringIO()
        write_csv(result.rows, buf)
        filename = csv_filename(from_date, to_date)

        total = sum((r.fixed_amount for r in result.rows), Decimal("0.00"))
        try:
            DriverPaymentExport.objects.create(
                created_by=request.user,
                from_date=from_date,
                to_date=to_date,
                csv_file_name=filename,
                selected_driver_count=len(result.rows),
                total_amount=total,
                exported_payment_ids=[r.payment.id for r in result.rows],
            )
        except Exception as e:
            # Don't block the download just because audit logging hiccupped —
            # but make some noise in the server logs.
            logger.warning(f"Failed to log DriverPaymentExport: {e}", exc_info=True)

        # utf-8-sig advertises the BOM `write_csv` already wrote — needed
        # for Gusto's Smart Import parser to recognize the header row.
        response = HttpResponse(buf.getvalue(), content_type="text/csv; charset=utf-8-sig")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    # ── GET: render the page ──
    for err in period_errors:
        messages.error(request, err)

    rows = build_rows_for_period(from_date, to_date)

    # Annotate "statement emailed?" by looking at EmailLog metadata.
    # One query covers every payment shown on the page.
    statement_emailed_payment_ids: set = set()
    if rows:
        try:
            from ops.models import EmailLog
            payment_id_strs = [str(r.payment.id) for r in rows]
            payment_id_ints = [r.payment.id for r in rows]
            email_qs = EmailLog.objects.filter(
                email_type="driver_statement",
                success=True,
            ).filter(
                Q(metadata__payment_id__in=payment_id_ints)
                | Q(metadata__payment_id__in=payment_id_strs)
            ).values_list("metadata", flat=True)
            for md in email_qs:
                pid = md.get("payment_id") if isinstance(md, dict) else None
                if pid is None:
                    continue
                try:
                    statement_emailed_payment_ids.add(int(pid))
                except (TypeError, ValueError):
                    pass
        except Exception as e:
            logger.warning(f"Gusto export: failed to look up statement-emailed status: {e}")

    eligible_count = sum(1 for r in rows if r.is_eligible)
    eligible_total = sum((r.fixed_amount for r in rows if r.is_eligible), Decimal("0.00"))

    recent_exports = DriverPaymentExport.objects.select_related("created_by").all()[:10]

    context = {
        "from_date": from_date,
        "to_date": to_date,
        "rows": rows,
        "eligible_count": eligible_count,
        "eligible_total": eligible_total,
        "statement_emailed_payment_ids": statement_emailed_payment_ids,
        "recent_exports": recent_exports,
        "header_columns": GUSTO_CSV_HEADER,
    }
    return render(request, "dispatching/gusto_export.html", context)


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
            # Unassign drivers from legs being refunded. A refund is a FACT —
            # it always writes live, even while the day is held in a draft
            # (the draft's live-change awareness surfaces it to the drafter).
            affected_legs = Leg.objects.filter(id__in=leg_ids, driver__isnull=False)
            dates_to_invalidate = set()
            with sanctioned_live_write():
                for leg in affected_legs:
                    dates_to_invalidate.add(leg.pickup_date.isoformat())
                    leg.driver = None
                    leg.save(update_fields=['driver'])
            for date_str in dates_to_invalidate:
                cache.delete(f"capacity_planner_{date_str}")
        elif refund_type == 'full_cancellation':
            refund_request.legs.set(reservation.legs.all())
            # Unassign drivers from all legs (fact-write — see above)
            dates_to_invalidate = set()
            with sanctioned_live_write():
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
    type_filter = request.GET.get("type", "")
    # "Review" tab: completed Full Cancellations — the ones worth eyeballing for
    # a dispatcher who full-cancelled when they meant a partial / price adjustment.
    review_mode = request.GET.get("review") == "full_cancellations"

    base_qs = RefundRequest.objects.select_related(
        'reservation',
        'reservation__customer',
        'requested_by',
        'processed_by',
    ).prefetch_related('legs')

    if review_mode:
        refund_requests = base_qs.filter(
            status='completed', refund_type='full_cancellation'
        ).order_by('-processed_at')
    else:
        if status_filter:
            refund_requests = base_qs.filter(status=status_filter)
        else:
            refund_requests = base_qs.filter(status__in=['requested', 'processing', 'approved'])
        if type_filter:
            refund_requests = refund_requests.filter(refund_type=type_filter)
        refund_requests = refund_requests.order_by('-requested_at')

    status_counts = {
        'requested': RefundRequest.objects.filter(status='requested').count(),
        'processing': RefundRequest.objects.filter(status='processing').count(),
        'approved': RefundRequest.objects.filter(status='approved').count(),
        'completed': RefundRequest.objects.filter(status='completed').count(),
        'rejected': RefundRequest.objects.filter(status='rejected').count(),
        'full_cancel_review': RefundRequest.objects.filter(
            status='completed', refund_type='full_cancellation'
        ).count(),
    }

    context = {
        'refund_requests': refund_requests,
        'status_filter': status_filter,
        'type_filter': type_filter,
        'review_mode': review_mode,
        'status_counts': status_counts,
    }

    return render(request, "dispatching/refund_management.html", context)


def _process_stripe_refund(reservation, refund_amount, idem_prefix=None):
    """
    Helper: process Stripe refund across paid payments. Returns (refunded_amount, errors, stripe_ids).

    Caps each payment at its REMAINING (amount - already-refunded) headroom, not its full
    amount, so a partially-refunded payment (still status='paid') can't be over-refunded.
    Pass idem_prefix to make each Stripe refund idempotent against a double-submit.
    """
    paid_payments = reservation.payments.filter(status='paid').order_by('-created_at')
    refunded_amount = Decimal('0.00')
    refund_errors = []
    stripe_ids = []

    for payment in paid_payments:
        if refunded_amount >= refund_amount:
            break

        # Remaining headroom on THIS payment (a partial refund leaves status='paid').
        available = payment.amount - (payment.refunded_amount or Decimal('0.00'))
        if available <= 0:
            continue

        remaining_to_refund = refund_amount - refunded_amount
        amount_to_refund = min(remaining_to_refund, available)

        try:
            if not payment.stripe_payment_intent_id:
                refund_errors.append(f"Payment #{payment.id} has no Stripe payment intent ID")
                continue

            _refund_kwargs = {}
            if idem_prefix:
                _refund_kwargs["idempotency_key"] = (
                    f"{idem_prefix}-{payment.id}-{int(amount_to_refund * 100)}"
                )
            refund = stripe.Refund.create(
                payment_intent=payment.stripe_payment_intent_id,
                amount=int(amount_to_refund * 100),
                reason='requested_by_customer',
                **_refund_kwargs,
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


def _execute_refund_approval(rr, user, refund_notes=""):
    """
    Execute an approved refund for a single, already-loaded RefundRequest whose
    refund_type is final. Shared by process_refund (single) and
    bulk_approve_refunds (many) so the money path never drifts between them.

    Runs the Stripe refund (idempotency-keyed on the request), applies the
    type-specific side effects (cancel legs / reservation for partial & full),
    syncs the reservation's flat refund fields, and invalidates the capacity
    cache. Returns a plain dict the caller turns into a response; it does not
    raise for expected conditions.
    """
    reservation = rr.reservation

    refund_amount = rr.amount
    if not refund_amount or refund_amount <= 0:
        return {"ok": False, "status": 400, "error": "No refund amount set"}

    # Idempotency guard: atomically claim this request. If it isn't in an active
    # state right now (already completed, or being processed elsewhere), bail
    # instead of re-running the Stripe refunds.
    claimed = RefundRequest.objects.filter(
        id=rr.id, status__in=['requested', 'processing', 'approved'],
    ).update(status='processing')
    if not claimed:
        return {"ok": False, "status": 409, "error": "This refund request was already processed."}
    rr.refresh_from_db()

    # Process Stripe refund (idempotency-keyed so a retry can't double-refund).
    refunded_amount, refund_errors, stripe_ids = _process_stripe_refund(
        reservation, refund_amount, idem_prefix=f"refund-{rr.id}"
    )

    if refund_errors and refunded_amount == 0:
        # Total failure — release the claim so it can be retried, then report.
        RefundRequest.objects.filter(id=rr.id).update(status='requested')
        return {"ok": False, "status": 500,
                "error": f"Failed to process refund: {'; '.join(refund_errors)}"}

    # Partial failure: keep going but record the shortfall in the notes.
    if refund_errors:
        _shortfall = f"[PARTIAL REFUND — refunded ${refunded_amount} of ${refund_amount}; errors: {'; '.join(refund_errors)}]"
        refund_notes = (refund_notes + "\n" + _shortfall).strip() if refund_notes else _shortfall

    rr.stripe_refund_ids = stripe_ids

    dates_to_invalidate = set()

    if rr.refund_type == 'price_adjustment':
        rr.status = 'completed'
        rr.processed_by = user
        rr.processed_at = timezone.now()
        rr.notes = refund_notes
        rr.save()

    elif rr.refund_type == 'partial_cancellation':
        # Cancel selected legs, keep reservation active. Cancellation is a FACT —
        # always live, even on a held (drafted) day.
        legs_to_cancel = rr.legs.all()
        with sanctioned_live_write():
            for leg in legs_to_cancel:
                dates_to_invalidate.add(leg.pickup_date.isoformat())
                leg.status = 'cancelled'
                leg.payment_status = 'canceled'
                leg.driver = None
                leg.save(update_fields=['status', 'payment_status', 'driver'])

        rr.status = 'completed'
        rr.processed_by = user
        rr.processed_at = timezone.now()
        rr.notes = refund_notes
        rr.save()

        # If ALL legs are now cancelled, cancel the reservation too.
        active_legs = reservation.legs.exclude(status='cancelled')
        if not active_legs.exists():
            reservation.status = 'cancelled'

    elif rr.refund_type == 'full_cancellation':
        # Cancel all legs + reservation (fact-write — always live).
        with sanctioned_live_write():
            for leg in reservation.legs.all():
                dates_to_invalidate.add(leg.pickup_date.isoformat())
                if leg.status != 'cancelled':
                    leg.status = 'cancelled'
                    leg.payment_status = 'canceled'
                    leg.driver = None
                    leg.save(update_fields=['status', 'payment_status', 'driver'])

        reservation.status = 'cancelled'

        rr.status = 'completed'
        rr.processed_by = user
        rr.processed_at = timezone.now()
        rr.notes = refund_notes
        rr.save()

    # Sync flat fields on Reservation.
    reservation.refund_status = 'completed'
    reservation.refund_processed_by = user
    reservation.refund_processed_at = timezone.now()
    reservation.refund_notes = refund_notes
    if refund_errors:
        reservation.refund_notes = (refund_notes or "") + f"\n\nRefund processing notes: {'; '.join(refund_errors)}"
    reservation.save()

    # Invalidate capacity planner cache for affected dates.
    for date_str in dates_to_invalidate:
        cache.delete(f"capacity_planner_{date_str}")

    logger.info(
        f"Refund #{rr.id} ({rr.refund_type}) processed for reservation {reservation.id} "
        f"by {user.username}. Amount: ${refunded_amount}"
    )

    return {
        "ok": True, "status": 200,
        "message": f"Refund processed successfully. Amount refunded: ${refunded_amount}",
        "refunded_amount": refunded_amount,
        "warnings": refund_errors if refund_errors else None,
    }


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
        result = _execute_refund_approval(rr, request.user, refund_notes)
        if not result["ok"]:
            return JsonResponse({"success": False, "error": result["error"]}, status=result["status"])
        return JsonResponse({
            "success": True,
            "message": result["message"],
            "warnings": result.get("warnings"),
        })

    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON data"}, status=400)
    except Exception as e:
        logger.error(f"Error processing refund: {str(e)}", exc_info=True)
        return JsonResponse({"success": False, "error": f"Server error: {str(e)}"}, status=500)


@login_required
@require_POST
def bulk_approve_refunds(request):
    """
    Superuser: approve several ALREADY-REQUESTED refunds in one batch.

    Full Cancellations are deliberately excluded — they wipe an entire
    reservation and must be approved one at a time (with the "CANCEL ALL"
    guardrail) so a batch can never mass-cancel bookings by accident. Each
    selected request is processed independently through the SAME money path as
    single approval (_execute_refund_approval); one failure never aborts the
    others.
    """
    if not request.user.is_superuser:
        return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

    try:
        data = json.loads(request.body)
        raw_ids = data.get("refund_request_ids", []) or []
        try:
            ids = [int(x) for x in raw_ids]
        except (ValueError, TypeError):
            return JsonResponse({"success": False, "error": "Invalid selection."}, status=400)
        if not ids:
            return JsonResponse({"success": False, "error": "No refunds selected."}, status=400)

        results = []
        for rr in RefundRequest.objects.select_related('reservation').filter(id__in=ids):
            if rr.refund_type == 'full_cancellation':
                results.append({
                    "id": rr.id, "reservation": rr.reservation.id, "ok": False,
                    "error": "Full Cancellations must be approved individually.",
                })
                continue
            if rr.status != 'requested':
                results.append({
                    "id": rr.id, "reservation": rr.reservation.id, "ok": False,
                    "error": f"Not awaiting approval (status: {rr.get_status_display()}).",
                })
                continue

            res = _execute_refund_approval(rr, request.user)
            results.append({
                "id": rr.id,
                "reservation": rr.reservation.id,
                "ok": res["ok"],
                "error": res.get("error"),
                "amount": str(res.get("refunded_amount")) if res["ok"] else None,
            })

        approved = [r for r in results if r["ok"]]
        failed = [r for r in results if not r["ok"]]

        logger.info(
            f"Bulk refund approval by {request.user.username}: "
            f"{len(approved)} approved, {len(failed)} skipped/failed (ids={ids})."
        )

        return JsonResponse({
            "success": True,
            "approved_count": len(approved),
            "failed_count": len(failed),
            "results": results,
            "message": (
                f"Approved {len(approved)} refund(s)."
                + (f" {len(failed)} were skipped (see details)." if failed else "")
            ),
        })

    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON data"}, status=400)
    except Exception as e:
        logger.error(f"Error in bulk refund approval: {str(e)}", exc_info=True)
        return JsonResponse({"success": False, "error": f"Server error: {str(e)}"}, status=500)


def _prior_leg_state(leg):
    """
    Recover a leg's status + payment_status from just before it was cancelled,
    using simple_history. When a leg is cancelled, the pre-cancel snapshot is the
    most recent historical row whose status was NOT 'cancelled'.

    Falls back to sensible active defaults (in-progress / paid) if no such history
    row exists — a refunded leg was, by definition, paid for at some point.
    Driver is intentionally NOT recovered here; correction leaves it unassigned
    for manual reassignment on the board.
    """
    try:
        prior = (
            leg.history.exclude(status='cancelled')
            .order_by('-history_date', '-history_id')
            .first()
        )
    except Exception:
        prior = None

    if prior is not None:
        return (prior.status or 'in-progress'), (prior.payment_status or 'paid')
    return 'in-progress', 'paid'


@login_required
@require_POST
def correct_refund(request):
    """
    Superuser correction for a PROCESSED refund whose type was wrong — e.g. a
    dispatcher chose "Full Cancellation" when it should have been a Price
    Adjustment or Partial Cancellation, which wrongly cancelled every leg and the
    whole reservation.

    Restores the wrongly-cancelled legs (status + payment_status recovered from
    history; driver left blank for manual reassignment), reactivates the
    reservation, and reclassifies the RefundRequest — appending an audit note.

    It deliberately does NOT touch Stripe: money already refunded stays refunded.
    This only repairs the operational state so the trip runs again; any
    over-refund is re-collected separately as a new charge.
    """
    if not request.user.is_superuser:
        return JsonResponse({"success": False, "error": "Unauthorized"}, status=403)

    try:
        data = json.loads(request.body)
        refund_request_id = data.get("refund_request_id")
        new_type = data.get("new_refund_type")
        keep_cancelled_leg_ids = data.get("keep_cancelled_leg_ids", []) or []
        note = (data.get("note") or "").strip()

        if new_type not in ('price_adjustment', 'partial_cancellation'):
            return JsonResponse(
                {"success": False, "error": "Corrected type must be Price Adjustment or Partial Cancellation."},
                status=400,
            )

        rr = get_object_or_404(
            RefundRequest.objects.select_related('reservation'), id=refund_request_id
        )
        reservation = rr.reservation

        if rr.status != 'completed':
            return JsonResponse(
                {"success": False, "error": "Only a completed refund can be corrected."}, status=400
            )
        if rr.refund_type != 'full_cancellation':
            return JsonResponse(
                {"success": False, "error": "Only a Full Cancellation refund can be corrected here."},
                status=400,
            )

        # The legs the full-cancel took down are exactly the ones currently
        # cancelled on this reservation.
        cancelled_legs = list(reservation.legs.filter(status='cancelled'))

        try:
            keep_ids = {int(x) for x in keep_cancelled_leg_ids}
        except (ValueError, TypeError):
            return JsonResponse({"success": False, "error": "Invalid leg selection."}, status=400)

        # Partial correction with no leg left cancelled is really a price
        # adjustment — guard against a no-op "partial".
        if new_type == 'partial_cancellation' and not keep_ids:
            return JsonResponse(
                {"success": False, "error": "Select at least one leg to keep cancelled, or choose Price Adjustment."},
                status=400,
            )

        dates_to_invalidate = set()
        restored_ids = []
        kept_ids = []
        with sanctioned_live_write():
            for leg in cancelled_legs:
                dates_to_invalidate.add(leg.pickup_date.isoformat())
                if new_type == 'partial_cancellation' and leg.id in keep_ids:
                    kept_ids.append(leg.id)
                    continue  # genuinely cancelled — leave it
                prior_status, prior_payment = _prior_leg_state(leg)
                leg.status = prior_status
                leg.payment_status = prior_payment
                # driver intentionally left as-is (unassigned) for manual reassignment
                leg.save(update_fields=['status', 'payment_status'])
                restored_ids.append(leg.id)

        # Reactivate the reservation if it now has any active leg.
        if reservation.status == 'cancelled' and reservation.legs.exclude(status='cancelled').exists():
            reservation.status = 'confirmed'

        # Reclassify the refund request + rebuild its leg set.
        rr.refund_type = new_type
        if new_type == 'partial_cancellation':
            rr.legs.set([l for l in cancelled_legs if l.id in keep_ids])
        else:
            rr.legs.clear()

        type_label = dict(RefundRequest.REFUND_TYPE_CHOICES)[new_type]
        stamp = timezone.now().strftime('%Y-%m-%d %H:%M')
        who = request.user.get_full_name() or request.user.username
        audit = (
            f"[CORRECTED {stamp} by {who}] Full Cancellation → {type_label}. "
            f"Restored {len(restored_ids)} leg(s)"
            + (f", kept {len(kept_ids)} cancelled" if kept_ids else "")
            + f". Drivers left unassigned for manual reassignment. "
            f"Stripe refund of ${rr.amount} was NOT changed."
        )
        if note:
            audit += f" Note: {note}"
        rr.notes = (rr.notes + "\n" + audit).strip() if rr.notes else audit
        rr.save(update_fields=['refund_type', 'notes'])

        # Keep the reservation's flat refund fields honest for the detail view.
        reservation.refund_notes = (
            (reservation.refund_notes + "\n" + audit).strip()
            if reservation.refund_notes else audit
        )
        reservation.save()

        for date_str in dates_to_invalidate:
            cache.delete(f"capacity_planner_{date_str}")

        logger.info(
            f"Refund #{rr.id} corrected (full_cancellation → {new_type}) by {request.user.username}. "
            f"Restored legs: {restored_ids}; kept cancelled: {kept_ids}"
        )

        return JsonResponse({
            "success": True,
            "message": (
                f"Refund corrected to {type_label}. {len(restored_ids)} leg(s) restored"
                + (f", {len(kept_ids)} kept cancelled" if kept_ids else "")
                + f". Reassign drivers on the board. The ${rr.amount} already refunded "
                f"via Stripe was not changed — re-collect any over-refund separately."
            ),
            "restored_leg_ids": restored_ids,
        })

    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON data"}, status=400)
    except Exception as e:
        logger.error(f"Error correcting refund: {str(e)}", exc_info=True)
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
        build_sharer_partners,
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
            "reservation__vehicle", "vehicle",
            "reservation__travel_agent",
            "reservation__travel_agent__agency",  # for Leg.is_vip agency-keyword check (no N+1)
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
            # Prefetched so reservation.payment_status reads live state without N+1.
            # The denormalized `is_paid` column is unreliable on older rows, so we
            # walk payments instead.
            Prefetch(
                "reservation__payments",
                queryset=Payment.objects.order_by('-created_at'),
            ),
        )
        .order_by("pickup_time")
    )
    legs_list = list(legs)

    # ── Sandbox draft overlay (shared with board + dashboard) ──
    # When the day is held, re-point each leg's in-memory driver to its proposed
    # draft value so capacity/coverage/suggestions reflect the PROPOSED world.
    # Live Leg.driver in the DB is untouched.
    _draft_ctx = _draft_view_context(request, selected_date)
    _apply_draft_overlay(_draft_ctx["draft"], legs_list, None)

    # Get drivers — inactive drivers are excluded from the planner + vehicle
    # assignment (they remain visible only in the driver directory).
    inhouse_drivers = (
        Driver.objects.filter(driver_type="inhouse", is_active=True)
        .select_related("profile")
        .prefetch_related(
            "weekly_schedule", "date_overrides",
            "certified_vehicle_types", "preferred_vehicle_types", "preferred_vehicles",
        )
        .order_by("profile__first_name")
    )
    all_drivers = Driver.objects.select_related("profile").all()

    # Memoize effective availability per driver for this request (DISP-01).
    # It's pure for a given (driver, date) — weekly_schedule + date_overrides are
    # prefetched above — but is otherwise recomputed for the same eligible driver
    # in both the vehicle_assign_rows loop and the eligible-driver timeline loop.
    _cp_eff_cache = {}
    def _cp_get_eff(driver):
        if driver.id not in _cp_eff_cache:
            _cp_eff_cache[driver.id] = driver.get_effective_availability(selected_date)
        return _cp_eff_cache[driver.id]

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

    # Fleet vehicles for quick-assign panel.
    # Units marked out of service FOR THE SELECTED DATE are split off rather than
    # dropped: a dispatcher who can't find #7 will go looking for it, so the pool
    # shows it greyed with the reason and refuses the drop. Per-date, so a car in
    # the shop this week still appears normally on next week's board.
    inhouse_vehicles = _annotate_vehicle_status(
        list(FleetVehicle.objects.filter(is_active=True)
             .select_related("vehicle_type").order_by("vehicle_number")),
        selected_date,
    )
    # Down units sort last but stay in the pool — see the card markup for why.
    inhouse_vehicles.sort(key=lambda v: bool(v.oos_label))
    out_of_service_count = sum(1 for v in inhouse_vehicles if v.oos_label)

    # Build vehicle_assign_rows with off-today and leg count info
    _planner_leg_counts = {}
    for _leg in legs_list:
        if _leg.driver_id:
            _planner_leg_counts[_leg.driver_id] = _planner_leg_counts.get(_leg.driver_id, 0) + 1

    vehicle_assign_rows = []
    for d in inhouse_drivers:
        _assignment = assignment_map.get(d.id)
        # Driver availability for this date — combines weekly + active exception
        _va_eff = _cp_get_eff(d)
        _va_is_avail = _va_eff["is_available"]
        # Underlying schedule state — preserved so the UI can flag override-working drivers
        _was_scheduled_off = not _va_is_avail
        _is_off = _was_scheduled_off
        # If driver has a vehicle assigned today, treat them as working regardless of schedule.
        # _is_off flips to False, but _was_scheduled_off retains the original signal so the
        # card can warn dispatch that this person was supposed to be off.
        if _is_off and _assignment and _assignment.vehicle_id:
            _is_off = False
        _va_sh, _va_eh, _va_pref, _va_flex = (
            _va_eff["start_hour"], _va_eff["end_hour"],
            _va_eff["preference"], _va_eff["flexible"],
        )

        _VA_PREF_SHORT = {
            "prefer_arrival": "Pref Arrivals", "prefer_return": "Pref Returns",
            "prefer_cruise": "Pref Cruises", "heavy_arrival": "Heavy Arrivals",
            "heavy_return": "Heavy Returns", "heavy_cruise": "Heavy Cruises",
            "only_arrival": "Only Arrivals", "only_return": "Only Returns",
            "only_cruise": "Only Cruises",
        }

        _va_vnotes = ''
        if _assignment and _assignment.vehicle:
            _va_vnotes = _assignment.vehicle.notes or ''

        _va_stype = _va_eff.get("shift_type", "full_day")
        _va_mhrs = _va_eff.get("max_hours")
        _va_shift_disp = _va_eff["display_label"] if _va_is_avail else ''
        if _va_is_avail and _va_mhrs and _va_eff["status"] != "limited":
            _va_shift_disp += f" ({int(_va_mhrs)}h)"

        vehicle_assign_rows.append({
            "driver": d,
            "assignment": _assignment,
            # Same reason as the legs dashboard: a car marked down after it was
            # assigned must go red on the driver's card, not only in the pool.
            "vehicle_oos_label": (
                _assignment.vehicle.out_of_service_label(selected_date)
                if _assignment and _assignment.vehicle else ""
            ),
            "is_off_today": _is_off,
            "was_scheduled_off": _was_scheduled_off,
            "leg_count": _planner_leg_counts.get(d.id, 0),
            "shift_display": _va_shift_disp,
            "shift_type": _va_stype,
            "shift_start": _va_sh,
            "shift_end": _va_eh,
            "flexible": _va_flex,
            "max_hours": float(_va_mhrs) if _va_mhrs else None,
            "preference": _va_pref,
            "pref_short": _VA_PREF_SHORT.get(_va_pref, ''),
            "driver_notes": d.notes or '',
            "driver_phone": d.phone_number or '',
            "vehicle_notes": _va_vnotes,
            "preferred_shift": _va_eff.get("preferred_shift", ""),
            "scheduling_notes": _va_eff.get("scheduling_notes", ""),
            "avail_status": _va_eff["status"],
            "avail_tooltip": _va_eff["tooltip"],
            "exception_notes": _va_eff["exception_notes"],
            "has_exception": _va_eff["has_exception"],
            "exc_badge": format_exception_badge(_va_eff),
            "cert_labels": d.cert_labels(),
            "sprinter_ok": bool(d.cert_labels()),
            "pref_vehicle": d.preferred_vehicle_label(),
            "shift_pref_label": format_shift_preference(_va_eff),
        })

    # Sort: assigned drivers first (by vehicle number), then unassigned, off last
    vehicle_assign_rows.sort(
        key=lambda r: (
            2 if r["is_off_today"] else (1 if r["assignment"] is None else 0),
            r["assignment"].vehicle.vehicle_number if r["assignment"] and r["assignment"].vehicle else "",
        )
    )
    va_off_count = sum(1 for r in vehicle_assign_rows if r["is_off_today"])

    # Heavy scheduling computation — cache for 60s keyed by date.
    # LocMemCache (single worker) stores Python objects directly; no serialization needed.
    # Suggestions reference leg IDs, not ORM instances, so cached results are safe to reuse.
    _sched_cache_key = f"capacity_planner_{selected_date.isoformat()}"
    # While a day is held, bypass the cache so the proposed-overlay state is always
    # fresh (and never pollutes the live-state cache).
    _is_held = _draft_ctx["is_held"]
    _cached_sched = None if _is_held else cache.get(_sched_cache_key)

    _unassigned_legs = [leg for leg in legs_list if leg.driver is None]

    if _cached_sched is not None:
        driver_schedules, suggestions, coverage = _cached_sched
    else:
        driver_schedules = build_driver_schedules(legs_list, all_drivers, selected_date)
        _inhouse_for_suggestions = {did: s for did, s in driver_schedules.items() if s.driver_type == 'inhouse'}
        # Shared-car gate: a driver who splits one physical unit with a partner can't be
        # offered a job that overlaps the partner's jobs, even if his own calendar is free.
        _sharer_partners = build_sharer_partners(set(_inhouse_for_suggestions), selected_date)
        # Same turn buffer Auto-Assign will apply (Guard B'). There is no per-run choice on
        # a page load, so this is the saved default plus any per-driver overrides. Without
        # it the page suggested a driver at zero slack that Auto-Assign would then decline —
        # the inline hint and the button disagreeing about the same leg.
        from dispatching.scheduler import resolve_run_min_buffer, load_driver_min_buffers
        _sugg_buffer = resolve_run_min_buffer(None)
        suggestions = suggest_assignments_clustered(
            _unassigned_legs, _inhouse_for_suggestions, selected_date,
            sharer_partners=_sharer_partners,
            min_buffer=_sugg_buffer,
            driver_min_buffers=load_driver_min_buffers(list(_inhouse_for_suggestions)))
        coverage = get_coverage_stats(legs_list)
        if not _is_held:
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
    # O(1) leg lookup so the gap chips can re-anchor a turn on the RECORDED pickup
    # (see _gap_turn_slack) exactly like the dispatch board does.
    _cp_leg_by_id = {_l.id: _l for _l in legs_list}
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
            # Earliest 'picked-up' tap, naive local to match the gap math. _sh_list is
            # newest-first, so overwriting keeps the EARLIEST — the true start.
            _cp_picked_up_local = None
            for _sh in _sh_list:
                if _sh.status == 'picked-up':
                    _cp_picked_up_local = timezone.localtime(_sh.timestamp).replace(tzinfo=None)
            _cp_leg_status_map[_cpleg.id] = {
                'status_label': _status_label,
                'status_time': _local_ts.strftime('%I:%M %p').lstrip('0'),
                'status_ago': _ago_str,
                'picked_up_dt': _cp_picked_up_local,
            }

    # Get previous day's last leg per driver (for overnight turnaround display).
    # Covers ALL in-house drivers, not just ones already assigned a vehicle —
    # the assignment board uses last-night clear times to decide who gets a car.
    _cp_prev_day = selected_date - timedelta(days=1)
    _cp_prev_day_last = {}
    _cp_prev_day_late = {}
    _cp_prev_legs = (
        Leg.objects.filter(pickup_date=_cp_prev_day, driver__in=inhouse_drivers)
        .exclude(status="cancelled")
        # reservation + flight_information are read by estimate_job_end_time
        # (store_stop, flight arrival) — pull them in to avoid a .get() per leg.
        .select_related("driver", "reservation", "flight_information")
        .order_by("driver_id", "-pickup_time")
    )
    for _cpl in _cp_prev_legs:
        if _cpl.driver_id not in _cp_prev_day_last:
            try:
                _cp_end = estimate_job_end_time(_cpl, _cp_prev_day)
                _cp_prev_day_last[_cpl.driver_id] = _cp_end.strftime('%I:%M %p').lstrip('0')
                _cp_prev_day_late[_cpl.driver_id] = _cp_end.hour >= 21
            except Exception:
                _cp_prev_day_last[_cpl.driver_id] = _cpl.pickup_time.strftime('%I:%M %p').lstrip('0') + '?'
                _cp_prev_day_late[_cpl.driver_id] = _cpl.pickup_time.hour >= 21

    # Get previous day's vehicle assignments
    _cp_prev_day_vehicle = {}
    _cp_prev_assigns = DriverVehicleAssignment.objects.filter(
        date=_cp_prev_day, driver__in=inhouse_drivers
    ).select_related('vehicle', 'vehicle__vehicle_type')
    for _cpda in _cp_prev_assigns:
        if _cpda.vehicle:
            _vn = _cpda.vehicle.vehicle_number or ''
            _vt = str(_cpda.vehicle.vehicle_type) if _cpda.vehicle.vehicle_type else ''
            _cp_prev_day_vehicle[_cpda.driver_id] = f"#{_vn} {_vt}".strip() if _vn else _vt

    # Surface previous-night clear time on the assignment cards too (not just timelines)
    for _var in vehicle_assign_rows:
        _var["prev_night_cleared"] = _cp_prev_day_last.get(_var["driver"].id, "")
        _var["prev_night_late"] = _cp_prev_day_late.get(_var["driver"].id, False)
        _var["prev_night_vehicle"] = _cp_prev_day_vehicle.get(_var["driver"].id, "")

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
            # Same feasibility-backed banding as the dispatch board's timeline (see
            # _gap_turn_slack) — these two gap blocks are the same chip and must not
            # drift apart again, including the recorded-pickup re-anchor.
            _prev_sinfo = _cp_leg_status_map.get(sched.slots[i].leg_id)
            _turn_band = pickup_policy.turn_band(_gap_turn_slack(
                sched.slots[i], sched.slots[i + 1], selected_date,
                prev_leg=_cp_leg_by_id.get(sched.slots[i].leg_id),
                prev_picked_up_dt=(_prev_sinfo.get('picked_up_dt') if _prev_sinfo else None)))
            gaps.append({
                'after_leg': sched.slots[i].leg_id,
                'before_leg': sched.slots[i + 1].leg_id,
                'gap_minutes': gap_min,
                'gap_display': gap_display,
                'is_tight': _turn_band == 'tight',
                'is_critical': _turn_band == 'critical',
                'position_pct': gap_pos,
                'width_pct': gap_width,
            })

        _cp_assign = assignment_map.get(driver.id)
        _cp_vnum = ''
        _cp_vtype = ''
        _cp_vnotes = ''
        if _cp_assign and _cp_assign.vehicle:
            _cp_vnum = _cp_assign.vehicle.vehicle_number or ''
            _cp_vnotes = _cp_assign.vehicle.notes or ''
            if _cp_assign.vehicle.vehicle_type:
                _cp_vtype = str(_cp_assign.vehicle.vehicle_type)

        # Driver availability for selected date
        _cp_avail = driver.get_availability_for_date(selected_date)
        _cp_is_avail, _cp_sh, _cp_eh, _cp_pref, _cp_flex = _cp_avail
        _cp_eff = _cp_get_eff(driver)

        _CP_PREF_SHORT = {
            "prefer_arrival": "Pref Arrivals", "prefer_return": "Pref Returns",
            "prefer_cruise": "Pref Cruises", "heavy_arrival": "Heavy Arrivals",
            "heavy_return": "Heavy Returns", "heavy_cruise": "Heavy Cruises",
            "only_arrival": "Only Arrivals", "only_return": "Only Returns",
            "only_cruise": "Only Cruises",
        }

        _cp_stype = _cp_eff.get("shift_type", "full_day")
        _cp_mhrs = _cp_eff.get("max_hours")
        _cp_shift_disp = _cp_eff["display_label"] if _cp_eff["is_available"] else "Off"
        if _cp_eff["is_available"] and _cp_mhrs and _cp_eff["status"] != "limited":
            _cp_shift_disp += f" ({int(_cp_mhrs)}h)"

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
            'shift_display': _cp_shift_disp,
            'shift_type': _cp_stype,
            'shift_start': _cp_sh,
            'shift_end': _cp_eh,
            'flexible': _cp_flex,
            'max_hours': float(_cp_mhrs) if _cp_mhrs else None,
            'preference': _cp_pref,
            'pref_short': _CP_PREF_SHORT.get(_cp_pref, ''),
            'driver_notes': driver.notes or '',
            'driver_phone': driver.phone_number or '',
            'vehicle_notes': _cp_vnotes,
            'preferred_shift': _cp_eff.get("preferred_shift", ""),
            'scheduling_notes': _cp_eff.get("scheduling_notes", ""),
            'avail_status': _cp_eff["status"],
            'avail_tooltip': _cp_eff["tooltip"],
            'exception_notes': _cp_eff["exception_notes"],
            'has_exception': _cp_eff["has_exception"],
            'exc_badge': format_exception_badge(_cp_eff),
            'avail_blocks': availability_block_bands(_cp_eff, display_start, total_display_minutes),
            'cert_labels': driver.cert_labels(),
            'sprinter_ok': bool(driver.cert_labels()),
            'pref_vehicle': driver.preferred_vehicle_label(),
            'shift_pref_label': format_shift_preference(_cp_eff),
        })

    # Build per-driver availability for the selected date (for auto-assign modal defaults)
    driver_availability = {}
    for d in eligible_drivers:
        is_avail, start_h, end_h, pref, flex = d.get_availability_for_date(selected_date)
        _entry = {
            "is_available": is_avail,
            "start_hour": start_h,
            "end_hour": end_h,
            "preference": pref,
            "flexible": flex,
        }
        # Planned shared-car window (Day Setup AM/PM split): the partitioned hours OVERRIDE
        # the saved-availability prefill and force non-flexible, so the engine's hard
        # window machinery keeps the two drivers off the same car at the same time.
        _a = assignment_map.get(d.id)
        if _a is not None and _a.planned_start_hour is not None and _a.planned_end_hour is not None:
            _entry.update({
                "is_available": True,
                "start_hour": _a.planned_start_hour,
                "end_hour": _a.planned_end_hour,
                "flexible": False,
                "planned_share": True,
            })
        driver_availability[d.id] = _entry

    from dispatching.models import SchedulerSettings as _SchedSettings
    _sched_settings = _SchedSettings.get_settings()

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
        'out_of_service_count': out_of_service_count,
        'vehicle_assign_rows': vehicle_assign_rows,
        'va_off_count': va_off_count,
        'driver_availability_json': json.dumps(driver_availability),
        # Shown as the "Use default (N min)" label on the builder / auto-assign buffer
        # controls, so the dispatcher can see what "default" means without opening settings.
        'default_min_turn_buffer': _sched_settings.min_turn_buffer,
        # ── Sandbox draft context (banner, review modal, controls) ──
        **_draft_ctx,
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


# ──────────────────────────────────────────────────────────────────────────
# Sandbox scheduling (draft / review / publish)
#
# While a date has an active ScheduleDraft, dispatcher edits for that date are
# routed into the DraftAssignment overlay instead of Leg.driver — so nothing
# reaches drivers until a manager publishes. The two helpers below are the gate
# (is this date held?) and the overlay writer (never calls leg.save()).
# ──────────────────────────────────────────────────────────────────────────

# NOTE: _active_draft_for_date / _log_draft_event / _upsert_draft_assignment /
# can_use_sandbox moved to dispatching/assignment.py (the write front door);
# imported at the top of this module under the same names.


def _driver_label(driver):
    """Human label for a Driver (full name, else username)."""
    if not driver:
        return None
    try:
        return driver.profile.get_full_name() or driver.profile.username
    except Exception:
        return f"Driver {driver.id}"


def _user_label(user):
    """Human label for an auth User (full name, else username)."""
    if not user:
        return None
    try:
        return user.get_full_name() or user.username
    except Exception:
        return None


def _fmt_t(hhmm):
    """Format an 'HH:MM' string (or time) as '7:30 AM'."""
    try:
        if hasattr(hhmm, "strftime"):
            t = hhmm
        else:
            from datetime import datetime as _dt
            t = _dt.strptime(hhmm, "%H:%M").time()
        return t.strftime("%I:%M %p").lstrip("0")
    except Exception:
        return str(hhmm or "")


def _compute_draft_diff(draft):
    """Diff the draft overlay against the live schedule for the manager review.

    Returns reassignments / new_assignments / unassignments (what publishing will
    change for drivers) plus needs_attention (legs booked after the draft opened
    that have no proposed driver yet — the live-merge surface). Each row keeps the
    Leg object (for template rendering) alongside scalar fields.
    """
    from reservations.models import DraftAssignment

    legs = (
        Leg.objects.filter(pickup_date=draft.schedule_date)
        .exclude(reservation__status="cancelled")
        .exclude(status="cancelled")
        .select_related("driver", "driver__profile", "driver_assigned_by",
                        "reservation", "reservation__customer")
        .order_by("pickup_time")
    )
    overlay = {
        da.leg_id: da
        for da in DraftAssignment.objects.filter(draft=draft).select_related(
            "proposed_driver", "proposed_driver__profile", "assigned_by"
        )
    }
    baseline = set(draft.baseline_leg_ids or [])
    base_legs = draft.baseline_legs or {}   # per-leg fields at hold time

    reassignments, new_assignments, unassignments, needs_attention = [], [], [], []
    live_conflicts, field_changes = [], []

    def _row(leg, live_driver, proposed):
        customer = ""
        try:
            if leg.reservation and leg.reservation.customer:
                customer = leg.reservation.customer.get_full_name()
        except Exception:
            pass
        return {
            "leg": leg,
            "leg_id": leg.id,
            "time": leg.pickup_time,
            "pickup": leg.pickup_location,
            "dropoff": leg.dropoff_location,
            "customer": customer,
            "live_driver": _driver_label(live_driver),
            "proposed_driver": _driver_label(proposed),
        }

    for leg in legs:
        b = base_legs.get(str(leg.id))
        da = overlay.get(leg.id)
        live_driver = leg.driver
        live_id = live_driver.id if live_driver else None

        # ── pickup-time changed live since hold (informational; publish doesn't
        #    touch time, so this is a notice, not a publish blocker) ──
        if b and b.get("pickup_time"):
            cur_t = leg.pickup_time.strftime("%H:%M") if leg.pickup_time else None
            if cur_t and cur_t != b["pickup_time"]:
                fc = _row(leg, live_driver, da.proposed_driver if da else None)
                fc["field"] = "Pickup time"
                fc["old"] = _fmt_t(b["pickup_time"])
                fc["new"] = _fmt_t(leg.pickup_time)
                field_changes.append(fc)

        if da is not None:
            proposed = da.proposed_driver
            prop_id = proposed.id if proposed else None
            base_id = b.get("driver_id") if b else None
            is_conflict = (live_id != base_id) and (live_id != prop_id)
            if is_conflict:
                # Someone changed the live driver under this staged leg. Show the
                # full picture: who's live now (+when) and what you staged (+when).
                # Publishing will overwrite the live driver with your staged pick.
                cb = leg.driver_assigned_by
                row = _row(leg, live_driver, proposed)          # live_driver=live, proposed=your pick
                row["changed_by"] = _user_label(cb)
                row["changed_at"] = leg.driver_assigned_at
                row["staged_by"] = _user_label(da.assigned_by)
                row["staged_at"] = da.assigned_at
                live_conflicts.append(row)
            elif prop_id != live_id:
                row = _row(leg, live_driver, proposed)
                if live_driver and proposed:
                    reassignments.append(row)
                elif proposed and not live_driver:
                    new_assignments.append(row)
                else:  # live_driver and not proposed
                    unassignments.append(row)
        else:
            # No draft opinion. If the leg appeared after the draft opened and is
            # still unassigned, flag it for triage (live-merge).
            if leg.id not in baseline and not live_driver:
                needs_attention.append(_row(leg, live_driver, None))

    # Attribute time changes to whoever last edited the leg since the hold opened.
    if field_changes:
        try:
            changed_ids = [fc["leg_id"] for fc in field_changes]
            latest = {}
            for h in (Leg.history.filter(id__in=changed_ids, history_date__gt=draft.created_at)
                      .select_related("history_user").order_by("id", "-history_date")):
                if h.id not in latest:
                    latest[h.id] = h
            for fc in field_changes:
                h = latest.get(fc["leg_id"])
                if h:
                    fc["changed_by"] = _user_label(h.history_user)
                    fc["changed_at"] = h.history_date
        except Exception:
            pass

    return {
        "reassignments": reassignments,
        "new_assignments": new_assignments,
        "unassignments": unassignments,
        "needs_attention": needs_attention,
        "live_conflicts": live_conflicts,
        "field_changes": field_changes,
        "proposed_change_count": len(reassignments) + len(new_assignments) + len(unassignments),
        "needs_attention_count": len(needs_attention),
        "live_conflict_count": len(live_conflicts),
        "field_change_count": len(field_changes),
    }


def _apply_draft_overlay(draft, legs, drivers):
    """Re-point each leg's IN-MEMORY driver to its effective draft value (never saved)
    and tag it for the template's draft visual treatment. Live Leg.driver is untouched.

    Sets on every leg (non-underscore names so Django templates can read them):
    `draft_proposed` (staged change shown as proposed), `draft_new_attention`
    (booked after hold, still unassigned), `draft_live_conflict` (a staged leg whose
    LIVE driver was changed under the draft — we then show the LIVE driver, not the
    proposed one, plus `draft_staged_label`/`draft_staged_at` = your pick & when, and
    `draft_live_by_label`/`draft_live_at` = who set live & when), and
    `draft_time_changed`/`draft_old_time` (pickup time moved live since hold).
    Shared by the board, dashboard and planner so all three render the same world.
    """
    from reservations.models import DraftAssignment
    for leg in legs:
        leg.draft_live_driver = leg.driver
        leg.draft_proposed = False
        leg.draft_new_attention = False
        leg.draft_live_conflict = False
        leg.draft_staged_label = None
        leg.draft_staged_at = None
        leg.draft_live_by_label = None
        leg.draft_live_at = None
        leg.draft_time_changed = False
        leg.draft_old_time = None
    if draft is None:
        return
    overlay = {
        da.leg_id: da
        for da in DraftAssignment.objects.filter(draft=draft).select_related(
            "proposed_driver", "proposed_driver__profile"
        )
    }
    baseline = set(draft.baseline_leg_ids or [])
    base_legs = draft.baseline_legs or {}
    by_id = {d.id: d for d in (drivers or [])}
    for leg in legs:
        b = base_legs.get(str(leg.id))
        # ── field-change awareness: pickup time moved live since hold ──
        if b and b.get("pickup_time"):
            cur_t = leg.pickup_time.strftime("%H:%M") if leg.pickup_time else None
            if cur_t and cur_t != b["pickup_time"]:
                leg.draft_time_changed = True
                leg.draft_old_time = b["pickup_time"]
        # ── driver overlay ──
        da = overlay.get(leg.id)
        if da is not None:
            prop = da.proposed_driver
            live_id = leg.driver.id if leg.driver else None   # current LIVE driver
            prop_id = prop.id if prop else None
            base_id = b.get("driver_id") if b else None
            if live_id != base_id and live_id != prop_id:
                # Someone changed the live driver under this staged leg. Show the
                # LIVE driver (reality) and remember your staged pick + who/when.
                leg.draft_live_conflict = True
                leg.draft_staged_label = _driver_label(prop)
                leg.draft_staged_at = da.assigned_at
                leg.draft_live_by_label = _user_label(leg.driver_assigned_by)
                leg.draft_live_at = leg.driver_assigned_at
                # leave leg.driver = live (do NOT re-point to the proposed value)
            else:
                leg.draft_proposed = prop_id != live_id
                leg.driver = by_id.get(prop_id, prop) if prop else None
        else:
            leg.draft_new_attention = (leg.id not in baseline) and (leg.driver is None)


def _draft_view_context(request, selected_date):
    """Shared draft banner/review context for any dispatcher page (board, dashboard,
    planner). Does NOT mutate legs — call _apply_draft_overlay() separately to render
    the proposed assignments. Returns a dict ready to merge into the page context.
    """
    from reservations.models import ScheduleDraft as _SD
    is_manager = request.user.is_superuser

    # Users without the sandbox grant edit the live schedule exactly as before:
    # no banner, no overlay, no "held" — return a fully inert context.
    if not can_use_sandbox(request.user):
        return {
            "selected_date": selected_date, "draft": None, "is_held": False,
            "draft_state": None, "draft_holder": None, "is_manager": is_manager,
            "board_locked": False, "draft_diff": None, "draft_events": [],
            "proposed_change_count": 0, "needs_attention_count": 0,
            "live_conflict_count": 0, "field_change_count": 0,
            "published_draft": None, "today": timezone.localdate(),
            "can_sandbox": False,
        }

    draft = _active_draft_for_date(selected_date)
    is_held = draft is not None
    draft_diff = _compute_draft_diff(draft) if is_held else None
    draft_events = list(draft.events.select_related("actor").all()) if is_held else []
    published_draft = None
    if not is_held:
        published_draft = (
            _SD.objects.filter(schedule_date=selected_date, state=_SD.State.PUBLISHED, notified_at__isnull=True)
            .order_by("-published_at").first()
        )
    return {
        "selected_date": selected_date,
        "draft": draft,
        "is_held": is_held,
        "draft_state": draft.state if is_held else None,
        "draft_holder": draft.created_by if is_held else None,
        "is_manager": is_manager,
        "board_locked": bool(is_held and draft.state == _SD.State.IN_REVIEW and not is_manager),
        "draft_diff": draft_diff,
        "draft_events": draft_events,
        "proposed_change_count": draft_diff["proposed_change_count"] if draft_diff else 0,
        "needs_attention_count": draft_diff["needs_attention_count"] if draft_diff else 0,
        "live_conflict_count": draft_diff["live_conflict_count"] if draft_diff else 0,
        "field_change_count": draft_diff["field_change_count"] if draft_diff else 0,
        "published_draft": published_draft,
        "today": timezone.localdate(),
        "can_sandbox": True,
    }


# ── Draft lifecycle endpoints ──────────────────────────────────────────────

@login_required
@require_POST
def open_draft(request):
    """Put a date on HOLD: open a sandbox draft so further edits stage privately."""
    if not can_use_sandbox(request.user):
        return JsonResponse({"success": False, "error": "You don't have access to the scheduling sandbox"}, status=403)
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

    from reservations.models import ScheduleDraft, ScheduleDraftEvent

    existing = _active_draft_for_date(target_date)
    if existing:
        return JsonResponse({
            "success": True, "draft_id": existing.id, "state": existing.state,
            "already_held": True, "message": f"{date_str} is already held.",
        })

    with transaction.atomic():
        hold_legs = list(
            Leg.objects.filter(pickup_date=target_date)
            .exclude(reservation__status="cancelled").exclude(status="cancelled")
        )
        baseline_ids = [l.id for l in hold_legs]
        # Snapshot schedule-critical fields at hold time, so we can later show what
        # a non-sandbox user changed LIVE while the draft was open (driver/time/date).
        baseline_legs = {
            str(l.id): {
                "driver_id": l.driver_id,
                "pickup_time": l.pickup_time.strftime("%H:%M") if l.pickup_time else None,
                "pickup_date": l.pickup_date.isoformat() if l.pickup_date else None,
                "pickup_location": l.pickup_location or "",
                "dropoff_location": l.dropoff_location or "",
            }
            for l in hold_legs
        }
        snapshot = _create_schedule_snapshot(target_date, request.user, 'manual')
        draft = ScheduleDraft.objects.create(
            schedule_date=target_date,
            state=ScheduleDraft.State.DRAFT,
            base_snapshot=snapshot,
            baseline_leg_ids=baseline_ids,
            baseline_legs=baseline_legs,
            created_by=request.user,
        )
        _log_draft_event(draft, ScheduleDraftEvent.EventType.CREATED, actor=request.user)

    return JsonResponse({
        "success": True, "draft_id": draft.id, "state": draft.state,
        "message": f"{date_str} is now held — edits are private until published.",
    })


@login_required
@require_POST
def submit_draft(request):
    """Dispatcher submits a draft for manager review."""
    if not can_use_sandbox(request.user):
        return JsonResponse({"success": False, "error": "You don't have access to the scheduling sandbox"}, status=403)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    from reservations.models import ScheduleDraft, ScheduleDraftEvent
    draft = ScheduleDraft.objects.filter(id=data.get("draft_id")).first()
    if not draft:
        return JsonResponse({"success": False, "error": "Draft not found"}, status=404)
    if draft.state not in (ScheduleDraft.State.DRAFT, ScheduleDraft.State.CHANGES_REQUESTED):
        return JsonResponse({"success": False, "error": "Draft is not in an editable state"}, status=409)

    draft.state = ScheduleDraft.State.IN_REVIEW
    draft.submitted_by = request.user
    draft.submitted_at = timezone.now()
    draft.save(update_fields=["state", "submitted_by", "submitted_at"])
    _log_draft_event(draft, ScheduleDraftEvent.EventType.SUBMITTED, actor=request.user,
                     note=(data.get("note") or "").strip())
    return JsonResponse({"success": True, "state": draft.state, "message": "Submitted for manager review."})


@login_required
@require_POST
def reject_draft(request):
    """Manager requests changes (rejects) with required notes."""
    if not request.user.is_superuser:
        return JsonResponse({"success": False, "error": "Manager approval required"}, status=403)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    note = (data.get("note") or "").strip()
    if not note:
        return JsonResponse({"success": False, "error": "A note explaining the requested changes is required"}, status=400)

    from reservations.models import ScheduleDraft, ScheduleDraftEvent
    draft = ScheduleDraft.objects.filter(id=data.get("draft_id")).first()
    if not draft:
        return JsonResponse({"success": False, "error": "Draft not found"}, status=404)
    if draft.state != ScheduleDraft.State.IN_REVIEW:
        return JsonResponse({"success": False, "error": "Draft is not awaiting review"}, status=409)

    draft.state = ScheduleDraft.State.CHANGES_REQUESTED
    draft.reviewed_by = request.user
    draft.reviewed_at = timezone.now()
    draft.save(update_fields=["state", "reviewed_by", "reviewed_at"])
    _log_draft_event(draft, ScheduleDraftEvent.EventType.REJECTED, actor=request.user, note=note)
    return JsonResponse({"success": True, "state": draft.state, "message": "Changes requested."})


@login_required
@require_POST
def discard_draft(request):
    """Discard a draft without publishing. Drivers are unaffected (live untouched)."""
    if not can_use_sandbox(request.user):
        return JsonResponse({"success": False, "error": "You don't have access to the scheduling sandbox"}, status=403)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    from reservations.models import ScheduleDraft, ScheduleDraftEvent
    draft = ScheduleDraft.objects.filter(id=data.get("draft_id")).first()
    if not draft:
        return JsonResponse({"success": False, "error": "Draft not found"}, status=404)
    if not draft.is_active:
        return JsonResponse({"success": False, "error": "Draft is already closed"}, status=409)

    # Managers may discard any active draft; dispatchers only their own un-submitted draft.
    allowed = request.user.is_superuser or (
        draft.created_by_id == request.user.id and draft.state == ScheduleDraft.State.DRAFT
    )
    if not allowed:
        return JsonResponse({"success": False, "error": "Only a manager can discard a submitted draft"}, status=403)

    draft.state = ScheduleDraft.State.DISCARDED
    draft.save(update_fields=["state"])
    _log_draft_event(draft, ScheduleDraftEvent.EventType.DISCARDED, actor=request.user)
    return JsonResponse({"success": True, "state": draft.state, "message": "Draft discarded. Live schedule unchanged."})


@login_required
def draft_review(request):
    """Manager-only JSON: the publish diff + the event timeline for a draft."""
    if not request.user.is_superuser:
        return JsonResponse({"success": False, "error": "Manager approval required"}, status=403)

    from reservations.models import ScheduleDraft
    draft = ScheduleDraft.objects.filter(id=request.GET.get("draft_id")).first()
    if not draft:
        return JsonResponse({"success": False, "error": "Draft not found"}, status=404)

    diff = _compute_draft_diff(draft)

    def _serialize_rows(rows):
        return [{
            "leg_id": r["leg_id"],
            "time": r["time"].strftime("%I:%M %p").lstrip("0") if r["time"] else "",
            "pickup": r["pickup"], "dropoff": r["dropoff"], "customer": r["customer"],
            "live_driver": r["live_driver"], "proposed_driver": r["proposed_driver"],
        } for r in rows]

    events = [{
        "type": e.event_type,
        "type_display": e.get_event_type_display(),
        "actor": (e.actor.get_full_name() or e.actor.username) if e.actor else "System",
        "note": e.note,
        "metadata": e.metadata,
        "when": timezone.localtime(e.created_at).strftime("%b %d, %I:%M %p").replace(" 0", " "),
    } for e in draft.events.select_related("actor").all()]

    return JsonResponse({
        "success": True,
        "draft_id": draft.id,
        "state": draft.state,
        "schedule_date": draft.schedule_date.isoformat(),
        "reassignments": _serialize_rows(diff["reassignments"]),
        "new_assignments": _serialize_rows(diff["new_assignments"]),
        "unassignments": _serialize_rows(diff["unassignments"]),
        "needs_attention": _serialize_rows(diff["needs_attention"]),
        "proposed_change_count": diff["proposed_change_count"],
        "needs_attention_count": diff["needs_attention_count"],
        "events": events,
    })


@login_required
@require_POST
def publish_draft(request):
    """Manager approves & publishes: apply the overlay onto live Leg.driver."""
    if not request.user.is_superuser:
        return JsonResponse({"success": False, "error": "Manager approval required"}, status=403)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    force = bool(data.get("force"))
    from reservations.models import ScheduleDraft, ScheduleDraftEvent

    with transaction.atomic():
        draft = ScheduleDraft.objects.select_for_update().filter(id=data.get("draft_id")).first()
        if not draft:
            return JsonResponse({"success": False, "error": "Draft not found"}, status=404)
        # A manager may publish from in_review/changes_requested, or directly from
        # their own draft (skip-review path).
        publishable = draft.state in (ScheduleDraft.State.IN_REVIEW, ScheduleDraft.State.CHANGES_REQUESTED) or (
            draft.state == ScheduleDraft.State.DRAFT
        )
        if not publishable:
            return JsonResponse({"success": False, "error": "Draft is not in a publishable state"}, status=409)

        # ── Conflict detection (three-way: baseline vs live-now vs proposed) ──
        base = {}
        if draft.base_snapshot_id:
            base = {e.leg_id: e.driver_id for e in draft.base_snapshot.entries.all()}
        deltas = list(draft.assignments.select_related("leg", "leg__reservation", "proposed_driver"))
        touched_ids = [d.leg_id for d in deltas]
        live_now = dict(Leg.objects.filter(id__in=touched_ids).values_list("id", "driver_id"))

        conflicts = []
        for d in deltas:
            # Baseline = the leg's driver when the draft opened. base_snapshot only
            # records ASSIGNED legs, so a leg missing from it was unassigned (or did
            # not exist) at hold → baseline None. We must NOT skip those: if someone
            # set them live since (e.g. a non-sandbox dispatcher assigned a fresh
            # leg), publishing the overlay would silently overwrite that change.
            base_driver = base.get(d.leg_id)            # None = unassigned/new at hold
            live_driver = live_now.get(d.leg_id)        # current live value
            if live_driver != base_driver and live_driver != d.proposed_driver_id:
                conflicts.append({
                    "leg_id": d.leg_id,
                    "base_driver": base_driver,
                    "live_now": live_driver,
                    "draft_wants": d.proposed_driver_id,
                })
        if conflicts:
            # Enrich with human labels + WHO changed the live value (from
            # Leg.driver_assigned_by), so the publisher can reconcile — e.g.
            # "9:00 MCO→Universal: draft wants Carlos, but Mike was set live by
            # Person Y". Driver pay/identity is read-only here.
            from drivers.models import Driver as _Drv
            _ids = set()
            for c in conflicts:
                _ids.update([c["base_driver"], c["live_now"], c["draft_wants"]])
            _ids.discard(None)
            _labels = {dv.id: _driver_label(dv) for dv in _Drv.objects.filter(id__in=_ids).select_related("profile")}
            _legmeta = {
                l.id: l for l in Leg.objects.filter(id__in=[c["leg_id"] for c in conflicts])
                .select_related("driver_assigned_by")
            }
            for c in conflicts:
                c["base_label"] = _labels.get(c["base_driver"]) or "Unassigned"
                c["live_label"] = _labels.get(c["live_now"]) or "Unassigned"
                c["wants_label"] = _labels.get(c["draft_wants"]) or "Unassigned"
                lm = _legmeta.get(c["leg_id"])
                if lm:
                    cb = lm.driver_assigned_by
                    c["changed_by"] = (cb.get_full_name() or cb.username) if cb else None
                    c["time"] = lm.pickup_time.strftime("%I:%M %p").lstrip("0") if lm.pickup_time else ""
                    c["route"] = f"{lm.pickup_location or ''} → {lm.dropoff_location or ''}".strip(" →")
        if conflicts and not force:
            _log_draft_event(draft, ScheduleDraftEvent.EventType.CONFLICT, actor=request.user, conflicts=conflicts)
            return JsonResponse({"success": False, "conflicts": conflicts,
                                 "error": "The live schedule changed under this draft. Review the conflicts."}, status=409)

        # ── Apply overlay onto live ──
        now = timezone.now()
        applied = 0
        skipped = 0
        affected = set()
        for d in deltas:
            leg = d.leg
            # Skip legs cancelled or drifted off this date since the draft opened.
            if leg.status == "cancelled" or leg.reservation.status == "cancelled":
                skipped += 1
                continue
            if leg.pickup_date != draft.schedule_date:
                _log_draft_event(draft, ScheduleDraftEvent.EventType.CONFLICT, actor=request.user,
                                 leg_id=leg.id, reason="leg_moved_off_date")
                skipped += 1
                continue
            old_driver_id = leg.driver_id
            if old_driver_id == d.proposed_driver_id:
                continue  # no-op; never re-save (avoids redundant pay/NTFY)
            leg.driver = d.proposed_driver
            leg.driver_assigned_by = d.assigned_by or request.user
            leg.driver_assigned_at = now
            leg._reassigned_by = request.user
            leg._status_change_user = request.user
            # FULL save (no update_fields) so pay/gratuity/night-bonus/NTFY recompute now.
            # Publish IS the sanctioned live application of the draft.
            with sanctioned_live_write():
                leg.save()
            applied += 1
            if old_driver_id:
                affected.add(old_driver_id)
            if d.proposed_driver_id:
                affected.add(d.proposed_driver_id)

        draft.state = ScheduleDraft.State.PUBLISHED
        draft.reviewed_by = draft.reviewed_by or request.user
        draft.reviewed_at = draft.reviewed_at or now
        draft.published_by = request.user
        draft.published_at = now
        draft.save(update_fields=["state", "reviewed_by", "reviewed_at", "published_by", "published_at"])
        _log_draft_event(draft, ScheduleDraftEvent.EventType.PUBLISHED, actor=request.user,
                         applied=applied, affected_driver_ids=sorted(affected))
        cache.delete(f"capacity_planner_{draft.schedule_date.isoformat()}")

    return JsonResponse({
        "success": True, "applied": applied, "skipped": skipped,
        "affected_driver_count": len(affected),
        "message": f"Published {applied} assignment(s) to drivers.",
    })


@login_required
@require_POST
def notify_drivers(request):
    """Manager-triggered Twilio text to drivers affected by a published draft."""
    if not request.user.is_superuser:
        return JsonResponse({"success": False, "error": "Manager approval required"}, status=403)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    from reservations.models import ScheduleDraft

    with transaction.atomic():
        draft = ScheduleDraft.objects.select_for_update().filter(id=data.get("draft_id")).first()
        if not draft:
            return JsonResponse({"success": False, "error": "Draft not found"}, status=404)
        if draft.state != ScheduleDraft.State.PUBLISHED:
            return JsonResponse({"success": False, "error": "Only a published schedule can be sent to drivers"}, status=409)
        if draft.notified_at:
            return JsonResponse({"success": True, "already_sent": True, "message": "Drivers were already notified."})
        # Claim before spawning the background send so a double-click can't double-send.
        draft.notified_by = request.user
        draft.notified_at = timezone.now()
        draft.save(update_fields=["notified_by", "notified_at"])

    from .confirmation_sms import notify_drivers_of_release
    _run_in_background(notify_drivers_of_release, draft.id, actor_id=request.user.id)
    return JsonResponse({"success": True, "message": "Texting affected drivers in the background."})


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
    raw_driver_hours = data.get("driver_hours", {})  # {driver_id_str: {start: int, end: int, flexible: bool}}
    raw_build_first = data.get("build_first", [])  # [driver_id, ...] — seed these drivers' full day FIRST
    excluded_leg_ids = data.get("excluded_leg_ids", [])  # legs to skip
    raw_manual = data.get("manual_assignments", {})  # {leg_id_str: driver_id} overrides
    raw_preferences = data.get("driver_preferences", {})  # {driver_id_str: "prefer_arrival"}
    apply_driver_ids = data.get("apply_driver_ids", None)  # optional: only apply for these drivers
    # Treat unpaid reservations as if they don't exist for auto-assign — manual overrides
    # still apply. Mirrors the schedule builder's "Skip unpaid" toggle.
    exclude_unpaid = bool(data.get("exclude_unpaid", False))
    # Turn buffer for THIS run (Guard B'): spare minutes the engine must leave between two
    # jobs on top of the drive. None/absent => the saved SchedulerSettings default. A
    # per-driver typed number still beats it (see load_driver_min_buffers below).
    _raw_min_buffer = data.get("min_buffer", None)
    try:
        run_min_buffer = None if _raw_min_buffer in (None, "") else max(0, int(_raw_min_buffer))
    except (TypeError, ValueError):
        run_min_buffer = None

    from datetime import datetime as dt
    from dispatching.scheduler import (
        build_driver_schedules, suggest_assignments_clustered,
        ScheduleSlot, estimate_job_end_time, preload_timing_cache as _preload_cache,
        resolve_run_min_buffer, load_driver_min_buffers,
    )
    from dispatching.analytics import categorize_location
    from copy import deepcopy
    from decimal import Decimal
    # Pre-load the route-timing cache once (1 query) so the build + pre-farm swap + gap-compaction
    # passes don't each fall back to per-leg RouteTimingMetric DB hits (~1,500 queries → 1).
    # Mirrors capacity_planner; independent of the USE_LIVE_DISTANCE setting.
    _preload_cache()

    try:
        target_date = dt.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return JsonResponse({"success": False, "error": "Invalid date format"}, status=400)

    # Get all legs for this date (exclude cancelled reservations).
    # legstop_set / legflight_set are prefetched: build_driver_schedules counts them per
    # leg and runs once PER PASS (greedy + swap + evict + rescue + trim + gap), so without
    # the prefetch each rebuild fired 2 COUNT queries per leg — the dominant assign-all N+1
    # (thousands of round-trips on a busy day). flight_information is select_related so the
    # arrival clearing/chain estimates don't lazy-load it per leg either.
    legs = list(
        Leg.objects.filter(pickup_date=target_date)
        .exclude(reservation__status="cancelled")
        .exclude(status="cancelled")
        .select_related("driver", "driver__profile", "reservation", "reservation__vehicle", "vehicle",
                        "reservation__customer", "flight_information")
        .prefetch_related(
            Prefetch("reservation__payments", queryset=Payment.objects.order_by('-created_at')),
            "legstop_set", "legflight_set",
        )
    )

    # Get inhouse drivers with vehicle assignments for this date
    eligible_driver_ids = set(
        DriverVehicleAssignment.objects.filter(
            date=target_date, driver__driver_type="inhouse"
        ).values_list("driver_id", flat=True)
    )
    inhouse_drivers = list(
        Driver.objects.filter(driver_type="inhouse", is_active=True, id__in=eligible_driver_ids)
        .select_related("profile")
        .prefetch_related("weekly_schedule", "date_overrides")
    )

    # Resolve the turn buffer ONCE for the whole run (settings lookup + one query for the
    # per-driver overrides), then hand the pair to every seating pass below.
    run_min_buffer = resolve_run_min_buffer(run_min_buffer)
    driver_min_buffers = load_driver_min_buffers([d.id for d in inhouse_drivers])

    # ── Driver availability ──
    # When the dispatcher uses the Auto-Assign modal it sends `driver_hours` ONLY for the drivers
    # who are working (ones marked "Off" are omitted) — so the modal is AUTHORITATIVE: any eligible
    # driver NOT in the payload is treated as OFF and excluded from the candidate pool. Each entry
    # may carry "flexible": true (driver works anytime — window NOT enforced); otherwise the
    # Start/End are a HARD window that IS enforced. driver_hours: {driver_id: (start_hour, end_hour)}.
    driver_hours = {}
    flexible_drivers = set()
    driver_max_hours = {}
    # Drivers whose Max hrs the dispatcher explicitly TYPED in the modal — STRICT caps: the
    # span-cap rescue never lifts them (the modal is authoritative; a typed number means it).
    strict_span_caps = {}
    if raw_driver_hours:
        working_ids = set()
        for did_str, hours in raw_driver_hours.items():
            try:
                did = int(did_str); sh = int(hours["start"]); eh = int(hours["end"])
            except (ValueError, KeyError, TypeError):
                continue
            if did not in eligible_driver_ids:
                continue
            working_ids.add(did)
            driver_hours[did] = (sh, eh)
            if hours.get("flexible"):
                flexible_drivers.add(did)        # works anytime; Start/End not enforced
            try:
                _mh = float(hours.get("max_hours") or 0)
            except (ValueError, TypeError):
                _mh = 0
            if _mh > 0:
                strict_span_caps[did] = _mh
                driver_max_hours[did] = _mh
        inhouse_drivers = [d for d in inhouse_drivers if d.id in working_ids]  # Off drivers excluded
    else:
        # No modal payload (e.g. a programmatic call) -> use each driver's saved availability.
        for d in inhouse_drivers:
            is_avail, sh, eh, pref, flex = d.get_availability_for_date(target_date)
            if is_avail:
                driver_hours[d.id] = (sh, eh)
                if flex:
                    flexible_drivers.add(d.id)
        inhouse_drivers = [d for d in inhouse_drivers if d.id in driver_hours]  # data-off excluded

    for d in inhouse_drivers:
        full_avail = d.get_full_availability(target_date)
        if full_avail.get("max_hours"):
            # Modal-typed Max hrs wins over the saved-availability value.
            driver_max_hours.setdefault(d.id, float(full_avail["max_hours"]))

    # ── Span Governor: one cap-clamped, modal-aware window per working driver ──
    # max_hours via the get_effective_window funnel: min(stub, 15h default) — but a
    # modal-typed/DB per-driver value is INTENT and may raise past the default, up to
    # the 17h absolute ceiling.
    # Built for EVERY working driver and handed to the swap + rescue passes (find_swaps
    # restricts its receiver pool to this dict's keys, so a partial map would silently
    # shrink swap recovery). The greedy + gap passes get the same caps through their own
    # get_effective_window calls.
    from dispatching import feasibility_guards as fg
    capped_windows = {}
    for d in inhouse_drivers:
        _sh_eh = driver_hours.get(d.id)
        capped_windows[d.id] = fg.get_effective_window(d.id, configured={
            "start": _sh_eh[0] if _sh_eh else None,
            "end": _sh_eh[1] if _sh_eh else None,
            "max_hours": driver_max_hours.get(d.id),
            "flexible": d.id in flexible_drivers,
        })

    # Shared-car partner map: two WORKING drivers on one physical unit (Day Setup planned
    # AM/PM share or an advisor freed-unit accept). Every engine pass gates inserts against
    # the partner's jobs — the planned windows alone are not airtight (modal End is a
    # last-pickup bound; a 14:50 pickup clears past the partner's 15:05 start).
    from dispatching.scheduler import build_sharer_partners
    sharer_partners = build_sharer_partners(
        {d.id for d in inhouse_drivers}, target_date)

    schedules = build_driver_schedules(legs, inhouse_drivers, target_date)

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

    # Vehicle (number + type) per driver for the date — shown in the schedule header.
    veh_by_driver = {}
    for _dva in (DriverVehicleAssignment.objects
                 .filter(date=target_date, driver_id__in=[d.id for d in inhouse_drivers])
                 .select_related("vehicle", "vehicle__vehicle_type")):
        if _dva.vehicle:
            _num = _dva.vehicle.vehicle_number
            _vt = str(_dva.vehicle.vehicle_type).upper() if _dva.vehicle.vehicle_type else ""
            veh_by_driver[_dva.driver_id] = f"{_vt} · {_num}" if _vt else (_num or "")

    # Get unassigned legs (excluding user-excluded ones)
    excluded_set = set(excluded_leg_ids)
    unassigned = [l for l in legs if not l.driver and l.id not in excluded_set]

    # Separate manually-assigned legs from auto-assign pool
    manual_leg_ids = set(manual_assignments.keys())
    auto_unassigned = [l for l in unassigned if l.id not in manual_leg_ids]

    # Drop unpaid reservations from the auto pool when the dispatcher asked to skip
    # them. Manual assignments are kept (deliberate override).
    if exclude_unpaid:
        auto_unassigned = [
            l for l in auto_unassigned
            if l.reservation and l.reservation.payment_status == 'paid'
        ]

    # ── "Build first" priority seeding ──
    # Drivers the dispatcher marked "Build first" get their FULL day built BEFORE the general
    # assignment — mirrors building a fixed driver's day (e.g. Yovanny) by hand and shuffling the
    # rest around it, so flexible drivers don't out-compete them for legs they could do. Coverage
    # and feasibility are unchanged (build_smart_schedule gates every leg); this only reserves their
    # legs first. Most-constrained (narrowest window) priority driver is seeded first.
    seeded_assignments = {}
    assign_board = schedules   # board the general assigner sees (gets seeded occupancy below)
    _priority_ids = [int(x) for x in raw_build_first if str(x).isdigit() and int(x) in driver_hours
                     and int(x) not in sharer_partners]  # seeding bypasses the shared-car gate
    _priority_ids.sort(key=lambda did: 24 if did in flexible_drivers else (driver_hours[did][1] - driver_hours[did][0]))
    if _priority_ids:
        from dispatching.scheduler import build_smart_schedule
        _pool = list(auto_unassigned)
        for did in _priority_ids:
            sh, eh = driver_hours[did]
            existing = schedules.get(did)
            existing_ids = {s.leg_id for s in existing.slots} if existing else set()
            res = build_smart_schedule(
                driver_id=did, driver_name=str(drivers_by_id[did]),
                available_legs=_pool, target_date=target_date,
                start_hour=sh, end_hour=eh, existing_schedule=existing,
                # Span Governor: Build-1st seeding was the one path with NO span bound
                # (max_hours used to be hardcoded None) — pass the same clamped cap the
                # rest of the pipeline enforces.
                max_hours=(capped_windows.get(did) or {}).get("max_hours"),
                # Build-1st seeding is still the ENGINE choosing legs, so it pays the same
                # turn buffer as the general pass (build_smart_schedule applies this
                # driver's own typed override on top).
                min_buffer=run_min_buffer,
            )
            for s in res.get('schedule', []):
                if s.leg_id not in existing_ids and s.leg_id not in seeded_assignments:
                    seeded_assignments[s.leg_id] = did
            _pool = [l for l in _pool if l.id not in seeded_assignments]
        # Build a SEPARATE board (assign_board) that includes the seeded occupancy so the general
        # pass sees these drivers as busy. Do NOT mutate `schedules` itself: the preview deepcopies
        # `schedules` as the pre-existing board and re-adds final_assignments on top, so seeded legs
        # must live ONLY in final_assignments — else they render twice (the "15 legs" duplication).
        for lid, did in seeded_assignments.items():
            lg = legs_by_id.get(lid)
            if lg is not None:
                lg.driver = drivers_by_id.get(did); lg.driver_id = did
        assign_board = build_driver_schedules(legs, inhouse_drivers, target_date)
        for lid in seeded_assignments:   # restore: seeded are tracked via final_assignments, not leg.driver
            lg = legs_by_id.get(lid)
            if lg is not None:
                lg.driver = None; lg.driver_id = None
        auto_unassigned = [l for l in auto_unassigned if l.id not in seeded_assignments]

    # ── Rest Advisor: previous day's last drop-off per working driver ──
    # Feeds the overnight-rest deficit penalty (suggest_assignments scorer) AND the rest
    # advisory cards below. max(end) across ALL of yesterday's legs = the real clear time
    # (a slightly earlier pickup with a longer drive can be the one that clears last).
    # A driver with no legs yesterday is absent from the map => treated as fully rested.
    prev_end_by_driver = {}
    try:
        from dispatching.scheduler import estimate_job_end_time as _est_end
        _prev_day = target_date - timedelta(days=1)
        _wids = set(driver_hours.keys())
        if _wids:
            _prev_legs = (Leg.objects.filter(pickup_date=_prev_day, driver_id__in=_wids)
                          .exclude(status="cancelled")
                          .select_related("reservation", "flight_information"))
            for _pl in _prev_legs:
                try:
                    _end = _est_end(_pl, _prev_day)
                except Exception:
                    continue
                if _end > prev_end_by_driver.get(_pl.driver_id, datetime.min):
                    prev_end_by_driver[_pl.driver_id] = _end
    except Exception:
        prev_end_by_driver = {}

    # Run suggestion engine on remaining unassigned legs
    suggestions = suggest_assignments_clustered(auto_unassigned, assign_board, target_date,
                                                driver_hours=driver_hours or None,
                                                driver_preferences=driver_preferences or None,
                                                flexible_drivers=flexible_drivers or None,
                                                driver_max_hours=driver_max_hours or None,
                                                sharer_partners=sharer_partners or None,
                                                prev_end_by_driver=prev_end_by_driver or None,
                                                min_buffer=run_min_buffer,
                                                driver_min_buffers=driver_min_buffers) if auto_unassigned else []

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
    # "Build first" seeded legs are part of the final board (and locked from later passes).
    for lid, did in seeded_assignments.items():
        final_assignments[lid] = did

    # Manual + seeded assignments are LOCKED — never relocated by the swap / gap passes.
    locked_ids = set(manual_assignments.keys()) | set(seeded_assignments.keys())

    # ── Auto pre-farm swap pass ──
    # The greedy build is single-leg and can't rearrange, so it farms legs that a cascade of
    # existing assignments could absorb. Before finalizing the farm list, try to recover each
    # would-be-farmed auto leg in-house via find_swaps. Read-only; updates final_assignments
    # (recovered + any moved legs). Manual + build-first assignments are locked (never relocated).
    _span_warnings = []
    _evict_moves = []
    if auto_unassigned:
        from dispatching.scheduler import (
            recover_residuals_via_swaps, rescue_span_blocked_residuals,
            evict_to_farm_for_value, load_all_driver_vtypes,
        )
        _dvtypes = load_all_driver_vtypes(target_date)
        final_assignments, _swap_recovered = recover_residuals_via_swaps(
            final_assignments, [l.id for l in auto_unassigned], legs_by_id,
            inhouse_drivers, drivers_by_id, target_date, _dvtypes,
            locked_leg_ids=locked_ids,
            driver_windows=capped_windows or None,
            driver_hours=driver_hours or None,
            flexible_drivers=flexible_drivers or None,
            sharer_partners=sharer_partners or None,
        )
        # ── Evict-to-farm value pass (founder brain R1+R2) ──
        # An assigned leg is not sacred: a residual that outvalues an engine-proposed
        # ARRIVAL (a departure, a higher booked class) evicts it to the farm pool and
        # takes the seat — arrivals are the farm-out currency; true departures are never
        # evicted (is_departure parity with the farm-out optimizer). Runs AFTER the swap
        # pass (cheaper cascades first), BEFORE the span rescue (so the rescue re-seats
        # evicted arrivals anywhere they still fit) and BEFORE the trim/gap passes
        # (which polish a settled board). Manual/seeded/pre-existing stay locked; every
        # move re-validates the whole chain through the guards.
        final_assignments, _evict_moves = evict_to_farm_for_value(
            final_assignments, [l.id for l in auto_unassigned], legs_by_id,
            inhouse_drivers, drivers_by_id, target_date, _dvtypes,
            locked_leg_ids=locked_ids,
            driver_windows=capped_windows or None,
            driver_hours=driver_hours or None,
            flexible_drivers=flexible_drivers or None,
            sharer_partners=sharer_partners or None,
            min_buffer=run_min_buffer, driver_min_buffers=driver_min_buffers,
        )
        if _evict_moves:
            import logging as _logging
            _ev_log = _logging.getLogger(__name__)
            for _mv in _evict_moves:
                _ev_log.info("AUTO-ASSIGN evict pass: %s", _mv["reason"])
        # ── Span-cap coverage rescue ──
        # Priority #1: the duty-span cap may never cost an in-house job. Any residual whose
        # ONLY blocker was the cap is assigned anyway with a loud RED preview warning —
        # except drivers with a dispatcher-TYPED Max hrs (strict; the leg stays residual
        # with a named reason). Runs BEFORE gap compaction so rescued legs can still be healed.
        final_assignments, _span_rescued, _span_warnings = rescue_span_blocked_residuals(
            final_assignments, [l.id for l in auto_unassigned], legs_by_id,
            inhouse_drivers, drivers_by_id, target_date, _dvtypes,
            capped_windows,
            driver_hours=driver_hours or None,
            flexible_drivers=flexible_drivers or None,
            strict_cap_driver_ids=set(strict_span_caps.keys()),
            locked_leg_ids=locked_ids,
            sharer_partners=sharer_partners or None,
            min_buffer=run_min_buffer, driver_min_buffers=driver_min_buffers,
        )

    # ── Span-trim relocation pass ──
    # Coverage is settled; now actively SHORTEN over-long days: peel a long driver's first or
    # last leg onto a driver with room (the founder's "Roberto just starts later" move). Never
    # farms (keyset asserted unchanged); moved legs are locked against the gap pass below.
    from dispatching.scheduler import trim_spans_via_relocation, compact_gaps_via_relocation, load_all_driver_vtypes
    final_assignments, _trim_moves = trim_spans_via_relocation(
        final_assignments, legs_by_id, inhouse_drivers, drivers_by_id, target_date,
        load_all_driver_vtypes(target_date),
        locked_leg_ids=locked_ids,
        driver_hours=driver_hours or None,
        flexible_drivers=flexible_drivers or None,
        capped_windows=capped_windows or None,
        sharer_partners=sharer_partners or None,
        min_buffer=run_min_buffer, driver_min_buffers=driver_min_buffers,
    )
    locked_ids = locked_ids | {m["leg_id"] for m in _trim_moves}

    # ── Gap-compaction relocation pass ──
    # Coverage is settled above; now compact for quality. If a driver has a big internal hole
    # and another driver holds a job sitting inside it, relocate that job to fill the hole (the
    # donor just starts later / finishes earlier) — but only when it heals more gap than it
    # opens. Manual assignments stay locked (never relocated). Read-only; updates final_assignments.
    final_assignments, _gap_moves = compact_gaps_via_relocation(
        final_assignments, legs_by_id, inhouse_drivers, drivers_by_id, target_date,
        load_all_driver_vtypes(target_date),
        locked_leg_ids=locked_ids,
        driver_hours=driver_hours or None,
        flexible_drivers=flexible_drivers or None,
        sharer_partners=sharer_partners or None,
        min_buffer=run_min_buffer, driver_min_buffers=driver_min_buffers,
    )

    # ── Final free-insertion sweep (founder brain) ──
    # The trim/gap relocations above can open seats that did not exist when coverage was
    # settled — never leave a leg farmed that fits the FINAL board as-is (the founder's
    # answer key missed two such insertions on 6/14; the engine must not). No evictions
    # here (free_insert_only) — pure coverage wins, every insert re-runs the guards.
    if auto_unassigned:
        from dispatching.scheduler import evict_to_farm_for_value as _evict_pass
        final_assignments, _final_inserts = _evict_pass(
            final_assignments, [l.id for l in auto_unassigned], legs_by_id,
            inhouse_drivers, drivers_by_id, target_date,
            load_all_driver_vtypes(target_date),
            locked_leg_ids=locked_ids,
            driver_windows=capped_windows or None,
            driver_hours=driver_hours or None,
            flexible_drivers=flexible_drivers or None,
            sharer_partners=sharer_partners or None,
            free_insert_only=True,
        )
        _evict_moves.extend(_final_inserts)

    assigned_count = len(final_assignments)
    remaining = len(unassigned) - assigned_count

    if apply_mode:
        # Filter to selected drivers only if specified
        if apply_driver_ids is not None:
            selected_dids = set(int(d) for d in apply_driver_ids)
            final_assignments = {lid: did for lid, did in final_assignments.items() if did in selected_dids}

        # Sandbox gate: if this date is held AND the runner is a granted sandbox
        # user, apply auto-assign results into the draft overlay instead of live.
        # Non-granted dispatchers auto-assign live, exactly as before.
        draft = _active_draft_for_date(target_date)
        if draft and can_use_sandbox(request.user):
            from reservations.models import DraftAssignment
            now = timezone.now()
            for lid, did in final_assignments.items():
                DraftAssignment.objects.update_or_create(
                    draft=draft, leg_id=lid,
                    defaults={"proposed_driver_id": did, "assigned_by": request.user, "assigned_at": now},
                )
            _log_draft_event(draft, "edited", actor=request.user,
                             source="auto_assign", count=len(final_assignments))
            return JsonResponse({
                "success": True,
                "assigned": len(final_assignments),
                "remaining": len(unassigned) - len(final_assignments),
                "held": True,
                "message": f"Staged {len(final_assignments)} assignments in the draft for {target_date.isoformat()}.",
            })

        # ── Apply mode (live): save assignments to DB ──
        _create_schedule_snapshot(target_date, request.user, 'before_auto_assign')

        # PERF TEMP START
        import time as _time; _t_assign = _time.monotonic()
        import logging as _logging; _perf = _logging.getLogger('perf')
        # PERF TEMP END
        now = timezone.now()
        saved = 0
        # Sanctioned: the sandbox gate above already routed held-day granted
        # users into the draft; reaching here means live apply is intended
        # (no draft, or a non-granted dispatcher on a held day — by design).
        with sanctioned_live_write():
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
            "evict_moves": _evict_moves,
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
        from dispatching.scheduler import chain_clear_dt as _chain_clear_dt
        return ScheduleSlot(
            leg_id=leg.id, pickup_time=leg.pickup_time,
            pickup_location=leg.pickup_location, pickup_category=pickup_cat,
            dropoff_location=leg.dropoff_location, dropoff_category=dropoff_cat,
            trip_type=leg.get_trip_type(), estimated_end_time=end_time,
            reservation_id=leg.reservation_id, customer_name=customer_name,
            status=leg.status or 'pending', has_flight=has_flight,
            flight_info=flight_info, revenue=leg.revenue_share,
            chain_clear_dt=_chain_clear_dt(leg, target_date),
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
    from dispatching.scheduler import effective_span_hours
    _rescued_by_driver = {}
    for _w in _span_warnings:
        if _w.get("kind") == "rescued":
            _rescued_by_driver.setdefault(_w["driver_id"], []).append(_w)
    # Second-Shift Advisor inputs: drivers still over-target after the trim pass, with the
    # legs this run assigned (unlocked) — the movable tail a second-shift driver could absorb.
    _overload_map = {}
    _movable_ids = set(final_assignments.keys()) - locked_ids
    driver_schedules = []
    for schedule in sorted(proposed.values(), key=lambda s: s.driver_name):
        schedule.slots.sort(key=lambda s: s.pickup_time)
        if not schedule.slots:
            continue

        first_pickup = schedule.slots[0].pickup_time.strftime("%I:%M %p").lstrip("0")
        last_end = schedule.slots[-1].estimated_end_time.strftime("%I:%M %p").lstrip("0") if schedule.slots[-1].estimated_end_time else ""
        # Total on-duty span (first pickup -> last clear)
        _last_dt = schedule.slots[-1].estimated_end_time
        if _last_dt:
            _mins = int((_last_dt - dt.combine(target_date, schedule.slots[0].pickup_time)).total_seconds() / 60)
            hours_label = f"{_mins // 60}h {_mins % 60}m" if _mins % 60 else f"{_mins // 60}h"
        else:
            hours_label = ""

        # Span Governor badge: RED when the rescue lifted this driver past a cap to keep a
        # leg in-house; AMBER when his EFFECTIVE duty (raw span minus one >=2h break) runs
        # STRICTLY past the 13.5h target. Effective-span credit means a founder-style
        # split-day (16.5h raw with a 4.5h hole) is correctly NOT flagged.
        _raw_h, _eff_h = effective_span_hours(schedule.slots, target_date)
        span_warn, span_note = "", ""
        if schedule.driver_id in _rescued_by_driver:
            _r = _rescued_by_driver[schedule.driver_id][0]
            span_warn = "red"
            span_note = (f"{_raw_h:.1f}h — over the {_r['cap_hours']:.0f}h cap; "
                         f"kept {len(_rescued_by_driver[schedule.driver_id])} leg(s) in-house instead of farming")
        elif fg.ENFORCE_SPAN_CAPS and _eff_h > fg.SPAN_SOFT_EFFECTIVE_HOURS:
            span_warn = "amber"
            _brk = "" if abs(_raw_h - _eff_h) < 0.05 else f" ({_raw_h:.1f}h with break)"
            span_note = f"{_eff_h:.1f}h on duty{_brk} — over the {fg.SPAN_SOFT_EFFECTIVE_HOURS:g}h target"
        if span_warn:
            _overload_map[schedule.driver_id] = {
                "name": schedule.driver_name,
                "slots": list(schedule.slots),
                "movable_ids": _movable_ids,
            }

        slots_data = []
        for slot in schedule.slots:
            # Look up vehicle type and store stop from the actual leg
            vtype = ""
            has_store_stop = False
            leg_obj = legs_by_id.get(slot.leg_id)
            if leg_obj and leg_obj.reservation:
                if leg_obj.effective_vehicle:
                    vtype = str(leg_obj.effective_vehicle_type).upper()
                has_store_stop = leg_obj.shows_store_stop
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
            "hours": hours_label,
            "vehicle": veh_by_driver.get(schedule.driver_id, ""),
            "span_hours": round(_raw_h, 1),
            "effective_span_hours": round(_eff_h, 1),
            "span_warn": span_warn,
            "span_note": span_note,
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
        vtype = leg.effective_vehicle_type or ''
        trip_type = leg.get_trip_type()
        has_store_stop = leg.shows_store_stop
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
            "is_paid": (leg.reservation.payment_status == 'paid') if leg.reservation else True,
        })

    # Driver list for manual assignment dropdown
    driver_list = [
        {"id": d.id, "name": str(d)}
        for d in sorted(inhouse_drivers, key=lambda d: str(d))
    ]

    # Span Governor warnings strip — loud, explainable (no silent behavior changes).
    span_warnings_out = []
    for _w in _span_warnings:
        if _w["kind"] == "rescued":
            span_warnings_out.append({
                "level": "red", "leg_id": _w["leg_id"],
                "text": (f"Leg #{_w['leg_id']} ({_w['pickup']}) kept in-house on {_w['driver_name']} "
                         f"— stretches his day to {_w['span_after']}h, past the {_w['cap_hours']:.0f}h cap "
                         f"(farming avoided)."),
            })
        elif _w["kind"] == "strict_blocked":
            span_warnings_out.append({
                "level": "strict", "leg_id": _w["leg_id"],
                "text": (f"Leg #{_w['leg_id']} ({_w['pickup']}) left for affiliates: only {_w['driver_name']} "
                         f"could take it, but your {_w['cap_hours']:g}h Max-hrs cap on him holds "
                         f"(would be {_w['span_after']}h). Raise his Max hrs to keep it in-house."),
            })
        elif _w["kind"] == "ceiling_blocked":
            span_warnings_out.append({
                "level": "strict", "leg_id": _w["leg_id"],
                "text": (f"Leg #{_w['leg_id']} ({_w['pickup']}) left for affiliates: keeping it in-house "
                         f"would stretch {_w['driver_name']} to {_w['span_after']}h — past the "
                         f"{_w['cap_hours']:g}h day ceiling. Type a Max hrs on a driver (up to "
                         f"{fg.SPAN_ABS_CEILING_HOURS:g}h) to allow a longer day, or cover it with "
                         f"a second-shift driver / an affiliate."),
            })

    # ── Second-Shift Advisor (Phase 5) ──
    # "This day's volume needs another driver" — clusters residual legs + untrimmable
    # over-target tails into shift proposals with a concrete idle-driver + unit source.
    # Advisory only; an exception here must never break the preview.
    advisor_proposals = []
    try:
        _residual_objs = [l for l in unassigned if l.id not in final_assignments]
        if exclude_unpaid:
            # "Skip unpaid" means those legs don't exist for THIS build — they must not
            # read as "the day needs more coverage": one skipped unpaid leg was enough to
            # suppress every Fold-Out card and to spawn Second-Shift cards for jobs the
            # dispatcher deliberately ignored. Mirror of the auto-pool filter above.
            _residual_objs = [l for l in _residual_objs
                              if l.reservation and l.reservation.payment_status == 'paid']
    except Exception:
        _residual_objs = None   # board state unreadable — BOTH advisors stay silent
    try:
        from dispatching.shift_advisor import build_shift_proposals
        if _residual_objs is not None and (_residual_objs or _overload_map):
            advisor_proposals = build_shift_proposals(
                target_date, _residual_objs, _overload_map,
                set(driver_hours.keys()), proposed, legs_by_id)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("second-shift advisor failed (advisory only)")

    # ── Fold-Out Advisor (demand-aware staffing) ──
    # The mirror image: "this day can run leaner" — propose releasing a thin driver
    # whose whole (engine-proposed, unlocked) day verifiably fits on the others.
    # Suppressed when residuals exist: a day that still needs MORE coverage never
    # shows a release card. Advisory only; an exception must never break the preview.
    try:
        from dispatching.fold_advisor import build_fold_out_proposals
        if _residual_objs is not None and not _residual_objs:
            advisor_proposals.extend(build_fold_out_proposals(
                target_date, proposed, final_assignments, locked_ids,
                driver_hours, flexible_drivers, capped_windows, sharer_partners,
                legs_by_id, drivers_by_id,
                build_first_ids=set(_priority_ids),
                residual_count=len(_residual_objs)))
    except Exception:
        import logging
        logging.getLogger(__name__).exception("fold-out advisor failed (advisory only)")

    # ── Rebalance Advisor (demand-aware staffing, round 2) ──
    # "Spread it evenly, keep it dense": relative-balance FILL cards (move jobs to a
    # thin-but-needed driver) + hollow-day COMPRESS cards (move boundary outliers so a
    # long-and-empty day ends early). Runs AFTER fold — a driver with a live fold card
    # folds, he doesn't get filled. Advisory only.
    try:
        from dispatching.rebalance_advisor import build_rebalance_proposals
        if _residual_objs is not None and not _residual_objs:
            advisor_proposals.extend(build_rebalance_proposals(
                target_date, proposed, final_assignments, locked_ids,
                driver_hours, flexible_drivers, capped_windows, sharer_partners,
                legs_by_id, drivers_by_id,
                build_first_ids=set(_priority_ids),
                residual_count=len(_residual_objs),
                exclude_driver_ids={p.get("driver_id") for p in advisor_proposals
                                    if p.get("kind") == "fold_out"}))
    except Exception:
        import logging
        logging.getLogger(__name__).exception("rebalance advisor failed (advisory only)")

    # ── Rest Advisor (overnight rest awareness) ──
    # Verifies the FINAL board: any working driver pulled to an early first pickup without
    # the minimum overnight rest (since yesterday's last drop-off) gets a card naming a
    # rested same-class alternative, or "no alternative — accept/farm". The scorer prevents
    # most; this catches manual/locked/only-driver cases. NOT gated on residual/fold state
    # (a rest violation matters whether or not the day is fully covered). Advisory only.
    try:
        from dispatching.rest_advisor import build_rest_advisories
        advisor_proposals.extend(build_rest_advisories(
            target_date, proposed, prev_end_by_driver,
            set(driver_hours.keys()), drivers_by_id))
    except Exception:
        import logging
        logging.getLogger(__name__).exception("rest advisor failed (advisory only)")

    return JsonResponse({
        "success": True,
        "assigned": assigned_count,
        "remaining": remaining,
        "total": len(legs),
        "driver_schedules": driver_schedules,
        "unassigned_legs": still_unassigned,
        "driver_list": driver_list,
        "span_warnings": span_warnings_out,
        "trim_moves": len(_trim_moves),
        "evict_moves": _evict_moves,
        "advisor": advisor_proposals,
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

    # Sandbox gate: a granted sandbox user resetting a held day clears the DRAFT to
    # all-unassigned (proposed_driver=NULL), leaving live Leg.driver untouched.
    # Non-granted dispatchers reset the live schedule, exactly as before.
    draft = _active_draft_for_date(target_date)
    if draft and can_use_sandbox(request.user):
        from reservations.models import DraftAssignment
        legs = Leg.objects.filter(
            pickup_date=target_date
        ).exclude(reservation__status="cancelled").exclude(status="cancelled")
        DraftAssignment.objects.filter(draft=draft).delete()
        now = timezone.now()
        DraftAssignment.objects.bulk_create([
            DraftAssignment(draft=draft, leg=leg, proposed_driver=None,
                            assigned_by=request.user, assigned_at=now)
            for leg in legs
        ])
        count = legs.count()
        _log_draft_event(draft, "edited", actor=request.user, source="reset_in_draft", count=count)
        return JsonResponse({
            "success": True,
            "reset_count": count,
            "held": True,
            "message": f"Reset the draft for {date_str} to all-unassigned ({count} legs). Live schedule unchanged.",
        })

    # Auto-snapshot before resetting
    snapshot = _create_schedule_snapshot(target_date, request.user, 'before_reset')

    legs = Leg.objects.filter(
        pickup_date=target_date, driver__isnull=False
    ).exclude(reservation__status="cancelled").exclude(status="cancelled")
    count = legs.count()
    # Completed legs lose only the driver (status sticks); everything else
    # also resets to 'in-progress' — same rule as the per-leg unassign in
    # Leg.save(), which this bulk update bypasses.
    # KEOI invariant: this is the only queryset .update() on leg status, and it
    # NEVER crosses a terminal boundary — the first .update() below nulls the
    # driver on completed legs, dropping them from the lazy driver__isnull=False
    # queryset before the second .update() forces 'in-progress' on the rest;
    # cancelled legs are already excluded above. So the KEOI auto-reactivate
    # signal (which only fires via instance .save()) is correctly not needed here.
    legs.filter(status="completed").update(
        driver=None, driver_assigned_by=None, driver_assigned_at=None
    )
    legs.update(
        driver=None, driver_assigned_by=None, driver_assigned_at=None,
        status="in-progress", status_changed_by=request.user,
        status_changed_at=timezone.now(),
    )

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

    # Held day + granted user: load the snapshot INTO the draft (drivers keep
    # seeing the live board until publish). Otherwise restore live as before.
    draft = _active_draft_for_date(snapshot.schedule_date)
    staging = bool(draft) and can_use_sandbox(request.user)

    restored = 0
    cleared = 0
    for leg in all_legs:
        entry = assignment_map.get(leg.id)
        if entry:
            if staging:
                _upsert_draft_assignment(draft, leg, entry.driver, request.user, source="snapshot_restore")
            else:
                # Restore saved assignment (snapshot's original attribution kept)
                leg.driver = entry.driver
                leg.driver_assigned_by = entry.driver_assigned_by
                leg.driver_assigned_at = entry.driver_assigned_at
                with sanctioned_live_write():
                    leg.save(update_fields=['driver', 'driver_assigned_by', 'driver_assigned_at'])
            restored += 1
        elif staging:
            da_exists = leg.driver is not None
            if da_exists:
                _upsert_draft_assignment(draft, leg, None, request.user, source="snapshot_restore")
                cleared += 1
        elif leg.driver is not None:
            # This leg was unassigned in the snapshot, clear it
            mode, _ = set_leg_driver(leg, None, request.user, source="snapshot_restore")
            cleared += 1

    # Invalidate capacity planner cache so it rebuilds with fresh data
    cache.delete(f"capacity_planner_{snapshot.schedule_date.isoformat()}")

    return JsonResponse({
        "success": True,
        "restored": restored,
        "cleared": cleared,
        "held": staging,
        "message": (
            f"Loaded snapshot into the draft: {restored} assignments staged, {cleared} staged as unassigned."
            if staging else
            f"Restored {restored} assignments from snapshot. {cleared} legs cleared."
        ),
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
    from dispatching.scheduler import (
        build_driver_schedules,
        build_smart_schedule,
        get_driver_vehicle_type,
        get_compatible_vehicle_types,
    )
    from drivers.models import Driver as DriverModel

    # Parse parameters
    driver_id = data.get("driver_id")
    date_str = data.get("date")
    start_hour = int(data.get("start_hour", 0))
    end_hour = int(data.get("end_hour", 23))
    pinned_leg_ids = data.get("pinned_leg_ids", [])
    preferred_trip_type = data.get("preferred_trip_type", "")
    vehicle_pref_mode = data.get("vehicle_pref_mode", "")  # '', 'prefer', 'heavy', 'only'
    preferred_vehicle_types = data.get("preferred_vehicle_types", []) or []  # list of type strings
    excluded_leg_ids = data.get("excluded_leg_ids", [])
    apply_assignments = data.get("apply", False)
    # When true, unpaid reservations are treated as if they don't exist for scheduling
    # purposes (not auto-fitted, not surfaced as alternatives). Pinning them by hand
    # still works.
    exclude_unpaid = bool(data.get("exclude_unpaid", False))
    # Turn buffer (Guard B'): spare minutes the engine must leave between two jobs on top of
    # the drive between them. Absent => the saved SchedulerSettings default. This driver's
    # own typed Driver.default_min_turn_buffer still beats whatever is chosen here.
    _raw_min_buffer = data.get("min_buffer", None)
    try:
        min_buffer = None if _raw_min_buffer in (None, "") else max(0, int(_raw_min_buffer))
    except (TypeError, ValueError):
        min_buffer = None

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
        .select_related("driver", "driver__profile", "reservation", "reservation__customer", "reservation__vehicle", "vehicle")
        .prefetch_related(
            Prefetch("reservation__payments", queryset=Payment.objects.order_by('-created_at')),
        )
    )

    # Build existing schedule for this driver (already assigned legs)
    all_drivers = DriverModel.objects.select_related("profile").all()
    schedules = build_driver_schedules(legs, all_drivers, target_date)
    existing_schedule = schedules.get(driver.id)

    # Get unassigned legs + excluded existing legs (so they can be swapped/replaced)
    available_legs = [l for l in legs if not l.driver or l.id in excluded_leg_ids]
    # Drop unpaid reservations from auto-fit + alternatives unless they were explicitly
    # pinned by the user. Pinning is treated as a deliberate override.
    if exclude_unpaid:
        _pinned_set = set(pinned_leg_ids or [])
        available_legs = [
            l for l in available_legs
            if (l.reservation and l.reservation.payment_status == 'paid') or l.id in _pinned_set
        ]

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
        vehicle_pref_mode=vehicle_pref_mode or None,
        preferred_vehicle_types=preferred_vehicle_types or None,
        min_buffer=min_buffer,
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
                if res.rf_carseats: cs_parts.append(f"{res.rf_carseats} rf")
                if res.ff_carseats: cs_parts.append(f"{res.ff_carseats} ff")
                if res.booster_seats: cs_parts.append(f"{res.booster_seats} b")
            slot_data['carseats'] = ", ".join(cs_parts)
            slot_data['is_paid'] = res.payment_status == 'paid'
            slot_data['reservation_uuid'] = str(res.uuid) if res.uuid else ''
            slot_data['store_stop'] = leg_obj.shows_store_stop
        else:
            slot_data['is_paid'] = True
            slot_data['reservation_uuid'] = ''
            slot_data['store_stop'] = False
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

    # Build alternatives: unassigned legs NOT in the built schedule.
    # Filter by vehicle compatibility so an SUV-only driver doesn't see Van
    # legs in the swap panel. When the driver has no resolved vehicle type
    # for the day (affiliate / unscheduled), skip filtering so the panel
    # stays permissive.
    driver_vtype = get_driver_vehicle_type(driver.id, target_date)
    compatible_vtypes = (
        set(get_compatible_vehicle_types(driver_vtype)) if driver_vtype else None
    )
    alternatives = []
    for leg_alt in available_legs:
        if leg_alt.id in scheduled_leg_ids or leg_alt.id in excluded_leg_ids:
            continue
        # Hide vehicle-incompatible legs (same rule as build_smart_schedule)
        if compatible_vtypes is not None:
            alt_vtype = leg_alt.effective_vehicle_type
            if alt_vtype and alt_vtype not in compatible_vtypes:
                continue
        res = leg_alt.reservation
        veh = res.vehicle if res else None
        alt_cs = []
        if res and res.need_carseats:
            if res.rf_carseats: alt_cs.append(f"{res.rf_carseats} rf")
            if res.ff_carseats: alt_cs.append(f"{res.ff_carseats} ff")
            if res.booster_seats: alt_cs.append(f"{res.booster_seats} b")
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
            'store_stop': leg_alt.shows_store_stop,
            'is_paid': (res.payment_status == 'paid') if res else True,
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
        staged_any = False
        for lid in new_leg_ids:
            try:
                leg = Leg.objects.get(id=lid)
                if not leg.driver:  # safety check
                    # Front door: held day + granted user -> staged in draft.
                    mode, _ = set_leg_driver(leg, driver, request.user, source="build_first")
                    staged_any = staged_any or (mode == "staged")
                    assigned += 1
            except Leg.DoesNotExist:
                continue

        response['applied'] = True
        response['assigned_count'] = assigned
        response['held'] = staged_any
        response['message'] = (
            f"Staged {assigned} new legs for {driver} in the draft." if staged_any
            else f"Assigned {assigned} new legs to {driver}."
        )
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
        leg_time_of_day_category,
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
            time_cat = leg_time_of_day_category(leg)
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
            time_cat = leg_time_of_day_category(leg)
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
        time_cat = leg_time_of_day_category(leg)
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
                if leg.effective_vehicle:
                    vehicle_name = str(leg.effective_vehicle)

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
# VEHICLE PROFIT REPORT
# ============================================================================

def _build_vehicle_profit_report(start_date, end_date, detail_vehicle_id=None):
    """Aggregate completed-trip profit per physical fleet vehicle.

    A Leg only references a vehicle *type*; the physical car is attributed via
    the driver's daily vehicle assignment (DriverVehicleAssignment is unique per
    driver+date), so a leg's car = the vehicle assigned to its driver on its
    pickup_date. Completed legs whose driver had no fleet assignment that day
    (e.g. affiliates) fall into an "unassigned" bucket.
    """
    from decimal import Decimal
    from django.db.models import Count
    from drivers.models import FleetVehicle, DriverVehicleAssignment

    ZERO = Decimal("0.00")
    CENTS = Decimal("0.01")

    legs = list(
        Leg.objects.filter(
            status="completed",
            pickup_date__gte=start_date,
            pickup_date__lte=end_date,
            driver__isnull=False,
        )
        .select_related(
            "driver", "driver__profile",
            "reservation", "reservation__customer", "reservation__vehicle",
            "vehicle",
        )
        .order_by("-pickup_date", "-pickup_time")
    )

    # (driver_id, date) -> FleetVehicle, for the same window.
    assign_map = {
        (a.driver_id, a.date): a.vehicle
        for a in DriverVehicleAssignment.objects.filter(
            date__gte=start_date, date__lte=end_date, vehicle__isnull=False
        ).select_related("vehicle")
    }

    # Total leg count per reservation (the revenue-share denominator). Counts ALL
    # legs of each reservation, not just the completed ones in this window.
    res_ids = {leg.reservation_id for leg in legs if leg.reservation_id}
    leg_counts = dict(
        Leg.objects.filter(reservation_id__in=res_ids)
        .values("reservation_id")
        .annotate(c=Count("id"))
        .values_list("reservation_id", "c")
    )

    def _revenue_for(leg):
        """Leg revenue with the same fallback the admin uses: stored revenue_share
        when present, otherwise reservation total_price / number of legs (so a
        one-way reservation's single leg gets the full price)."""
        rs = leg.revenue_share
        if rs:  # non-null and non-zero
            return rs
        res = leg.reservation
        if res and res.total_price:
            n = leg_counts.get(leg.reservation_id) or 1
            return (res.total_price / Decimal(n)).quantize(CENTS)
        return ZERO

    def _blank(vehicle):
        return {
            "vehicle": vehicle,
            "vehicle_id": vehicle.id if vehicle else None,
            "label": str(vehicle) if vehicle else "Unassigned / affiliate",
            "trips": 0,
            "revenue": ZERO,
            "driver_pay": ZERO,
            "profit": ZERO,
        }

    buckets = {}          # vehicle_id -> aggregate dict
    unassigned = _blank(None)
    detail_trips = []

    for leg in legs:
        vehicle = assign_map.get((leg.driver_id, leg.pickup_date))
        # Compute revenue and profit from the SAME basis so they always reconcile
        # (profit = revenue - driver pay). The stored profit_estimate is not used
        # because it can be out of sync with a stale/null revenue_share.
        revenue = _revenue_for(leg)
        driver_pay = leg.total_driver_pay or ZERO
        profit = (revenue - driver_pay).quantize(CENTS)

        if vehicle is None:
            agg = unassigned
        else:
            agg = buckets.get(vehicle.id)
            if agg is None:
                agg = buckets[vehicle.id] = _blank(vehicle)
        agg["trips"] += 1
        agg["revenue"] += revenue
        agg["driver_pay"] += driver_pay
        agg["profit"] += profit

        if detail_vehicle_id is not None and vehicle is not None and vehicle.id == detail_vehicle_id:
            customer_name = ""
            if leg.reservation and leg.reservation.customer:
                customer_name = leg.reservation.customer.get_full_name()
            detail_trips.append({
                "pickup_date": leg.pickup_date,
                "pickup_time": leg.pickup_time,
                "customer": customer_name,
                "pickup_location": leg.pickup_location,
                "dropoff_location": leg.dropoff_location,
                "trip_type": leg.get_trip_type(),
                "driver": leg.driver.profile.get_full_name() if leg.driver and leg.driver.profile else "",
                "vehicle_type": str(leg.effective_vehicle) if leg.effective_vehicle else "",
                "revenue": revenue,
                "driver_pay": driver_pay,
                "profit": profit,
                "reservation_uuid": leg.reservation.uuid if leg.reservation else None,
            })

    def _finalize(agg):
        rev = agg["revenue"]
        trips = agg["trips"] or 0
        agg["margin"] = (agg["profit"] / rev * 100) if rev else None
        agg["avg_profit"] = (agg["profit"] / trips) if trips else None
        agg["avg_revenue"] = (rev / trips) if trips else None
        return agg

    def _veh_sort_key(agg):
        # Natural sort by vehicle_number ("4" before "10", "004" handled too).
        num = (agg["vehicle"].vehicle_number or "") if agg["vehicle"] else ""
        return (0, int(num)) if num.isdigit() else (1, num.lower())

    vehicles = sorted(
        (_finalize(b) for b in buckets.values()),
        key=_veh_sort_key,
    )
    _finalize(unassigned)

    totals = {
        "trips": sum(a["trips"] for a in vehicles) + unassigned["trips"],
        "revenue": sum((a["revenue"] for a in vehicles), ZERO) + unassigned["revenue"],
        "driver_pay": sum((a["driver_pay"] for a in vehicles), ZERO) + unassigned["driver_pay"],
        "profit": sum((a["profit"] for a in vehicles), ZERO) + unassigned["profit"],
    }
    totals["margin"] = (totals["profit"] / totals["revenue"] * 100) if totals["revenue"] else None
    totals["avg_profit"] = (totals["profit"] / totals["trips"]) if totals["trips"] else None

    return {
        "vehicles": vehicles,
        "unassigned": unassigned,
        "totals": totals,
        "detail_trips": detail_trips,
    }


def _parse_report_date_range(request):
    """Shared date-range parsing for the vehicle profit report (default: last 30 days)."""
    date_from = request.GET.get("date_from", "")
    date_to = request.GET.get("date_to", "")
    if not date_from:
        date_from = (timezone.localdate() - timedelta(days=30)).strftime("%Y-%m-%d")
    if not date_to:
        date_to = timezone.localdate().strftime("%Y-%m-%d")
    try:
        start_date = datetime.strptime(date_from, "%Y-%m-%d").date()
    except ValueError:
        start_date = timezone.localdate() - timedelta(days=30)
        date_from = start_date.strftime("%Y-%m-%d")
    try:
        end_date = datetime.strptime(date_to, "%Y-%m-%d").date()
    except ValueError:
        end_date = timezone.localdate()
        date_to = end_date.strftime("%Y-%m-%d")
    return date_from, date_to, start_date, end_date


@login_required(login_url="login")
def vehicle_profit_report(request):
    """Profit per physical fleet vehicle from completed trips (superuser only)."""
    if not request.user.is_superuser:
        return redirect("dashboard")

    from drivers.models import FleetVehicle

    date_from, date_to, start_date, end_date = _parse_report_date_range(request)

    selected_vehicle_id = request.GET.get("vehicle", "")
    detail_vehicle_id = None
    selected_vehicle = None
    if selected_vehicle_id:
        try:
            detail_vehicle_id = int(selected_vehicle_id)
            selected_vehicle = FleetVehicle.objects.filter(id=detail_vehicle_id).first()
        except (TypeError, ValueError):
            detail_vehicle_id = None

    report = _build_vehicle_profit_report(start_date, end_date, detail_vehicle_id)

    # Summary stats for the selected vehicle (detail mode).
    selected_summary = None
    if selected_vehicle is not None:
        selected_summary = next(
            (v for v in report["vehicles"] if v["vehicle_id"] == detail_vehicle_id), None
        )

    context = {
        "fleet_vehicles": FleetVehicle.objects.filter(is_active=True).select_related("vehicle_type"),
        "selected_vehicle": selected_vehicle,
        "selected_vehicle_id": selected_vehicle_id,
        "selected_summary": selected_summary,
        "date_from": date_from,
        "date_to": date_to,
        "vehicles": report["vehicles"],
        "unassigned": report["unassigned"],
        "totals": report["totals"],
        "detail_trips": report["detail_trips"],
    }
    return render(request, "dispatching/vehicle_profit_report.html", context)


@login_required(login_url="login")
def vehicle_profit_report_csv(request):
    """CSV export of the per-vehicle profit overview (superuser only)."""
    if not request.user.is_superuser:
        return redirect("dashboard")

    import csv

    date_from, date_to, start_date, end_date = _parse_report_date_range(request)
    report = _build_vehicle_profit_report(start_date, end_date)

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = (
        f'attachment; filename="vehicle_profit_{date_from}_to_{date_to}.csv"'
    )
    writer = csv.writer(response)
    writer.writerow(["Vehicle", "Completed Trips", "Revenue", "Driver Pay", "Profit", "Margin %"])
    for v in report["vehicles"]:
        margin = f"{v['margin']:.1f}" if v["margin"] is not None else ""
        writer.writerow([v["label"], v["trips"], v["revenue"], v["driver_pay"], v["profit"], margin])
    u = report["unassigned"]
    if u["trips"]:
        margin = f"{u['margin']:.1f}" if u["margin"] is not None else ""
        writer.writerow([u["label"], u["trips"], u["revenue"], u["driver_pay"], u["profit"], margin])
    t = report["totals"]
    tmargin = f"{t['margin']:.1f}" if t["margin"] is not None else ""
    writer.writerow(["TOTAL", t["trips"], t["revenue"], t["driver_pay"], t["profit"], tmargin])
    return response


# Recommendation hint per binding-constraint family (shown on the dashboard).
_FLEET_FAMILY_ADVICE = {
    "capacity": "Capacity-bound: in-house units were busy or the vehicle type wasn't deployed. "
                "Add a unit (buy/hire) ONLY if repeatable — confirm with the +1 buy analysis.",
    "driver": "Driver-bound: a vehicle existed but no driver was on shift. Fix coverage/scheduling, "
              "not the fleet.",
    "process": "Process-bound: positioning, swaps, or flight buffers could have kept it in-house. "
               "A dispatch/scheduling improvement, not a purchase.",
    "strategic": "Mostly intentional farm-outs that protected better work.",
    "unknown": "Insufficient data to classify (missing vehicle type / route / window).",
}


@login_required(login_url="login")
def fleet_intel_dashboard(request):
    """Fleet Capacity Intelligence — farm-out economics + binding-constraint leaks (superuser only).

    Read-only. Reuses ``dispatching.fleet_intel.summarize_range``; result cached 5 min per range
    (classification replays the engine, so caching keeps the request fast like capacity_planner).
    """
    if not request.user.is_superuser:
        return redirect("dashboard")

    from django.core.cache import cache
    from dispatching import fleet_intel as fi

    date_from, date_to, start_date, end_date = _parse_report_date_range(request)

    cache_key = f"fleet_intel_{start_date.isoformat()}_{end_date.isoformat()}"
    report = cache.get(cache_key)
    if report is None:
        report = fi.summarize_range(start_date, end_date, classify=True)
        cache.set(cache_key, report, 300)

    def _rows(d, labels=None):
        out = []
        for k, v in d.items():
            spend = v.get("spend", 0) or 0
            net = v["net"]
            out.append({
                "key": labels.get(k, k) if labels else k,
                "raw_key": k,
                "count": v["count"],
                "net": net,
                "positive": v["positive"],
                "negative": v["negative"],
                "available": v["available"],
                "paid": spend,                       # what we paid affiliates for this group
                "inhouse": v.get("inhouse", 0) or 0,  # what in-house would have cost
                "margin_pct": round(float(net) / float(spend) * 100, 1) if spend else None,
            })
        out.sort(key=lambda r: r["count"], reverse=True)
        return out

    reason_rows = _rows(report["by_reason"], report["reason_labels"])
    for r in reason_rows:
        r["family"] = fi.REASON_FAMILY.get(r["raw_key"], "unknown")
        r["action"] = fi.REASON_ACTION.get(r["raw_key"], fi.ACT_REVIEW)
        r["remedy"] = report["reason_remedies"].get(r["raw_key"], "")
        r["preventable"] = fi.is_preventable(r["raw_key"])

    family_rows = _rows(report["by_family"])
    dominant = family_rows[0]["raw_key"] if family_rows else None

    context = {
        "date_from": date_from,
        "date_to": date_to,
        "range_days": report["range"]["days"],
        "op": report["operational"],
        "fin": report["financial"],
        "reason_rows": reason_rows,
        "family_rows": family_rows,
        "dominant_family": dominant,
        "dominant_advice": _FLEET_FAMILY_ADVICE.get(dominant, ""),
        "vehicle_rows": _rows(report["by_vehicle_type"]),
        "zone_pickup_rows": _rows(report["by_zone_pickup"]),
        "affiliate_rows": _rows(report["by_affiliate"])[:15],
        "dow_rows": _rows(report["by_day_of_week"]),
        "fleet_size": sorted(report["fleet_size_by_type"].items(), key=lambda x: -x[1]),
    }
    return render(request, "dispatching/fleet_intel_dashboard.html", context)


@login_required(login_url="login")
def fleet_intel_leaks(request):
    """Per-leg farm-out LEAK finder (superuser only).

    Every farmed leg, grouped into founder-facing action buckets (preventable / hire / delay / buy /
    positioning) with the evidence behind each verdict — including WHO farmed it and which in-house
    driver(s) could have taken it. Read-only; result cached 5 min per range.
    """
    if not request.user.is_superuser:
        return redirect("dashboard")

    from django.core.cache import cache
    from dispatching import fleet_intel as fi

    date_from, date_to, start_date, end_date = _parse_report_date_range(request)
    action = request.GET.get("action", "")

    cache_key = f"fleet_leaks_{start_date.isoformat()}_{end_date.isoformat()}"
    data = cache.get(cache_key)
    if data is None:
        data = fi.collect_leaks(start_date, end_date)
        cache.set(cache_key, data, 300)

    items = data["items"]
    if action in fi.ACTION_ORDER:
        items = [it for it in items if it["action"] == action]

    bucket_cards = []
    for a in data["action_order"]:
        b = data["buckets"].get(a)
        if not b:
            continue
        bucket_cards.append({
            "action": a,
            "label": data["action_labels"].get(a, a),
            "count": b["count"],
            "spend": b["spend"],
            "net": b["net"],
        })

    context = {
        "date_from": date_from,
        "date_to": date_to,
        "range_days": data["range"]["days"],
        "items": items[:400],          # cap rows rendered
        "item_count": len(items),
        "bucket_cards": bucket_cards,
        "selected_action": action,
    }
    return render(request, "dispatching/fleet_intel_leaks.html", context)


def _farmout_min_savings(request):
    """Read the ``min_savings`` query param (accepts '$20', '20', or '20.00'); fall back to the engine
    default ($20). Gates ONLY opportunity swaps — free in-house keeps + policy departures ignore it."""
    from dispatching.farmout_optimizer import DEFAULT_MIN_SAVINGS
    raw = (request.GET.get("min_savings") or "").replace("$", "").replace(",", "").strip()
    if not raw:
        return DEFAULT_MIN_SAVINGS
    try:
        val = Decimal(raw)
    except (InvalidOperation, ValueError, TypeError):
        return DEFAULT_MIN_SAVINGS
    return val if val >= 0 else DEFAULT_MIN_SAVINGS


@login_required(login_url="login")
def farmout_optimizer(request):
    """Farm-Out Opportunity-Cost Optimizer — standalone read-only planning page.

    Pick a service date (and optional min-savings threshold); for each job headed for farm-out it
    shows whether it's cheaper to keep it in-house (free, or by swapping a lower-cost leg out). The
    ANALYSIS is read-only and the rendered context is cached 5 min per (date, threshold, version)
    (the engine replays the board, ~1-2s). Mutations happen only through the page's Apply/Farm
    buttons -> ``farmout_apply`` below, which re-validates live state before writing.
    """
    if not request.user.is_staff:
        return redirect("dashboard")

    from dispatching import farmout_optimizer as fo
    from dispatching import farmout_report

    selected_date_str = request.GET.get("date")
    try:
        d = (datetime.strptime(selected_date_str, "%Y-%m-%d").date()
             if selected_date_str else timezone.localdate())
    except (ValueError, TypeError):
        d = timezone.localdate()

    ms = _farmout_min_savings(request)
    # Per-date version token: bumped by farmout_apply so an Apply invalidates every cached
    # threshold's entry for the date at once (min_savings varies, keys can't be enumerated).
    from dispatching.farmout_actions import farmout_page_cache_version
    cache_key = f"farmout_page_{d.isoformat()}_{ms}_v{farmout_page_cache_version(d)}"
    context = cache.get(cache_key)
    if context is None:
        report = fo.summarize_savings_range(d, d, min_savings=ms)
        context = farmout_report.build_page_context(report)
        # Freshness stamp: the page is a SNAPSHOT (cached, and it sits open in a tab) — showing
        # when it was computed stops "the timeline doesn't match my live board" confusion.
        _now = timezone.localtime()
        context["computed_at"] = _now.strftime("%I:%M %p").lstrip("0")
        cache.set(cache_key, context, 300)
    return render(request, "dispatching/farmout_optimizer.html", context)


@login_required
@require_POST
def farmout_apply(request):
    """Apply one Farm-Out Optimizer plan (keep / swap / farm) — the page's write endpoint.

    Thin JSON shim over ``dispatching.farmout_actions.apply_farmout_plan``, which re-validates
    CURRENT state (staleness guard, VIP/departure hard rules, affiliate eligibility + real
    capacity, live-board feasibility) and writes through ``set_leg_driver`` (front door), so
    held-day staging and all assignment side effects behave like any dispatch-board edit.
    """
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    from dispatching.farmout_actions import apply_farmout_plan
    status, payload = apply_farmout_plan(data, request.user)
    return JsonResponse(payload, status=status)


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

    drivers = Driver.objects.filter(driver_type="inhouse", is_active=True).select_related("profile").prefetch_related("weekly_schedule", "date_overrides")
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
        driver.default_shift_type = d_data.get("default_shift_type", "full_day")
        driver.default_max_hours = d_data.get("default_max_hours") or None
        driver.default_preferred_shift = d_data.get("default_preferred_shift", "")
        driver.default_preference = d_data.get("default_preference", "")
        if "notes" in d_data:
            driver.notes = d_data["notes"].strip() or None
        driver.save(update_fields=[
            "default_start_hour", "default_end_hour", "default_flexible",
            "default_shift_type", "default_max_hours", "default_preferred_shift",
            "default_preference", "notes",
        ])

        # Update weekly entries
        weekly = d_data.get("weekly", {})
        for day_str, entry in weekly.items():
            day = int(day_str)
            mh = entry.get("max_hours")
            DriverWeeklySchedule.objects.update_or_create(
                driver=driver,
                day_of_week=day,
                defaults={
                    "is_available": entry.get("is_available", True),
                    "shift_type": entry.get("shift_type", "full_day"),
                    "start_hour": int(entry.get("start_hour", 6)),
                    "end_hour": int(entry.get("end_hour", 23)),
                    "flexible": entry.get("flexible", True),
                    "max_hours": float(mh) if mh else None,
                    "preferred_shift": entry.get("preferred_shift", ""),
                    "preference": entry.get("preference", ""),
                    "scheduling_notes": entry.get("scheduling_notes", ""),
                },
            )
        updated_count += 1

    return JsonResponse({"success": True, "message": f"Updated schedules for {updated_count} drivers"})


@login_required(login_url="login")
def manage_driver_date_overrides(request):
    """Add, edit, or delete driver availability exceptions (off, partial-day, ranges)."""
    from drivers.models import DriverDateOverride
    from datetime import datetime as dt

    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

    def _parse_date(s):
        return dt.strptime(s, "%Y-%m-%d").date() if s else None

    def _parse_time(s):
        if not s:
            return None
        for fmt in ("%H:%M", "%H:%M:%S"):
            try:
                return dt.strptime(s, fmt).time()
            except ValueError:
                continue
        return None

    def _serialize(o):
        return {
            "id": o.id,
            "date": o.date.strftime("%Y-%m-%d"),
            "end_date": o.end_date.strftime("%Y-%m-%d") if o.end_date else None,
            "exception_type": o.exception_type,
            "exception_type_label": dict(DriverDateOverride.EXCEPTION_TYPE_CHOICES).get(o.exception_type, o.exception_type),
            "start_time": o.start_time.strftime("%H:%M") if o.start_time else None,
            "end_time": o.end_time.strftime("%H:%M") if o.end_time else None,
            "is_available": o.is_available,
            "reason": o.reason,
            "reason_label": dict(DriverDateOverride.REASON_CHOICES).get(o.reason, o.reason),
            "notes": o.notes,
            "date_range_display": o.date_range_display,
        }

    if request.method == "GET":
        driver_id = request.GET.get("driver_id")
        if not driver_id:
            return JsonResponse({"success": False, "error": "Missing driver_id"}, status=400)
        today = timezone.localdate()
        # Show single-day exceptions today/future and multi-day exceptions whose end is today/future
        qs = DriverDateOverride.objects.filter(driver_id=driver_id).filter(
            Q(end_date__isnull=True, date__gte=today) | Q(end_date__gte=today)
        ).order_by("date")
        return JsonResponse({"success": True, "overrides": [_serialize(o) for o in qs]})

    if request.method == "POST":
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

        action = data.get("action")

        if action in ("add", "edit"):
            driver_id = data.get("driver_id")
            override_id = data.get("id")
            date_str = data.get("date")
            end_date_str = data.get("end_date") or None
            exception_type = data.get("exception_type", "off")
            start_time_str = data.get("start_time") or None
            end_time_str = data.get("end_time") or None
            reason = data.get("reason", "day_off")
            notes = (data.get("notes") or "").strip()

            if action == "add" and not driver_id:
                return JsonResponse({"success": False, "error": "Missing driver_id"}, status=400)
            if not date_str:
                return JsonResponse({"success": False, "error": "Missing date"}, status=400)
            try:
                start_date = _parse_date(date_str)
                end_date = _parse_date(end_date_str)
                start_time = _parse_time(start_time_str)
                end_time = _parse_time(end_time_str)
            except ValueError:
                return JsonResponse({"success": False, "error": "Invalid date or time format"}, status=400)
            if end_date and end_date < start_date:
                return JsonResponse({"success": False, "error": "End date must be on or after start date"}, status=400)
            if exception_type not in dict(DriverDateOverride.EXCEPTION_TYPE_CHOICES):
                return JsonResponse({"success": False, "error": "Invalid exception type"}, status=400)
            # Time fields are required for the windowed exception types
            time_required = {
                "available_until":    (False, True),   # need end_time
                "available_after":    (True, False),   # need start_time
                "available_window":   (True, True),    # need both
                "unavailable_window": (True, True),
            }
            if exception_type in time_required:
                need_st, need_en = time_required[exception_type]
                if need_st and start_time is None:
                    return JsonResponse({"success": False, "error": "Start time is required for this exception type"}, status=400)
                if need_en and end_time is None:
                    return JsonResponse({"success": False, "error": "End time is required for this exception type"}, status=400)

            if action == "edit":
                if not override_id:
                    return JsonResponse({"success": False, "error": "Missing id"}, status=400)
                try:
                    obj = DriverDateOverride.objects.get(id=override_id)
                except DriverDateOverride.DoesNotExist:
                    return JsonResponse({"success": False, "error": "Exception not found"}, status=404)
            else:
                try:
                    driver = Driver.objects.get(id=driver_id, driver_type="inhouse")
                except Driver.DoesNotExist:
                    return JsonResponse({"success": False, "error": "Driver not found"}, status=404)
                # Squash accidental double-submits: if an equivalent override
                # already exists (pending or approved), return it instead of
                # creating another one.
                existing = DriverDateOverride.find_duplicate(
                    driver=driver, date=start_date, end_date=end_date,
                    exception_type=exception_type,
                    start_time=start_time, end_time=end_time,
                )
                if existing:
                    return JsonResponse({
                        "success": True, "created": False, "duplicate": True,
                        "override": _serialize(existing),
                    })
                obj = DriverDateOverride(driver=driver, created_by=request.user)

            obj.date = start_date
            obj.end_date = end_date
            obj.exception_type = exception_type
            obj.start_time = start_time
            obj.end_time = end_time
            obj.reason = reason
            obj.notes = notes
            obj.save()
            return JsonResponse({"success": True, "created": action == "add", "override": _serialize(obj)})

        elif action == "delete":
            override_id = data.get("id")
            if not override_id:
                return JsonResponse({"success": False, "error": "Missing id"}, status=400)
            deleted, _ = DriverDateOverride.objects.filter(id=override_id).delete()
            return JsonResponse({"success": True, "deleted": deleted > 0})

        return JsonResponse({"success": False, "error": "Unknown action"}, status=400)

    return JsonResponse({"success": False, "error": "Method not allowed"}, status=405)


@login_required(login_url="login")
def inhouse_schedule(request):
    """
    In-house driver schedule manager — V1 Heatmap design.

    Layout: dark page header (week selector), coverage strip, exceptions ribbon,
    filter row, driver × day grid, side drawer (Default / Exceptions / About /
    History) overlay for per-driver editing.

    Editing flows through the existing /save-driver-weekly-schedules/ and
    /driver-date-overrides/ JSON endpoints.
    """
    if not request.user.is_staff:
        messages.error(request, "Permission denied.")
        return redirect("legs_list")

    import hashlib as _hashlib
    import json as _json
    from datetime import date as _date_type

    DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    DAY_FULL  = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    COVERAGE_TARGET = 14

    # Map the project's shift_type enum onto the design's shift-type palette.
    # Project enum: morning / midday / evening / night / split / full_day / custom
    # Design palette buckets: morning, midday, evening, night, split, flex, set
    DESIGN_BUCKET = {
        "morning":  "morning",
        "midday":   "midday",
        "evening":  "evening",
        "night":    "night",
        "split":    "split",
        "full_day": "flex",     # full_day flexible == design's "flex"
        "custom":   "set",      # custom hours fall into "set hours"
    }

    def _fmt_hour(h):
        """Return a short label like 2a, 12p, 11p."""
        if h is None:
            return ""
        if h == 0:  return "12a"
        if h < 12:  return f"{h}a"
        if h == 12: return "12p"
        return f"{h - 12}p"

    def _fmt_hour_long(h):
        if h == 0:  return "12 AM"
        if h < 12:  return f"{h} AM"
        if h == 12: return "12 PM"
        return f"{h - 12} PM"

    def _fmt_time_short(t):
        if t is None:
            return ""
        h, m = t.hour, t.minute
        if m == 0:
            return _fmt_hour(h)
        suffix = "a" if h < 12 else "p"
        h12 = 12 if h % 12 == 0 else h % 12
        return f"{h12}:{m:02d}{suffix}"

    # Stable per-driver avatar color (the design assumes drivers have a
    # `color` field; we derive one from the driver id so it's consistent
    # across renders without a schema change).
    AVATAR_PALETTE = [
        "#C9A227", "#9B7BC4", "#7BAEC4", "#C47B95", "#E89B5C", "#5CB8E8",
        "#7BC49B", "#E8C95C", "#B85CE8", "#5CE89B", "#E85C95", "#5CE8E0",
        "#E8855C", "#9BE85C", "#5C95E8", "#E8A85C",
    ]
    def _color_for(driver):
        h = int(_hashlib.md5(str(driver.id).encode()).hexdigest()[:8], 16)
        return AVATAR_PALETTE[h % len(AVATAR_PALETTE)]

    today = timezone.localdate()

    # ── as-of date (drives the week range shown in the header subtitle
    # and which dates the cell exception ring overlay applies to) ──
    as_of = None
    date_param = request.GET.get("date")
    if date_param:
        try:
            as_of = _date_type.fromisoformat(date_param)
        except (ValueError, TypeError):
            as_of = None
    if as_of is None:
        as_of = today

    monday = as_of - timedelta(days=as_of.weekday())
    week_dates = [monday + timedelta(days=i) for i in range(7)]
    next_week_iso = (monday + timedelta(days=7)).isoformat()

    hour_choices = [{"value": h, "label": _fmt_hour_long(h)} for h in range(24)]

    inhouse_drivers = list(
        Driver.objects.filter(driver_type="inhouse", is_active=True)
        .select_related("profile")
        .prefetch_related("weekly_schedule", "date_overrides")
        .order_by("profile__first_name", "profile__last_name", "profile__username")
    )

    EXC_TYPE_LABELS = dict(DriverDateOverride.EXCEPTION_TYPE_CHOICES)
    REASON_LABELS = dict(DriverDateOverride.REASON_CHOICES)

    def _exception_short(o):
        et = o.exception_type
        if et == "off":                 return "Off"
        if et == "available_until":     return f"Until {_fmt_time_short(o.end_time)}" if o.end_time else "Until ?"
        if et == "available_after":     return f"After {_fmt_time_short(o.start_time)}" if o.start_time else "After ?"
        if et == "available_window":    return f"Window {_fmt_time_short(o.start_time)}-{_fmt_time_short(o.end_time)}"
        if et == "unavailable_window":  return f"Block {_fmt_time_short(o.start_time)}-{_fmt_time_short(o.end_time)}"
        if et == "flexible":            return "Flexible"
        if et == "note_only":           return "Note"
        return EXC_TYPE_LABELS.get(et, et)

    def _exception_design_type(o):
        """Map override → design exception-type palette: partial / pto / leave / swap / note."""
        if o.reason == "vacation":  return "pto"
        if o.reason == "sick":      return "leave"
        if o.exception_type == "off":
            # Multi-day off requests are PTO-tone, single-day are leave-tone.
            return "pto" if (o.end_date and o.end_date != o.date) else "leave"
        if o.exception_type == "note_only":
            return "swap"
        # available_until / after / window / unavailable_window / flexible all
        # mean "the driver is partly available" — partial-day tone.
        return "partial"

    def _exception_bucket_label(design_type):
        return {
            "partial": "Partial",
            "pto":     "PTO",
            "leave":   "Leave",
            "swap":    "Note",
        }.get(design_type, design_type.upper())

    # Bucket helper — design surfaces today / next 7 / next 30 / all upcoming.
    def _in_bucket(start_d, end_d, bucket):
        end_d = end_d or start_d
        diff_start = (start_d - today).days
        diff_end   = (end_d   - today).days
        if bucket == "today":  return diff_start <= 0 and diff_end >= 0
        if bucket == "week":   return diff_end >= 0 and diff_start <= 6
        if bucket == "month":  return diff_end >= 0 and diff_start <= 30
        if bucket == "all":    return diff_end >= 0
        return True

    # ── per-driver row data ──
    driver_rows = []
    all_exceptions = []   # flat list for the ribbon

    for driver in inhouse_drivers:
        weekly_map = {e.day_of_week: e for e in driver.weekly_schedule.all()}
        days = []
        working_days = 0
        total_hours = 0
        for day_idx in range(7):
            entry = weekly_map.get(day_idx)
            sh = entry.start_hour if entry else driver.default_start_hour
            eh = entry.end_hour   if entry else driver.default_end_hour
            avail = entry.is_available if entry else True
            pref  = entry.preference   if entry else driver.default_preference
            flex  = entry.flexible if entry else driver.default_flexible
            stype = entry.shift_type if entry else (driver.default_shift_type or "full_day")
            pshift = entry.preferred_shift if entry else driver.default_preferred_shift
            mhrs  = entry.max_hours if entry else driver.default_max_hours
            snotes = entry.scheduling_notes if entry else ""

            # "Open / flexible" only when the day is BOTH full-day AND flagged
            # flexible. A full_day shift with flexible=False is a fixed window
            # (e.g. 6a–5p) and must render as such. Mirrors the canonical
            # resolver in drivers/availability.py (_underlying_label /
            # _classify_status) — keep the two in sync.
            is_open_flex = (stype == "full_day" and flex)

            if not avail:
                design_bucket = "off"
            elif is_open_flex:
                design_bucket = "flex"
            elif stype == "full_day":
                design_bucket = "set"   # full day, but fixed hours
            else:
                design_bucket = DESIGN_BUCKET.get(stype, "flex")

            if avail:
                working_days += 1
                if is_open_flex:
                    total_hours += float(mhrs) if mhrs else 10  # truly flexible ≈ 10h
                else:
                    span = (eh - sh) if eh > sh else (24 - sh) + eh
                    total_hours += span

            if not avail:
                hours_label = ""
                shift_label = "Off"
            elif is_open_flex:
                hours_label = "open / flex"
                shift_label = "Flexible"
            else:
                hours_label = f"{_fmt_hour(sh)} → {_fmt_hour(eh)}"
                shift_label = {
                    "morning": "AM", "midday": "MID", "evening": "PM",
                    "night": "NIGHT", "split": "SPLIT", "custom": "SET",
                    "full_day": "SET",   # full day with fixed hours
                }.get(stype, stype.upper())

            days.append({
                "day_idx":     day_idx,
                "day_name":    DAY_NAMES[day_idx],
                "is_available": avail,
                "is_off":      not avail,
                "shift_type":  stype,
                "design_bucket": design_bucket,   # off / morning / midday / evening / night / split / flex / set
                "start_hour":  sh,
                "end_hour":    eh,
                "flexible":    flex,
                "locked":      not flex,
                "max_hours":   float(mhrs) if mhrs else None,
                "preferred_shift": pshift,
                "preference":  pref,
                "scheduling_notes": snotes,
                "hours_label": hours_label,
                "shift_label": shift_label,
            })

        # Per-driver exceptions (kept on the row + appended to the global list).
        driver_excs = []
        for o in driver.date_overrides.all():
            applies_future = (o.end_date or o.date) >= today
            if not applies_future:
                continue   # ribbon shows future-only; past lives in the drawer
            design_type = _exception_design_type(o)
            ex = {
                "id":            o.id,
                "driver_id":     driver.id,
                "date":          o.date.strftime("%Y-%m-%d"),
                "end_date":      o.end_date.strftime("%Y-%m-%d") if o.end_date else "",
                "date_short":    f"{o.date.strftime('%b')} {o.date.day}",
                "end_date_short": f"{o.end_date.strftime('%b')} {o.end_date.day}" if o.end_date else "",
                "date_display":  o.date_range_display,
                "exception_type": o.exception_type,
                "exception_type_label": EXC_TYPE_LABELS.get(o.exception_type, o.exception_type),
                "exception_short": _exception_short(o),
                "design_type":   design_type,
                "design_label":  _exception_bucket_label(design_type),
                "start_time":    o.start_time.strftime("%H:%M") if o.start_time else "",
                "end_time":      o.end_time.strftime("%H:%M")   if o.end_time   else "",
                "reason":        o.reason,
                "reason_label":  REASON_LABELS.get(o.reason, o.reason),
                "notes":         o.notes,
                "status":        o.status,
                "is_pending":    o.status == "pending",
                "_start":        o.date,
                "_end":          o.end_date or o.date,
            }
            driver_excs.append(ex)

        driver_excs.sort(key=lambda x: (x["_start"], x["_end"]))
        upcoming_count = sum(1 for e in driver_excs if _in_bucket(e["_start"], e["_end"], "all"))
        next7_count    = sum(1 for e in driver_excs if _in_bucket(e["_start"], e["_end"], "week"))

        # Per-row, per-day exception lookup (only checks the visible week so
        # we can paint the cell ring without scanning every override client-side).
        per_day_exc = [None] * 7
        for ex in driver_excs:
            for i, d in enumerate(week_dates):
                if ex["_start"] <= d <= ex["_end"]:
                    per_day_exc[i] = ex
        for i, day in enumerate(days):
            day["exception"] = per_day_exc[i]

        # Past exceptions live only in the drawer; serialize them too.
        past_excs = []
        for o in driver.date_overrides.all():
            applies_future = (o.end_date or o.date) >= today
            if applies_future:
                continue
            design_type = _exception_design_type(o)
            past_excs.append({
                "id": o.id,
                "driver_id": driver.id,
                "date": o.date.strftime("%Y-%m-%d"),
                "end_date": o.end_date.strftime("%Y-%m-%d") if o.end_date else "",
                "date_display": o.date_range_display,
                "exception_type": o.exception_type,
                "exception_type_label": EXC_TYPE_LABELS.get(o.exception_type, o.exception_type),
                "exception_short": _exception_short(o),
                "design_type": design_type,
                "design_label": _exception_bucket_label(design_type),
                "reason": o.reason,
                "reason_label": REASON_LABELS.get(o.reason, o.reason),
                "notes": o.notes,
            })
        past_excs.sort(key=lambda x: x["date"], reverse=True)

        all_exceptions.extend(driver_excs)

        # Strip the internal date objects before serializing for JS.
        for e in driver_excs:
            e.pop("_start", None)
            e.pop("_end", None)

        # Stub schedule version timeline (no DB-backed history yet — surface a
        # single 'Current' marker so the History tab is non-empty without
        # claiming false provenance).
        versions = [{
            "effective_from": "May 01, 2026",
            "note":           "Current schedule",
            "planned":        False,
            "current":        True,
            "change":         None,
        }]

        # Flag/avatar color, drawer-friendly meta
        first_name = (driver.profile.first_name or "").strip()
        last_name  = (driver.profile.last_name or "").strip()
        display_name = f"{first_name} {last_name}".strip() or driver.profile.username
        initials = ((first_name[:1] + last_name[:1]) or display_name[:2]).upper()
        avatar_color = _color_for(driver)

        driver_rows.append({
            "driver":         driver,
            "id":             driver.id,
            "name":           display_name,
            "initials":       initials,
            "color":          avatar_color,
            "phone":          driver.phone_number or "",
            "notes":          driver.notes or "",
            "days":           days,
            "exceptions":     driver_excs,
            "past_exceptions": past_excs,
            "exc_count":      len(driver_excs),
            "next7_count":    next7_count,
            "working_days":   working_days,
            "total_hours":    int(round(total_hours)),
            "versions":       versions,
            "version_count":  len(versions),
            "next_change":    None,           # stubbed — no planned-change model
            "search_key":     display_name.lower(),
        })

    # ── coverage rollup per day ──
    SHIFT_BUCKETS = ["morning", "midday", "evening", "night", "split", "flex", "set"]
    coverage = []
    for i in range(7):
        buckets = {k: 0 for k in SHIFT_BUCKETS}
        working = 0
        for row in driver_rows:
            day = row["days"][i]
            if day["is_off"]:
                continue
            working += 1
            buckets[day["design_bucket"]] = buckets.get(day["design_bucket"], 0) + 1
        delta = working - COVERAGE_TARGET
        if working >= COVERAGE_TARGET:
            tone = "good"
        elif working >= COVERAGE_TARGET - 2:
            tone = "warn"
        else:
            tone = "crit"
        # Stacked-bar segments (width as % of working).
        segments = []
        for k in SHIFT_BUCKETS:
            v = buckets[k]
            if v <= 0:
                continue
            segments.append({
                "key": k,
                "count": v,
                "pct": round((v / working) * 100, 2) if working else 0,
            })
        # Up to 4 breakdown chips, biggest first.
        chips = sorted(segments, key=lambda s: -s["count"])[:4]
        coverage.append({
            "day_idx":  i,
            "day_name": DAY_NAMES[i],
            "date":     week_dates[i],
            "date_short": f"{week_dates[i].strftime('%b')} {week_dates[i].day}",
            "is_weekend": i >= 5,
            "is_today": week_dates[i] == today,
            "is_past":  week_dates[i] < today,
            "working":  working,
            "total":    len(driver_rows),
            "delta":    delta,
            "delta_sign": "+" if delta >= 0 else "",
            "tone":     tone,
            "segments": segments,
            "chips":    chips,
        })

    # ── risk engine: per-day risk, action items, gaps, exception impact ──
    from dispatching.schedule_risk import (
        compute_week_risk, attach_exception_impacts,
        build_action_items, build_coverage_gaps,
        COVERAGE_TARGET_DEFAULT,
    )

    # Build (driver_id, date) → list-of-override-dicts lookup for the engine.
    # The existing view strips ``_start``/``_end`` from each exception
    # dict before this runs, so fall back to the iso strings.
    overrides_by_driver_date: dict = {}
    for r in driver_rows:
        for ex in r["exceptions"]:
            start = ex.get("_start")
            end = ex.get("_end") or start
            if start is None:
                try:
                    start = _date_type.fromisoformat(ex.get("date", ""))
                    end = (_date_type.fromisoformat(ex["end_date"])
                           if ex.get("end_date") else start)
                except ValueError:
                    continue
            for i, d in enumerate(week_dates):
                if start <= d <= end:
                    overrides_by_driver_date.setdefault((r["id"], d), []).append(ex)

    week_risk = compute_week_risk(
        driver_rows,
        overrides_by_driver_date,
        week_dates,
        target=COVERAGE_TARGET,
    )
    day_risks = week_risk["days"]

    # Merge engine output back into the legacy ``coverage`` cells so the
    # template can render the new badges alongside the existing stacked-bar
    # layout without a parallel data path.
    for cov_cell, dr in zip(coverage, day_risks):
        cov_cell["risk_level"]              = dr["risk_level"]
        cov_cell["risk_after_pending"]      = dr["risk_after_pending"]
        cov_cell["survives_one_callout"]    = dr["survives_one_callout"]
        cov_cell["survivability_label"]     = dr["survivability_label"]
        cov_cell["off_count"]               = dr["off_count"]
        cov_cell["pending_off_count"]       = dr["pending_off_count"]
        cov_cell["flexible_count"]          = dr["flexible_count"]
        cov_cell["scheduled_after_pending"] = dr["scheduled_after_pending"]
        cov_cell["delta_after_pending"]     = dr["delta_after_pending"]
        cov_cell["shift_gaps"]              = dr["shift_gaps"]
        cov_cell["gaps"]                    = dr["gaps"]
        cov_cell["recommended_actions"]     = dr["recommended_actions"]

    # Per-exception impact (mutates each ribbon entry's ex["impact"]).
    exception_impacts = attach_exception_impacts(
        week_risk, all_exceptions, week_dates, target=COVERAGE_TARGET,
    )
    # Decorate impact records with driver display info (used by the action-
    # items list above the page).
    drivers_by_id_for_impact = {r["id"]: r for r in driver_rows}
    for imp in exception_impacts:
        r = drivers_by_id_for_impact.get(imp["driver_id"])
        if r:
            imp["driver_name"] = r["name"]

    action_items = build_action_items(day_risks, exception_impacts)
    # Drop alerts for days that have already elapsed. The grid shows the full
    # current week (Mon–Sun), so e.g. on Saturday the Mon–Fri risk rollups are
    # past-due and just add noise — keep only today-onward (and any item with
    # no specific day).
    action_items = [
        it for it in action_items
        if it.get("day_idx") is None or week_dates[it["day_idx"]] >= today
    ]
    coverage_gaps = build_coverage_gaps(day_risks)

    # ── ribbon counts (across all drivers' upcoming exceptions) ──
    def _bucket_for(ex_list, bucket):
        return [e for e in ex_list if _in_bucket(_date_type.fromisoformat(e["date"]),
                                                  _date_type.fromisoformat(e["end_date"]) if e["end_date"] else _date_type.fromisoformat(e["date"]),
                                                  bucket)]
    bucket_counts = {
        "today": len(_bucket_for(all_exceptions, "today")),
        "week":  len(_bucket_for(all_exceptions, "week")),
        "month": len(_bucket_for(all_exceptions, "month")),
        "all":   len(all_exceptions),
    }
    # Sort ribbon entries by start date, attach driver display info.
    drivers_by_id = {r["id"]: r for r in driver_rows}
    ribbon = []
    for e in sorted(all_exceptions, key=lambda x: x["date"]):
        r = drivers_by_id[e["driver_id"]]
        item = dict(e)
        item["driver_name"] = r["name"]
        item["driver_initials"] = r["initials"]
        item["driver_color"] = r["color"]
        ribbon.append(item)

    # Serializable driver payload for the drawer (avoids re-rendering on every
    # drawer open — the page bundles all drivers up front).
    drivers_payload = []
    for r in driver_rows:
        drivers_payload.append({
            "id":            r["id"],
            "name":          r["name"],
            "initials":      r["initials"],
            "color":         r["color"],
            "phone":         r["phone"],
            "notes":         r["notes"],
            "working_days":  r["working_days"],
            "total_hours":   r["total_hours"],
            "version_count": r["version_count"],
            "next_change":   r["next_change"],
            "days":          [{
                "day_idx":     d["day_idx"],
                "day_name":    d["day_name"],
                "is_available": d["is_available"],
                "shift_type":  d["shift_type"],
                "design_bucket": d["design_bucket"],
                "start_hour":  d["start_hour"],
                "end_hour":    d["end_hour"],
                "flexible":    d["flexible"],
                "max_hours":   d["max_hours"],
                "preferred_shift": d["preferred_shift"],
                "preference":  d["preference"],
                "scheduling_notes": d["scheduling_notes"],
                "hours_label": d["hours_label"],
                "shift_label": d["shift_label"],
            } for d in r["days"]],
            "exceptions":    r["exceptions"],
            "past_exceptions": r["past_exceptions"],
            "exc_count":     r["exc_count"],
            "versions":      r["versions"],
        })

    # Serializable day-risk payload for the Day Detail drawer (mirrors
    # ``coverage`` but trimmed to what the JS drawer needs).
    day_risks_payload = []
    for dr in day_risks:
        day_risks_payload.append({
            "day_idx":              dr["day_idx"],
            "day_name":             dr["day_name"],
            "day_name_full":        dr["day_name_full"],
            "date":                 dr["date"].isoformat(),
            "date_short":           dr["date_short"],
            "target":               dr["target"],
            "scheduled_count":      dr["scheduled_count"],
            "scheduled_after_pending": dr["scheduled_after_pending"],
            "off_count":            dr["off_count"],
            "pending_off_count":    dr["pending_off_count"],
            "flexible_count":       dr["flexible_count"],
            "shift_coverage":       dr["shift_coverage"],
            "delta":                dr["delta"],
            "delta_label":          dr["delta_label"],
            "delta_after_pending":  dr["delta_after_pending"],
            "risk_level":           dr["risk_level"],
            "risk_after_pending":   dr["risk_after_pending"],
            "survives_one_callout": dr["survives_one_callout"],
            "survivability_label":  dr["survivability_label"],
            "shift_gaps":           dr["shift_gaps"],
            "gaps":                 dr["gaps"],
            "recommended_actions":  dr["recommended_actions"],
        })

    # Coverage strip shows only today-onward so elapsed days of the current
    # week don't clutter it. If the whole displayed week is already past (user
    # navigated backward), fall back to the full week rather than an empty strip.
    coverage_strip = [c for c in coverage if not c["is_past"]] or coverage

    context = {
        "driver_rows":         driver_rows,
        "drivers_payload_json": _json.dumps(drivers_payload),
        "coverage":            coverage,
        "coverage_strip":      coverage_strip,
        "coverage_target":     COVERAGE_TARGET,
        "day_risks":           day_risks,
        "day_risks_json":      _json.dumps(day_risks_payload),
        "action_items":        action_items,
        "coverage_gaps":       coverage_gaps,
        "exception_impacts":   exception_impacts,
        "ribbon":              ribbon,
        "ribbon_json":         _json.dumps(ribbon),
        "bucket_counts":       bucket_counts,
        "day_names":           DAY_NAMES,
        "day_full":            DAY_FULL,
        "week_dates":          week_dates,
        "week_start":          monday,
        "week_end":            week_dates[6],
        "as_of":               as_of,
        "as_of_iso":           as_of.isoformat(),
        "today_iso":           today.isoformat(),
        "next_week_iso":       next_week_iso,
        "is_this_week":        monday <= today <= week_dates[6],
        "hour_choices":        hour_choices,
        "shift_type_choices":  DriverWeeklySchedule.SHIFT_TYPE_CHOICES,
        "preferred_shift_choices": DriverWeeklySchedule.PREFERRED_SHIFT_CHOICES,
        "preference_choices":  DriverWeeklySchedule.PREFERENCE_CHOICES,
        "day_off_reason_choices": DriverDateOverride.REASON_CHOICES,
        "exception_type_choices": DriverDateOverride.EXCEPTION_TYPE_CHOICES,
        "today":               today,
        "today_legs_url":      f"/dispatching/?date={today.strftime('%Y-%m-%d')}",
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

    inhouse_drivers = list(
        Driver.objects.filter(driver_type="inhouse", is_active=True)
        .select_related("profile")
        .prefetch_related("weekly_schedule", "date_overrides")
        .order_by("profile__first_name", "profile__last_name")
    )

    # Vehicle assignments for the selected date (for showing vehicle info on timeline)
    from drivers.models import DriverVehicleAssignment
    _dsd_assignments = {
        a.driver_id: a
        for a in DriverVehicleAssignment.objects.filter(
            driver__in=inhouse_drivers, date=selected_date
        ).select_related("vehicle", "vehicle__vehicle_type")
    }

    _DSD_PREF_SHORT = {
        "prefer_arrival": "Pref Arrivals", "prefer_return": "Pref Returns",
        "prefer_cruise": "Pref Cruises", "heavy_arrival": "Heavy Arrivals",
        "heavy_return": "Heavy Returns", "heavy_cruise": "Heavy Cruises",
        "only_arrival": "Only Arrivals", "only_return": "Only Returns",
        "only_cruise": "Only Cruises",
    }

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
        _dsd_assign = _dsd_assignments.get(ds["driver"].id)
        _dsd_vnum = ''
        _dsd_vtype = ''
        _dsd_vnotes = ''
        if _dsd_assign and _dsd_assign.vehicle:
            _dsd_vnum = _dsd_assign.vehicle.vehicle_number or ''
            _dsd_vnotes = _dsd_assign.vehicle.notes or ''
            if _dsd_assign.vehicle.vehicle_type:
                _dsd_vtype = str(_dsd_assign.vehicle.vehicle_type)
        timeline_rows.append({
            "driver": ds["driver"],
            "is_available": sel["is_available"],
            "start_hour": sel["start_hour"],
            "end_hour": sel["end_hour"],
            "hours": sel["hours"],
            "preference": sel["preference"],
            "pref_short": _DSD_PREF_SHORT.get(sel["preference"], ''),
            "start_label": sel["start_label"],
            "end_label": sel["end_label"],
            "left_pct": left_pct,
            "width_pct": width_pct,
            "color": color,
            "vehicle_number": _dsd_vnum,
            "vehicle_type_label": _dsd_vtype,
            "vehicle_notes": _dsd_vnotes,
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
        build_sharer_partners,
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
            "reservation__vehicle", "vehicle",
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
        Driver.objects.filter(driver_type="inhouse", is_active=True, id__in=eligible_driver_ids)
        .select_related("profile")
    )

    # Load all legs for this date (assigned to in-house drivers)
    all_legs = list(
        Leg.objects.filter(pickup_date=target_date, driver__isnull=False, driver__driver_type="inhouse")
        .exclude(reservation__status="cancelled")
        .exclude(status="cancelled")
        .select_related(
            "reservation", "reservation__customer",
            "reservation__vehicle", "vehicle",
            "driver", "driver__profile", "flight_information",
        )
        .prefetch_related(
            # build_driver_schedules reads per-leg stop counts, the reservation's
            # payment_status and the controlling flight. Without these it N+1s
            # (~3 queries x leg) — 953 queries on a 161-leg day. Same prefetches
            # conflict_advisor.build_board_state carries, for the same reason.
            "legflight_set__flight", "legstop_set", "reservation__payments",
        )
    )

    # Build current schedules
    schedules = build_driver_schedules(all_legs, inhouse_drivers, target_date)
    driver_vtypes = load_all_driver_vtypes(target_date)
    all_legs_by_id = {leg.id: leg for leg in all_legs}

    # Shared-car partner map: a driver sharing one physical unit can't take a leg that
    # overlaps the partner's jobs, even if his own calendar is free at that time.
    sharer_partners = build_sharer_partners(
        {d.id for d in inhouse_drivers}, target_date)

    # Run swap search
    result = find_swaps(
        target_leg=target_leg,
        inhouse_schedules=schedules,
        all_legs_by_id=all_legs_by_id,
        driver_vtypes=driver_vtypes,
        target_date=target_date,
        sharer_partners=sharer_partners,
    )

    # One board for every solution — the planning-clock sweep caches onto it,
    # so drawing N solutions costs one sweep, not N. The target leg is added
    # explicitly: it is unassigned, so it is not in the in-house legs query.
    from dispatching.advisor_display import (safe_display, swap_board,
                                             swap_timeline)
    tl_legs = dict(all_legs_by_id)
    tl_legs.setdefault(target_leg.id, target_leg)
    tl_board = safe_display(swap_board, schedules, tl_legs, target_date)

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
            # The board after this swap, drawn by the SAME builder the Recovery
            # Advisor uses (dispatching/advisor_display.py). None if it could
            # not be built — the move list below it still renders.
            "timeline": (safe_display(swap_timeline, tl_board, sol.moves,
                                      target_leg.id)
                         if tl_board is not None else None),
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


class _SwapInfeasible(Exception):
    """Raised inside execute_swap's transaction to roll back an infeasible swap."""


def _revalidate_swap_feasibility(valid_moves, target_date):
    """Thin delegate — promoted to ``board_validation.revalidate_moves_against_db``
    (full docstring there) so the Recovery Advisor's apply path re-runs the exact
    same DB-backed check ``execute_swap`` uses. Kept under this name for existing
    call sites and the tests that patch it here."""
    from dispatching.board_validation import revalidate_moves_against_db
    return revalidate_moves_against_db(valid_moves, target_date)


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

    # Validate all moves before starting the transaction
    valid_moves = []
    for i, move in enumerate(moves):
        leg_id = move.get("leg_id")
        to_driver_id = move.get("to_driver_id")
        if not leg_id or not to_driver_id:
            return JsonResponse({
                "success": False,
                "error": f"Move {i+1} is missing leg_id or to_driver_id",
            }, status=400)
        valid_moves.append((int(leg_id), int(to_driver_id)))

    if not valid_moves:
        return JsonResponse({"success": False, "error": "No valid moves to apply"}, status=400)

    # Held day + granted user: stage the whole cascade in the draft overlay
    # (drivers see nothing; no live revalidation — drafts may be messy and the
    # manager reviews before publish, same contract as drag-drop staging).
    draft = _active_draft_for_date(target_date)
    if draft and can_use_sandbox(request.user):
        staged = 0
        for leg_id, to_driver_id in valid_moves:
            try:
                leg = Leg.objects.get(id=leg_id)
                driver = Driver.objects.get(id=to_driver_id)
            except (Leg.DoesNotExist, Driver.DoesNotExist):
                continue
            set_leg_driver(leg, driver, request.user, source="swap")
            staged += 1
        _log_draft_event(draft, "edited", actor=request.user, source="swap", count=staged)
        return JsonResponse({"success": True, "applied": staged, "held": True,
                             "message": f"Staged {staged} swap move(s) in the draft."})

    try:
        applied = 0
        with transaction.atomic():
            # Re-validate the FULL resulting board (Guards A+B+C) before persisting.
            # If any touched leg would be infeasible, roll back and write nothing.
            ok, reason = _revalidate_swap_feasibility(valid_moves, target_date)
            if not ok:
                raise _SwapInfeasible(reason)
            for leg_id, to_driver_id in valid_moves:
                leg = Leg.objects.select_for_update().get(id=leg_id)
                driver = Driver.objects.get(id=to_driver_id)
                set_leg_driver(leg, driver, request.user, source="swap")
                applied += 1
    except _SwapInfeasible as e:
        return JsonResponse({"success": False, "error": f"Swap rejected — would create an infeasible schedule: {e}"}, status=409)
    except Leg.DoesNotExist:
        return JsonResponse({"success": False, "error": f"Leg {leg_id} not found"}, status=404)
    except Driver.DoesNotExist:
        return JsonResponse({"success": False, "error": f"Driver {to_driver_id} not found"}, status=404)
    except Exception as e:
        logger.exception("execute_swap failed: %s", e)
        return JsonResponse({"success": False, "error": f"Swap failed: {e}"}, status=500)

    cache.delete(f"capacity_planner_{target_date.isoformat()}")
    return JsonResponse({"success": True, "applied": applied})


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
            # Front door: stages into the draft overlay when the day is held
            # and the user is a granted sandbox user; writes live otherwise.
            mode, _ = set_leg_driver(leg, driver, request.user, source="takeback")
    except Leg.DoesNotExist:
        return JsonResponse({"success": False, "error": "Leg not found"}, status=404)
    except Driver.DoesNotExist:
        return JsonResponse({"success": False, "error": "Driver not found"}, status=404)
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)

    cache.delete(f"capacity_planner_{target_date.isoformat()}")
    return JsonResponse({"success": True, "held": mode == "staged"})


@login_required
def swap_tester(request):
    """Standalone swap tester / debugger page."""
    if not request.user.is_staff:
        return redirect("dashboard")

    from dispatching.scheduler import (
        build_driver_schedules, build_sharer_partners, suggest_assignments_clustered,
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
            "reservation__vehicle", "vehicle",
            "driver", "driver__profile", "flight_information",
        )
        .prefetch_related(
            # build_driver_schedules reads per-leg stop counts, the reservation's
            # payment_status and the controlling flight. Without these it N+1s
            # (~3 queries x leg) — 953 queries on a 161-leg day. Same prefetches
            # conflict_advisor.build_board_state carries, for the same reason.
            "legflight_set__flight", "legstop_set", "reservation__payments",
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
    # Shared-car gate: don't offer a split-unit driver a job overlapping his partner's.
    sharer_partners = build_sharer_partners({d.id for d in inhouse_drivers}, selected_date)
    suggestions = suggest_assignments_clustered(
        unassigned_legs, schedules, selected_date, driver_vtypes=driver_vtypes,
        sharer_partners=sharer_partners,
    ) if unassigned_legs else []
    suggestion_map = {s.leg_id: s for s in suggestions}

    # Build no-fit legs (unassigned legs where suggestion has no driver)
    nofit_legs = []
    for leg in unassigned_legs:
        s = suggestion_map.get(leg.id)
        if s and s.suggested_driver_id:
            continue  # has a suggestion, not no-fit
        trip_type = leg.get_trip_type()
        vtype = leg.effective_vehicle_type
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
        vtype = leg.effective_vehicle_type
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
def reservation_sources(request):
    """
    Reservation source-attribution dashboard: reservation count + booked
    revenue per acquisition channel (Google / Meta / Travel Agents / Other),
    grouped by booking-created date, with sub-channel and per-travel-agent
    drill-downs and a filterable reservation list.

    Accuracy guarantee for the travel-agent edge case: ANY reservation linked to
    a travel agent is counted as Travel Agent, regardless of any gclid/fbclid/utm
    it also carries — and even if the stored booking_source drifted (e.g. the
    agent was linked after the booking was created). We derive the "effective
    source" live here rather than trusting the possibly-stale booking_source
    column, so the numbers are always right.
    """
    if not request.user.is_superuser:
        return redirect("dashboard")

    from datetime import datetime as _dt
    from decimal import Decimal as _Dec
    from django.db.models import Count, Sum, Case, When, Value, F, CharField, DecimalField
    from django.db.models.functions import Coalesce
    from django.core.paginator import Paginator
    from reservations.models import Reservation

    from reservations.attribution import CHANNEL_LABELS, CHANNEL_GROUPS, channel_label

    today = timezone.localdate()
    SOURCE_LABELS = dict(CHANNEL_LABELS)
    SOURCE_LABELS["travel_agent"] = "Travel Agent"

    # ── date range on booking CREATED date (rolling presets + custom) ──
    PRESETS = {"7": 7, "30": 30, "90": 90, "365": 365}
    days_param = (request.GET.get("days") or "90").strip()
    start = (request.GET.get("start") or "").strip()
    end = (request.GET.get("end") or "").strip()

    def _pd(s):
        try:
            return _dt.strptime(s, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None

    cstart, cend = _pd(start), _pd(end)
    if cstart or cend:
        days_param = "custom"
        end_date = cend or today
        start_date = cstart or (end_date - timedelta(days=89))
    elif days_param == "all":
        start_date, end_date = None, today
    else:
        if days_param not in PRESETS:
            days_param = "90"
        start_date = today - timedelta(days=PRESETS[days_param] - 1)
        end_date = today

    base = Reservation.objects.all()
    if start_date is not None:
        base = base.filter(created_at__date__gte=start_date)
    base = base.filter(created_at__date__lte=end_date)

    # ── effective source: travel agent ALWAYS wins over ad params ──
    eff = Case(
        When(travel_agent__isnull=False, then=Value("travel_agent")),
        default=F("booking_source"),
        output_field=CharField(),
    )
    rev_sum = Coalesce(
        Sum("total_price"),
        Value(0),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )

    rows = list(
        base.annotate(eff_source=eff)
        .values("eff_source")
        .annotate(count=Count("id"), revenue=rev_sum)
    )
    by_src = {r["eff_source"]: r for r in rows}
    total_count = sum(r["count"] for r in rows)
    total_revenue = sum((r["revenue"] for r in rows), _Dec("0"))

    def _row(key):
        r = by_src.get(key)
        c = r["count"] if r else 0
        rev = r["revenue"] if r else _Dec("0")
        return {
            "key": key,
            "label": channel_label(key),
            "count": c,
            "revenue": rev,
            "pct": round(c * 100.0 / total_count, 1) if total_count else 0,
        }

    # Group cards come from the central taxonomy (Search / AI / Social / Agents /
    # Referral / Direct). Any channel that shows up in the data but isn't a named
    # member of a group (e.g. a brand-new tagged utm_source) is appended to the
    # "Direct / Other" card below, so a real source is NEVER silently dropped.
    GROUP_DEFS = CHANNEL_GROUPS
    covered = {s for _k, _l, _c, subs in GROUP_DEFS for s in subs}
    leftover = sorted(
        k for k in by_src
        if k and k not in covered and k != "travel_agent"
    )

    groups = []
    for gkey, glabel, gcolor, subs in GROUP_DEFS:
        member_keys = list(subs)
        if gkey == "direct":
            member_keys = member_keys + leftover  # absorb unrecognized channels
        # Only show sub-rows that actually have data (keeps cards tight when a
        # taxonomy has many possible channels but the window used only a few).
        sub_rows = [_row(s) for s in member_keys if s in by_src]
        gcount = sum(s["count"] for s in sub_rows)
        grev = sum((s["revenue"] for s in sub_rows), _Dec("0"))
        # Name the specific channels for any multi-member group (Search / AI /
        # Social / Referral) so "ChatGPT", "Bing Ads", etc. are visible even when
        # they're the only populated channel in the group. Single-member groups
        # (Travel Agents) need no breakdown.
        show_subs = sub_rows if len(member_keys) > 1 else []
        groups.append({
            "key": gkey,
            "label": glabel,
            "color": gcolor,
            "count": gcount,
            "revenue": grev,
            "pct": round(gcount * 100.0 / total_count, 1) if total_count else 0,
            "avg": (grev / gcount) if gcount else _Dec("0"),
            "subs": show_subs,
        })

    # ── per-travel-agent breakdown ──
    agent_rows = list(
        base.filter(travel_agent__isnull=False)
        .values("travel_agent_id", "travel_agent__agent_name")
        .annotate(count=Count("id"), revenue=rev_sum)
        .order_by("-revenue")
    )

    # ── reservation list (filtered by channel / agent) ──
    channel = (request.GET.get("channel") or "all").strip()
    if channel == "travel_agent":
        channel = "travel_agents"
    agent_id = (request.GET.get("agent") or "").strip()

    GROUP_SUBS = {g[0]: list(g[3]) for g in GROUP_DEFS}
    GROUP_SUBS["direct"] = GROUP_SUBS.get("direct", []) + leftover  # match the card
    lst = base.select_related("customer", "rate__route", "travel_agent")
    if agent_id.isdigit():
        lst = lst.filter(travel_agent_id=int(agent_id))
        channel = "travel_agents"
    elif channel == "travel_agents":
        lst = lst.filter(travel_agent__isnull=False)
    elif channel in GROUP_SUBS:
        # non-agent channels: exclude travel-agent rows (they're reclassified)
        lst = lst.filter(travel_agent__isnull=True, booking_source__in=GROUP_SUBS[channel])
    elif channel != "all":
        # a single channel slug (known or brand-new) — filter on it directly
        lst = lst.filter(travel_agent__isnull=True, booking_source=channel)
    # channel == "all" → no extra filter

    channel_to_group = {ch: gkey for gkey, _l, _c, subs in GROUP_DEFS for ch in subs}
    lst = lst.order_by("-created_at")
    page_obj = Paginator(lst, 50).get_page(request.GET.get("page"))
    for r in page_obj:
        k = "travel_agent" if r.travel_agent_id else r.booking_source
        r.source_key = k
        r.source_label = channel_label(k)
        # group key drives the badge color (unrecognized -> "direct" bucket)
        r.source_group = "travel_agents" if r.travel_agent_id else channel_to_group.get(r.booking_source, "direct")

    qd = request.GET.copy()
    qd.pop("page", None)

    # Stored-column drift (independent of the date filter) — shown as a one-click
    # "Fix attribution" banner so the founder can repair it in-app (no command /
    # app restart). This page itself is already accurate (derives source live).
    from reservations.attribution import find_booking_source_drift
    _fwd, _rev = find_booking_source_drift()
    drift_count = _fwd.count() + _rev.count()

    context = {
        "drift_count": drift_count,
        "groups": groups,
        "agent_rows": agent_rows,
        "total_count": total_count,
        "total_revenue": total_revenue,
        "avg_value": (total_revenue / total_count) if total_count else _Dec("0"),
        "page_obj": page_obj,
        "channel": channel,
        "agent_id": agent_id,
        "days_param": days_param,
        "start_str": start_date.isoformat() if start_date else "",
        "end_str": end_date.isoformat(),
        "base_qs": qd.urlencode(),
        "source_labels": SOURCE_LABELS,
    }
    return render(request, "dispatching/reservation_sources.html", context)


@login_required(login_url="login")
def fix_booking_source_drift(request):
    """
    Superuser one-click repair of travel-agent drift in Reservation.booking_source,
    run in-request so the founder never needs a management command (which would
    mean a Railway one-off / possible restart). Fast (~dozens of UPDATEs in one
    transaction). POST-only; redirects back to the dashboard with a message.
    """
    if not request.user.is_superuser:
        return redirect("dashboard")
    if request.method != "POST":
        return redirect("reservation_sources")
    from reservations.attribution import repair_booking_source_drift
    result = repair_booking_source_drift(apply=True)
    if result["total"]:
        messages.success(
            request,
            f"Attribution fixed — {result['forward']} agent booking(s) re-tagged "
            f"Travel Agent and {result['reverse']} orphan label(s) corrected.",
        )
    else:
        messages.info(request, "Attribution already accurate — no drift to fix.")
    return redirect("reservation_sources")


@login_required(login_url="login")
def rederive_booking_sources(request):
    """
    Superuser one-click reclassification of EVERY booking's source under the
    current channel rules — the in-app equivalent of the
    ``rederive_booking_source --apply`` command, so the founder can pull older
    bookings into the new channels (Bing, ChatGPT, Gemini, ...) without a
    Railway one-off. Phone bookings are preserved. POST-only; redirects back
    with a summary message.
    """
    if not request.user.is_superuser:
        return redirect("dashboard")
    if request.method != "POST":
        return redirect("reservation_sources")
    from reservations.attribution import rederive_all_booking_sources, channel_label
    result = rederive_all_booking_sources(apply=True)
    if result["changed"]:
        # Lead with the most-moved channels so the founder sees the effect.
        top = "; ".join(
            f"{n} → {channel_label(new)}" for _old, new, n in result["transitions"][:4]
        )
        messages.success(
            request,
            f"Reclassified {result['changed']} booking(s) under the latest channel "
            f"rules ({top}).",
        )
    else:
        messages.info(request, "Sources already up to date — nothing to reclassify.")
    return redirect("reservation_sources")


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

    # ── Date range (rolling days OR calendar-month presets) ──
    from datetime import datetime as _dt, time as _time, date as _date
    now = timezone.now()
    today = timezone.localdate()

    def _aware(d):
        return timezone.make_aware(_dt.combine(d, _time.min))

    def _month_first(d):
        return d.replace(day=1)

    def _shift_month(first_of_month, delta):
        idx = (first_of_month.year * 12 + first_of_month.month - 1) + delta
        return _date(idx // 12, idx % 12 + 1, 1)

    period = (request.GET.get("period") or "").strip()
    days_param = request.GET.get("days", "30")
    end_date = now
    days_back = None          # which rolling tab is active (int) — None for month modes
    active_period = ""        # which month tab is active
    period_label = ""
    prev_label = ""
    prev_start = prev_end = None

    if period in ("this_month", "last_month"):
        active_period = period
        this_first = _month_first(today)
        if period == "this_month":
            start_date = _aware(this_first)
            end_date = now
            period_label = today.strftime("%B %Y") + " (MTD)"
            # Compare to the SAME span of last month (month-to-date pace)
            days_into = (today - this_first).days
            prev_first = _shift_month(this_first, -1)
            prev_start = _aware(prev_first)
            prev_end = _aware(prev_first + timedelta(days=days_into + 1))  # exclusive
            prev_label = prev_first.strftime("%b %Y") + " (same span)"
        else:  # last_month — full month
            lm_first = _shift_month(this_first, -1)
            start_date = _aware(lm_first)
            end_date = _aware(this_first)            # exclusive upper bound
            period_label = lm_first.strftime("%B %Y")
            pm_first = _shift_month(this_first, -2)
            prev_start = _aware(pm_first)
            prev_end = _aware(lm_first)
            prev_label = pm_first.strftime("%b %Y")
    elif days_param == "all":
        days_back = "all"
        start_date = None
        period_label = "All time"
    else:
        days_back = int(days_param)
        start_date = end_date - timedelta(days=days_back)
        period_label = f"Last {days_back} days"
        # Compare to the immediately preceding window of equal length
        prev_end = start_date
        prev_start = start_date - timedelta(days=days_back)
        prev_label = f"Previous {days_back} days"

    # ── Base queryset ──
    if start_date:
        leads_qs = Lead.objects.filter(created_at__gte=start_date, created_at__lt=end_date)
    else:
        leads_qs = Lead.objects.all()
    all_leads = leads_qs.count()

    # ── Period-over-period comparison (headline KPIs) ──
    def _lead_kpis(qs):
        a = qs.aggregate(
            leads_n=Count("id"),
            conv_n=Count("id", filter=Q(converted=True)),
            repl_n=Count("id", filter=Q(has_replied=True)),
            rev_sum=Sum(
                "converted_reservation__total_price",
                filter=Q(converted=True, converted_reservation__isnull=False),
            ),
        )
        n = a["leads_n"] or 0
        conv = a["conv_n"] or 0
        repl = a["repl_n"] or 0
        rev = a["rev_sum"] or Decimal("0.00")
        return {
            "leads": n,
            "converted": conv,
            "conv_rate": round(conv / n * 100, 1) if n else 0,
            "reply_rate": round(repl / n * 100, 1) if n else 0,
            "revenue": rev,
        }

    def _delta(cur, prev):
        """Percent change cur vs prev; None when there's no prior baseline."""
        if prev is None:
            return None
        cur, prev = float(cur), float(prev)
        if prev == 0:
            return {"pct": None, "abs": None, "dir": "up" if cur > 0 else "flat"}
        ch = (cur - prev) / prev * 100
        return {
            "pct": round(ch, 1),
            "abs": round(abs(ch), 1),
            "dir": "up" if ch > 0.05 else ("down" if ch < -0.05 else "flat"),
        }

    cmp_cur = _lead_kpis(leads_qs)
    if prev_start is not None:
        prev_qs = Lead.objects.filter(created_at__gte=prev_start, created_at__lt=prev_end)
        cmp_prev = _lead_kpis(prev_qs)
        comparison = {
            "has_prev": True,
            "prev_label": prev_label,
            "leads": _delta(cmp_cur["leads"], cmp_prev["leads"]),
            "conv_rate": _delta(cmp_cur["conv_rate"], cmp_prev["conv_rate"]),
            "reply_rate": _delta(cmp_cur["reply_rate"], cmp_prev["reply_rate"]),
            "revenue": _delta(cmp_cur["revenue"], cmp_prev["revenue"]),
            "prev_kpis": cmp_prev,
        }
    else:
        comparison = {"has_prev": False, "prev_label": "", "prev_kpis": None}

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

    # ── Source attribution — granular acquisition channels ──
    # source_data / revenue_by_source / source_quality / channel_groups are all
    # derived together from one per-lead classify_channel() pass further down (see
    # "Source quality" section) so every source surface on this page uses the SAME
    # taxonomy (Bing, ChatGPT, Gemini, … each on their own) instead of collapsing
    # into hardcoded Google/Meta/Organic/Direct buckets.

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

    # ════════════════════════════════════════════════════════════════
    # Funnel / sales-leak analytics (milestone-based, not status snapshot)
    # ════════════════════════════════════════════════════════════════
    D0 = Decimal("0.00")
    today = timezone.localdate()

    def _pct(n, d):
        return round(100.0 * n / d, 1) if d else 0

    # ── 1) True milestone funnel: Created → Contacted → Replied → Converted ──
    # Uses milestone flags/timestamps, so converted leads still count at every
    # earlier stage they passed (unlike the terminal `status` column).
    ms_total = all_leads
    ms_contacted = leads_qs.filter(
        Q(initial_sms_sent=True) | Q(initial_email_sent=True)
    ).count()
    ms_replied = leads_qs.filter(has_replied=True).count()
    ms_converted = converted_count
    milestone_funnel = [
        {"label": "Leads Created", "count": ms_total, "of_total": 100.0,
         "dropped": 0, "color": "var(--la-accent)"},
        {"label": "Contacted", "count": ms_contacted, "of_total": _pct(ms_contacted, ms_total),
         "dropped": ms_total - ms_contacted, "color": "var(--la-blue)"},
        {"label": "Replied", "count": ms_replied, "of_total": _pct(ms_replied, ms_total),
         "dropped": max(0, ms_contacted - ms_replied), "color": "var(--la-amber)"},
        {"label": "Converted", "count": ms_converted, "of_total": _pct(ms_converted, ms_total),
         "dropped": 0, "color": "var(--la-green)"},
    ]

    # ── 2) Speed-to-lead: conversion by time-to-first-contact bucket ──
    speed_contacted = (
        leads_qs.filter(initial_sms_sent_at__isnull=False)
        .annotate(ttf=ExpressionWrapper(
            F("initial_sms_sent_at") - F("created_at"), output_field=DurationField()))
        .filter(ttf__gt=timedelta(0))
    )
    _sp_defs = [
        ("≤5 min", timedelta(0), timedelta(minutes=5)),
        ("5–30 min", timedelta(minutes=5), timedelta(minutes=30)),
        ("30–60 min", timedelta(minutes=30), timedelta(hours=1)),
        ("1–6 hrs", timedelta(hours=1), timedelta(hours=6)),
        ("6–24 hrs", timedelta(hours=6), timedelta(days=1)),
        (">24 hrs", timedelta(days=1), timedelta(days=3650)),
    ]
    # Single aggregate: per-bucket leads + conversions via conditional counts.
    _sp_agg = {}
    for i, (name, lo, hi) in enumerate(_sp_defs):
        cond = Q(ttf__gte=lo, ttf__lt=hi)
        _sp_agg[f"n{i}"] = Count("id", filter=cond)
        _sp_agg[f"c{i}"] = Count("id", filter=cond & Q(converted=True))
    _sp_agg["fast_n"] = Count("id", filter=Q(ttf__lt=timedelta(minutes=30)))
    _sp_agg["fast_c"] = Count("id", filter=Q(ttf__lt=timedelta(minutes=30), converted=True))
    _sp_agg["slow_n"] = Count("id", filter=Q(ttf__gte=timedelta(hours=6)))
    _sp_agg["slow_c"] = Count("id", filter=Q(ttf__gte=timedelta(hours=6), converted=True))
    _sp = speed_contacted.aggregate(**_sp_agg)
    speed_data = []
    for i, (name, lo, hi) in enumerate(_sp_defs):
        n, c = _sp[f"n{i}"] or 0, _sp[f"c{i}"] or 0
        speed_data.append({"label": name, "leads": n, "converted": c, "conv_pct": _pct(c, n)})
    # Fast (<30m) vs slow (≥6h) + revenue at stake if slow matched fast
    fast_n, fast_c = _sp["fast_n"] or 0, _sp["fast_c"] or 0
    slow_n, slow_c = _sp["slow_n"] or 0, _sp["slow_c"] or 0
    speed_fast_conv = _pct(fast_c, fast_n)
    speed_slow_conv = _pct(slow_c, slow_n)
    speed_uplift_conv = round(slow_n * max(0.0, speed_fast_conv - speed_slow_conv) / 100.0)
    speed_uplift_revenue = Decimal(speed_uplift_conv) * (avg_lead_revenue or D0)

    # ── 3) Source quality: GRANULAR acquisition channels ──
    # Every lead is classified into a real channel (Google Ads vs Organic, Bing,
    # ChatGPT, Gemini, Perplexity, Meta, social, referral, direct, …) via the SAME
    # classify_channel() taxonomy that powers the Reservation Sources page — so a
    # brand-new tagged source (or a recognized organic referrer like chatgpt.com)
    # surfaces on its own instead of vanishing into "Organic / UTM" or "Direct".
    # Classification is per-lead in Python (one streamed query); the page is
    # superuser-only and lead volume is small, so this is cheap.
    from collections import defaultdict
    from reservations.attribution import (
        classify_channel, channel_label, CHANNEL_GROUPS,
    )

    _chan = defaultdict(lambda: {"leads": 0, "converted": 0, "revenue": D0})
    for utm_source, utm_medium, gclid, fbclid, ref_host, conv, price in (
        leads_qs.values_list(
            "utm_source", "utm_medium", "gclid", "fbclid", "referrer_host",
            "converted", "converted_reservation__total_price",
        ).iterator(chunk_size=2000)
    ):
        slug = classify_channel(
            src=utm_source, medium=utm_medium, gclid=gclid, fbclid=fbclid,
            referrer_host=ref_host,
        )
        acc = _chan[slug]
        acc["leads"] += 1
        if conv:
            acc["converted"] += 1
            if price:
                acc["revenue"] += price

    channel_rows = []
    for slug, acc in _chan.items():
        n = acc["leads"]
        channel_rows.append({
            "channel": slug,
            "label": channel_label(slug),
            "leads": n,
            "converted": acc["converted"],
            "conv_pct": _pct(acc["converted"], n),
            "revenue": acc["revenue"],
            "rev_per_lead": (acc["revenue"] / n) if n else D0,
        })

    # Source Quality table — every channel, busiest first (then by revenue).
    source_quality = sorted(
        channel_rows, key=lambda r: (r["leads"], r["revenue"]), reverse=True
    )
    # "Top sources by volume" + "Revenue by source" focused views (same data).
    source_data = source_quality[:10]
    revenue_by_source = sorted(
        (r for r in channel_rows if r["revenue"]),
        key=lambda r: r["revenue"], reverse=True,
    )[:10]

    # Channel-group rollup for the attribution card (Search / AI / Social / …).
    _group_of = {
        s: gkey for gkey, _glabel, _gcolor, slugs in CHANNEL_GROUPS for s in slugs
    }
    _grp = {
        gkey: {"label": glabel, "color": gcolor, "leads": 0}
        for gkey, glabel, gcolor, _slugs in CHANNEL_GROUPS
    }
    for slug, acc in _chan.items():
        # Unrecognized tagged sources fall into the catch-all "direct" group.
        _grp[_group_of.get(slug, "direct")]["leads"] += acc["leads"]
    channel_groups = [
        {**g, "pct": _pct(g["leads"], all_leads)}
        for g in _grp.values() if g["leads"]
    ]

    # ── 4) Open pipeline value + aging (current state, window-independent) ──
    _OPEN = ["new", "contacted", "interested", "future_contact"]
    open_pipe_qs = Lead.objects.filter(
        status__in=_OPEN, converted=False, pickup_date__gte=today
    )
    _pipe_defs = [("Next 3 days", 0, 3), ("4–7 days", 4, 7),
                  ("8–14 days", 8, 14), ("15–30 days", 15, 30),
                  ("30+ days", 31, 36500)]
    _pipe_agg = {"total_n": Count("id"), "total_v": Sum("estimated_price")}
    for i, (name, lo, hi) in enumerate(_pipe_defs):
        cond = Q(pickup_date__gte=today + timedelta(days=lo),
                 pickup_date__lte=today + timedelta(days=hi))
        _pipe_agg[f"n{i}"] = Count("id", filter=cond)
        _pipe_agg[f"v{i}"] = Sum("estimated_price", filter=cond)
    _pa = open_pipe_qs.aggregate(**_pipe_agg)
    open_pipe_count = _pa["total_n"] or 0
    open_pipe_value = _pa["total_v"] or D0
    pipe_buckets = [
        {"label": name, "leads": _pa[f"n{i}"] or 0, "value": _pa[f"v{i}"] or D0}
        for i, (name, lo, hi) in enumerate(_pipe_defs)
    ]
    pipe_max_value = max([b["value"] for b in pipe_buckets], default=D0) or D0

    context = {
        "days_back": days_back,
        "active_period": active_period,
        "period_label": period_label,
        "comparison": comparison,
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
        "channel_groups": channel_groups,
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
        # Funnel / sales-leak analytics
        "milestone_funnel": milestone_funnel,
        "speed_data": speed_data,
        "speed_fast_conv": speed_fast_conv,
        "speed_slow_conv": speed_slow_conv,
        "speed_slow_n": slow_n,
        "speed_uplift_conv": speed_uplift_conv,
        "speed_uplift_revenue": speed_uplift_revenue,
        "source_quality": source_quality,
        "open_pipe_count": open_pipe_count,
        "open_pipe_value": open_pipe_value,
        "pipe_buckets": pipe_buckets,
        "pipe_max_value": pipe_max_value,
    }
    return render(request, "dispatching/lead_analytics.html", context)


# =============================================
# AFFILIATE PAYMENT DASHBOARD
# =============================================

from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Min, Max
from users.models import TravelAgent, Agency, CommissionPayout, AgencyCommissionPayout
from users.services import (
    process_agent_payout as svc_process_agent_payout,
    process_agency_payout as svc_process_agency_payout,
    preview_agent_payout as svc_preview_agent_payout,
    preview_agency_payout as svc_preview_agency_payout,
    process_bulk_payouts as svc_process_bulk_payouts,
)


@staff_member_required
def affiliate_payments(request, section_lock=None):
    """Commission ops command center.

    Single hub view: KPIs across all owing payees, KPI breakdown by payment method,
    six tab sections (Pay Today / Agencies / Direct Agents / Missing Info / Overdue / History),
    plus the AJAX preview + bulk-mark-paid actions.

    `section_lock` is set by dedicated URLs (`affiliate_payments_agents`,
    `affiliate_payments_agencies`, `affiliate_payments_history`) so each
    entity type has its own bookmarkable URL. When unset, the legacy URL
    falls back to the `?section=` querystring with `pay_today` as default.
    """
    from django.db.models import F, ExpressionWrapper, DecimalField, Value
    from django.db.models.functions import Coalesce as CoalesceFunc
    from reservations.models import Reservation

    # ---------- Filters ----------
    VALID_SECTIONS = {"pay_today", "agencies", "agents", "missing", "overdue", "history"}
    if section_lock in VALID_SECTIONS:
        section = section_lock
    else:
        section = request.GET.get("section", "pay_today")
        if section not in VALID_SECTIONS:
            section = "pay_today"

    search = request.GET.get("q", "").strip()
    show = request.GET.get("show", "owing")  # owing | all
    sort = request.GET.get("sort", "amount")  # amount | name | date | overdue
    pay_method = request.GET.get("pay_method", "").strip()

    try:
        min_amount = Decimal(request.GET.get("min_amount", "") or "0")
    except (InvalidOperation, TypeError):
        min_amount = Decimal("0")
    if min_amount < 0:
        min_amount = Decimal("0")

    try:
        per_page = int(request.GET.get("per_page", 50))
    except (TypeError, ValueError):
        per_page = 50
    if per_page not in (25, 50, 100, 200):
        per_page = 50

    today_local = timezone.localtime(timezone.now())
    today = today_local.date()
    thirty_days_ago = timezone.now() - timedelta(days=30)
    month_start = today_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # ---------- Shared SQL expressions ----------
    # Commission = base_price * travel_agent.commission_rate / 100, evaluated on a Reservation row.
    res_commission_expr = ExpressionWrapper(
        F("base_price") * F("travel_agent__commission_rate") / Value(Decimal("100.00")),
        output_field=DecimalField(max_digits=12, decimal_places=4),
    )

    # ---------- AGENTS: per-agent Subqueries (avoid JOIN explosion + huge GROUP BY) ----------
    # SQL-level approximation of the Ready bucket from users.eligibility. We pick
    # a SUPERSET of Ready so no eligible agent is hidden -- the per-page Python
    # recalculation (_agent_live_unpaid) then provides the exact displayed amount.
    # Rules baked in here (matching get_commission_eligibility):
    #   commission_paid=False  -> not yet paid
    #   total_refunded=0       -> no refund (partial would go to Review)
    #   status != cancelled    -> not a hard-excluded reservation
    #   AND (status=completed  OR any non-cancelled leg is older than the grace window)
    # The leg-date OR fixes the original "stuck pending forever" bug where a dispatcher
    # forgot to click one leg completed; once a non-cancelled leg's pickup_date is at
    # least 1 day in the past, the reservation surfaces here.
    # NOTE: we intentionally do NOT require is_paid=True. The is_paid flag is set
    # only by Stripe payment signals; travel-agent bookings are typically settled
    # off-platform (invoiced/cash), so requiring it would silently empty the queue.
    res_eligible_q = (
        Q(commission_paid=False)
        & Q(total_refunded=0)
        & ~Q(status="cancelled")
        # Manually excluded (personal trips etc.) — mirrors the Python
        # eligibility engine so SQL-driven "has_unpaid" and the displayed
        # ready total agree. Without this, an agent whose only open
        # reservation has been excluded still shows under "Owing only"
        # with $0.00, because SQL counts the row but Python returns $0.
        & Q(commission_excluded=False)
        # No commissionable base price — mirrors get_commission_eligibility's
        # REASON_NO_COMMISSIONABLE_AMOUNT exclusion. Catches comped/freebie
        # reservations that would otherwise mark an agent as owing $0.
        & Q(base_price__gt=0)
    )
    # Precise 24h grace window matching users.eligibility._last_leg_datetime.
    # A "recent" non-cancelled leg = pickup_datetime within the last 24h. The
    # Python engine treats any reservation with a recent leg as still in the
    # grace bucket (pending, $0 Ready). We mirror that here so SQL has_unpaid
    # doesn't surface agents whose only "unpaid" reservation hasn't cleared
    # the grace window yet (the bug: $0.00 agents under "Owing only").
    #
    # Date-only check would over-include reservations with a leg yesterday
    # late at night (e.g. 11pm), which are still well within the 24h grace
    # at 8am the next morning.
    grace_cutoff_dt = timezone.localtime(timezone.now()) - timedelta(hours=24)
    grace_cutoff_date = grace_cutoff_dt.date()
    grace_cutoff_time = grace_cutoff_dt.time()
    recent_leg_q = (
        Q(pickup_date__gt=grace_cutoff_date)
        | (Q(pickup_date=grace_cutoff_date) & Q(pickup_time__gte=grace_cutoff_time))
    )
    recent_leg_exists = Exists(
        Leg.objects.filter(reservation=OuterRef("pk"))
        .exclude(status="cancelled")
        .filter(recent_leg_q)
    )
    # At least one non-cancelled leg exists at all (so we can distinguish
    # "no legs" — which Python flags REVIEW, not Ready — from "all legs in
    # the past more than 24h ago" — which IS Ready).
    any_leg_exists = Exists(
        Leg.objects.filter(reservation=OuterRef("pk")).exclude(status="cancelled")
    )

    # Ready candidate: status='completed' (fast path) OR
    # (has any non-cancelled leg) AND (no non-cancelled leg in last 24h).
    agent_unpaid_base = Reservation.objects.filter(
        travel_agent=OuterRef("pk")
    ).filter(res_eligible_q).annotate(
        _any_leg=any_leg_exists,
        _recent_leg=recent_leg_exists,
    ).filter(
        Q(status="completed") | (Q(_any_leg=True) & Q(_recent_leg=False))
    )

    agent_unpaid_subquery = (
        agent_unpaid_base
        .annotate(calc=res_commission_expr)
        .values("travel_agent")
        .annotate(total=Sum("calc"))
        .values("total")
    )
    agent_oldest_subquery = (
        agent_unpaid_base
        .values("travel_agent")
        .annotate(o=Min("created_at"))
        .values("o")
    )
    agent_res_count_subquery = (
        agent_unpaid_base
        .values("travel_agent")
        .annotate(c=Count("id"))
        .values("c")
    )

    # has_unpaid: cheap EXISTS used to short-circuit the WHERE for "show owing" filters.
    # Uses the same Ready-candidate filter so stuck-pending reservations DO mark an
    # agent as owing (the bug fix).
    agent_has_unpaid_subquery = agent_unpaid_base

    agents_qs_base = (
        TravelAgent.objects.filter(is_active=True)
        .select_related("user", "agency")
        .annotate(
            has_unpaid=Exists(agent_has_unpaid_subquery),
            live_unpaid=CoalesceFunc(
                Subquery(
                    agent_unpaid_subquery,
                    output_field=DecimalField(max_digits=12, decimal_places=4),
                ),
                Value(Decimal("0")),
                output_field=DecimalField(max_digits=12, decimal_places=4),
            ),
            oldest_unpaid=Subquery(agent_oldest_subquery),
            unpaid_res_count=CoalesceFunc(Subquery(agent_res_count_subquery), Value(0)),
        )
    )

    # Direct agents = paid directly (no agency OR agency doesn't handle payment).
    direct_agents_base = agents_qs_base.filter(
        Q(agency__isnull=True) | Q(agency_handles_payment=False)
    )
    # Agency-handled agents — used only to build the per-agency expandable body.
    agency_handled_agents_base = agents_qs_base.filter(
        agency__isnull=False, agency_handles_payment=True
    )

    # ---------- AGENCIES: same Ready-candidate filter, scoped through agency_handles_payment ----------
    # Agency-level subquery: same eligibility + same precise grace window.
    # Re-declared against the outer Reservation row so it joins correctly
    # when scoped through travel_agent__agency.
    agency_unpaid_base = Reservation.objects.filter(
        travel_agent__agency=OuterRef("pk"),
        travel_agent__agency_handles_payment=True,
    ).filter(res_eligible_q).annotate(
        _any_leg=any_leg_exists,
        _recent_leg=recent_leg_exists,
    ).filter(
        Q(status="completed") | (Q(_any_leg=True) & Q(_recent_leg=False))
    )

    agency_unpaid_subquery = (
        agency_unpaid_base
        .annotate(
            calc_commission=ExpressionWrapper(
                F("base_price") * F("travel_agent__commission_rate") / Value(Decimal("100.00")),
                output_field=DecimalField(max_digits=12, decimal_places=4),
            )
        )
        .values("travel_agent__agency")
        .annotate(total=Sum("calc_commission"))
        .values("total")
    )
    agency_oldest_subquery = (
        agency_unpaid_base
        .values("travel_agent__agency")
        .annotate(o=Min("created_at"))
        .values("o")
    )
    agency_res_count_subquery = (
        agency_unpaid_base
        .values("travel_agent__agency")
        .annotate(c=Count("id"))
        .values("c")
    )

    # Count of agents within the agency with any Ready-candidate reservation.
    # Uses the new eligibility filter so stuck-pending reservations correctly
    # count their agent as "owing".
    agency_owing_agent_count_subquery = (
        TravelAgent.objects.filter(
            agency=OuterRef("pk"),
            agency_handles_payment=True,
        )
        .filter(
            Exists(
                Reservation.objects.filter(travel_agent=OuterRef("pk"))
                .filter(res_eligible_q)
                .annotate(
                    _any_leg=any_leg_exists,
                    _recent_leg=recent_leg_exists,
                )
                .filter(Q(status="completed") | (Q(_any_leg=True) & Q(_recent_leg=False)))
            )
        )
        .values("agency")
        .annotate(c=Count("id", distinct=True))
        .values("c")
    )
    # Most-recent agency payout time. Subquery so the agencies query stays JOIN-free.
    agency_last_paid_subquery = (
        AgencyCommissionPayout.objects.filter(agency=OuterRef("pk"))
        .order_by("-paid_at")
        .values("paid_at")[:1]
    )

    # Cheap EXISTS used to filter agencies before the expensive SUM/COUNT subqueries run.
    agency_has_unpaid_subquery = agency_unpaid_base

    agencies_qs_base = (
        Agency.objects.filter(is_active=True)
        .prefetch_related("heads")
        .annotate(
            has_unpaid=Exists(agency_has_unpaid_subquery),
            unpaid_total=CoalesceFunc(
                Subquery(agency_unpaid_subquery),
                Value(Decimal("0")),
                output_field=DecimalField(max_digits=12, decimal_places=4),
            ),
            owing_agent_count=CoalesceFunc(
                Subquery(agency_owing_agent_count_subquery), Value(0)
            ),
            unpaid_res_count=CoalesceFunc(Subquery(agency_res_count_subquery), Value(0)),
            oldest_unpaid=Subquery(agency_oldest_subquery),
            last_paid_at=Subquery(agency_last_paid_subquery),
        )
    )

    # ---------- Global KPIs (aggregate directly from Reservation to avoid
    #               summing an already-aggregated annotation) ----------
    # Use the same Ready-candidate filter so KPIs match the per-agent display.
    direct_res_qs = (
        Reservation.objects.filter(travel_agent__is_active=True)
        .filter(res_eligible_q)
        .annotate(_any_leg=any_leg_exists, _recent_leg=recent_leg_exists)
        .filter(Q(status="completed") | (Q(_any_leg=True) & Q(_recent_leg=False)))
        .filter(Q(travel_agent__agency__isnull=True) | Q(travel_agent__agency_handles_payment=False))
        .annotate(calc=res_commission_expr)
    )
    agency_res_qs = (
        Reservation.objects.filter(
            travel_agent__is_active=True,
            travel_agent__agency__isnull=False,
            travel_agent__agency_handles_payment=True,
            travel_agent__agency__is_active=True,
        )
        .filter(res_eligible_q)
        .annotate(_any_leg=any_leg_exists, _recent_leg=recent_leg_exists)
        .filter(Q(status="completed") | (Q(_any_leg=True) & Q(_recent_leg=False)))
        .annotate(calc=res_commission_expr)
    )

    # Combine overall + missing aggregates into one DB round-trip each.
    missing_direct_q = (
        Q(travel_agent__payment_method__isnull=True) | Q(travel_agent__payment_method="")
        | Q(travel_agent__payment_info__isnull=True) | Q(travel_agent__payment_info="")
    )
    direct_owing_agg = direct_res_qs.aggregate(
        total=Sum("calc"),
        count=Count("travel_agent_id", distinct=True),
        missing_total=Sum("calc", filter=missing_direct_q),
        missing_count=Count("travel_agent_id", distinct=True, filter=missing_direct_q),
    )
    kpi_direct_owing_total = direct_owing_agg["total"] or Decimal("0")
    kpi_direct_owing_count = direct_owing_agg["count"] or 0

    missing_agency_q = (
        Q(travel_agent__agency__payment_method__isnull=True) | Q(travel_agent__agency__payment_method="")
        | Q(travel_agent__agency__payment_info__isnull=True) | Q(travel_agent__agency__payment_info="")
    )
    agencies_owing_agg = agency_res_qs.aggregate(
        total=Sum("calc"),
        count=Count("travel_agent__agency_id", distinct=True),
        missing_total=Sum("calc", filter=missing_agency_q),
        missing_count=Count("travel_agent__agency_id", distinct=True, filter=missing_agency_q),
    )
    kpi_agencies_owing_total = agencies_owing_agg["total"] or Decimal("0")
    kpi_agencies_owing_count = agencies_owing_agg["count"] or 0

    kpi_total_owing = kpi_direct_owing_total + kpi_agencies_owing_total
    kpi_missing_total = (direct_owing_agg["missing_total"] or Decimal("0")) + (agencies_owing_agg["missing_total"] or Decimal("0"))
    kpi_missing_count = (direct_owing_agg["missing_count"] or 0) + (agencies_owing_agg["missing_count"] or 0)

    # Overdue (>30 days). Approximate "payee count" by counting distinct payees whose oldest
    # unpaid is at least 30 days old. Easiest: aggregate by payee, filter by oldest, count.
    overdue_direct_payees = (
        direct_res_qs.values("travel_agent_id")
        .annotate(oldest=Min("created_at"), amt=Sum("calc"))
        .filter(oldest__lte=thirty_days_ago)
    )
    od_amt = Decimal("0"); od_cnt = 0
    for row in overdue_direct_payees:
        od_amt += row["amt"] or Decimal("0"); od_cnt += 1

    overdue_agency_payees = (
        agency_res_qs.values("travel_agent__agency_id")
        .annotate(oldest=Min("created_at"), amt=Sum("calc"))
        .filter(oldest__lte=thirty_days_ago)
    )
    for row in overdue_agency_payees:
        od_amt += row["amt"] or Decimal("0"); od_cnt += 1
    kpi_overdue_total = od_amt
    kpi_overdue_count = od_cnt

    # Paid this month — direct agent payouts (no agency) + every agency payout (which already
    # aggregates its children). Avoids double-counting agency-handled CommissionPayouts.
    paid_direct_agent = CommissionPayout.objects.filter(
        paid_at__gte=month_start, agency__isnull=True
    ).aggregate(total=Sum("total_amount"))["total"] or Decimal("0")
    paid_agency = AgencyCommissionPayout.objects.filter(
        paid_at__gte=month_start
    ).aggregate(total=Sum("total_amount"))["total"] or Decimal("0")
    kpi_paid_this_month = paid_direct_agent + paid_agency

    # ---------- By-method breakdown pills ----------
    PAYMENT_METHOD_LABELS = dict(TravelAgent.PAYMENT_METHOD_CHOICES)
    by_method_totals = {}

    def _bump(method_key, amount, count):
        key = (method_key or "").strip().lower() or "_missing"
        slot = by_method_totals.setdefault(key, {
            "key": key,
            "label": PAYMENT_METHOD_LABELS.get(key, "Missing info") if key != "_missing" else "Missing info",
            "amount": Decimal("0"),
            "count": 0,
        })
        slot["amount"] += amount or Decimal("0")
        slot["count"] += count or 0

    for row in (
        direct_res_qs.values("travel_agent__payment_method")
        .annotate(amount=Sum("calc"), count=Count("travel_agent_id", distinct=True))
    ):
        _bump(row["travel_agent__payment_method"], row["amount"], row["count"])

    for row in (
        agency_res_qs.values("travel_agent__agency__payment_method")
        .annotate(amount=Sum("calc"), count=Count("travel_agent__agency_id", distinct=True))
    ):
        _bump(row["travel_agent__agency__payment_method"], row["amount"], row["count"])

    # Quantize for display; preserve original keys order by amount desc.
    by_method_pills = sorted(
        by_method_totals.values(),
        key=lambda d: d["amount"] or Decimal("0"),
        reverse=True,
    )
    for slot in by_method_pills:
        slot["amount"] = (slot["amount"] or Decimal("0")).quantize(Decimal("0.01"))

    # Display-only: segment widths + a monochrome navy ramp (dark -> light by
    # size) so the template can render the "owed by method" data as one slim
    # stacked bar instead of a row of loud colored pills. Does not touch any
    # query/commission math — purely presentational annotations on the slots.
    by_method_total = sum((s["amount"] for s in by_method_pills), Decimal("0"))
    _NAVY_RAMP = [
        "#0F1B3D", "#233152", "#374768", "#4B5D7E", "#5F7393",
        "#8090B0", "#A6B2CB", "#C5CDDF", "#DEE3EE",
    ]
    for idx, slot in enumerate(by_method_pills):
        if by_method_total > 0:
            slot["pct"] = round(float(slot["amount"] / by_method_total) * 100, 2)
        else:
            slot["pct"] = 0
        slot["color"] = _NAVY_RAMP[idx] if idx < len(_NAVY_RAMP) else _NAVY_RAMP[-1]

    # ---------- Tab-specific filtering ----------
    agencies_qs = agencies_qs_base
    direct_agents_qs = direct_agents_base

    # Apply search across both querysets where present (so search works on every tab).
    if search:
        agencies_qs = agencies_qs.filter(
            Q(name__icontains=search)
            | Q(agents__agent_name__icontains=search)
            | Q(agents__user__email__icontains=search)
        ).distinct()
        direct_agents_qs = direct_agents_qs.filter(
            Q(agent_name__icontains=search)
            | Q(user__email__icontains=search)
            | Q(agency__name__icontains=search)
        )

    # show=owing applies to non-history tabs. Filter via the EXISTS annotation rather
    # than `live_unpaid__gt=0` so Postgres uses the partial unpaid-completed index instead
    # of recomputing a SUM subquery per agent/agency.
    if section != "history" and show == "owing":
        agencies_qs = agencies_qs.filter(has_unpaid=True)
        direct_agents_qs = direct_agents_qs.filter(has_unpaid=True)

    # min_amount filter
    if min_amount > 0:
        agencies_qs = agencies_qs.filter(unpaid_total__gte=min_amount)
        direct_agents_qs = direct_agents_qs.filter(live_unpaid__gte=min_amount)

    # Payment method filter (applies to direct agents; agencies have their own method but we
    # keep the UX simple — the pill filter narrows direct-agent list).
    if pay_method == "no_agency":
        direct_agents_qs = direct_agents_qs.exclude(payment_method="agency")
    elif pay_method == "none":
        direct_agents_qs = direct_agents_qs.filter(
            Q(payment_method__isnull=True) | Q(payment_method="")
        )
        agencies_qs = agencies_qs.filter(
            Q(payment_method__isnull=True) | Q(payment_method="")
        )
    elif pay_method:
        direct_agents_qs = direct_agents_qs.filter(payment_method=pay_method)
        agencies_qs = agencies_qs.filter(payment_method=pay_method)

    # Section-specific filters
    if section == "missing":
        direct_agents_qs = direct_agents_qs.filter(has_unpaid=True).filter(
            Q(payment_method__isnull=True) | Q(payment_method="")
            | Q(payment_info__isnull=True) | Q(payment_info="")
        )
        agencies_qs = agencies_qs.filter(has_unpaid=True).filter(
            Q(payment_method__isnull=True) | Q(payment_method="")
            | Q(payment_info__isnull=True) | Q(payment_info="")
        )
    elif section == "overdue":
        direct_agents_qs = direct_agents_qs.filter(
            has_unpaid=True, oldest_unpaid__lte=thirty_days_ago
        )
        agencies_qs = agencies_qs.filter(
            has_unpaid=True, oldest_unpaid__lte=thirty_days_ago
        )
    elif section == "agencies":
        # Show only agencies list; clear agents.
        direct_agents_qs = direct_agents_qs.none()
    elif section == "agents":
        agencies_qs = agencies_qs.none()
    elif section == "history":
        direct_agents_qs = direct_agents_qs.none()
        agencies_qs = agencies_qs.none()
    # pay_today: show both, but cap (handled below in pagination).

    # ---------- Sorting ----------
    # `sort` is a comma-separated list of `key:direction` pairs (e.g.
    # "amount:desc,overdue:asc"). Direct agents apply every key as a stable
    # tiebreaker chain; agencies use only the first key since their UI doesn't
    # expose multi-sort. The legacy single-value form (e.g. ?sort=amount) is
    # still accepted and defaults to descending.
    AGENT_SORT_FIELDS = {
        "amount": "live_unpaid",
        "name": "agent_name",
        "date": "last_payment_date",
        "overdue": "oldest_unpaid",
        "reservations": "unpaid_res_count",
    }
    AGENCY_SORT_FIELDS = {
        "amount": "unpaid_total",
        "name": "name",
        "date": "last_paid_at",
        "overdue": "oldest_unpaid",
        "reservations": "unpaid_res_count",
    }

    def _parse_sort_pairs(raw):
        out, seen = [], set()
        for chunk in (raw or "").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if ":" in chunk:
                key, direction = chunk.split(":", 1)
            else:
                key, direction = chunk, "desc"
            key = key.strip().lower()
            if key in seen or key not in AGENT_SORT_FIELDS:
                continue
            seen.add(key)
            out.append((key, direction.strip().lower() == "desc"))
        return out or [("amount", True)]

    sort_pairs = _parse_sort_pairs(sort)

    def _order_term(field_name, descending):
        # Per-field null handling matches the prior single-key behavior so
        # ordering doesn't change for users who haven't opted into multi-sort.
        f = F(field_name)
        if field_name in ("last_payment_date", "last_paid_at"):
            return f.desc(nulls_last=True) if descending else f.asc(nulls_first=True)
        if field_name == "oldest_unpaid":
            return f.desc(nulls_last=True) if descending else f.asc(nulls_last=True)
        return f.desc() if descending else f.asc()

    agent_order_terms = [_order_term(AGENT_SORT_FIELDS[k], desc) for k, desc in sort_pairs]
    agent_order_terms.append(F("agent_name").asc())  # stable final tiebreaker
    direct_agents_qs = direct_agents_qs.order_by(*agent_order_terms)

    first_key, first_desc = sort_pairs[0]
    agencies_qs = agencies_qs.order_by(
        _order_term(AGENCY_SORT_FIELDS[first_key], first_desc), "name"
    )

    # ---------- Pagination for direct agents ----------
    agents_paginator = Paginator(direct_agents_qs, per_page)
    agents_page = request.GET.get("agents_page", 1)
    try:
        agents_page_obj = agents_paginator.page(agents_page)
    except (PageNotAnInteger, EmptyPage):
        agents_page_obj = agents_paginator.page(1)

    # ---------- Materialize visible payees + bulk-compute Ready totals ----------
    # The SQL annotation (live_unpaid / unpaid_total) is a SUPERSET of the
    # Ready bucket -- it includes some pending reservations whose service
    # date is close to the grace boundary. We recompute the exact Ready
    # total in Python so the operator sees the same amount that "Pay Now"
    # will actually transfer. Doing this in bulk for every visible agent +
    # agency child in a single query pair avoids the N+1 that made this
    # page slow (one Reservation+legs fetch per agent per page-load).
    from users.eligibility import bulk_ready_totals

    page_agents = list(agents_page_obj)
    visible_agencies = list(agencies_qs)

    children_by_agency = {a.id: [] for a in visible_agencies}
    visible_children = []
    if visible_agencies:
        agency_ids = [a.id for a in visible_agencies]
        visible_children = list(
            agency_handled_agents_base.filter(agency_id__in=agency_ids, has_unpaid=True)
            .order_by("-live_unpaid")
        )
        for child in visible_children:
            children_by_agency.setdefault(child.agency_id, []).append(child)

    ready_agent_ids = {a.id for a in page_agents}
    ready_agent_ids.update(c.id for c in visible_children)
    ready_by_agent_id = bulk_ready_totals(ready_agent_ids)

    # ---------- Per-row enrichment (status flags, paid_today, overdue days) ----------
    def _enrich_payee(payee, amount_field):
        if isinstance(payee, TravelAgent):
            display = ready_by_agent_id.get(payee.id, Decimal("0"))
        else:
            # Agency total = sum of its visible children's Ready totals. Children
            # filtered to has_unpaid=True are the only non-zero contributors, so
            # iterating just those (instead of re-querying payee.agents.filter)
            # gives the same number with no extra query.
            display = sum(
                (ready_by_agent_id.get(c.id, Decimal("0")) for c in children_by_agency.get(payee.id, [])),
                Decimal("0"),
            ).quantize(Decimal("0.01"))
        payee.display_amount = display
        amount = display
        # Overdue
        oldest = getattr(payee, "oldest_unpaid", None)
        if oldest:
            delta = (today_local - oldest).days
            payee.overdue_days = max(delta, 0)
            payee.is_overdue = delta >= 30
        else:
            payee.overdue_days = 0
            payee.is_overdue = False
        # Missing info
        payee.is_missing_info = bool(amount) and not payee.payment_info_complete
        # Paid today
        last_paid = getattr(payee, "last_paid_at", None) or getattr(payee, "last_payment_date", None)
        payee.paid_today = bool(last_paid and timezone.localtime(last_paid).date() == today)
        # Status label
        if amount <= 0:
            payee.status_label = "paid_up"
        elif payee.is_overdue:
            payee.status_label = "overdue"
        elif payee.is_missing_info:
            payee.status_label = "missing_info"
        elif not last_paid:
            payee.status_label = "never_paid"
        else:
            payee.status_label = "ready"

    page_total_unpaid = Decimal("0")
    for agent in page_agents:
        _enrich_payee(agent, "live_unpaid")
        page_total_unpaid += agent.display_amount

    for child in visible_children:
        _enrich_payee(child, "live_unpaid")
    for agency in visible_agencies:
        _enrich_payee(agency, "unpaid_total")
        agency.child_agents = children_by_agency.get(agency.id, [])

    # ---------- Re-sort visible page by the *displayed* amount ----------
    # SQL sorts by live_unpaid (a SUPERSET of Ready), but we render the
    # Python-recomputed display_amount. When those diverge (e.g. a reservation
    # in the 24h grace window inflates live_unpaid but is excluded from Ready),
    # the on-screen order looks wrong. Re-sort the visible page here so the
    # rendered amounts are monotonic per the operator's chosen direction.
    # We only re-sort when amount is in the sort chain — other keys (name,
    # date, overdue, reservations) come straight from SQL columns that always
    # match what's rendered, so they don't need this correction.
    amount_pair = next(((k, d) for k, d in sort_pairs if k == "amount"), None)
    if amount_pair is not None:
        amount_desc = amount_pair[1]
        # Stable secondary keys mirror the SQL multi-sort chain so ties still
        # break by whatever the operator added (date, name, etc.). We walk the
        # chain in reverse and apply each key as a stable sort layer.
        def _key_for(payee, key):
            if key == "amount":
                return payee.display_amount or Decimal("0")
            if key == "date":
                v = getattr(payee, "last_payment_date", None) or getattr(payee, "last_paid_at", None)
                # None sorts last on desc, first on asc — match _order_term semantics.
                return v or (datetime.min.replace(tzinfo=timezone.get_current_timezone()))
            if key == "overdue":
                return getattr(payee, "oldest_unpaid", None) or (datetime.min.replace(tzinfo=timezone.get_current_timezone()))
            if key == "reservations":
                return getattr(payee, "unpaid_res_count", 0) or 0
            if key == "name":
                return (getattr(payee, "agent_name", "") or "").lower()
            return 0
        for key, desc in reversed(sort_pairs):
            page_agents.sort(key=lambda p, k=key: _key_for(p, k), reverse=desc)
        # Reflect the new order in the paginator's object_list so anything
        # downstream (Pay Today scoring, etc.) sees the same sequence.
        agents_page_obj.object_list = page_agents

    # Agencies use only the primary sort key (their UI doesn't expose
    # multi-sort), and their displayed total is summed in Python from their
    # children's display_amount — so the same SQL/Python divergence applies.
    if sort_pairs and sort_pairs[0][0] == "amount":
        visible_agencies.sort(
            key=lambda a: a.display_amount or Decimal("0"),
            reverse=sort_pairs[0][1],
        )

    # ---------- Pay Today: curated recommended queue ----------
    # Reuse the already-loaded visible_agencies and agents_page_obj instead of
    # firing two more heavy aggregate queries — the page-1 set (sorted by amount)
    # plus the full owing-agencies list is plenty to score top-8 by amount × overdue.
    pay_today_recommended = []
    if section == "pay_today":
        candidates = []
        for agent in agents_page_obj.object_list:
            amt = getattr(agent, "display_amount", None) or Decimal("0")
            if amt <= 0:
                continue
            score = float(amt) * max(getattr(agent, "overdue_days", 0) or 0, 1)
            candidates.append((score, agent, "agent"))
        for agency in visible_agencies:
            amt = getattr(agency, "display_amount", None) or Decimal("0")
            if amt <= 0:
                continue
            score = float(amt) * max(getattr(agency, "overdue_days", 0) or 0, 1)
            candidates.append((score, agency, "agency"))
        candidates.sort(key=lambda t: t[0], reverse=True)
        pay_today_recommended = [
            {"payee": p, "kind": k} for _, p, k in candidates[:8]
        ]

    # ---------- Payout history ----------
    # Only build the heavy paginated/prefetched querysets when the user is on
    # the History tab. Other tabs only need the total count for the nav badge.
    history_tab = request.GET.get("history_tab", "agency")
    agency_payouts_page_obj = None
    agent_payouts_page_obj = None
    if section == "history":
        agency_payouts = (
            AgencyCommissionPayout.objects.select_related("agency")
            .prefetch_related("agent_payouts__agent__user")
            .order_by("-paid_at")
        )
        agency_payouts_paginator = Paginator(agency_payouts, 15)
        agency_payouts_page = request.GET.get("ap_page", 1)
        try:
            agency_payouts_page_obj = agency_payouts_paginator.page(agency_payouts_page)
        except (PageNotAnInteger, EmptyPage):
            agency_payouts_page_obj = agency_payouts_paginator.page(1)

        agent_payouts = (
            CommissionPayout.objects.select_related("agent", "agent__user", "agency")
            .order_by("-paid_at")
        )
        agent_payouts_paginator = Paginator(agent_payouts, 15)
        agent_payouts_page = request.GET.get("cp_page", 1)
        try:
            agent_payouts_page_obj = agent_payouts_paginator.page(agent_payouts_page)
        except (PageNotAnInteger, EmptyPage):
            agent_payouts_page_obj = agent_payouts_paginator.page(1)
        total_payouts = agent_payouts_paginator.count + agency_payouts_paginator.count
    else:
        total_payouts = (
            CommissionPayout.objects.count() + AgencyCommissionPayout.objects.count()
        )

    context = {
        # tab + filter state
        "section": section,
        "search": search,
        "show": show,
        "sort": sort,
        "pay_method": pay_method,
        "min_amount": min_amount,
        "per_page": per_page,

        # main lists
        "agencies": visible_agencies,
        "agents_page_obj": agents_page_obj,
        "page_total_unpaid": page_total_unpaid.quantize(Decimal("0.01")),
        "pay_today_recommended": pay_today_recommended,

        # KPIs
        "total_owing": kpi_total_owing.quantize(Decimal("0.01")),
        "total_owing_agencies": kpi_agencies_owing_count,
        "total_owing_agencies_amount": kpi_agencies_owing_total.quantize(Decimal("0.01")),
        "total_owing_agents": kpi_direct_owing_count,
        "total_owing_agents_amount": kpi_direct_owing_total.quantize(Decimal("0.01")),
        "kpi_missing_count": kpi_missing_count,
        "kpi_missing_total": kpi_missing_total.quantize(Decimal("0.01")),
        "kpi_overdue_count": kpi_overdue_count,
        "kpi_overdue_total": kpi_overdue_total.quantize(Decimal("0.01")),
        "kpi_paid_this_month": kpi_paid_this_month.quantize(Decimal("0.01")),

        # tab counters (so nav can show numbers)
        "pay_today_count": kpi_direct_owing_count + kpi_agencies_owing_count,
        "missing_info_count": kpi_missing_count,
        "overdue_count": kpi_overdue_count,

        # by-method pills
        "by_method_pills": by_method_pills,
        "by_method_total": by_method_total.quantize(Decimal("0.01")),

        # history
        "history_tab": history_tab,
        "agency_payouts_page_obj": agency_payouts_page_obj,
        "agent_payouts_page_obj": agent_payouts_page_obj,
        "total_payouts": total_payouts,

        # for the review modal dropdown
        "payment_method_choices": TravelAgent.PAYMENT_METHOD_CHOICES,
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
@require_POST
def process_bulk_payout_view(request):
    """AJAX endpoint that marks many payees paid in one operator action.

    Request body (JSON):
      {
        "items": [
          {"type": "agent"|"agency", "id": int, "reference": str?, "method": str?, "email": bool?},
          ...
        ],
        "default_method": str?,
        "default_reference": str?,
        "send_email": bool?
      }

    Each item runs independently — one failure does not roll back others.
    Returns { success, processed, failed, items: [{ok, id, ...}, ...] }.
    """
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON."}, status=400)

    raw_items = data.get("items") or []
    if not isinstance(raw_items, list) or not raw_items:
        return JsonResponse({"success": False, "error": "No items to process."}, status=400)

    default_method = (data.get("default_method") or "").strip()
    default_reference = (data.get("default_reference") or "").strip()
    default_email = bool(data.get("send_email"))

    items = []
    for raw in raw_items[:200]:  # safety cap per request
        if not isinstance(raw, dict):
            continue
        items.append({
            "type": raw.get("type"),
            "id": raw.get("id"),
            "reference": (raw.get("reference") or default_reference or "").strip(),
            "method": (raw.get("method") or default_method or "").strip(),
            "email": bool(raw.get("email", default_email)),
        })

    results = svc_process_bulk_payouts(items, sent_by=request.user)
    processed = sum(1 for r in results if r.get("ok"))
    failed = len(results) - processed
    return JsonResponse({
        "success": True,
        "processed": processed,
        "failed": failed,
        "items": results,
    })


def _recompute_commission_amount(reservation):
    """Recompute commission_amount = base_price * rate, with rounding to 2dp.

    Returns Decimal("0") when there's no agent or no base price. Used when
    un-excluding a reservation, so the stored amount matches what the
    eligibility engine would calculate from the current agent rate.
    """
    agent = reservation.travel_agent
    if agent is None or reservation.base_price is None:
        return Decimal("0")
    rate = (agent.commission_rate or Decimal("0")) / Decimal("100")
    return (reservation.base_price * rate).quantize(Decimal("0.01"))


@staff_member_required
@require_POST
def toggle_reservation_commission_exclusion(request):
    """Mark a reservation as non-commissionable (or restore it).

    Body (JSON):
      { "reservation_id": int, "exclude": bool, "reason": str? }

    When `exclude` is true, the reservation drops into the agent's Excluded
    bucket via `get_commission_eligibility`. When false, the flag is cleared
    and the reservation flows back through the normal pipeline. The reason
    string is shown verbatim in the Excluded bucket so the agent knows why.
    """
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON."}, status=400)

    res_id = data.get("reservation_id")
    if not res_id:
        return JsonResponse({"success": False, "error": "Missing reservation_id."}, status=400)

    exclude = bool(data.get("exclude", True))
    reason = (data.get("reason") or "").strip()[:255]

    reservation = get_object_or_404(Reservation, id=res_id)
    if reservation.commission_paid:
        return JsonResponse({
            "success": False,
            "error": "Commission already paid — cannot change exclusion.",
        }, status=400)

    if exclude:
        reservation.commission_excluded = True
        reservation.commission_exclusion_reason = reason or "Personal trip — non-commissionable"
        reservation.commission_excluded_at = timezone.now()
        reservation.commission_excluded_by = request.user
        # Zero the stored commission so dashboards, lifetime stats, and any
        # downstream consumer that reads commission_amount directly see $0.
        reservation.commission_amount = Decimal("0")
    else:
        reservation.commission_excluded = False
        reservation.commission_exclusion_reason = ""
        reservation.commission_excluded_at = None
        reservation.commission_excluded_by = None
        # Recompute from current rate when un-excluding so the stored amount
        # matches what the eligibility engine would calculate.
        reservation.commission_amount = _recompute_commission_amount(reservation)

    reservation.save(update_fields=[
        "commission_excluded",
        "commission_exclusion_reason",
        "commission_excluded_at",
        "commission_excluded_by",
        "commission_amount",
    ])
    return JsonResponse({
        "success": True,
        "reservation_id": reservation.id,
        "excluded": reservation.commission_excluded,
        "reason": reservation.commission_exclusion_reason,
    })


@staff_member_required
@require_POST
def toggle_reservation_vip(request):
    """Flag/unflag a reservation as VIP (gold board highlight).

    Body (JSON): { "reservation_id": int, "is_vip": bool }

    VIP is a per-reservation flag, so toggling from any one of its legs lights up
    all of them. Travel-agency VIPs (Small World Big Fun) already show as VIP via
    Leg.is_vip without this flag; this is for the "other reservations I select"
    case. Saves only the single field so no other signals/recalcs are triggered.
    """
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON."}, status=400)

    res_id = data.get("reservation_id")
    if not res_id:
        return JsonResponse({"success": False, "error": "Missing reservation_id."}, status=400)

    reservation = get_object_or_404(Reservation, id=res_id)
    reservation.is_vip = bool(data.get("is_vip", not reservation.is_vip))
    reservation.save(update_fields=["is_vip"])

    # The planner caches its driver schedules per date for 60s; drop the cache for
    # every date this reservation touches so the gold VIP highlight shows on the
    # next load instead of lagging up to a minute.
    for d in (
        reservation.legs.exclude(pickup_date__isnull=True)
        .values_list("pickup_date", flat=True)
        .distinct()
    ):
        cache.delete(f"capacity_planner_{d.isoformat()}")

    return JsonResponse({
        "success": True,
        "reservation_id": reservation.id,
        "is_vip": reservation.is_vip,
    })


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

    # Group by (last_name_lower, phone_last10, pickup_date) so dupes across
    # separate Customer rows (e.g. same person booked under two different emails)
    # still collapse together. Falls back to first_name if last_name is blank.
    groups = defaultdict(list)
    for res in reservations:
        customer = res.customer
        if not customer:
            continue
        first_leg = res.legs.all().first()
        if not first_leg:
            continue
        phone_digits = "".join(ch for ch in (customer.phone_number or "") if ch.isdigit())[-10:]
        if not phone_digits:
            continue
        name_part = (customer.last_name or customer.first_name or "").strip().lower()
        if not name_part:
            continue
        key = (name_part, phone_digits, first_leg.pickup_date)
        groups[key].append(res)

    # Find groups where at least one paid + one unpaid
    duplicate_groups = []
    total_unpaid = 0
    for (_name_part, _phone_digits, pickup_date), res_list in groups.items():
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
# All pricing rules live in dispatching/quote_engine.py so they can be tested
# without a request and validated against the published rate card. See
# dispatching/tests_quote_engine.py and docs/quote-calculator-audit.md.
#
# OPEN TO ALL DISPATCHERS (is_staff) as of 2026-07-29. The page carries a standing
# "Demo - still in progress" banner telling dispatchers to double-check unusual
# numbers with Ab & Ray before quoting a guest, which is what makes that safe
# while the rates are still being calibrated. SOP-002 D06 updated to match.


# Static art per vehicle for the calculator's picker. Mirrors the landing page's
# optimized assets rather than Vehicle.image, which is not guaranteed to exist in
# every environment.
QUOTE_VEHICLE_IMAGES = {
    "towncar": "images/towncar.webp",
    "mini_van": "images/minivan.webp",
    "suv": "images/suburban.webp",
    "van": "images/van.webp",
    "Van(14 Pax)": "images/sprinter.webp",
}


def _quote_result_to_json(result):
    """Serialise a QuoteResult for the calculator page.

    `price` is the only figure to show a guest. `internal` and `notes` are the
    dispatcher-only panel — they explain where the number came from, including
    the empty-return share the guest never sees.
    """
    internal = {k: str(v) for k, v in result.breakdown.items()}
    internal["direction"] = result.direction
    return {
        "price": str(result.price),
        "oneway": str(result.oneway_price) if result.oneway_price is not None else None,
        "roundtrip": (
            str(result.roundtrip_price) if result.roundtrip_price is not None else None
        ),
        "vehicle_type": result.vehicle_type,
        "vehicle_label": result.vehicle_label,
        "source": result.source,
        "source_label": {
            quote_engine.SOURCE_RATE_CARD: "Published rate card",
            quote_engine.SOURCE_LOCAL_CUSTOM: "Local custom",
        }.get(result.source, "Custom estimate"),
        "card_route": result.card_route,
        "gratuity_suggested": result.gratuity_suggested,
        "gratuity_mandatory": result.gratuity_mandatory,
        "internal": internal,
        "notes": result.notes,
    }


@login_required(login_url="login")
def quote_calculator(request):
    """Quote calculator page — any dispatcher."""
    if not request.user.is_staff:
        return redirect("dashboard")

    # Driven by the rate config, not the Vehicle table: the dropdown should
    # offer exactly what can be priced, and it must agree with the all-vehicles
    # comparison in the result. (Previously it was every Vehicle row while the
    # formula silently fell back to towncar pricing for anything unrecognised —
    # and it went blank entirely if the Vehicle table was empty.)
    vehicles = [
        {"value": vt, "label": rates.label}
        for vt, rates in quote_engine.VEHICLE_RATES.items()
    ]
    # The rate STRUCTURE (base fees, per-mile rates, minimums, hourly floors) is
    # deliberately not passed to the template. Dispatchers get the price, plus a
    # per-quote internal breakdown they can open if a guest pushes back — not the
    # standing rate sheet for every vehicle.
    context = {
        "vehicles": vehicles,
        "vehicle_images": {
            vt: static(path)
            for vt, path in QUOTE_VEHICLE_IMAGES.items()
            if vt in quote_engine.VEHICLE_RATES
        },
        "google_maps_key": getattr(settings, "GOOGLE_MAPS_API_KEY", ""),
    }
    return render(request, "dispatching/quote_calculator.html", context)


@login_required(login_url="login")
@require_POST
def quote_calculator_api(request):
    """AJAX endpoint: price a trip from pickup/dropoff addresses."""
    if not request.user.is_staff:
        return JsonResponse({"error": "Unauthorized"}, status=403)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid request"}, status=400)

    pickup = (data.get("pickup") or "").strip()
    dropoff = (data.get("dropoff") or "").strip()
    vehicle_type = data.get("vehicle") or "towncar"
    trip_type = data.get("trip_type") or "oneway"

    if not pickup or not dropoff:
        return JsonResponse({"error": "Both addresses are required."})
    if vehicle_type not in quote_engine.VEHICLE_RATES:
        return JsonResponse(
            {"error": f"No quote rates are configured for '{vehicle_type}'."}
        )

    from drivers.utils import get_drive_time

    drive_info = get_drive_time(pickup, dropoff)
    if not drive_info:
        return JsonResponse({
            "error": "Could not calculate distance. Check the addresses and try again."
        })

    miles = quote_engine.parse_distance_miles(drive_info.get("distance_text"))
    if miles is None:
        return JsonResponse({"error": "Could not read the distance for that route."})

    duration_seconds = drive_info.get("duration_seconds")
    minutes = int(round(duration_seconds / 60)) if duration_seconds else None

    # Match both ends against the published card. Longest alias wins, so the
    # result no longer depends on Location row order.
    locations = list(Location.objects.all())
    pickup_location, pickup_keyword = quote_engine.match_location(pickup, locations)
    dropoff_location, dropoff_keyword = quote_engine.match_location(dropoff, locations)

    # Direction only changes the price on genuinely long trips, so only spend a
    # second Distance Matrix call when it can actually matter.
    pickup_miles_from_base = None
    if miles > quote_engine.LONG_DISTANCE_THRESHOLD_MI:
        base_info = get_drive_time(quote_engine.BASE_LOCATION, pickup)
        if base_info:
            pickup_miles_from_base = quote_engine.parse_distance_miles(
                base_info.get("distance_text")
            )

    # An end that did not match a zone by name may still sit INSIDE one — a
    # residence near MCO should price off the MCO routes, which is how the
    # founder prices these by hand. Only worth measuring for trips short enough
    # to be in-area; get_drive_time caches, so repeat addresses are free.
    def _snap(address):
        if miles > quote_engine.SERVICE_AREA_RADIUS_MI:
            return None, None

        def measure(zone_address):
            info = get_drive_time(address, zone_address)
            return quote_engine.parse_distance_miles(
                info.get("distance_text")
            ) if info else None

        return quote_engine.snap_to_zone(address, locations, measure)

    snapped_pickup, snap_pickup_mi = (
        (pickup_location, None) if pickup_location else _snap(pickup)
    )
    snapped_dropoff, snap_dropoff_mi = (
        (dropoff_location, None) if dropoff_location else _snap(dropoff)
    )

    quote_kwargs = {
        "miles": miles,
        "minutes": minutes,
        "pickup_location": pickup_location,
        "dropoff_location": dropoff_location,
        "pickup_miles_from_base": pickup_miles_from_base,
        "snapped_pickup": snapped_pickup,
        "snapped_dropoff": snapped_dropoff,
        # Commercial lane / tunnel access when we collect at a terminal. Never
        # applied to a published card price — see quote_engine. On a ROUND trip
        # the return leg collects at the outbound drop-off, so an airport there
        # is also an airport pickup; without this, the same two addresses priced
        # $20 apart depending on which box they were typed into.
        "airport_pickup": quote_engine.is_airport_pickup(pickup) or (
            trip_type == "roundtrip" and quote_engine.is_airport_pickup(dropoff)
        ),
    }

    try:
        selected = quote_engine.quote(
            vehicle_type=vehicle_type, trip_type=trip_type, **quote_kwargs
        )
        all_vehicles = quote_engine.quote_all_vehicles(
            trip_type=trip_type, **quote_kwargs
        )
    except (KeyError, ValueError) as exc:
        logger.warning("Quote calculator failed for %s -> %s: %s", pickup, dropoff, exc)
        return JsonResponse({"error": "Could not price that trip."})

    payload = _quote_result_to_json(selected)
    payload.update({
        "trip_type": trip_type,
        "trip_type_label": "Round Trip" if trip_type == "roundtrip" else "One Way",
        "distance_text": drive_info.get("distance_text", "N/A"),
        "duration_text": drive_info.get("duration_text", "N/A"),
        "miles": str(miles.quantize(Decimal("0.1"))),
        "matched_pickup": pickup_location.name if pickup_location else None,
        "matched_dropoff": dropoff_location.name if dropoff_location else None,
        "matched_pickup_keyword": pickup_keyword,
        "matched_dropoff_keyword": dropoff_keyword,
        "snapped_pickup": (
            snapped_pickup.name if snapped_pickup and not pickup_location else None
        ),
        "snapped_dropoff": (
            snapped_dropoff.name if snapped_dropoff and not dropoff_location else None
        ),
        "snapped_pickup_miles": (
            str(snap_pickup_mi.quantize(Decimal("0.1"))) if snap_pickup_mi else None
        ),
        "snapped_dropoff_miles": (
            str(snap_dropoff_mi.quantize(Decimal("0.1"))) if snap_dropoff_mi else None
        ),
        "pickup_miles_from_base": (
            str(pickup_miles_from_base.quantize(Decimal("0.1")))
            if pickup_miles_from_base is not None
            else None
        ),
        "all_vehicles": [_quote_result_to_json(r) for r in all_vehicles],
    })
    return JsonResponse(payload)


# ═════════════════════════════════════════════════════════════════════════════
# TRAVEL AGENT / AGENCY MANAGEMENT (admin)
# ─────────────────────────────────────────────────────────────────────────────
# Improved management UI: see every agent in one place, see every agency in one
# place, drill into individual agents/agencies, and assign agents to agencies.
# ═════════════════════════════════════════════════════════════════════════════


def _agent_live_unpaid(agent):
    """Live unpaid commission (Ready bucket only) for one agent.

    Delegates to users.eligibility.sum_ready so the displayed amount agrees
    with what process_commission_payment would actually pay. Catches the
    "stuck pending" case (leg never marked completed but service date is
    safely in the past) that the old SQL filter missed.
    """
    from users.eligibility import sum_ready
    return sum_ready(agent)


def _agent_bucket_summary(agent):
    """Per-bucket totals/counts for the agent detail page.

    Returns a dict with keys ready, review, pending, excluded -- each value is
    a dict {amount, count, items} where items is a short list of preview rows
    so the template can render the reason line per reservation without re-running
    the eligibility check.
    """
    from users.eligibility import bucket_agent_reservations, STATUS_READY, STATUS_REVIEW, STATUS_PENDING, STATUS_EXCLUDED

    buckets = bucket_agent_reservations(agent)
    summary = {}
    for key in (STATUS_READY, STATUS_REVIEW, STATUS_PENDING, STATUS_EXCLUDED):
        items = buckets.get(key, [])
        amount = sum((r.commission for _, r in items), Decimal("0")).quantize(Decimal("0.01"))
        summary[key] = {
            "amount": amount,
            "count": len(items),
            "items": [
                {
                    "reservation": res,
                    "reason": result.reason,
                    "reason_code": result.reason_code,
                    "commission": result.commission,
                    "last_leg_at": result.last_leg_at,
                }
                for res, result in items
            ],
        }
    return summary


def _agent_lifetime_stats(agent):
    """Total reservations, lifetime revenue, lifetime commission for one agent."""
    from reservations.models import Reservation as _R
    rate = (agent.commission_rate or Decimal("0")) / Decimal("100")
    rows = _R.objects.filter(travel_agent=agent).exclude(status="cancelled").only(
        "base_price", "total_price", "status"
    )
    total = 0
    revenue = Decimal("0")
    commission = Decimal("0")
    for r in rows:
        total += 1
        revenue += r.total_price or Decimal("0")
        commission += (r.base_price or Decimal("0")) * rate
    return {
        "reservations": total,
        "revenue": revenue.quantize(Decimal("0.01")),
        "commission": commission.quantize(Decimal("0.01")),
    }


@login_required
@staff_member_required
def admin_travel_agents(request):
    """All travel agents in one searchable list."""
    search = (request.GET.get("q") or "").strip()
    status = request.GET.get("status", "active")  # active | inactive | all
    agency_filter = request.GET.get("agency", "")  # "<id>" | "none" | ""
    sort = request.GET.get("sort", "name")  # name | unpaid | recent | rate
    agency_pays = request.GET.get("agency_pays", "")  # yes | no | ""
    payment_method_filter = request.GET.get("pmethod", "")  # paypal | venmo | ... | none | ""
    has_unpaid = request.GET.get("has_unpaid", "")  # yes | ""

    agents_qs = TravelAgent.objects.select_related("user", "agency")

    if status == "active":
        agents_qs = agents_qs.filter(is_active=True)
    elif status == "inactive":
        agents_qs = agents_qs.filter(is_active=False)

    if agency_filter == "none":
        agents_qs = agents_qs.filter(agency__isnull=True)
    elif agency_filter.isdigit():
        agents_qs = agents_qs.filter(agency_id=int(agency_filter))

    if agency_pays == "yes":
        agents_qs = agents_qs.filter(agency_handles_payment=True)
    elif agency_pays == "no":
        agents_qs = agents_qs.filter(agency_handles_payment=False)

    if payment_method_filter == "none":
        agents_qs = agents_qs.filter(Q(payment_method__isnull=True) | Q(payment_method=""))
    elif payment_method_filter:
        agents_qs = agents_qs.filter(payment_method=payment_method_filter)

    if has_unpaid == "yes":
        # Agents with at least one completed reservation whose commission is unpaid.
        from reservations.models import Reservation as _R
        unpaid_ids = _R.objects.filter(
            commission_paid=False, status="completed"
        ).values_list("travel_agent_id", flat=True).distinct()
        agents_qs = agents_qs.filter(id__in=list(unpaid_ids))

    if search:
        agents_qs = agents_qs.filter(
            Q(agent_name__icontains=search)
            | Q(agency_name__icontains=search)
            | Q(agency__name__icontains=search)
            | Q(user__email__icontains=search)
            | Q(user__username__icontains=search)
            | Q(user__first_name__icontains=search)
            | Q(user__last_name__icontains=search)
            | Q(phone__icontains=search)
        )

    # Sort
    if sort == "name":
        agents_qs = agents_qs.order_by("agent_name", "user__email")
    elif sort == "rate":
        agents_qs = agents_qs.order_by("-commission_rate", "agent_name")
    elif sort == "recent":
        agents_qs = agents_qs.order_by("-created_at")

    # Paginate first, compute live stats per page
    paginator = Paginator(agents_qs, 30)
    page_num = request.GET.get("page", 1)
    try:
        page = paginator.page(page_num)
    except (PageNotAnInteger, EmptyPage):
        page = paginator.page(1)

    from reservations.models import Reservation as _R

    page_agent_ids = [a.id for a in page]
    res_counts = dict(
        _R.objects.filter(travel_agent_id__in=page_agent_ids)
        .exclude(status="cancelled")
        .values_list("travel_agent")
        .annotate(c=Count("id"))
        .values_list("travel_agent", "c")
    )
    unpaid_counts = dict(
        _R.objects.filter(
            travel_agent_id__in=page_agent_ids,
            commission_paid=False,
            status="completed",
        )
        .values_list("travel_agent")
        .annotate(c=Count("id"))
        .values_list("travel_agent", "c")
    )

    rows = []
    for agent in page:
        live_unpaid = _agent_live_unpaid(agent)
        rows.append({
            "agent": agent,
            "live_unpaid": live_unpaid,
            "res_count": res_counts.get(agent.id, 0),
            "unpaid_res_count": unpaid_counts.get(agent.id, 0),
        })

    if sort == "unpaid":
        rows.sort(key=lambda r: r["live_unpaid"], reverse=True)

    # Summary across the FULL filtered set (not just this page)
    total_agents = agents_qs.count()
    agents_with_agency = agents_qs.filter(agency__isnull=False).count()
    agents_no_agency = total_agents - agents_with_agency

    # Agencies for the filter dropdown
    agencies_for_filter = Agency.objects.order_by("name").only("id", "name")

    context = {
        "rows": rows,
        "page": page,
        "paginator": paginator,
        "search": search,
        "status_filter": status,
        "agency_filter": agency_filter,
        "sort": sort,
        "agency_pays_filter": agency_pays,
        "payment_method_filter": payment_method_filter,
        "has_unpaid_filter": has_unpaid,
        "total_agents": total_agents,
        "agents_with_agency": agents_with_agency,
        "agents_no_agency": agents_no_agency,
        "agencies_for_filter": agencies_for_filter,
        "payment_method_choices": TravelAgent.PAYMENT_METHOD_CHOICES,
    }
    return render(request, "dispatching/travel_agents_list.html", context)


@login_required
@staff_member_required
def admin_travel_agencies(request):
    """All travel agencies in one searchable list."""
    search = (request.GET.get("q") or "").strip()
    status = request.GET.get("status", "active")
    sort = request.GET.get("sort", "name")  # name | agents | recent

    agencies_qs = Agency.objects.all()

    if status == "active":
        agencies_qs = agencies_qs.filter(is_active=True)
    elif status == "inactive":
        agencies_qs = agencies_qs.filter(is_active=False)

    if search:
        agencies_qs = agencies_qs.filter(
            Q(name__icontains=search)
            | Q(phone__icontains=search)
            | Q(agents__agent_name__icontains=search)
        ).distinct()

    agencies_qs = agencies_qs.annotate(
        agent_count=Count("agents", distinct=True),
        active_agent_count=Count(
            "agents", filter=Q(agents__is_active=True), distinct=True
        ),
    )

    if sort == "name":
        agencies_qs = agencies_qs.order_by("name")
    elif sort == "agents":
        agencies_qs = agencies_qs.order_by("-agent_count", "name")
    elif sort == "recent":
        agencies_qs = agencies_qs.order_by("-created_at")

    paginator = Paginator(agencies_qs, 30)
    page_num = request.GET.get("page", 1)
    try:
        page = paginator.page(page_num)
    except (PageNotAnInteger, EmptyPage):
        page = paginator.page(1)

    from reservations.models import Reservation as _R

    # Per-page lifetime reservation counts and unpaid commission totals
    page_agency_ids = [a.id for a in page]
    res_counts = dict(
        _R.objects.filter(travel_agent__agency_id__in=page_agency_ids)
        .exclude(status="cancelled")
        .values_list("travel_agent__agency")
        .annotate(c=Count("id"))
        .values_list("travel_agent__agency", "c")
    )

    rows = []
    for agency in page:
        # Live unpaid commission across all the agency's agents
        live_unpaid = Decimal("0")
        for agent in agency.agents.all():
            live_unpaid += _agent_live_unpaid(agent)
        rows.append({
            "agency": agency,
            "live_unpaid": live_unpaid.quantize(Decimal("0.01")),
            "res_count": res_counts.get(agency.id, 0),
        })

    total_agencies = agencies_qs.count()
    total_active = agencies_qs.filter(is_active=True).count()

    context = {
        "rows": rows,
        "page": page,
        "paginator": paginator,
        "search": search,
        "status_filter": status,
        "sort": sort,
        "total_agencies": total_agencies,
        "total_active": total_active,
    }
    return render(request, "dispatching/travel_agencies_list.html", context)


@login_required
@staff_member_required
def admin_travel_agent_detail(request, pk):
    """Per-agent admin detail with assign-agency control."""
    from reservations.models import Reservation as _R

    agent = get_object_or_404(
        TravelAgent.objects.select_related("user", "agency"), pk=pk
    )

    lifetime = _agent_lifetime_stats(agent)
    live_unpaid = _agent_live_unpaid(agent)

    # Recent reservations (last 25)
    recent = (
        _R.objects.filter(travel_agent=agent)
        .select_related("customer")
        .order_by("-created_at")[:25]
    )

    # Status breakdown
    status_breakdown = list(
        _R.objects.filter(travel_agent=agent)
        .values("status")
        .annotate(count=Count("id"))
        .order_by("-count")
    )

    # Pending payment breakdown (reuses the same service the AJAX preview uses)
    pending_preview = svc_preview_agent_payout(agent)

    # Per-bucket summary so the detail page can show Ready / Needs Review /
    # Pending / Excluded counts with the per-reservation reason from the
    # centralized eligibility helper. This is what makes "why is this stuck?"
    # answerable without digging through DB rows.
    bucket_summary = _agent_bucket_summary(agent)

    # Paginated commission payouts
    payouts_qs = (
        CommissionPayout.objects.filter(agent=agent)
        .select_related("agency")
        .order_by("-paid_at")
    )
    payouts_paginator = Paginator(payouts_qs, 25)
    payouts_page = request.GET.get("payouts_page", 1)
    try:
        payouts_page_obj = payouts_paginator.page(payouts_page)
    except (PageNotAnInteger, EmptyPage):
        payouts_page_obj = payouts_paginator.page(1)

    # All agencies for the assign dropdown
    all_agencies = Agency.objects.order_by("name")

    # Can this agent be paid directly from the profile?
    can_pay_directly = (
        not agent.agency_handles_payment and float(live_unpaid or 0) > 0
    )

    context = {
        "agent": agent,
        "lifetime": lifetime,
        "live_unpaid": live_unpaid,
        "recent_reservations": recent,
        "status_breakdown": status_breakdown,
        "payouts_page_obj": payouts_page_obj,
        "all_agencies": all_agencies,
        "pending_preview": pending_preview,
        "can_pay_directly": can_pay_directly,
        "bucket_summary": bucket_summary,
    }
    return render(request, "dispatching/travel_agent_detail.html", context)


@login_required
@staff_member_required
@require_POST
def admin_travel_agent_set_agency(request, pk):
    """
    Assign an agent to an agency. Accepts:
      - existing_agency: <agency_id> | "" (none = unassign)
      - new_agency_name: optional — if filled, creates a new Agency and assigns it

    Responds with JSON when the request asks for it
    (X-Requested-With: XMLHttpRequest or Accept: application/json),
    otherwise redirects to the agent detail page.
    """
    agent = get_object_or_404(TravelAgent, pk=pk)
    wants_json = (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in request.headers.get("Accept", "")
    )

    new_name = (request.POST.get("new_agency_name") or "").strip()
    existing = (request.POST.get("existing_agency") or "").strip()

    flash = ""
    if new_name:
        agency = Agency.objects.filter(name__iexact=new_name).first()
        created = False
        if not agency:
            agency = Agency.objects.create(name=new_name, is_active=True)
            created = True
        agent.agency = agency
        agent.save(update_fields=["agency"])
        flash = (
            f"Created agency \"{agency.name}\" and assigned {agent} to it."
            if created else
            f"Assigned {agent} to existing agency \"{agency.name}\"."
        )
    elif existing == "":
        agent.agency = None
        agent.save(update_fields=["agency"])
        flash = f"Removed {agent} from their agency."
    elif existing.isdigit():
        agency = get_object_or_404(Agency, pk=int(existing))
        agent.agency = agency
        agent.save(update_fields=["agency"])
        flash = f"Assigned {agent} to {agency.name}."
    else:
        if wants_json:
            return JsonResponse({"success": False, "error": "No agency selection provided."}, status=400)
        messages.error(request, "No agency selection provided.")
        return redirect("admin_travel_agent_detail", pk=agent.pk)

    if wants_json:
        return JsonResponse({
            "success": True,
            "message": flash,
            "agency": (
                {"id": agent.agency.id, "name": agent.agency.name}
                if agent.agency else None
            ),
        })
    messages.success(request, flash)
    return redirect("admin_travel_agent_detail", pk=agent.pk)


@login_required
@staff_member_required
@require_POST
def admin_travel_agent_toggle_agency_pays(request, pk):
    """JSON: flip the agency_handles_payment flag on a single agent."""
    agent = get_object_or_404(TravelAgent, pk=pk)
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)
    val = bool(data.get("agency_handles_payment"))
    if val and not agent.agency_id:
        return JsonResponse(
            {"success": False, "error": "Assign an agency before enabling agency-pays."},
            status=400,
        )
    agent.agency_handles_payment = val
    agent.save(update_fields=["agency_handles_payment"])
    return JsonResponse({"success": True, "agency_handles_payment": val})


@login_required
@staff_member_required
@require_POST
def admin_travel_agent_set_rate(request, pk):
    """JSON: update commission_rate (0-100) on a single agent."""
    agent = get_object_or_404(TravelAgent, pk=pk)
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)
    raw = data.get("commission_rate")
    try:
        rate = Decimal(str(raw))
    except (InvalidOperation, TypeError):
        return JsonResponse({"success": False, "error": "Invalid number"}, status=400)
    if rate < 0 or rate > 100:
        return JsonResponse({"success": False, "error": "Rate must be between 0 and 100."}, status=400)
    agent.commission_rate = rate
    agent.save(update_fields=["commission_rate"])
    return JsonResponse({"success": True, "commission_rate": str(rate.quantize(Decimal('0.01')))})


@login_required
@staff_member_required
@require_POST
def admin_travel_agents_bulk_assign(request):
    """JSON: assign many agents to one agency in a single call.

    Body:
      - agent_ids: [int]
      - agency_id: <int> | null  (null = unassign)
    """
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    agent_ids = data.get("agent_ids") or []
    if not isinstance(agent_ids, list) or not agent_ids:
        return JsonResponse({"success": False, "error": "agent_ids required"}, status=400)
    try:
        agent_ids = [int(x) for x in agent_ids]
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "error": "agent_ids must be ints"}, status=400)

    raw_agency = data.get("agency_id")
    agency = None
    if raw_agency not in (None, "", "null"):
        try:
            agency = Agency.objects.get(pk=int(raw_agency))
        except (Agency.DoesNotExist, TypeError, ValueError):
            return JsonResponse({"success": False, "error": "Agency not found"}, status=404)

    updated = TravelAgent.objects.filter(id__in=agent_ids).update(agency=agency)
    return JsonResponse({
        "success": True,
        "updated": updated,
        "agency": ({"id": agency.id, "name": agency.name} if agency else None),
    })


@login_required
@staff_member_required
def admin_travel_agency_detail(request, pk):
    """Per-agency admin detail. Lists all agents under it with quick stats."""
    from reservations.models import Reservation as _R

    agency = get_object_or_404(Agency, pk=pk)

    agents = list(
        agency.agents.select_related("user").order_by(
            "-is_active", "agent_name"
        )
    )

    agent_rows = []
    total_unpaid = Decimal("0")
    total_revenue = Decimal("0")
    total_commission = Decimal("0")
    total_reservations = 0
    for agent in agents:
        live_unpaid = _agent_live_unpaid(agent)
        lifetime = _agent_lifetime_stats(agent)
        agent_rows.append({
            "agent": agent,
            "live_unpaid": live_unpaid,
            "reservations": lifetime["reservations"],
            "revenue": lifetime["revenue"],
            "commission": lifetime["commission"],
        })
        total_unpaid += live_unpaid
        total_revenue += lifetime["revenue"]
        total_commission += lifetime["commission"]
        total_reservations += lifetime["reservations"]

    # Recent reservations across the whole agency
    recent = (
        _R.objects.filter(travel_agent__agency=agency)
        .select_related("customer", "travel_agent")
        .order_by("-created_at")[:20]
    )

    # Pending payment breakdown (reuses the same service the AJAX preview uses)
    pending_preview = svc_preview_agency_payout(agency)
    can_pay_agency = (
        any(a.agency_handles_payment for a in agents)
        and float(pending_preview.get("total") or 0) > 0
    )

    # Paginated agency payouts
    agency_payouts_qs = (
        AgencyCommissionPayout.objects.filter(agency=agency)
        .prefetch_related("agent_payouts__agent__user")
        .order_by("-paid_at")
    )
    agency_payouts_paginator = Paginator(agency_payouts_qs, 25)
    agency_payouts_page = request.GET.get("payouts_page", 1)
    try:
        agency_payouts_page_obj = agency_payouts_paginator.page(agency_payouts_page)
    except (PageNotAnInteger, EmptyPage):
        agency_payouts_page_obj = agency_payouts_paginator.page(1)

    context = {
        "agency": agency,
        "agent_rows": agent_rows,
        "total_unpaid": total_unpaid.quantize(Decimal("0.01")),
        "total_revenue": total_revenue.quantize(Decimal("0.01")),
        "total_commission": total_commission.quantize(Decimal("0.01")),
        "total_reservations": total_reservations,
        "active_agent_count": sum(1 for a in agents if a.is_active),
        "recent_reservations": recent,
        "pending_preview": pending_preview,
        "can_pay_agency": can_pay_agency,
        "agency_payouts_page_obj": agency_payouts_page_obj,
    }
    return render(request, "dispatching/travel_agency_detail.html", context)


# ─────────────────────────────────────────────────────────────────────────────
# Accrual Revenue Report (admin-only)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_accrual_request(request):
    """
    Parse start/end/quick/vehicle/source/payment_status from request.GET.
    Returns dict suitable for both the page render context and the service call.
    """
    from .services import accrual_revenue as ar_service

    today = timezone.localdate()
    quick = request.GET.get("quick", "")
    start_str = request.GET.get("start", "")
    end_str = request.GET.get("end", "")

    quick_range = ar_service.resolve_quick_filter(quick, today=today)
    if quick_range:
        start_date, end_date = quick_range
    else:
        try:
            start_date = datetime.strptime(start_str, "%Y-%m-%d").date() if start_str else today.replace(day=1)
        except ValueError:
            start_date = today.replace(day=1)
        try:
            end_date = datetime.strptime(end_str, "%Y-%m-%d").date() if end_str else today
        except ValueError:
            end_date = today
        quick = "custom"

    vehicle_id = request.GET.get("vehicle_id") or ""
    try:
        vehicle_id_int = int(vehicle_id) if vehicle_id else None
    except ValueError:
        vehicle_id_int = None

    booking_source = request.GET.get("booking_source") or None
    payment_status = request.GET.get("payment_status") or None

    return {
        "start_date": start_date,
        "end_date": end_date,
        "quick": quick,
        "vehicle_id": vehicle_id_int,
        "booking_source": booking_source,
        "payment_status": payment_status,
    }


@login_required(login_url="login")
def accrual_revenue_report(request):
    """
    Accrual Revenue Report — superuser-only.

    Shows revenue earned (rides fulfilled with status='completed') within a
    date range, anchored on Leg.pickup_date. Independent of payment date.
    """
    if not can_view_revenue(request.user):
        messages.error(request, "You don't have permission to access this page.")
        return redirect("dashboard")

    from .services import accrual_revenue as ar_service

    params = _parse_accrual_request(request)
    report = ar_service.build_report(
        params["start_date"],
        params["end_date"],
        vehicle_id=params["vehicle_id"],
        booking_source=params["booking_source"],
        payment_status=params["payment_status"],
    )

    # Filter dropdown options (cheap)
    vehicles = list(Vehicle.objects.order_by("vehicle_type"))
    booking_sources = [
        ("google_ads", "Google Ads"),
        ("google_organic", "Google Organic"),
        ("meta_ads", "Meta Ads"),
        ("meta_organic", "Meta Organic"),
        ("travel_agent", "Travel Agent"),
        ("referral", "Referral"),
        ("direct", "Direct"),
        ("phone", "Phone"),
        ("other", "Other"),
    ]
    payment_statuses = [
        ("paid", "Paid"),
        ("unpaid", "Unpaid"),
        ("card_saved", "Card Saved"),
        ("pending", "Pending"),
        ("failed", "Failed"),
    ]

    context = {
        "report": report,
        "params": params,
        "vehicles": vehicles,
        "booking_sources": booking_sources,
        "payment_statuses": payment_statuses,
        "flag_labels": ar_service.FLAG_LABELS,
    }
    return render(request, "dispatching/accrual_revenue_report.html", context)


@login_required(login_url="login")
def accrual_revenue_csv(request):
    """CSV detail export of the accrual revenue report (one row per included leg)."""
    if not can_view_revenue(request.user):
        return redirect("home")

    from .services import accrual_revenue as ar_service

    params = _parse_accrual_request(request)
    report = ar_service.build_report(
        params["start_date"],
        params["end_date"],
        vehicle_id=params["vehicle_id"],
        booking_source=params["booking_source"],
        payment_status=params["payment_status"],
    )

    fieldnames = [
        "reservation_id",
        "leg_id",
        "customer_name",
        "pickup_date",
        "pickup_time",
        "status_changed_at",
        "trip_type",
        "pickup_location",
        "dropoff_location",
        "leg_status",
        "vehicle",
        "booking_source",
        "reservation_payment_status",
        "leg_payment_status",
        "gross_fare_in_range",
        "allocation_method",
        "tip_allocated",
        "additional_charges_allocated",
        "base_price_allocated",
        "reservation_total_price",
        "reservation_total_refunded",
        "driver_pay",
        "driver_name",
        "route_category",
        "flags",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for r in report.legs:
        writer.writerow({
            "reservation_id": r.reservation_id,
            "leg_id": r.leg_id,
            "customer_name": r.customer_name,
            "pickup_date": r.pickup_date.isoformat(),
            "pickup_time": r.pickup_time.isoformat() if r.pickup_time else "",
            "status_changed_at": r.status_changed_at.isoformat() if r.status_changed_at else "",
            "trip_type": r.trip_type,
            "pickup_location": r.pickup_location,
            "dropoff_location": r.dropoff_location,
            "leg_status": r.leg_status,
            "vehicle": r.vehicle,
            "booking_source": r.booking_source,
            "reservation_payment_status": r.reservation_payment_status,
            "leg_payment_status": r.leg_payment_status,
            "gross_fare_in_range": str(r.gross_fare_in_range),
            "allocation_method": r.allocation_method,
            "tip_allocated": str(r.tip_allocated),
            "additional_charges_allocated": str(r.additional_charges_allocated),
            "base_price_allocated": str(r.base_price_allocated),
            "reservation_total_price": str(r.reservation_total_price),
            "reservation_total_refunded": str(r.reservation_total_refunded),
            "driver_pay": str(r.driver_pay),
            "driver_name": r.driver_name,
            "route_category": r.route_category,
            "flags": ";".join(r.flags),
        })

    csv_bytes = output.getvalue().encode("utf-8")
    response = HttpResponse(csv_bytes, content_type="text/csv; charset=utf-8")
    fname = f"accrual_revenue_{params['start_date'].isoformat()}_to_{params['end_date'].isoformat()}.csv"
    response["Content-Disposition"] = f'attachment; filename="{fname}"'
    return response


@login_required(login_url="login")
def accrual_revenue_txt(request):
    """TXT summary export — accountant-friendly."""
    if not can_view_revenue(request.user):
        return redirect("home")

    from .services import accrual_revenue as ar_service

    params = _parse_accrual_request(request)
    report = ar_service.build_report(
        params["start_date"],
        params["end_date"],
        vehicle_id=params["vehicle_id"],
        booking_source=params["booking_source"],
        payment_status=params["payment_status"],
    )

    lines = []
    lines.append("ACCRUAL REVENUE REPORT")
    lines.append("Supreme Transportations / Grayson Towncar")
    lines.append("")
    lines.append(f"Date range: {report.start_date} -> {report.end_date}  ({report.timezone_name})")
    lines.append(f"Generated:  {timezone.now().strftime('%Y-%m-%d %H:%M %Z')}  by {request.user.email or request.user.username}")
    lines.append(f"Filters:    vehicle_id={params['vehicle_id']!r}, booking_source={params['booking_source']!r}, payment_status={params['payment_status']!r}")
    lines.append("")
    lines.append("--- TOTALS ---")
    lines.append(f"Legs included (status != cancelled): {report.total_legs}")
    lines.append(f"Unique reservations included:        {report.total_reservations}")
    lines.append(f"GROSS ACCRUAL REVENUE:               ${report.gross_accrual_revenue:,.2f}")
    lines.append(f"Gross excluding gratuity:            ${report.gross_excluding_gratuity:,.2f}  (likely-driver-passthrough view)")
    lines.append("")
    lines.append(f"Avg fare per leg:                    ${report.avg_fare_per_leg:,.2f}")
    lines.append(f"Avg fare per reservation:            ${report.avg_fare_per_reservation:,.2f}")
    lines.append("")
    lines.append("--- BREAKDOWN (informational, NOT subtracted from gross) ---")
    lines.append(f"Tips/gratuity (allocated):           ${report.tips_allocated:,.2f}")
    lines.append(f"Additional charges (allocated):      ${report.additional_charges_allocated:,.2f}")
    lines.append(f"Base price (allocated):              ${report.base_price_allocated:,.2f}")
    lines.append(f"Driver pay (informational):          ${report.driver_pay_total:,.2f}")
    lines.append(f"Estimated margin (gross-driver pay): ${report.estimated_margin:,.2f}")
    lines.append("")
    lines.append("--- REFUND VIEWS (gross is never reduced; lines below are computed for display) ---")
    lines.append(f"Refunds tied to in-range reservations (any refund date): ${report.refunds_for_inrange_reservations:,.2f}")
    lines.append(f"Refund payments issued in window (Payment.updated_at)  : ${report.refund_payments_in_window:,.2f}")
    lines.append(f"Net after in-range refunds:                              ${report.net_after_inrange_refunds:,.2f}")
    lines.append(f"Net after window refund payments:                        ${report.net_after_window_refund_payments:,.2f}")
    lines.append("")
    lines.append("--- BY DAY ---")
    for d in report.by_day:
        lines.append(f"  {d.day}   {d.leg_count:>4} legs   ${d.revenue:,.2f}")
    lines.append("")
    lines.append("--- BY VEHICLE ---")
    for b in report.by_vehicle:
        lines.append(f"  {b.label:<30} {b.leg_count:>4} legs   ${b.revenue:,.2f}")
    lines.append("")
    lines.append("--- BY SOURCE ---")
    for b in report.by_source:
        lines.append(f"  {b.label:<30} {b.leg_count:>4} legs   ${b.revenue:,.2f}")
    lines.append("")
    lines.append("--- BY ROUTE CATEGORY ---")
    for b in report.by_route_category:
        lines.append(f"  {b.label:<60} {b.leg_count:>4} legs   ${b.revenue:,.2f}")
    lines.append("")
    lines.append("--- BY PAYMENT STATUS ---")
    for b in report.by_payment_status:
        lines.append(f"  {b.label:<20} {b.leg_count:>4} legs   ${b.revenue:,.2f}")
    lines.append("")
    lines.append("--- INCLUDED ---")
    lines.append("Legs with pickup_date in range AND status != 'cancelled' AND")
    lines.append("exclude_from_analytics = False AND reservation.status != 'cancelled'.")
    lines.append("This INCLUDES legs in 'in-progress', 'confirmed', 'on-the-way',")
    lines.append("'on-location', 'picked-up' as well as 'completed'. Pending-status")
    lines.append("legs are flagged for review.")
    lines.append("")
    lines.append("--- EXCLUDED ---")
    lines.append("Cancelled legs, legs of cancelled reservations, legs flagged")
    lines.append("exclude_from_analytics, legs whose pickup_date is outside the")
    lines.append("selected range.")
    lines.append("")
    lines.append("--- ANOMALIES (review) ---")
    lines.append(f"Reallocated reservations (rev_share didn't reconcile): {len(report.anomalies.reallocated)}")
    lines.append(f"Pending-status legs (not 'completed'):                 {len(report.anomalies.pending_status)}")
    lines.append(f"Split-range reservations:                              {len(report.anomalies.split_range)}")
    lines.append(f"Zero/negative-fare legs:                               {len(report.anomalies.zero_or_negative)}")
    lines.append(f"Cancelled-reservation-with-non-cancelled-leg:          {len(report.anomalies.cancelled_with_completed)}")
    lines.append(f"Refunded reservations (any refund date):               {len(report.anomalies.refunded)}")
    lines.append("")
    lines.append("--- DECISIONS APPLIED (per user, 2026-05-06) ---")
    lines.append("- Revenue source: smart fallback. Use leg.revenue_share when sum over")
    lines.append("  non-cancelled legs reconciles to reservation.total_price (within $0.02).")
    lines.append("  Otherwise allocate total_price across non-cancelled legs, weighted by")
    lines.append("  leg_base_price when any leg has it set, else equal split.")
    lines.append("  revenue_share is NEVER modified in the database.")
    lines.append("- Gratuity: included in headline gross. Separate 'Gross excluding gratuity'")
    lines.append("  line shown for accountant's discretion. Drivers receive ~98% of")
    lines.append("  customer-paid gratuity in production data — likely pass-through.")
    lines.append("- Stale-status legs: include all legs whose status != 'cancelled'")
    lines.append("  (in-progress, confirmed, on-the-way, on-location, picked-up, completed).")
    lines.append("- Refunds: never reduce gross. Two views shown:")
    lines.append("    Net after refunds tied to in-range completed reservations")
    lines.append("    Net after refund payments dated in window (Payment.updated_at)")
    lines.append("")
    lines.append("--- NOTES ---")
    lines.append("- Stripe processing fees are NOT included (no source-of-truth field).")
    lines.append("- Gross figures include gratuity and additional charges (already part")
    lines.append("  of Reservation.total_price = base + additional + gratuity).")
    lines.append("- Driver pay is informational; NOT subtracted from gross accrual revenue.")

    body = "\n".join(lines) + "\n"
    response = HttpResponse(body.encode("utf-8"), content_type="text/plain; charset=utf-8")
    fname = f"accrual_revenue_{params['start_date'].isoformat()}_to_{params['end_date'].isoformat()}.txt"
    response["Content-Disposition"] = f'attachment; filename="{fname}"'
    return response



# ──────────────────────────────────────────────────────────────────────
# Time-off request review (founder/dispatcher side).
# Companion to drivers.views.request_timeoff. The data model is
# DriverDateOverride with status in {pending, approved, denied, cancelled}.
# ──────────────────────────────────────────────────────────────────────

@login_required(login_url="login")
def dispatcher_timeoff_requests(request):
    """Founder queue.

    Pending requests are grouped by (driver, date range, exception type)
    so accidental double-submissions show as one card with a "n duplicates"
    pill instead of a wall of identical rows. Each pending card is enriched
    with a coverage-impact preview from the schedule_risk engine so the
    dispatcher can see "if approved, Fri drops +2 → 0 (Tight)" before
    clicking Approve.
    """
    from drivers.models import DriverDateOverride, Driver
    from reservations.models import Leg
    from datetime import date as _date_type, timedelta
    from dispatching.schedule_risk import (
        compute_week_risk, compute_exception_impact,
        COVERAGE_TARGET_DEFAULT,
    )

    if not request.user.is_staff:
        return redirect("home")

    pending_qs = (
        DriverDateOverride.objects
        .filter(status="pending")
        .select_related("driver", "driver__profile", "created_by")
        .order_by("date", "id")
    )

    # ── Group accidental dupes ────────────────────────────────────────
    # Key by (driver_id, date, end_date, exception_type, start_time, end_time).
    # Anything matching that key is treated as the same request. We surface
    # the OLDEST row (lowest id) as the canonical one and stash the rest in
    # `duplicates` for the approve/deny cascade.
    groups: dict = {}
    for o in pending_qs:
        key = (o.driver_id, o.date, o.end_date, o.exception_type,
               o.start_time, o.end_time)
        groups.setdefault(key, []).append(o)

    pending = []
    for key, rows in groups.items():
        canonical = rows[0]
        dupes = rows[1:]
        canonical.duplicate_ids = [r.id for r in dupes]
        canonical.duplicate_count = len(dupes)
        pending.append(canonical)

    # Conflict count per request: trips already assigned to that driver
    # in the requested window. Soft signal — founder still decides.
    for o in pending:
        start = o.date
        end = o.end_date or o.date
        o.conflict_count = (
            Leg.objects
            .filter(driver=o.driver, pickup_date__gte=start, pickup_date__lte=end)
            .exclude(reservation__status="cancelled")
            .count()
        )

    # ── Coverage-impact preview per pending request ────────────────────
    # We compute risk for the week containing each pending's start date.
    # Caching by week-start avoids recomputing the whole roster when many
    # requests fall in the same week.
    today = timezone.localdate()
    inhouse_drivers = list(
        Driver.objects.filter(driver_type="inhouse", is_active=True)
        .select_related("profile")
        .prefetch_related("weekly_schedule", "date_overrides")
    )

    DESIGN_BUCKET = {
        "morning":  "morning", "midday":   "midday", "evening":  "evening",
        "night":    "night",   "split":    "split",  "full_day": "flex",
        "custom":   "set",
    }

    def _resolve_driver_rows(week_dates):
        """Build the minimal driver_rows shape the risk engine needs."""
        rows = []
        for d in inhouse_drivers:
            weekly_map = {e.day_of_week: e for e in d.weekly_schedule.all()}
            days = []
            for day_idx in range(7):
                entry = weekly_map.get(day_idx)
                avail = entry.is_available if entry else True
                stype = entry.shift_type if entry else (d.default_shift_type or "full_day")
                flex = entry.flexible if entry else d.default_flexible
                days.append({
                    "is_off": not avail,
                    "design_bucket": "off" if not avail else DESIGN_BUCKET.get(stype, "flex"),
                    "flexible": bool(flex),
                })
            rows.append({"id": d.id, "name": str(d), "days": days})
        return rows

    def _overrides_for_week(week_dates):
        """(driver_id, date) → list of override dicts for the engine."""
        lookup: dict = {}
        for d in inhouse_drivers:
            for o in d.date_overrides.all():
                if o.status not in ("approved", "pending"):
                    continue
                start = o.date
                end = o.end_date or o.date
                for d_in_week in week_dates:
                    if start <= d_in_week <= end:
                        lookup.setdefault((d.id, d_in_week), []).append({
                            "id": o.id,
                            "status": o.status,
                            "exception_type": o.exception_type,
                        })
        return lookup

    week_cache: dict = {}

    def _risk_for_date(dt):
        monday = dt - timedelta(days=dt.weekday())
        if monday in week_cache:
            return week_cache[monday]
        week_dates = [monday + timedelta(days=i) for i in range(7)]
        rows = _resolve_driver_rows(week_dates)
        overrides = _overrides_for_week(week_dates)
        wr = compute_week_risk(rows, overrides, week_dates,
                               target=COVERAGE_TARGET_DEFAULT)
        week_cache[monday] = (wr, week_dates)
        return week_cache[monday]

    IMPACT_RANK = {"critical": 0, "understaffed": 1, "tight": 2, "no_issue": 3}

    for o in pending:
        wr, week_dates = _risk_for_date(o.date)
        impact = compute_exception_impact(
            {
                "id": o.id, "driver_id": o.driver_id,
                "date": o.date.isoformat(),
                "end_date": o.end_date.isoformat() if o.end_date else "",
                "exception_type": o.exception_type,
                "status": "pending",
            },
            wr["days"], week_dates, wr["driver_states_by_date"],
            target=COVERAGE_TARGET_DEFAULT,
        )
        o.impact = impact
        o.impact_rank = IMPACT_RANK.get(impact.get("impact_level"), 9)

    # Sort: most impactful (critical → understaffed → tight → no_issue) first,
    # then by date within the same impact tier.
    pending.sort(key=lambda o: (o.impact_rank, o.date, o.id))

    recent = list(
        DriverDateOverride.objects
        .filter(status__in=["approved", "denied"], submitted_by_driver=True)
        .select_related("driver", "driver__profile", "decided_by")
        .order_by("-decided_at")[:20]
    )

    # Headline counts for the stat strip.
    critical_count = sum(1 for o in pending
                         if o.impact.get("impact_level") in ("critical", "understaffed"))
    tight_count = sum(1 for o in pending
                      if o.impact.get("impact_level") == "tight")
    dupe_count = sum(o.duplicate_count for o in pending)

    return render(
        request,
        "dispatching/timeoff_requests.html",
        {
            "pending": pending,
            "recent": recent,
            "critical_count": critical_count,
            "tight_count": tight_count,
            "dupe_count": dupe_count,
        },
    )


@login_required(login_url="login")
@require_POST
def approve_timeoff_request(request, override_id):
    from django.utils import timezone as _tz
    from drivers.models import DriverDateOverride
    from drivers.context_processors import invalidate_pending_timeoff_count
    from drivers.timeoff_notifications import notify_driver_of_decision

    if not request.user.is_staff:
        return redirect("home")

    override = get_object_or_404(DriverDateOverride, id=override_id)
    if override.status != "pending":
        messages.error(request, "Only pending requests can be approved.")
        return redirect("dispatcher_timeoff_requests")

    # Cascade approval across any pending duplicates so dispatchers don't
    # have to click Approve three times on the same accidental triple-submit.
    dupes = list(override.duplicate_group().filter(status="pending").exclude(id=override.id))

    override.status = "approved"
    override.decided_by = request.user
    override.decided_at = _tz.now()
    override.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])
    for d in dupes:
        d.status = "approved"
        d.decided_by = request.user
        d.decided_at = _tz.now()
        d.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])

    try:
        notify_driver_of_decision(override)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Driver notify failed for override %s", override.id)

    invalidate_pending_timeoff_count()
    if dupes:
        messages.success(
            request,
            f"Approved time off for {override.driver} "
            f"(rolled up {len(dupes) + 1} duplicate submissions).",
        )
    else:
        messages.success(request, f"Approved time off for {override.driver}.")
    return redirect("dispatcher_timeoff_requests")


@login_required(login_url="login")
@require_POST
def deny_timeoff_request(request, override_id):
    from django.utils import timezone as _tz
    from drivers.models import DriverDateOverride
    from drivers.context_processors import invalidate_pending_timeoff_count
    from drivers.timeoff_notifications import notify_driver_of_decision

    if not request.user.is_staff:
        return redirect("home")

    override = get_object_or_404(DriverDateOverride, id=override_id)
    if override.status != "pending":
        messages.error(request, "Only pending requests can be denied.")
        return redirect("dispatcher_timeoff_requests")

    reason = (request.POST.get("denial_reason") or "").strip()[:200]

    # Cascade denial across any pending duplicates.
    dupes = list(override.duplicate_group().filter(status="pending").exclude(id=override.id))

    override.status = "denied"
    override.denial_reason = reason
    override.decided_by = request.user
    override.decided_at = _tz.now()
    override.save(update_fields=["status", "denial_reason", "decided_by", "decided_at", "updated_at"])
    for d in dupes:
        d.status = "denied"
        d.denial_reason = reason
        d.decided_by = request.user
        d.decided_at = _tz.now()
        d.save(update_fields=["status", "denial_reason", "decided_by", "decided_at", "updated_at"])

    try:
        notify_driver_of_decision(override)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Driver notify failed for override %s", override.id)

    invalidate_pending_timeoff_count()
    messages.success(request, f"Denied time off for {override.driver}.")
    return redirect("dispatcher_timeoff_requests")


# ============================================================================
# CHAUFFEUR LOAD & FAIRNESS
# Two pages over one metrics core (dispatching/load_metrics.py) plus one rules
# module (dispatching/load_insights.py):
#   chauffeur_load  — any dispatcher. Counted workload + findings. No judgement calls.
#   chauffeur_kpis  — superuser. Adds the "worth a conversation" exceptions list.
# Money deliberately absent from both — it moves to the future Driver economics page.
# Reference for what every section means: SOPS/chauffeur-load-metrics.md (SOP-003).
# Plan + audit findings: docs/chauffeur-load-views.md
# ============================================================================

_LOAD_WINDOWS = {"7": 7, "30": 30, "90": 90}
_LOAD_WINDOW_DEFAULT = "30"


def _chauffeur_load_context(request, *, include_exceptions):
    """Shared context builder. The ONLY difference between the two pages is
    include_exceptions: the dispatcher context never contains the exceptions list or
    the row flags derived from it, so no template branch can leak them."""
    from dispatching.load_insights import build_insights
    from dispatching.load_metrics import (
        build_fleet_summary, build_load_rows, serialize_rows,
    )

    win_key = request.GET.get("window", _LOAD_WINDOW_DEFAULT)
    if win_key not in _LOAD_WINDOWS:
        win_key = _LOAD_WINDOW_DEFAULT
    window_days = _LOAD_WINDOWS[win_key]

    today = timezone.localdate()
    end = today - timedelta(days=1)          # yesterday: today is still in progress
    start = end - timedelta(days=window_days - 1)
    prior_end = start - timedelta(days=1)    # the equal window immediately before
    prior_start = prior_end - timedelta(days=window_days - 1)

    rows = build_load_rows(start, end, today=today)
    summary = build_fleet_summary(rows, window_days=window_days)
    # Lite pass: counted numbers only, for the tile comparisons and the trend finding.
    prior_rows = build_load_rows(prior_start, prior_end, today=today, lite=True)
    prior_summary = build_fleet_summary(prior_rows, window_days=window_days)

    insights = build_insights(rows, window_days, prior_rows=prior_rows,
                              include_admin_link=include_exceptions)
    exceptions, handled_exceptions = [], []
    if include_exceptions:
        exceptions, handled_exceptions = _apply_exception_dismissals(
            insights["exceptions"], insights["fired"], win_key,
            roster_ids={r["id"] for r in rows})
    # Handled entries lose the roster highlight too — the flag means "waiting".
    flagged_ids = {e["driver_id"] for e in exceptions}

    return {
        # Passed through |json_script in the template rather than pre-dumped, so
        # escaping is Django's problem and not ours.
        "rows_data": serialize_rows(rows, flagged_ids=flagged_ids),
        "summary": summary,
        "prior_summary": prior_summary if prior_summary["drivers"] else None,
        "findings": insights["findings"],
        "exceptions": exceptions,
        "handled_exceptions": handled_exceptions,
        "is_kpi_page": include_exceptions,
        "window_key": win_key,
        "window_days": window_days,
        "window_choices": [("7", "7 days"), ("30", "30 days"), ("90", "90 days")],
        "range_start": start,
        "range_end": end,
        "unlabelled_count": summary.get("unlabelled", 0),
        "page_kind": "kpis" if include_exceptions else "load",
    }


def _apply_exception_dismissals(exceptions, fired, win_key, roster_ids):
    """Split the exceptions list into (active, handled) using episode semantics.

    A dismissal suppresses its (driver, rule) entry while that rule keeps firing.
    Spending — setting cleared_at when the episode is over — happens only when the
    window the dismissal was MADE on is being viewed, and only while its driver is in
    the rendered roster. Rule floors scale with the window, so another window's render
    must not judge the episode; and a deactivated driver or an empty roster is an
    unevaluated episode, not an ended one — spending there would silently discard the
    dismissal (and its note) with no undo path.

    ``fired`` comes pre-collapse from build_insights and includes outranked rules, so
    a collapse or a bigger problem taking priority never spends a dismissal early. A
    dismissal whose pair fires but has no display entry (outranked or collapsed) still
    gets a fallback handled row, so it stays visible and undo-able.
    """
    from dispatching.load_insights import RULE_LABELS
    from dispatching.models import ChauffeurExceptionDismissal

    dismissals = list(
        ChauffeurExceptionDismissal.objects
        .filter(cleared_at__isnull=True)
        .select_related("dismissed_by", "driver__profile")
    )
    if not dismissals:
        return exceptions, []

    fired_set = set(fired)
    spent = [d.id for d in dismissals
             if d.window == win_key
             and d.driver_id in roster_ids
             and (d.driver_id, d.rule) not in fired_set]
    if spent:
        ChauffeurExceptionDismissal.objects.filter(id__in=spent).update(
            cleared_at=timezone.now())
        dismissals = [d for d in dismissals if d.id not in set(spent)]

    def _handled_meta(d):
        return {
            "dismissal_id": d.id,
            "dismissed_by": (d.dismissed_by.get_full_name()
                             or d.dismissed_by.get_username()) if d.dismissed_by else "",
            "dismissed_at": d.dismissed_at,
            "note": d.note,
        }

    by_pair = {(d.driver_id, d.rule): d for d in dismissals}
    active, handled = [], []
    for e in exceptions:
        d = by_pair.pop((e["driver_id"], e["rule"]), None)
        if d is None:
            active.append(e)
        else:
            handled.append({**e, **_handled_meta(d)})

    # Suppressions with no display entry this render: the rule still fires but was
    # outranked or collapsed. Without a fallback row the dismissal would be invisible
    # and impossible to undo until it happened to win the listing again.
    from dispatching.load_metrics import _display_name, _initials, avatar_color
    for (driver_id, rule), d in by_pair.items():
        if (driver_id, rule) not in fired_set:
            continue
        handled.append({
            "driver_id": driver_id,
            "name": _display_name(d.driver),
            "initials": _initials(d.driver),
            "color": avatar_color(driver_id),
            "employment_type": d.driver.employment_type or "",
            "employment_label": "",
            "rule": rule,
            "reason": f"{RULE_LABELS.get(rule, rule)} — still applies.",
            **_handled_meta(d),
        })
    return active, handled


@login_required(login_url="login")
def chauffeur_load(request):
    """Dispatcher-facing chauffeur load. Counted workload + findings only."""
    if not request.user.is_staff:
        messages.error(request, "Permission denied.")
        return redirect("dashboard")
    return render(request, "dispatching/chauffeur_load.html",
                  _chauffeur_load_context(request, include_exceptions=False))


@login_required(login_url="login")
def chauffeur_kpis(request):
    """Management chauffeur KPIs. Adds the exceptions list and its row flags."""
    if not request.user.is_superuser:
        messages.error(request, "Permission denied.")
        return redirect("dashboard")
    return render(request, "dispatching/chauffeur_load.html",
                  _chauffeur_load_context(request, include_exceptions=True))


def _redirect_to_kpis(request):
    win = request.POST.get("window", _LOAD_WINDOW_DEFAULT)
    if win not in _LOAD_WINDOWS:
        win = _LOAD_WINDOW_DEFAULT
    return redirect(f"{reverse('chauffeur_kpis')}?window={win}")


@login_required(login_url="login")
@require_POST
def chauffeur_exception_dismiss(request):
    """Mark one "Worth a conversation" entry handled (episode semantics — see the
    ChauffeurExceptionDismissal docstring)."""
    from dispatching.load_insights import EXCEPTION_RULES
    from dispatching.models import ChauffeurExceptionDismissal
    from drivers.models import Driver

    if not request.user.is_superuser:
        messages.error(request, "Permission denied.")
        return redirect("dashboard")

    rule = request.POST.get("rule", "")
    if rule not in EXCEPTION_RULES:
        messages.error(request, "Unknown rule.")
        return _redirect_to_kpis(request)
    try:
        driver_id = int(request.POST.get("driver_id", ""))
    except (TypeError, ValueError):
        messages.error(request, "Unknown chauffeur.")
        return _redirect_to_kpis(request)
    driver = get_object_or_404(Driver, id=driver_id)
    note = (request.POST.get("note") or "").strip()[:200]
    window = request.POST.get("window", _LOAD_WINDOW_DEFAULT)
    if window not in _LOAD_WINDOWS:
        window = _LOAD_WINDOW_DEFAULT

    obj, created = ChauffeurExceptionDismissal.objects.get_or_create(
        driver=driver, rule=rule, cleared_at=None,
        defaults={"dismissed_by": request.user, "note": note, "window": window},
    )
    if not created and note and not obj.note:
        obj.note = note
        obj.save(update_fields=["note"])
    messages.success(
        request,
        f"Marked handled for {driver}. It stays under Handled while the situation "
        f"lasts, and will come back only if it happens again after clearing.",
    )
    return _redirect_to_kpis(request)


@login_required(login_url="login")
@require_POST
def chauffeur_exception_undo(request):
    """Bring a handled entry back to the active list."""
    from dispatching.models import ChauffeurExceptionDismissal

    if not request.user.is_superuser:
        messages.error(request, "Permission denied.")
        return redirect("dashboard")

    try:
        dismissal_id = int(request.POST.get("dismissal_id", ""))
    except (TypeError, ValueError):
        messages.error(request, "Not found.")
        return _redirect_to_kpis(request)
    dismissal = get_object_or_404(
        ChauffeurExceptionDismissal,
        id=dismissal_id, cleared_at__isnull=True,
    )
    dismissal.delete()
    messages.success(request, "Back on the list.")
    return _redirect_to_kpis(request)
