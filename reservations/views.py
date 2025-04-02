from django.shortcuts import render, get_object_or_404
from rates.models import Vehicle, Rate
from .forms import ReservationForm, CustomerForm, LegForm, FlightForm
from .utils import *
from django.forms import inlineformset_factory
from .models import Reservation, Leg, Customer


# Create your views here.
def index(request):
    """Returns the Landing Page"""
    return render(request, "reservations/index.html")


def reservation_form(request, pk):
    """Returns a Reservation Form either oneway or roundtrip with a car type & rate & route or 404"""
    rate = get_object_or_404(Rate.objects.select_related("route", "vehicle"), pk=pk)
    trip_type, price = get_form_details(request, rate)

    if request.method == "POST":
        customer_form = CustomerForm(request.POST)
        reservation_form = ReservationForm(request.POST)
        flight_form = FlightForm(request.POST)
        leg_form = LegForm(request.POST)
        if (
            reservation_form.is_valid()
            and customer_form.is_valid()
            and leg_form.is_valid()
        ):
            customer = customer_form.save()
            reservation = reservation_form.save(commit=False)
            reservation.customer = customer
            reservation.trip_type = trip_type
            reservation.route = rate.route
            reservation.vehicle = rate.vehicle
            reservation.total_price = price
            reservation.base_price = price
            reservation.save()

            flight = flight_form.save()

            leg = leg_form.save(commit=False)
            leg.reservation = reservation
            leg.flight_information = flight
            leg.save()

        return redirect("home")

    else:
        customer_form = CustomerForm()
        reservation_form = ReservationForm(
            initial={
                "vehicle": rate.vehicle,
                "base_price": price,
                "total_price": price,
                "route": rate.route,
            }
        )
        flight_form = FlightForm()
        leg_form = LegForm()
    context = {
        "customer_form": customer_form,
        "reservation_form": reservation_form,
        "flight_form": flight_form,
        "leg_form": leg_form,
    }
    return render(request, "reservations/book_form.html", context)


def about_us(request):
    return render(request, "reservations/about.html")


def faqs(request):
    return render(request, "reservations/faqs.html")
