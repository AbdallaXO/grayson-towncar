from django.shortcuts import get_object_or_404, render, redirect
from .models import Driver, DriverPayment, LegPayment, DriverPayoutAdjustment, DriverDateOverride
from datetime import datetime, timedelta
from reservations.models import Leg, LegStatus
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods, require_POST
from django.http import FileResponse
from dispatching.aeroapi_service import AeroAPIService
import json
import os
from django.conf import settings
from django.contrib import messages
from django.db.models import Q, Prefetch, Count, Sum, Value, DecimalField, Subquery, OuterRef
from django.db.models.functions import Coalesce
from drivers.utils import get_drive_time as google_drive_time
from drivers.availability import format_shift_preference
from dispatching.scheduler import (
    estimate_job_end_time,
    get_drive_time as scheduler_drive_time,
    preload_timing_cache,
    INTER_JOB_BUFFER,
)
from dispatching.analytics import categorize_location


def _compute_eta_string(pickup_date, pickup_time, now, today):
    """Compute a human-friendly ETA string for a leg."""
    from datetime import datetime as _dt
    pickup_dt = timezone.make_aware(_dt.combine(pickup_date, pickup_time))
    diff = pickup_dt - now
    total_mins = int(diff.total_seconds() / 60)
    days_away = (pickup_date - today).days

    if total_mins < 0:
        return "Now"
    elif total_mins < 60:
        return f"In {total_mins} min{'s' if total_mins != 1 else ''}"
    elif days_away == 0:
        hours = total_mins // 60
        return f"Today in {hours} hr{'s' if hours != 1 else ''}"
    elif days_away == 1:
        return "Tomorrow"
    else:
        return f"In {days_away} days"


def _annotate_legs_with_scheduling(legs_list, target_date):
    """
    Enrich legs with estimated end times and smart conflict detection
    that accounts for drive time, dwell time, and repositioning.
    """
    from datetime import datetime as _dt

    preload_timing_cache()

    # Compute estimated end time for each leg
    for leg in legs_list:
        try:
            leg.estimated_end = estimate_job_end_time(leg, target_date)
        except Exception:
            leg.estimated_end = None

    # Smart conflict detection between consecutive same-day trips
    for i in range(len(legs_list) - 1):
        curr = legs_list[i]
        nxt = legs_list[i + 1]
        if curr.pickup_date != nxt.pickup_date:
            continue
        if not curr.estimated_end:
            continue

        # Compute repositioning time from current dropoff to next pickup
        curr_dropoff_cat = categorize_location(curr.dropoff_location)
        nxt_pickup_cat = categorize_location(nxt.pickup_location)
        reposition_mins = scheduler_drive_time(curr_dropoff_cat, nxt_pickup_cat)

        nxt_pickup_dt = _dt.combine(nxt.pickup_date, nxt.pickup_time)
        # Buffer = time between earliest availability and next pickup
        earliest_available = curr.estimated_end + timedelta(minutes=reposition_mins + INTER_JOB_BUFFER)
        buffer_mins = int((nxt_pickup_dt - earliest_available).total_seconds() / 60)

        curr.reposition_mins = reposition_mins
        curr.buffer_to_next = buffer_mins

        if buffer_mins < 0:
            curr.conflict_next = True
            curr.conflict_severity = "overlap"
            curr.conflict_gap_mins = abs(buffer_mins)
        elif buffer_mins < 15:
            curr.conflict_next = True
            curr.conflict_severity = "tight"
            curr.conflict_gap_mins = buffer_mins
        elif buffer_mins < 30:
            curr.conflict_next = True
            curr.conflict_severity = "close"
            curr.conflict_gap_mins = buffer_mins
        else:
            # Comfortable buffer — show as green info strip
            curr.buffer_ok = True


def _annotate_next_early(legs_list):
    """Set `leg.next_early` when the driver's NEXT pickup (same day) is an airport
    arrival whose flight is landing early. Drives a subtle heads-up on the current
    card so the driver can be proactive and reach the airport early. Reuses the same
    early-flight signal as the dispatcher board (Leg.flight_timing_flag). Call AFTER
    _annotate_legs_with_scheduling so `conflict_next` is available for the tight flag."""
    for i in range(len(legs_list) - 1):
        curr, nxt = legs_list[i], legs_list[i + 1]
        if curr.pickup_date != nxt.pickup_date:
            continue
        try:
            if nxt.get_trip_type() == "arrival" and nxt.flight_information:
                nflag = nxt.flight_timing_flag()
                if nflag and nflag["direction"] == "early":
                    curr.next_early = {
                        "minutes": nflag["minutes"],
                        "arrival_label": nflag["arrival_label"],
                        "level": nflag["level"],
                        "tight": bool(getattr(curr, "conflict_next", False)),
                    }
        except Exception:
            pass


def _annotate_legs_with_live_eta(legs_list):
    """
    For legs that are on-the-way or picked-up, fetch the latest GPS snapshot
    and attach live_eta_minutes to the leg.
    Gracefully handles missing table (pre-migration deployment).
    """
    from reservations.models import DriverLocation

    en_route_ids = [
        leg.id for leg in legs_list
        if leg.status in ("on-the-way", "picked-up", "on-location")
    ]
    if not en_route_ids:
        return

    try:
        # Fetch latest location per leg in one query
        from django.db.models import Max
        latest_timestamps = (
            DriverLocation.objects
            .filter(leg_id__in=en_route_ids)
            .values("leg_id")
            .annotate(latest=Max("timestamp"))
        )
        ts_map = {row["leg_id"]: row["latest"] for row in latest_timestamps}

        if not ts_map:
            return

        # Fetch the actual location records
        locations = DriverLocation.objects.filter(
            leg_id__in=ts_map.keys(),
            timestamp__in=ts_map.values(),
        )
        loc_map = {loc.leg_id: loc for loc in locations}

        for leg in legs_list:
            loc = loc_map.get(leg.id)
            if loc and loc.eta_minutes is not None:
                leg.live_eta_minutes = loc.eta_minutes
                leg.live_eta_status = loc.status
                age = (timezone.now() - loc.timestamp).total_seconds()
                leg.live_eta_age_mins = int(age / 60)
                # Compute estimated arrival time: snapshot time + eta
                arrival_dt = loc.timestamp + timedelta(minutes=loc.eta_minutes)
                leg.live_eta_arrival = timezone.localtime(arrival_dt).strftime('%I:%M %p').lstrip('0')
    except Exception:
        return


