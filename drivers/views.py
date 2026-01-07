from django.shortcuts import get_object_or_404, render, redirect
from .models import Driver
from datetime import datetime, timedelta
from reservations.models import Leg
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods, require_POST
import json
from django.contrib import messages
from django.db.models import Q, Prefetch, Count


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
    # This prevents N+1 query problems
    legs = (
        Leg.objects.select_related(
            "reservation", "reservation__customer", "reservation__vehicle",
            "flight_information", "cruise_information"
        )
        .filter(driver=driver, pickup_date=selected_date)
        .order_by("pickup_time")
    )

    # Add is_first_leg property to each leg
    for leg in legs:
        # Check if this is the first leg of the reservation (by ID - first created)
        first_leg = leg.reservation.legs.order_by('id').first()
        leg.is_first_leg = leg.id == first_leg.id

    return render(
        request, "drivers/index.html", {"legs": legs, "selected_date": selected_date}
    )


@login_required(login_url="login")
def completed_trips(request):
    driver = get_object_or_404(Driver, profile=request.user)

    # Use select_related to fetch related data efficiently
    legs = (
        Leg.objects.select_related(
            "reservation", "reservation__customer", "reservation__vehicle",
            "flight_information", "cruise_information"
        )
        .filter(driver=driver, status="completed")
        .order_by("-pickup_date", "-pickup_time")
    )  # Order by most recent first

    # Add is_first_leg property to each leg
    for leg in legs:
        first_leg = leg.reservation.legs.order_by('id').first()
        leg.is_first_leg = leg.id == first_leg.id

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
    legs = (
        Leg.objects.select_related(
            "reservation", "reservation__customer", "reservation__vehicle",
            "flight_information", "cruise_information"
        )
        .filter(
            driver=driver,
            pickup_date__gte=today,
            pickup_date__lte=next_week,
            status__in=["in-progress", "confirmed", "on-the-way", "picked-up", "on-location"],
        )
        .order_by("pickup_date", "pickup_time")
    )

    # Add is_first_leg property to each leg
    for leg in legs:
        first_leg = leg.reservation.legs.order_by('id').first()
        leg.is_first_leg = leg.id == first_leg.id

    return render(
        request,
        "drivers/weekly_schedule.html",
        {"legs": legs, "today": today, "next_week": next_week},
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


@login_required(login_url="login")
def extend(request):
    """
    Extended driver list view for dispatchers
    Shows all drivers with phone numbers, schedules, vehicles, and availability
    """
    if not request.user.is_superuser:
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
    
    # Get upcoming legs for each driver for display
    available_count = 0
    inhouse_count = 0
    
    for driver in drivers:
        driver.upcoming_legs = driver.get_upcoming_legs(days=7)
        driver.is_available_today = driver.is_available_today()
        driver.vehicle_display = driver.get_vehicle_display()
        
        # Count stats
        if driver.upcoming_count == 0:
            available_count += 1
        if driver.driver_type == "inhouse":
            inhouse_count += 1
    
    context = {
        "drivers": drivers,
        "driver_type_filter": driver_type_filter,
        "search_query": search_query,
        "availability_filter": availability_filter,
        "today": today,
        "next_week": next_week,
        "total_drivers": drivers.count(),
        "available_count": available_count,
        "inhouse_count": inhouse_count,
    }
    
    return render(request, "drivers/extend.html", context)


@login_required
@require_POST
def update_driver_notes_ajax(request, driver_id):
    """
    Update driver notes via AJAX.
    """
    if not request.user.is_superuser:
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
