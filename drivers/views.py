from django.shortcuts import get_object_or_404
from django.shortcuts import render
from .models import Driver
from datetime import datetime
from reservations.models import Leg
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
import json
from django.contrib import messages
from django.db.models import Q, Prefetch


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
            "reservation", "reservation__customer", "reservation__vehicle"
        )
        .filter(driver=driver, pickup_date=selected_date)
        .order_by("pickup_time")
    )

    # Add is_first_leg property to each leg
    for leg in legs:
        # Check if this is the first leg of the reservation (by pickup time)
        first_leg = leg.reservation.legs.order_by("pickup_time").first()
        leg.is_first_leg = leg.id == first_leg.id

    return render(
        request, "drivers/index.html", {"legs": legs, "selected_date": selected_date}
    )


@login_required
def completed_trips(request):
    driver = get_object_or_404(Driver, profile=request.user)

    # Use select_related to fetch related data efficiently
    legs = (
        Leg.objects.select_related(
            "reservation", "reservation__customer", "reservation__vehicle"
        )
        .filter(driver=driver, status="completed")
        .order_by("-pickup_date", "-pickup_time")
    )  # Order by most recent first

    # Add is_first_leg property to each leg
    for leg in legs:
        first_leg = leg.reservation.legs.order_by("pickup_time").first()
        leg.is_first_leg = leg.id == first_leg.id

    return render(
        request,
        "drivers/completed_trips.html",
        {"legs": legs, "title": "Completed Trips"},
    )


@login_required
def schedule(request):
    driver = get_object_or_404(Driver, profile=request.user)
    today = timezone.localdate()
    next_week = today + timezone.timedelta(days=60)

    # Use select_related to fetch related data efficiently
    legs = (
        Leg.objects.select_related(
            "reservation", "reservation__customer", "reservation__vehicle"
        )
        .filter(
            driver=driver,
            pickup_date__gte=today,
            pickup_date__lte=next_week,
            status__in=["picked-up", "in-progress", "on-location"],
        )
        .order_by("pickup_date", "pickup_time")
    )

    # Add is_first_leg property to each leg
    for leg in legs:
        first_leg = leg.reservation.legs.order_by("pickup_time").first()
        leg.is_first_leg = leg.id == first_leg.id

    return render(
        request,
        "drivers/weekly_schedule.html",
        {"legs": legs, "today": today, "next_week": next_week},
    )


@login_required
@require_http_methods(["POST"])
def update_leg_status(request, leg_id):
    try:
        # Ensure the leg belongs to the current driver - no need to fetch related objects here
        leg = get_object_or_404(Leg, id=leg_id, driver__profile=request.user)

        # Parse the request body
        data = json.loads(request.body)
        new_status = data.get("status")

        # Validate status
        VALID_STATUSES = ["in-progress", "picked-up", "on-location", "completed"]
        if new_status not in VALID_STATUSES:
            return JsonResponse(
                {"success": False, "error": "Invalid status"}, status=400
            )

        # Update and save the leg
        leg.status = new_status
        leg.save(update_fields=["status"])

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