@login_required(login_url="login")
def index(request):
    """
    Driver Dashboard Shows All Legs - Lets you Filter by Date
    """
    # Get the driver object only once
    driver = get_object_or_404(Driver, profile=request.user)

    # Parse selected date
    selected_date = request.GET.get("date")
    try:
        if selected_date:
            selected_date = datetime.strptime(selected_date, "%Y-%m-%d").date()
        else:
            selected_date = timezone.localdate()
    except ValueError:
        selected_date = timezone.localdate()

    # Use select_related to fetch related reservation and customer data in a single query
    # Prefetch all legs per reservation to avoid N+1 when checking is_first_leg
    legs = (
        Leg.objects.select_related(
            "reservation", "reservation__customer", "reservation__vehicle", "vehicle",
            "flight_information", "cruise_information"
        )
        .prefetch_related("reservation__legs")
        .filter(driver=driver, pickup_date=selected_date)
        .order_by("pickup_time")
    )

    now = timezone.localtime()
    today = timezone.localdate()
    legs_list = list(legs)

    for leg in legs_list:
        first_id = min(l.id for l in leg.reservation.legs.all())
        leg.is_first_leg = leg.id == first_id
        leg.eta = _compute_eta_string(leg.pickup_date, leg.pickup_time, now, today)

    # ── Flight delay alerts ──
    for leg in legs_list:
        if leg.flight_information and leg.get_trip_type() == 'arrival':
            delay = leg.get_flight_time_mismatch_display(30)
            if delay:
                leg.flight_delay = delay

    # ── Smart conflict detection (accounts for drive time + job duration) ──
    _annotate_legs_with_scheduling(legs_list, selected_date)

    # ── Heads-up: the NEXT pickup is an early-landing flight ──
    _annotate_next_early(legs_list)

    # ── Route preview (drive time) + estimated done ──
    if settings.GOOGLE_MAPS_API_KEY:
        for leg in legs_list:
            leg.drive_info = google_drive_time(leg.pickup_location, leg.dropoff_location)
            if leg.drive_info and leg.drive_info.get('duration_seconds') and leg.pickup_time:
                from datetime import datetime as _dt2, timedelta
                pickup_dt = _dt2.combine(selected_date, leg.pickup_time)
                done_dt = pickup_dt + timedelta(seconds=leg.drive_info['duration_seconds']) + timedelta(minutes=2)
                leg.estimated_done = timezone.localtime(timezone.make_aware(done_dt)).strftime('%I:%M %p').lstrip('0')

    # ── Live GPS ETA for en-route legs ──
    _annotate_legs_with_live_eta(legs_list)

    is_today = selected_date == today

    return render(
        request, "drivers/index.html", {
            "legs": legs_list,
            "selected_date": selected_date,
            "is_today": is_today,
        }
    )


@login_required(login_url="login")
def completed_trips(request):
    driver = get_object_or_404(Driver, profile=request.user)

    # Use select_related to fetch related data efficiently
    # Prefetch all legs per reservation to avoid N+1 when checking is_first_leg
    legs = (
        Leg.objects.select_related(
            "reservation", "reservation__customer", "reservation__vehicle", "vehicle",
            "flight_information", "cruise_information"
        )
        .prefetch_related("reservation__legs")
        .filter(driver=driver, status="completed")
        .exclude(reservation__status="cancelled")
        .order_by("-pickup_date", "-pickup_time")
    )

    # Add is_first_leg property using prefetched data (no extra queries)
    for leg in legs:
        first_id = min(l.id for l in leg.reservation.legs.all())
        leg.is_first_leg = leg.id == first_id

    return render(
        request,
        "drivers/completed_trips.html",
        {"legs": legs, "title": "Completed Trips"},
    )


@login_required(login_url="login")
def schedule(request):
    driver = get_object_or_404(Driver, profile=request.user)
    today = timezone.localdate()
    next_week = today + timezone.timedelta(days=60)

    # Use select_related to fetch related data efficiently
    # Prefetch all legs per reservation to avoid N+1 when checking is_first_leg
    legs_list = list(
        Leg.objects.select_related(
            "reservation", "reservation__customer", "reservation__vehicle", "vehicle",
            "flight_information", "cruise_information"
        )
        .prefetch_related("reservation__legs")
        .filter(
            driver=driver,
            pickup_date__gte=today,
            pickup_date__lte=next_week,
        )
        # Show every status the leg might be in EXCEPT terminal ones.
        # Whitelisting active statuses hid trips whose state was set outside
        # the DRIVER_STATUS choices (e.g. legacy "pending" rows) and is_null,
        # which is why the weekly schedule was empty while the date-search
        # view (which has no status filter) still showed them.
        .exclude(status__in=["completed", "cancelled"])
        .exclude(reservation__status="cancelled")
        .order_by("pickup_date", "pickup_time")
    )

    now = timezone.localtime()

    # Add is_first_leg property using prefetched data (no extra queries)
    for leg in legs_list:
        first_id = min(l.id for l in leg.reservation.legs.all())
        leg.is_first_leg = leg.id == first_id

    # ── Flight delay alerts ──
    for leg in legs_list:
        if leg.flight_information and leg.get_trip_type() == 'arrival':
            delay = leg.get_flight_time_mismatch_display(30)
            if delay:
                leg.flight_delay = delay

    # ── Smart conflict detection (accounts for drive time + job duration) ──
    _annotate_legs_with_scheduling(legs_list, today)

    # ── Heads-up: the NEXT pickup is an early-landing flight ──
    _annotate_next_early(legs_list)

    # ── Route preview (drive time) + estimated done ──
    if settings.GOOGLE_MAPS_API_KEY:
        for leg in legs_list:
            leg.drive_info = google_drive_time(leg.pickup_location, leg.dropoff_location)
            if leg.drive_info and leg.drive_info.get('duration_seconds') and leg.pickup_time:
                from datetime import datetime as _dt2, timedelta
                pickup_dt = _dt2.combine(leg.pickup_date, leg.pickup_time)
                done_dt = pickup_dt + timedelta(seconds=leg.drive_info['duration_seconds']) + timedelta(minutes=2)
                leg.estimated_done = timezone.localtime(timezone.make_aware(done_dt)).strftime('%I:%M %p').lstrip('0')

    # ── Live GPS ETA for en-route legs ──
    _annotate_legs_with_live_eta(legs_list)

    next_leg = legs_list[0] if legs_list else None
    next_leg_eta = ""
    if next_leg:
        next_leg_eta = _compute_eta_string(next_leg.pickup_date, next_leg.pickup_time, now, today)

    return render(
        request,
        "drivers/weekly_schedule.html",
        {"legs": legs_list, "today": today, "next_week": next_week, "next_leg": next_leg, "next_leg_eta": next_leg_eta},
    )


