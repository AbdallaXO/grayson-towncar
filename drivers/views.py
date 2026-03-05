from django.shortcuts import get_object_or_404, render, redirect
from .models import Driver, DriverPayment, LegPayment
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
from django.db.models import Q, Prefetch, Count
from drivers.utils import get_drive_time


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
            "reservation", "reservation__customer", "reservation__vehicle",
            "flight_information", "cruise_information"
        )
        .prefetch_related("reservation__legs")
        .filter(driver=driver, pickup_date=selected_date)
        .order_by("pickup_time")
    )

    # Add is_first_leg property and ETA using prefetched data (no extra queries)
    from datetime import datetime as _dt

    now = timezone.localtime()
    today = timezone.localdate()
    legs_list = list(legs)

    for leg in legs_list:
        first_id = min(l.id for l in leg.reservation.legs.all())
        leg.is_first_leg = leg.id == first_id

        # Compute ETA for each leg
        pickup_dt = timezone.make_aware(_dt.combine(leg.pickup_date, leg.pickup_time))
        diff = pickup_dt - now
        total_mins = int(diff.total_seconds() / 60)
        days_away = (leg.pickup_date - today).days

        if total_mins < 0:
            leg.eta = "Now"
        elif total_mins < 60:
            leg.eta = f"In {total_mins} min{'s' if total_mins != 1 else ''}"
        elif days_away == 0:
            hours = total_mins // 60
            leg.eta = f"Today in {hours} hr{'s' if hours != 1 else ''}"
        elif days_away == 1:
            leg.eta = "Tomorrow"
        else:
            leg.eta = f"In {days_away} days"

    # ── Flight delay alerts ──
    for leg in legs_list:
        if leg.flight_information and leg.get_trip_type() == 'arrival':
            delay = leg.get_flight_time_mismatch_display(30)
            if delay:
                leg.flight_delay = delay

    # ── Conflict warnings (< 30 min gap between consecutive trips) ──
    for i in range(len(legs_list) - 1):
        curr = legs_list[i]
        nxt = legs_list[i + 1]
        if curr.pickup_date == nxt.pickup_date:
            curr_dt = _dt.combine(curr.pickup_date, curr.pickup_time)
            nxt_dt = _dt.combine(nxt.pickup_date, nxt.pickup_time)
            gap_mins = int((nxt_dt - curr_dt).total_seconds() / 60)
            if 0 <= gap_mins < 30:
                curr.conflict_next = True
                curr.conflict_gap_mins = gap_mins

    # ── Route preview (drive time) ──
    if settings.GOOGLE_MAPS_API_KEY:
        for leg in legs_list:
            leg.drive_info = get_drive_time(leg.pickup_location, leg.dropoff_location)

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
            "reservation", "reservation__customer", "reservation__vehicle",
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
            "reservation", "reservation__customer", "reservation__vehicle",
            "flight_information", "cruise_information"
        )
        .prefetch_related("reservation__legs")
        .filter(
            driver=driver,
            pickup_date__gte=today,
            pickup_date__lte=next_week,
            status__in=["in-progress", "confirmed", "on-the-way", "picked-up", "on-location"],
        )
        .exclude(reservation__status="cancelled")
        .order_by("pickup_date", "pickup_time")
    )

    # Add is_first_leg property using prefetched data (no extra queries)
    for leg in legs_list:
        first_id = min(l.id for l in leg.reservation.legs.all())
        leg.is_first_leg = leg.id == first_id

    # ── Flight delay alerts ──
    from datetime import datetime as _dt
    for leg in legs_list:
        if leg.flight_information and leg.get_trip_type() == 'arrival':
            delay = leg.get_flight_time_mismatch_display(30)
            if delay:
                leg.flight_delay = delay

    # ── Conflict warnings (< 30 min gap between consecutive trips) ──
    for i in range(len(legs_list) - 1):
        curr = legs_list[i]
        nxt = legs_list[i + 1]
        if curr.pickup_date == nxt.pickup_date:
            curr_dt = _dt.combine(curr.pickup_date, curr.pickup_time)
            nxt_dt = _dt.combine(nxt.pickup_date, nxt.pickup_time)
            gap_mins = int((nxt_dt - curr_dt).total_seconds() / 60)
            if 0 <= gap_mins < 30:
                curr.conflict_next = True
                curr.conflict_gap_mins = gap_mins

    # ── Route preview (drive time) ──
    if settings.GOOGLE_MAPS_API_KEY:
        for leg in legs_list:
            leg.drive_info = get_drive_time(leg.pickup_location, leg.dropoff_location)

    next_leg = legs_list[0] if legs_list else None

    # Build a human-friendly ETA string for the next trip banner
    next_leg_eta = ""
    if next_leg:
        from datetime import datetime as _dt

        now = timezone.localtime()
        pickup_dt = timezone.make_aware(
            _dt.combine(next_leg.pickup_date, next_leg.pickup_time)
        )
        diff = pickup_dt - now
        total_mins = int(diff.total_seconds() / 60)

        days_away = (next_leg.pickup_date - today).days

        if total_mins < 0:
            next_leg_eta = "Now"
        elif total_mins < 60:
            next_leg_eta = f"In {total_mins} min{'s' if total_mins != 1 else ''}"
        elif days_away == 0:
            hours = total_mins // 60
            next_leg_eta = f"Today in {hours} hour{'s' if hours != 1 else ''}"
        elif days_away == 1:
            next_leg_eta = "Tomorrow"
        else:
            next_leg_eta = f"In {days_away} days"

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

        # Check if reservation should be auto-completed
        if new_status == "completed":
            reservation_updated = leg.reservation.check_and_update_completion_status()
            if reservation_updated:
                # Log for debugging
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
    
    # Get filter parameters
    driver_type_filter = request.GET.get("type", "")  # "inhouse" or "affiliate"
    search_query = request.GET.get("search", "")
    availability_filter = request.GET.get("availability", "")  # "available" or "busy"
    
    # Get all drivers with related profile data
    drivers = Driver.objects.select_related(
        "profile"
    ).all()
    
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
    
    # Annotate with upcoming leg counts
    drivers = drivers.annotate(
        upcoming_count=Count(
            "legs",
            filter=Q(
                legs__pickup_date__gte=today,
                legs__pickup_date__lte=next_week,
                legs__status__in=["confirmed", "in-progress", "on-the-way", "picked-up", "on-location"]
            )
        )
    )
    
    # Apply availability filter
    if availability_filter == "available":
        drivers = drivers.filter(upcoming_count=0)
    elif availability_filter == "busy":
        drivers = drivers.filter(upcoming_count__gt=0)
    
    # Order by driver type (inhouse first), then by name
    drivers = drivers.order_by("-driver_type", "profile__first_name", "profile__last_name")
    
    # Evaluate queryset once as a list to avoid repeated DB hits
    drivers_list = list(drivers)
    available_count = 0
    inhouse_count = 0

    for driver in drivers_list:
        # vehicle_display is pure Python — no DB query
        driver.vehicle_display = driver.get_vehicle_display()

        # Count stats using the already-annotated upcoming_count
        if driver.upcoming_count == 0:
            available_count += 1
        if driver.driver_type == "inhouse":
            inhouse_count += 1

    context = {
        "drivers": drivers_list,
        "driver_type_filter": driver_type_filter,
        "search_query": search_query,
        "availability_filter": availability_filter,
        "today": today,
        "next_week": next_week,
        "total_drivers": len(drivers_list),
        "available_count": available_count,
        "inhouse_count": inhouse_count,
    }
    
    return render(request, "drivers/extend.html", context)


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
    """
    if not request.user.is_staff:
        return redirect("home")

    driver = get_object_or_404(Driver, id=driver_id)
    payment = get_object_or_404(DriverPayment, id=payment_id, driver=driver)
    leg_payments = (
        LegPayment.objects.filter(payment=payment)
        .select_related("leg")
        .order_by("leg__pickup_date", "leg__pickup_time")
    )
    legs = [lp.leg for lp in leg_payments if lp.leg]
    leg_dates = [leg.pickup_date for leg in legs if leg.pickup_date]
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
                legs=legs,
                recipient_email=recipient_email,
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

    context = {
        "driver": driver,
        "payment": payment,
        "leg_payments": leg_payments,
        "pay_period_start": pay_period_start,
        "pay_period_end": pay_period_end,
    }

    return render(request, "drivers/driver_statement_detail.html", context)


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

    result = get_drive_time(leg.pickup_location, leg.dropoff_location, force_refresh=True)
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
                    "last_updated": flight.last_updated.strftime("%Y-%m-%d %I:%M %p")
                    if flight.last_updated
                    else "",
                },
            }
        )

    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)
