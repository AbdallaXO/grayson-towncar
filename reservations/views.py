from django.shortcuts import render
from django.views import View
from django.shortcuts import render, redirect, get_object_or_404
from .models import Vehicle, Route, Reservation, Rate
from .forms import ReservationForm
import stripe
from django.conf import settings



# Create your views here.
def index(request):
    return render(request, "reservations/index.html")


class BookRideView(View):
    """
    This Function Renders/Handles reservations
    """

    def get(self, request):
        # Pull Values from the URL .com/?vhicle=....
        vehicle_type = request.GET.get("vehicle".lower())
        route_id = request.GET.get("route")
        trip_type = request.GET.get("trip", "one_way")

        # Look up the database objects
        vehicle = get_object_or_404(Vehicle, vehicle_type=vehicle_type)
        route = get_object_or_404(Route, id=route_id)
        rate = get_object_or_404(Rate, vehicle=vehicle, route=route)

        if trip_type == "round_trip":
            base_price = rate.round_trip_price
        else:
            base_price = rate.oneway_price
        if not vehicle_type or not route_id:
            return redirect("rates")

        form = ReservationForm()
        context = {
            "form": form,
            "vehicle": vehicle,
            "route": route,
            "trip_type": trip_type,
            "base_price": base_price,
        }
        print(vehicle_type, route_id, trip_type, rate)

        return render(request, "reservations/book_form.html", context)

    def post(self, request):
        vehicle_type = request.GET.get("vehicle".lower())
        route_id = request.GET.get("route")
        trip_type = request.GET.get("trip", "one_way")

        # Validate Vehicle, Route, rate again
        vehicle = get_object_or_404(Vehicle, vehicle_type=vehicle_type)
        route = get_object_or_404(Route, id=route_id)
        rate = get_object_or_404(Rate, vehicle=vehicle, route=route)

        # Determine Price After Extra Validation
        base_price = (
            rate.round_trip_price if trip_type == "round_trip" else rate.oneway_price
        )

        # Get the Posted Data
        form = ReservationForm(request.POST)
        if form.is_valid():
            reservation = form.save(commit=False)
            reservation.vehicle = vehicle
            reservation.route = route
            reservation.trip_type = trip_type
            reservation.base_price = base_price
            reservation.additional_charges = 0
            reservation.status = "PENDING"
            reservation.payment_status = "PENDING"
            reservation.save()
            # redirect user after submitting form #TODO Link to Stripe.
            return redirect("home")
        # redisplay form if not valid
        return render(
            request,
            "reservations/book_form.html",
            {
                "form": form,
                "vehicle": vehicle,
                "route": route,
                "trip_type": trip_type,
                "base_price": base_price,
            },
        )


def faqs(request):
    return render(request, "reservations/faqs.html")