@login_required(login_url="login")
@require_http_methods(["POST"])
def update_leg_status(request, leg_id):
    try:
        # Ensure the leg belongs to the current driver - no need to fetch related objects here
        leg = get_object_or_404(Leg, id=leg_id, driver__profile=request.user)

        # Parse the request body
        data = json.loads(request.body)
        new_status = data.get("status")

        # Validate status
        VALID_STATUSES = ["in-progress", "confirmed", "on-the-way", "picked-up", "on-location", "completed"]
        if new_status not in VALID_STATUSES:
            return JsonResponse(
                {"success": False, "error": "Invalid status"}, status=400
            )

        # Update and save the leg
        leg.status = new_status
        leg.save(update_fields=["status"])

        # Create LegStatus history entry (driver made this update)
        LegStatus.objects.create(
            leg=leg,
            status=new_status,
            updated_by=request.user,  # Driver's user account
            timestamp=timezone.now()
        )

        # Save GPS snapshot if coordinates provided
        lat = data.get("latitude")
        lng = data.get("longitude")
        if lat is not None and lng is not None:
            from reservations.models import DriverLocation
            from reservations.utils import _run_in_background

            location = DriverLocation.objects.create(
                leg=leg,
                driver=leg.driver,
                status=new_status,
                latitude=lat,
                longitude=lng,
                accuracy_meters=data.get("accuracy"),
                heading=data.get("heading"),
                speed_mps=data.get("speed"),
            )

            # Compute ETA in background via Google Maps
            if settings.GOOGLE_MAPS_API_KEY and new_status in ("on-the-way", "on-location", "picked-up"):
                # on-the-way/on-location → ETA to pickup, picked-up → ETA to dropoff
                dest = leg.dropoff_location if new_status == "picked-up" else leg.pickup_location
                # Fallback origin: use pickup address if GPS coords don't resolve
                fallback = leg.pickup_location if new_status == "picked-up" else None
                _run_in_background(_compute_location_eta, location.id, dest, fallback)

        elif settings.GOOGLE_MAPS_API_KEY and new_status in ("on-the-way", "on-location", "picked-up"):
            # No GPS coords at all — compute ETA from known addresses as fallback
            from reservations.utils import _run_in_background
            dest = leg.dropoff_location if new_status == "picked-up" else leg.pickup_location
            _run_in_background(_compute_fallback_eta, leg, dest)

        # Check if reservation should be auto-completed
        if new_status == "completed":
            reservation_updated = leg.reservation.check_and_update_completion_status()
            if reservation_updated:
                print(f"Auto-completed reservation {leg.reservation.id} - all legs completed")

        return JsonResponse(
            {
                "success": True,
                "message": "Status updated successfully",
                "new_status": new_status,
            }
        )

    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


def _parse_duration_to_minutes(duration_text):
    """Parse Google Maps duration like '25 mins' or '1 hour 10 mins' into minutes."""
    import re
    text = duration_text.lower()
    minutes = 0
    if "hour" in text:
        parts = text.split("hour")
        minutes += int(parts[0].strip()) * 60
        text = parts[1] if len(parts) > 1 else ""
    if "min" in text:
        nums = re.findall(r'\d+', text)
        if nums:
            minutes += int(nums[0])
    return minutes


def _compute_location_eta(location_id, destination_address, fallback_origin=None):
    """Background: compute ETA from GPS snapshot to destination via Google Maps.
    If GPS-based lookup fails, falls back to fallback_origin (e.g. pickup address)."""
    try:
        from reservations.models import DriverLocation
        loc = DriverLocation.objects.get(id=location_id)
        origin = f"{loc.latitude},{loc.longitude}"
        result = google_drive_time(origin, destination_address)

        # Fallback: if GPS coords didn't resolve, try the address-based origin
        if not result or not result.get("duration_text"):
            if fallback_origin:
                result = google_drive_time(fallback_origin, destination_address)

        if result and result.get("duration_text"):
            minutes = _parse_duration_to_minutes(result["duration_text"])
            if minutes > 0:
                loc.eta_minutes = minutes
                loc.eta_destination = destination_address
                loc.save(update_fields=["eta_minutes", "eta_destination"])
    except Exception:
        pass  # Non-critical — ETA just won't be available


def _compute_fallback_eta(leg, destination_address):
    """Compute ETA using leg's pickup/dropoff address when no GPS is available.
    Creates a DriverLocation record with the fallback ETA so it shows up on dashboards."""
    try:
        from reservations.models import DriverLocation

        # For picked-up: origin is pickup location, dest is dropoff
        # For on-the-way/on-location: origin is some known point, dest is pickup
        origin = leg.pickup_location
        result = google_drive_time(origin, destination_address)
        if result and result.get("duration_text"):
            minutes = _parse_duration_to_minutes(result["duration_text"])
            if minutes > 0:
                DriverLocation.objects.create(
                    leg=leg,
                    driver=leg.driver,
                    status=leg.status,
                    latitude=0,
                    longitude=0,
                    eta_minutes=minutes,
                    eta_destination=destination_address,
                )
    except Exception:
        pass


@login_required
@require_http_methods(["GET"])
def get_driver_eta(request, leg_id):
    """Return the latest GPS snapshot + ETA for a leg (staff or assigned driver)."""
    from reservations.models import DriverLocation

    leg = get_object_or_404(Leg, id=leg_id)

    # Allow staff or the assigned driver
    if not request.user.is_staff and (not leg.driver or leg.driver.profile != request.user):
        return JsonResponse({"error": "Permission denied"}, status=403)

    latest = DriverLocation.objects.filter(leg=leg).first()
    if not latest:
        # No GPS data — compute fallback ETA from leg addresses
        if settings.GOOGLE_MAPS_API_KEY and leg.status in ("on-the-way", "on-location", "picked-up"):
            dest = leg.dropoff_location if leg.status == "picked-up" else leg.pickup_location
            origin = leg.pickup_location
            result = google_drive_time(origin, dest)
            if result and result.get("duration_text"):
                minutes = _parse_duration_to_minutes(result["duration_text"])
                if minutes > 0:
                    return JsonResponse({
                        "has_location": False,
                        "eta_minutes": minutes,
                        "is_fallback": True,
                        "status": leg.status,
                    })
        return JsonResponse({"has_location": False})

    age_seconds = (timezone.now() - latest.timestamp).total_seconds()

    # If GPS-based ETA is missing, compute fallback from addresses
    eta_minutes = latest.eta_minutes
    is_fallback = False
    if eta_minutes is None and settings.GOOGLE_MAPS_API_KEY:
        dest = leg.dropoff_location if leg.status == "picked-up" else leg.pickup_location
        origin = leg.pickup_location
        result = google_drive_time(origin, dest)
        if result and result.get("duration_text"):
            eta_minutes = _parse_duration_to_minutes(result["duration_text"])
            is_fallback = True

    return JsonResponse({
        "has_location": True,
        "latitude": float(latest.latitude),
        "longitude": float(latest.longitude),
        "eta_minutes": eta_minutes,
        "is_fallback": is_fallback,
        "status": latest.status,
        "timestamp": latest.timestamp.isoformat(),
        "age_seconds": int(age_seconds),
    })


