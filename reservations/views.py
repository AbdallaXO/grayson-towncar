from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.contrib import messages
from django.http import HttpResponseBadRequest
import logging
from .models import Vehicle, Route, Reservation, Rate
from .forms import ReservationForm

logger = logging.getLogger(__name__)


# Create your views here.
def index(request):
    return render(request, "reservations/index.html")


class BookRideView(View):
    """
    This Function Renders/Handles reservations
    """

    def _get_reservation_date(self, request):
        """Helper method to extract and validate reservation data from request rather than manually doing that in GET&POST"""
        vehicle_type = request.GET.get("vehicle", "")
        route_slug = request.GET.get("route", "")
        trip_type = request.GET.get("trip", "one_way")

        if not vehicle_type or not route_slug:
            logger.warning(
                f"Missing parameters: vehicle={vehicle_type}, route={route_slug}"
            )
            return None
        try:
            vehicle = get_object_or_404(Vehicle, vehicle_type=vehicle_type)
            route = get_object_or_404(Route, slug=route_slug)
            rate = get_object_or_404(Rate, vehicle=vehicle, route=route)

            # Determine the Correct price

            base_price = (
                rate.round_trip_price
                if trip_type == "round_trip"
                else rate.oneway_price
            )

            return {
                "vehicle": vehicle,
                "route": route,
                "rate": rate,
                "trip_type": trip_type,
                "base_price": base_price,
            }
        except Exception as e:
            logger.error(f"Error getting reservation data: {str(e)}")
            return None

    def get(self, request):
        """Handles GET requests for the booking form"""
        # Pull Values from the URL .com/?vhicle=....

        data = self._get_reservation_date(request)

        if not data:
            return redirect("rates")

        form = ReservationForm()
        context = {
            "form": form,
            "vehicle": data["vehicle"],
            "route": data["route"],
            "trip_type": data["trip_type"],
            "base_price": data["base_price"],
        }
        logger.debug(
            f"Loading booking form for Vehicle {data['vehicle'].vehicle_type}",
            f"Route: {data['route'].id}, Trip: {data['trip_type']}",
        )

        return render(request, "reservations/book_form.html", context)

    def post(self, request):
        """Handle POST requests for reservation form submission"""
        data = self._get_reservation_date(request)

        if not data:
            return redirect("rates")

        # Get the Posted Data
        form = ReservationForm(request.POST)
        if form.is_valid():
            reservation = form.save(commit=False)
            reservation.vehicle = data["vehicle"]
            reservation.route = data["route"]
            reservation.trip_type = data["trip_type"]
            reservation.base_price = data["base_price"]
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
                "vehicle": data["vehicle"],
                "route": data["route"],
                "trip_type": data["trip_type"],
                "base_price": data["base_price"],
            },
        )

def about_us(request):
    return render(request, 'reservations/about.html')
    
def faqs(request):
    return render(request, "reservations/faqs.html")
