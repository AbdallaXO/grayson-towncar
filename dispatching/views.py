from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from reservations.models import Reservation, Leg
from django.shortcuts import get_object_or_404
from reservations.forms import ReservationForm, CustomerForm
from datetime import datetime
from django.core.paginator import Paginator
from django.db.models import Sum
from django.contrib import messages
from django import forms
from django.utils import timezone
from reservations.forms import LegForm
from django.shortcuts import redirect
import logging

logger = logging.getLogger(__name__)


class DateForm(forms.Form):
    date = forms.DateField(widget=forms.SelectDateWidget)


@login_required(login_url="login")
def index(request):
    """
    Dispatcher dashboard with date-based leg filtering
    """
    # Use today's date if no date is provided, ensuring it's a date object
    selected_date = request.GET.get("date")
    try:
        selected_date = (
            datetime.strptime(selected_date, "%Y-%m-%d").date()
            if selected_date
            else timezone.localdate()
        )
    except ValueError:
        selected_date = timezone.localdate()
    legs = (
        Leg.objects.filter(pickup_date=selected_date)
        .select_related(
            "reservation",
            "reservation__customer",
            "reservation__vehicle",
            "flight_information",
        )
        .order_by("pickup_time")
    )
    context = {
        "legs": legs,
        "selected_date": selected_date,
        "total_legs": legs.count(),
        "total_revenue": sum(leg.reservation.total_price for leg in legs),
    }

    return render(request, "dispatching/index.html", context)


@login_required(login_url="login")
def all_reservations(request):
    """
    List all reservations with pagination and overview statistics
    """
    # Base queryset with optimized related field selections
    reservations_query = Reservation.objects.select_related(
        "customer", "rate", "vehicle"
    ).order_by("-created_at")

    # Pagination
    paginator = Paginator(reservations_query, 10)  # 10 reservations per page
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    # Calculate overview statistics
    total_reservations = reservations_query.count()
    pending_reservations = reservations_query.filter(status="pending").count()
    confirmed_reservations = reservations_query.filter(status="confirmed").count()
    total_revenue = reservations_query.aggregate(total=Sum("total_price"))["total"] or 0

    context = {
        "reservations": page_obj,
        "page_obj": page_obj,
        "total_reservations": total_reservations,
        "pending_reservations": pending_reservations,
        "confirmed_reservations": confirmed_reservations,
        "total_revenue": total_revenue,
    }

    return render(request, "dispatching/list.html", context)


@login_required(login_url="login")
def reservation_details(request, id):
    """
    Detailed view for a reservation
    """
    # Optimize query with prefetch and select_related
    reservation = get_object_or_404(
        Reservation.objects.prefetch_related("legs__flight_information").select_related(
            "customer", "vehicle", "rate"
        ),
        uuid=id,
    )

    context = {
        "reservation": reservation,
        "total_legs": reservation.legs.count(),
        "total_cost": {
            "base": reservation.base_price,
            "additional": reservation.additional_charges,
            "total": reservation.total_price,
        },
    }

    return render(request, "dispatching/reservation_view.html", context)


@login_required(login_url="login")
def modify_reservation(request, id):
    reservation = get_object_or_404(
        Reservation.objects.prefetch_related("legs"), uuid=id
    )

    if request.method == "POST":
        customer_form = CustomerForm(request.POST, instance=reservation.customer)
        reservation_form = ReservationForm(request.POST, instance=reservation)

        if customer_form.is_valid() and reservation_form.is_valid():
            # Save customer and reservation
            customer = customer_form.save()
            updated_reservation = reservation_form.save(
                commit=False,
                trip_type=reservation.trip_type,
                base_price=reservation.base_price,
                rate=reservation.rate,
                vehicle=reservation.vehicle,
            )
            updated_reservation.customer = customer
            updated_reservation.save()
            leg_forms = []
            for i in range(1, 3):  # Support up to 2 legs
                leg_prefix = f"leg_{i}"
                leg_data = {
                    f"{leg_prefix}-pickup_date": request.POST.get(
                        f"{leg_prefix}-pickup_date"
                    ),
                    f"{leg_prefix}-pickup_time": request.POST.get(
                        f"{leg_prefix}-pickup_time"
                    ),
                    f"{leg_prefix}-pickup_location": request.POST.get(
                        f"{leg_prefix}-pickup_location"
                    ),
                    f"{leg_prefix}-dropoff_location": request.POST.get(
                        f"{leg_prefix}-dropoff_location"
                    ),
                }

                if any(leg_data.values()):
                    leg_instance = (
                        reservation.legs.all()[i - 1]
                        if reservation.legs.count() >= i
                        else None
                    )
                    leg_form = LegForm(
                        leg_data, instance=leg_instance, prefix=leg_prefix
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
        reservation_form = ReservationForm(instance=reservation)
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
    # Get the date filter from request (optional filter)
    date_filter = request.GET.get("date")

    # Get today's date for comparison
    today = timezone.localdate()

    # Base query - select related fields for optimization
    legs_query = Leg.objects.select_related(
        "reservation", "reservation__customer", "reservation__vehicle"
    )

    # Always filter legs to show only today and future legs
    legs_query = legs_query.filter(pickup_date__gte=today)

    # If a specific date filter is provided, apply it as an additional filter
    if date_filter:
        try:
            filter_date = datetime.strptime(date_filter, "%Y-%m-%d").date()
            legs_query = legs_query.filter(pickup_date=filter_date)
        except ValueError:
            # Handle invalid date format - ignore the filter
            pass

    # Order by pickup date first, then pickup time for better readability
    legs = legs_query.order_by("pickup_date", "pickup_time")

    drivers = [
        {"id": 1, "name": "Select Driver"},
        {"id": 2, "name": "Wael"},
        {"id": 3, "name": "Mostafa"},
        {"id": 4, "name": "Placeholder 1"},
        {"id": 5, "name": "Placeholder 2"},
        {"id": 6, "name": "Place Holder 3"},
    ]

    context = {"legs": legs, "filter_date": date_filter, "drivers": drivers}

    return render(request, "dispatching/legs_list.html", context)