@login_required
@require_POST
def report_location(request):
    """Periodic GPS update from driver while en route. Called every 3 minutes by JS."""
    try:
        data = json.loads(request.body)
        leg_id = data.get("leg_id")
        lat = data.get("latitude")
        lng = data.get("longitude")

        if not leg_id or lat is None or lng is None:
            return JsonResponse({"success": False, "error": "Missing fields"}, status=400)

        leg = get_object_or_404(Leg, id=leg_id, driver__profile=request.user)

        # Only accept updates for en-route statuses
        if leg.status not in ("on-the-way", "on-location", "picked-up"):
            return JsonResponse({"success": False, "error": "Not en route"}, status=400)

        from reservations.models import DriverLocation
        from reservations.utils import _run_in_background

        location = DriverLocation.objects.create(
            leg=leg,
            driver=leg.driver,
            status=leg.status,
            latitude=lat,
            longitude=lng,
            accuracy_meters=data.get("accuracy"),
            heading=data.get("heading"),
            speed_mps=data.get("speed"),
        )

        # Compute ETA in background
        if settings.GOOGLE_MAPS_API_KEY and leg.status in ("on-the-way", "on-location", "picked-up"):
            dest = leg.dropoff_location if leg.status == "picked-up" else leg.pickup_location
            # Fallback: use pickup address if GPS coords don't resolve
            fallback = leg.pickup_location if leg.status == "picked-up" else None
            _run_in_background(_compute_location_eta, location.id, dest, fallback)

        return JsonResponse({"success": True})

    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required
@require_http_methods(["POST"])
def accept_job(request, leg_id):
    try:
        # Ensure the leg belongs to the current driver
        leg = get_object_or_404(Leg, id=leg_id, driver__profile=request.user)
        
        # Only allow accepting if status is in-progress
        if leg.status != "in-progress":
            return JsonResponse(
                {"success": False, "error": "Can only accept jobs that are in-progress"}, 
                status=400
            )

        # Update status to confirmed
        leg.status = "confirmed"
        leg.save(update_fields=["status"])

        # Create LegStatus history entry (driver accepted this job)
        LegStatus.objects.create(
            leg=leg,
            status="confirmed",
            updated_by=request.user,  # Driver's user account
            timestamp=timezone.now()
        )

        return JsonResponse(
            {
                "success": True,
                "message": "Job accepted successfully",
                "new_status": "confirmed",
            }
        )

    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required
@require_http_methods(["POST"])
def update_driver_notes(request, leg_id):
    try:
        # Ensure the leg belongs to the current driver
        leg = get_object_or_404(Leg, id=leg_id, driver__profile=request.user)

        # Parse the request body
        data = json.loads(request.body)
        driver_notes = data.get("driver_notes", "")

        # Update and save the leg
        leg.driver_notes = driver_notes
        leg.save(update_fields=["driver_notes"])

        return JsonResponse(
            {
                "success": True,
                "message": "Driver notes updated successfully",
            }
        )

    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


def service_worker(request):
    """Serve sw.js from /drivers/sw.js so the service worker scope covers /drivers/."""
    sw_path = os.path.join(settings.BASE_DIR, "content", "static", "drivers", "sw.js")
    return FileResponse(
        open(sw_path, "rb"),
        content_type="application/javascript",
        headers={"Service-Worker-Allowed": "/drivers/"},
    )


@login_required(login_url="login")
def extend(request):
    """
    Extended driver list view for dispatchers
    Shows all drivers with phone numbers, schedules, vehicles, and availability
    """
    if not request.user.is_staff:
        return redirect("home")
    
    # Get filter parameters. `tab` is the new pill-bar param; `type` kept for
    # backward compatibility with old links/bookmarks.
    tab_param = request.GET.get("tab", "")
    if tab_param in ("inhouse", "affiliate"):
        driver_type_filter = tab_param
        active_tab = tab_param
    elif tab_param == "all":
        driver_type_filter = ""
        active_tab = "all"
    else:
        driver_type_filter = request.GET.get("type", "")  # "inhouse" or "affiliate"
        active_tab = driver_type_filter or "all"
    search_query = request.GET.get("search", "")
    availability_filter = request.GET.get("availability", "")  # "available" or "busy"
    active_only = request.GET.get("active_only", "") == "1"

    # Get all drivers with related profile data
    drivers = Driver.objects.select_related(
        "profile"
    ).all()

    # Inactive drivers (departed / on extended leave) stay listed in the directory,
    # marked Inactive — the directory is the one place they remain visible. They are
    # excluded everywhere else (planner / board / vehicle assignment). The optional
    # "Active only" filter hides them here too.
    if active_only:
        drivers = drivers.filter(is_active=True)

    # Apply filters
    if driver_type_filter:
        drivers = drivers.filter(driver_type=driver_type_filter)
    
    if search_query:
        drivers = drivers.filter(
            Q(profile__first_name__icontains=search_query) |
            Q(profile__last_name__icontains=search_query) |
            Q(profile__username__icontains=search_query) |
            Q(vehicle__icontains=search_query)
        )
    
    # Get upcoming legs for each driver (next 7 days)
    today = timezone.localdate()
    next_week = today + timedelta(days=7)
    
    # Annotate with upcoming leg counts and lifetime total paid.
    # `lifetime_paid` is computed via subquery to avoid being inflated by the
    # legs join below (a Sum + Count in the same annotate() multiplies rows).
    lifetime_paid_sq = (
        DriverPayment.objects
        .filter(driver=OuterRef("pk"))
        .values("driver")
        .annotate(total=Sum("amount"))
        .values("total")
    )
    drivers = drivers.annotate(
        upcoming_count=Count(
            "legs",
            filter=Q(
                legs__pickup_date__gte=today,
                legs__pickup_date__lte=next_week,
                legs__status__in=["confirmed", "in-progress", "on-the-way", "picked-up", "on-location"]
            ),
        ),
        lifetime_paid=Coalesce(
            Subquery(lifetime_paid_sq, output_field=DecimalField(max_digits=12, decimal_places=2)),
            Value(0, output_field=DecimalField(max_digits=12, decimal_places=2)),
        ),
    )
    
    # Apply availability filter
    if availability_filter == "available":
        drivers = drivers.filter(upcoming_count=0)
    elif availability_filter == "busy":
        drivers = drivers.filter(upcoming_count__gt=0)
    
    # Order: active drivers first, then inhouse before affiliate, then by name
    # (inactive drivers sink to the bottom of the directory).
    drivers = drivers.order_by("-is_active", "-driver_type", "profile__first_name", "profile__last_name")
    
    # Prefetch today's active legs for mini-schedule display
    today_legs_prefetch = Prefetch(
        'legs',
        queryset=Leg.objects.filter(
            pickup_date=today,
            status__in=["confirmed", "in-progress", "on-the-way", "picked-up", "on-location"],
        ).select_related("reservation__customer").order_by("pickup_time"),
        to_attr='todays_legs',
    )
    drivers = drivers.prefetch_related(
        today_legs_prefetch,
        "certified_vehicle_types", "preferred_vehicle_types", "preferred_vehicles",
    )

    # Evaluate queryset once as a list to avoid repeated DB hits
    drivers_list = list(drivers)
    available_count = 0
    inhouse_count = 0

    for driver in drivers_list:
        # vehicle_display is pure Python — no DB query
        driver.vehicle_display = driver.get_vehicle_display()

        # Plain-language default shift preference (e.g. "Flexible · prefers mornings")
        driver.shift_pref_label = format_shift_preference({
            "is_available": True,
            "flexible": driver.default_flexible,
            "preferred_shift": driver.default_preferred_shift,
        })

        # Count stats using the already-annotated upcoming_count
        if driver.upcoming_count == 0:
            available_count += 1
        if driver.driver_type == "inhouse":
            inhouse_count += 1

        # Build mini-schedule summary for today
        if driver.todays_legs:
            next_leg = driver.todays_legs[0]
            driver.next_trip_time = next_leg.pickup_time
            driver.next_trip_pickup = next_leg.pickup_location
            driver.next_trip_dropoff = next_leg.dropoff_location
            driver.next_trip_status = next_leg.status
            driver.today_trip_count = len(driver.todays_legs)

    # Counts for tab pills — these reflect the *type* split across the roster
    # visible under the current "show inactive" state, so tab labels stay
    # consistent with what the user sees after toggling search/availability.
    base_count_qs = Driver.objects.filter(is_active=True) if active_only else Driver.objects.all()
    all_count_total = base_count_qs.count()
    inhouse_count_total = base_count_qs.filter(driver_type="inhouse").count()
    affiliate_count_total = all_count_total - inhouse_count_total
    inactive_count_total = Driver.objects.filter(is_active=False).count()

    context = {
        "drivers": drivers_list,
        "driver_type_filter": driver_type_filter,
        "active_tab": active_tab,
        "search_query": search_query,
        "availability_filter": availability_filter,
        "today": today,
        "next_week": next_week,
        "total_drivers": len(drivers_list),
        "available_count": available_count,
        "inhouse_count": inhouse_count,
        "all_count_total": all_count_total,
        "inhouse_count_total": inhouse_count_total,
        "affiliate_count_total": affiliate_count_total,
        "inactive_count_total": inactive_count_total,
        "active_only": active_only,
    }

    return render(request, "drivers/extend.html", context)


@login_required(login_url="login")
def driver_profile(request, driver_id):
    """
    Staff-facing driver profile page.

    Aggregates info that already exists on the Driver model — contact, vehicle,
    schedule, today/upcoming legs, unpaid pay summary, recent payouts — onto a
    single page reachable from the directory and pay management.
    """
    if not request.user.is_staff:
        return redirect("home")

    driver = get_object_or_404(
        Driver.objects.select_related("profile"), id=driver_id
    )

    today = timezone.localdate()
    horizon = today + timedelta(days=14)

    # Upcoming legs in the next 14 days (active/in-flight statuses).
    upcoming_legs = (
        Leg.objects
        .filter(
            driver=driver,
            pickup_date__gte=today,
            pickup_date__lte=horizon,
        )
        .exclude(status__in=["cancelled"])
        .select_related("reservation", "reservation__customer")
        .order_by("pickup_date", "pickup_time")
    )

    todays_legs = [leg for leg in upcoming_legs if leg.pickup_date == today]

    # Unpaid leg summary — reuse model helpers so totals match Pay Mgmt exactly.
    unpaid_legs = driver.get_unpaid_legs() if hasattr(driver, "get_unpaid_legs") else []
    total_unpaid = (
        driver.get_total_unpaid_amount()
        if hasattr(driver, "get_total_unpaid_amount")
        else 0
    )

    # Recent payouts (last 10).
    recent_payments = (
        DriverPayment.objects
        .filter(driver=driver)
        .select_related("created_by")
        .order_by("-payment_date")[:10]
    )
    last_payment = recent_payments[0] if recent_payments else None

    # Weekly schedule overrides (DriverWeeklySchedule rows, one per day-of-week
    # configured by the user). Sorted Mon-Sun.
    weekly_schedule = list(
        driver.weekly_schedule.all().order_by("day_of_week")
        if hasattr(driver, "weekly_schedule") else []
    )

    # Date overrides (day off / vacation / sick / etc.) within the next 30 days.
    upcoming_overrides = []
    if hasattr(driver, "date_overrides"):
        upcoming_overrides = list(
            driver.date_overrides
            .filter(date__gte=today, date__lte=today + timedelta(days=30))
            .order_by("date")
        )

    # Availability today (uses model helper if present).
    is_available_today = (
        driver.is_available_today()
        if hasattr(driver, "is_available_today")
        else len(todays_legs) == 0
    )

    context = {
        "driver": driver,
        "today": today,
        "horizon": horizon,
        "todays_legs": todays_legs,
        "upcoming_legs": upcoming_legs,
        "upcoming_count": len(upcoming_legs),
        "unpaid_legs_count": len(unpaid_legs) if hasattr(unpaid_legs, "__len__") else unpaid_legs.count(),
        "total_unpaid": total_unpaid,
        "recent_payments": recent_payments,
        "last_payment": last_payment,
        "weekly_schedule": weekly_schedule,
        "upcoming_overrides": upcoming_overrides,
        "is_available_today": is_available_today,
    }
    return render(request, "drivers/driver_profile.html", context)


@login_required(login_url="login")
def driver_statement_list(request, driver_id):
    """
    Staff view of a driver's payment statements.
    """
    if not request.user.is_staff:
        return redirect("home")

    driver = get_object_or_404(Driver, id=driver_id)
    payments = (
        DriverPayment.objects.filter(driver=driver)
        .prefetch_related("leg_payments__leg")
        .order_by("-payment_date")
    )

    payment_rows = []
    for payment in payments:
        legs = [lp.leg for lp in payment.leg_payments.all() if lp.leg]
        leg_dates = [leg.pickup_date for leg in legs if leg.pickup_date]
        pay_period_start = min(leg_dates) if leg_dates else None
        pay_period_end = max(leg_dates) if leg_dates else None
        payment_rows.append(
            {
                "payment": payment,
                "legs_count": len(legs),
                "pay_period_start": pay_period_start,
                "pay_period_end": pay_period_end,
            }
        )

    context = {
        "driver": driver,
        "payment_rows": payment_rows,
    }

    return render(request, "drivers/driver_statement_list.html", context)


@login_required(login_url="login")
def driver_statement_detail(request, driver_id, payment_id):
    """
    Staff view of a single driver payment statement.

    POST (existing behavior) sends the statement email — kept untouched
    so the existing flow keeps working.

    GET shows the statement with line-level edit controls (void, edit
    amount, add missing leg). Voided lines are pulled out into a
    separate history section. The adjustment audit trail and the
    "missing leg candidates" for the add-leg modal are included in
    the context.
    """
    if not request.user.is_staff:
        return redirect("home")

    driver = get_object_or_404(Driver, id=driver_id)
    payment = get_object_or_404(DriverPayment, id=payment_id, driver=driver)
    all_lines = (
        LegPayment.objects.filter(payment=payment)
        .select_related("leg")
        .order_by("leg__pickup_date", "leg__pickup_time")
    )
    active_lines = [lp for lp in all_lines if lp.status == LegPayment.STATUS_ACTIVE]
    voided_lines = [lp for lp in all_lines if lp.status == LegPayment.STATUS_VOIDED]
    active_legs = [lp.leg for lp in active_lines if lp.leg]
    leg_dates = [leg.pickup_date for leg in active_legs if leg.pickup_date]
    pay_period_start = min(leg_dates) if leg_dates else None
    pay_period_end = max(leg_dates) if leg_dates else None

    if request.method == "POST":
        recipient_email = request.POST.get("recipient_email", "").strip()
        if not recipient_email:
            messages.error(request, "Please enter an email address.")
        else:
            from users.emails import send_driver_payment_statement

            email_sent = send_driver_payment_statement(
                driver=driver,
                payment=payment,
                legs=active_legs,
                recipient_email=recipient_email,
                sent_by=request.user,
            )
            if email_sent:
                messages.success(
                    request, f"Statement emailed to {recipient_email}."
                )
            else:
                messages.error(request, "Unable to send statement email.")

        return redirect(
            "driver_statement_detail",
            driver_id=driver.id,
            payment_id=payment.id,
        )

    # Adjustment history (most recent first)
    adjustments = (
        DriverPayoutAdjustment.objects
        .filter(payment=payment)
        .select_related("created_by", "leg")
        .order_by("-created_at")
    )

    # Missing-leg candidates — driver's completed but unpaid legs.
    # Capped at 50 to keep the modal lightweight.
    candidate_legs = (
        Leg.objects
        .filter(driver=driver, status="completed", payment_status="unpaid")
        .select_related("reservation", "reservation__customer")
        .order_by("-pickup_date", "-pickup_time")[:50]
    )

    # Email / export status for the banner.
    from drivers.payout_adjustments import statement_email_status
    email_status = statement_email_status(payment)

    context = {
        "driver": driver,
        "payment": payment,
        # `leg_payments` kept for backward compatibility with anything
        # else that may have read it. The template uses `active_lines`
        # and `voided_lines` for the new controls.
        "leg_payments": active_lines,
        "active_lines": active_lines,
        "voided_lines": voided_lines,
        "pay_period_start": pay_period_start,
        "pay_period_end": pay_period_end,
        "adjustments": adjustments,
        "candidate_legs": candidate_legs,
        "email_status": email_status,
    }

    return render(request, "drivers/driver_statement_detail.html", context)


# ── Payout adjustment endpoints ─────────────────────────────────────
#
# All three flip the LegPayment line / DriverPayment total inside a
# transaction (via the helpers in drivers.payout_adjustments) and
# redirect back to the statement detail page with a flash message.
#
# Staff-only. Each requires a non-blank reason. ValidationErrors from
# the helper layer surface as user-visible flash errors — no 500s.


def _adjust_redirect(driver_id, payment_id):
    return redirect("driver_statement_detail", driver_id=driver_id, payment_id=payment_id)


@login_required(login_url="login")
@require_POST
def void_leg_payment_view(request, driver_id, payment_id, leg_payment_id):
    """Void a single LegPayment line. Reason required."""
    if not request.user.is_staff:
        return redirect("home")

    from django.core.exceptions import ValidationError
    from drivers.payout_adjustments import void_leg_payment

    payment = get_object_or_404(DriverPayment, id=payment_id, driver_id=driver_id)
    lp = get_object_or_404(LegPayment, id=leg_payment_id, payment=payment)
    reason = request.POST.get("reason", "")

    try:
        adj = void_leg_payment(lp, user=request.user, reason=reason)
    except ValidationError as e:
        messages.error(request, "; ".join(e.messages) if hasattr(e, "messages") else str(e))
        return _adjust_redirect(driver_id, payment_id)

    messages.success(
        request,
        f"Voided line for leg #{lp.leg_id} (−${abs(adj.delta):.2f}). "
        f"Leg is back in the unpaid queue.",
    )
    return _adjust_redirect(driver_id, payment_id)


@login_required(login_url="login")
@require_POST
def bulk_void_leg_payments_view(request, driver_id, payment_id):
    """Void multiple LegPayment lines on this payment in one go.

    Accepts a list of `leg_payment_ids` (checkbox values) and a single
    `reason` that applies to the whole batch. All-or-nothing: if any
    line in the batch fails (already voided, etc.), NONE are voided
    and the page redirects with an error.
    """
    if not request.user.is_staff:
        return redirect("home")

    from decimal import Decimal
    from django.core.exceptions import ValidationError
    from django.db import transaction
    from drivers.payout_adjustments import void_leg_payment

    payment = get_object_or_404(DriverPayment, id=payment_id, driver_id=driver_id)
    raw_ids = request.POST.getlist("leg_payment_ids")
    reason = request.POST.get("reason", "")

    if not raw_ids:
        messages.error(request, "Select at least one line to void.")
        return _adjust_redirect(driver_id, payment_id)

    voided = []
    try:
        with transaction.atomic():
            for raw_id in raw_ids:
                try:
                    lp = LegPayment.objects.get(id=int(raw_id), payment=payment)
                except (LegPayment.DoesNotExist, ValueError, TypeError):
                    raise ValidationError(f"Line #{raw_id} not found on this payment.")
                adj = void_leg_payment(lp, user=request.user, reason=reason)
                voided.append((lp, adj))
    except ValidationError as e:
        msg = "; ".join(e.messages) if hasattr(e, "messages") else str(e)
        prefix = "No lines were voided — " if voided else ""
        messages.error(request, f"{prefix}{msg}")
        return _adjust_redirect(driver_id, payment_id)

    total_removed = sum((abs(adj.delta) for (_, adj) in voided), Decimal("0.00"))
    if len(voided) == 1:
        lp, adj = voided[0]
        messages.success(
            request,
            f"Voided line for leg #{lp.leg_id} (−${abs(adj.delta):.2f}). "
            f"Leg is back in the unpaid queue.",
        )
    else:
        leg_id_list = ", ".join(f"#{lp.leg_id}" for (lp, _) in voided)
        messages.success(
            request,
            f"Voided {len(voided)} lines ({leg_id_list}) — total −${total_removed:.2f}. "
            f"Those legs are back in the unpaid queue.",
        )
    return _adjust_redirect(driver_id, payment_id)


@login_required(login_url="login")
@require_POST
def edit_leg_payment_amount_view(request, driver_id, payment_id, leg_payment_id):
    """Edit a single LegPayment line's amount. Reason required."""
    if not request.user.is_staff:
        return redirect("home")

    from django.core.exceptions import ValidationError
    from drivers.payout_adjustments import edit_leg_payment_amount

    payment = get_object_or_404(DriverPayment, id=payment_id, driver_id=driver_id)
    lp = get_object_or_404(LegPayment, id=leg_payment_id, payment=payment)
    new_amount = request.POST.get("new_amount", "")
    reason = request.POST.get("reason", "")

    try:
        adj = edit_leg_payment_amount(
            lp, new_amount=new_amount, user=request.user, reason=reason,
        )
    except ValidationError as e:
        messages.error(request, "; ".join(e.messages) if hasattr(e, "messages") else str(e))
        return _adjust_redirect(driver_id, payment_id)

    sign = "+" if adj.delta >= 0 else "−"
    messages.success(
        request,
        f"Updated leg #{lp.leg_id} amount to ${adj.new_amount:.2f} "
        f"({sign}${abs(adj.delta):.2f}).",
    )
    return _adjust_redirect(driver_id, payment_id)


@login_required(login_url="login")
@require_POST
def add_missing_leg_view(request, driver_id, payment_id):
    """Add one or more previously-unpaid legs to this DriverPayment.

    Accepts a list of `leg_ids` (checkbox values) and a per-leg amount
    in `amount_<leg_id>`. A single `reason` covers the whole batch —
    staff are adding the legs together as one corrective action.

    All-or-nothing: if any leg in the batch fails validation, NONE are
    added. The outer transaction.atomic() rolls back the savepoints
    that each helper call opened.
    """
    if not request.user.is_staff:
        return redirect("home")

    from decimal import Decimal
    from django.core.exceptions import ValidationError
    from django.db import transaction
    from drivers.payout_adjustments import add_missing_leg_to_payment

    payment = get_object_or_404(DriverPayment, id=payment_id, driver_id=driver_id)
    # Accept both the new plural form and the legacy singular form
    # (`leg_id`) for backward compatibility with any cached page.
    leg_ids = request.POST.getlist("leg_ids")
    if not leg_ids:
        legacy = request.POST.get("leg_id", "").strip()
        if legacy:
            leg_ids = [legacy]
    reason = request.POST.get("reason", "")

    if not leg_ids:
        messages.error(request, "Select at least one leg to add.")
        return _adjust_redirect(driver_id, payment_id)

    # Fall back to the singular `amount` field when no per-leg amount
    # was submitted — staff who only pick one leg shouldn't have to
    # think about the per-row UI quirk.
    fallback_amount = request.POST.get("amount", "")

    added = []
    try:
        with transaction.atomic():
            for raw_leg_id in leg_ids:
                try:
                    leg = Leg.objects.get(id=int(raw_leg_id), driver_id=driver_id)
                except (Leg.DoesNotExist, ValueError, TypeError):
                    raise ValidationError(f"Leg #{raw_leg_id} not found for this driver.")
                amount = request.POST.get(f"amount_{leg.id}", "").strip() or fallback_amount
                adj = add_missing_leg_to_payment(
                    payment, leg=leg, amount=amount,
                    user=request.user, reason=reason,
                )
                added.append((leg, adj))
    except ValidationError as e:
        msg = "; ".join(e.messages) if hasattr(e, "messages") else str(e)
        # Make it explicit nothing was added so staff don't think it half-applied.
        prefix = "No legs were added — " if added else ""
        messages.error(request, f"{prefix}{msg}")
        return _adjust_redirect(driver_id, payment_id)

    total = sum((adj.new_amount for (_, adj) in added), Decimal("0.00"))
    if len(added) == 1:
        leg, adj = added[0]
        messages.success(
            request,
            f"Added leg #{leg.id} to statement (+${adj.new_amount:.2f}).",
        )
    else:
        leg_id_list = ", ".join(f"#{leg.id}" for (leg, _) in added)
        messages.success(
            request,
            f"Added {len(added)} legs to statement ({leg_id_list}) — total +${total:.2f}.",
        )
    return _adjust_redirect(driver_id, payment_id)


@login_required
@require_POST
def update_driver_notes_ajax(request, driver_id):
    """
    Update driver notes via AJAX.
    """
    if not request.user.is_staff:
        return JsonResponse(
            {"success": False, "error": "Permission denied"}, status=403
        )

    try:
        data = json.loads(request.body)
        notes = data.get("notes", "")

        # Get the driver
        driver = get_object_or_404(Driver, id=driver_id)

        # Update notes
        driver.notes = notes
        driver.save(update_fields=["notes"])

        return JsonResponse({
            "success": True,
            "message": "Notes updated successfully"
        })

    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required
@require_POST
def toggle_timing_exclude(request, driver_id):
    """Toggle a driver's exclude_from_timing flag."""
    if not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
    try:
        data = json.loads(request.body)
        driver = get_object_or_404(Driver, id=driver_id)
        driver.exclude_from_timing = data.get("exclude", False)
        driver.save(update_fields=["exclude_from_timing"])
        name = driver.profile.get_full_name() or driver.profile.username
        status = "excluded from" if driver.exclude_from_timing else "included in"
        return JsonResponse({"success": True, "message": f"{name} {status} route timing"})
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required
@require_POST
@login_required(login_url="login")
@require_POST
def refresh_drive_time(request):
    """Bust cache and return fresh drive time for a leg."""
    try:
        data = json.loads(request.body)
        leg_id = int(data.get("leg_id", 0))
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "error": "Invalid leg ID"}, status=400)

    driver = get_object_or_404(Driver, profile=request.user)
    leg = get_object_or_404(Leg, id=leg_id, driver=driver)

    if not leg.pickup_location or not leg.dropoff_location:
        return JsonResponse({"success": False, "error": "Missing addresses"}, status=400)

    result = google_drive_time(leg.pickup_location, leg.dropoff_location, force_refresh=True)
    if result:
        return JsonResponse({"success": True, **result})
    return JsonResponse({"success": False, "error": "Could not fetch drive time"}, status=500)


def refresh_flight_data(request):
    """
    Refresh flight data for a leg assigned to the current driver.
    """
    try:
        data = json.loads(request.body)
        raw_leg_id = data.get("leg_id")
        if raw_leg_id is None or raw_leg_id == "":
            return JsonResponse(
                {"success": False, "error": "Missing leg ID"}, status=400
            )
        try:
            leg_id = int(raw_leg_id)
        except (TypeError, ValueError):
            return JsonResponse(
                {"success": False, "error": "Invalid leg ID"}, status=400
            )

        driver = get_object_or_404(Driver, profile=request.user)
        leg = get_object_or_404(Leg, id=leg_id, driver=driver)

        if not leg.flight_information:
            return JsonResponse(
                {"success": False, "error": "Leg does not have flight information"},
                status=400,
            )

        if leg.get_trip_type() != "arrival":
            return JsonResponse(
                {"success": False, "error": "Flight tracking is only available for arrival legs"},
                status=400,
            )

        flight = leg.flight_information
        flight_ident = flight.get_flight_ident()
        if not flight_ident:
            return JsonResponse(
                {"success": False, "error": "Could not determine flight identifier"},
                status=400,
            )

        flight_date = (
            leg.pickup_date.strftime("%Y-%m-%d") if leg.pickup_date else None
        )
        trip_type = leg.get_trip_type()

        aeroapi = AeroAPIService()
        flight_data = aeroapi.get_flight_data(
            flight_ident, flight_date=flight_date, trip_type=trip_type
        )

        if flight_data.get("status") != "success":
            error_msg = flight_data.get("error", "Unknown error")
            return JsonResponse({"success": False, "error": error_msg}, status=400)

        if flight_data.get("flight_iata"):
            flight.flight_iata = flight_data.get("flight_iata")
        if flight_data.get("origin"):
            flight.origin = flight_data.get("origin")
        if flight_data.get("destination"):
            flight.destination = flight_data.get("destination")
        flight_status = (
            flight_data.get("flight_status") or flight_data.get("status", "")
        )
        if flight_status:
            flight.status = flight_status

        scheduled_arrival = flight_data.get("scheduled_arrival_local")
        estimated_arrival = flight_data.get("estimated_arrival_local")
        scheduled_gate_arrival = flight_data.get("scheduled_gate_arrival_local")
        estimated_gate_arrival = flight_data.get("estimated_gate_arrival_local")
        actual_arrival = flight_data.get("actual_runway_arrival_local")
        actual_gate_arrival = flight_data.get("actual_gate_arrival_local")

        if scheduled_arrival is not None:
            flight.scheduled_arrival_local = scheduled_arrival
        if estimated_arrival is not None:
            flight.estimated_arrival_local = estimated_arrival
        if scheduled_gate_arrival is not None:
            flight.scheduled_gate_arrival_local = scheduled_gate_arrival
        if estimated_gate_arrival is not None:
            flight.estimated_gate_arrival_local = estimated_gate_arrival
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

        return JsonResponse(
            {
                "success": True,
                "message": "Flight data refreshed successfully",
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
        )

    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


# ──────────────────────────────────────────────────────────────────────
# Driver self-serve TIME-OFF REQUESTS
# Drivers submit pending DriverDateOverride rows; founders approve/deny
# from the dispatcher portal. Only approved rows affect availability
# (filter lives in drivers.availability.resolve_effective_availability).
# ──────────────────────────────────────────────────────────────────────

def _parse_date(s):
    """YYYY-MM-DD → date or None."""
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_time(s):
    """HH:MM (24h) → time or None."""
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%H:%M").time()
    except ValueError:
        return None


@login_required(login_url="login")
def request_timeoff(request):
    """GET: render the request form. POST: create a pending DriverDateOverride."""
    driver = get_object_or_404(Driver, profile=request.user)
    today = timezone.localdate()

    if request.method == "POST":
        kind = request.POST.get("kind", "full_day")
        start_date = _parse_date(request.POST.get("start_date"))
        end_date_raw = request.POST.get("end_date", "").strip()
        end_date = _parse_date(end_date_raw) if end_date_raw else None
        reason = request.POST.get("reason", "day_off")
        notes = request.POST.get("notes", "").strip()[:200]

        if not start_date:
            messages.error(request, "Please pick a start date.")
            return redirect("driver_request_timeoff")
        if start_date < today:
            messages.error(request, "Time-off requests must start today or later.")
            return redirect("driver_request_timeoff")

        if kind == "partial_day":
            # Partial-day requests are single-day only (v1). end_date is ignored.
            start_time = _parse_time(request.POST.get("start_time"))
            end_time = _parse_time(request.POST.get("end_time"))
            if not start_time or not end_time:
                messages.error(request, "Please provide both a start and end time for a partial-day request.")
                return redirect("driver_request_timeoff")
            if end_time <= start_time:
                messages.error(request, "End time must be after start time.")
                return redirect("driver_request_timeoff")
            existing = DriverDateOverride.find_duplicate(
                driver=driver, date=start_date, end_date=None,
                exception_type="unavailable_window",
                start_time=start_time, end_time=end_time,
            )
            if existing:
                messages.info(
                    request,
                    f"You already have a request for {start_date.strftime('%b %d')} "
                    f"({existing.get_status_display().lower()}). We didn't create another one.",
                )
                return redirect("driver_my_timeoff_requests")
            override = DriverDateOverride.objects.create(
                driver=driver,
                date=start_date,
                end_date=None,
                exception_type="unavailable_window",
                start_time=start_time,
                end_time=end_time,
                reason=reason,
                notes=notes,
                status="pending",
                submitted_by_driver=True,
                created_by=request.user,
            )
        else:
            # Full-day off, single day or range
            if end_date and end_date < start_date:
                messages.error(request, "End date must be on or after start date.")
                return redirect("driver_request_timeoff")
            effective_end = end_date if (end_date and end_date != start_date) else None
            existing = DriverDateOverride.find_duplicate(
                driver=driver, date=start_date, end_date=effective_end,
                exception_type="off",
            )
            if existing:
                when = existing.date_range_display
                messages.info(
                    request,
                    f"You already have a {existing.get_status_display().lower()} "
                    f"request for {when}. We didn't create another one.",
                )
                return redirect("driver_my_timeoff_requests")
            override = DriverDateOverride.objects.create(
                driver=driver,
                date=start_date,
                end_date=effective_end,
                exception_type="off",
                reason=reason,
                notes=notes,
                status="pending",
                submitted_by_driver=True,
                created_by=request.user,
            )

        # Fire-and-forget founder SMS. Notification failure should never
        # block the request itself — the row is the source of truth.
        try:
            from drivers.timeoff_notifications import notify_founders_of_new_request
            notify_founders_of_new_request(override)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Founder notify failed for override %s", override.id)

        from drivers.context_processors import invalidate_pending_timeoff_count
        invalidate_pending_timeoff_count()

        messages.success(request, "Time-off request submitted. You'll be notified once it's reviewed.")
        return redirect("driver_my_timeoff_requests")

    # GET — render the form. Surface any active (pending or approved)
    # requests so the driver doesn't accidentally double-submit; we list
    # them at the top of the form as live context.
    active_overrides = list(
        driver.date_overrides
        .filter(
            status__in=("pending", "approved"),
        )
        .filter(
            Q(end_date__isnull=True, date__gte=today) | Q(end_date__gte=today)
        )
        .order_by("date")
    )
    return render(
        request,
        "drivers/request_timeoff.html",
        {
            "driver": driver,
            "today": today,
            "active_overrides": active_overrides,
        },
    )


@login_required(login_url="login")
def my_timeoff_requests(request):
    """List the driver's own time-off requests, newest first."""
    driver = get_object_or_404(Driver, profile=request.user)
    today = timezone.localdate()
    overrides = list(
        driver.date_overrides
        .select_related("decided_by")
        .order_by("-date", "-id")[:50]
    )
    # Counts for the summary line so the driver can see at-a-glance what's
    # still waiting versus already decided.
    pending_count = sum(1 for o in overrides if o.status == "pending")
    approved_upcoming = sum(
        1 for o in overrides
        if o.status == "approved" and (o.end_date or o.date) >= today
    )
    return render(
        request,
        "drivers/my_timeoff_requests.html",
        {
            "driver": driver,
            "today": today,
            "overrides": overrides,
            "pending_count": pending_count,
            "approved_upcoming": approved_upcoming,
        },
    )


@login_required(login_url="login")
@require_POST
def cancel_timeoff(request, override_id):
    """Driver cancels their own pending request."""
    driver = get_object_or_404(Driver, profile=request.user)
    override = get_object_or_404(DriverDateOverride, id=override_id, driver=driver)
    if override.status != "pending":
        messages.error(request, "Only pending requests can be cancelled.")
    else:
        override.status = "cancelled"
        override.save(update_fields=["status", "updated_at"])
        messages.success(request, "Request cancelled.")
    return redirect("driver_my_timeoff_requests")
