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
        flight1_form = FlightForm(request.POST, prefix='flight1')
        flight2_form = FlightForm(request.POST, prefix='flight2') if trip_type == 'round_trip' else None
        leg1_form = LegForm(request.POST, prefix="leg1")
        leg2_form = (
            LegForm(request.POST, prefix="leg2") if trip_type == "round_trip" else None
        )
        # we validate all forms, and validate leg2 form only  if the trip_type is not = one_way
        forms_valid = (
            reservation_form.is_valid()
            and customer_form.is_valid()
            and leg1_form.is_valid()
            and (trip_type == "one_way" or leg2_form.is_valid())
        )
        if forms_valid:
            customer = customer_form.save()
            reservation = reservation_form.save(commit=False)
            reservation.customer = customer
            reservation.trip_type = trip_type
            reservation.route = rate.route
            reservation.vehicle = rate.vehicle
            reservation.total_price = price
            reservation.base_price = price
            reservation.save()

            flight1 = flight1_form.save()

            leg1 = leg1_form.save(commit=False)
            leg1.reservation = reservation
            leg1.flight_information = flight1
            leg1.save()
            if trip_type == "round_trip":
                flight2 = flight2_form.save()
                leg2 = leg2_form.save(commit=False)
                leg2.reservation = reservation
                leg2.flight_information = flight2
                leg2.save()

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
        leg1_form = LegForm(prefix="leg1")
        leg2_form = LegForm(prefix="leg2") if trip_type == "round_trip" else None
    context = {
        "customer_form": customer_form,
        "reservation_form": reservation_form,
        "flight_form": flight_form,
        "leg1_form": leg1_form,
        "leg2_form": leg2_form,
        "route": rate.route,
        "price": price,
        "trip_type": trip_type.replace('_', ' '),
    }
    return render(request, "reservations/book_form.html", context)


def about_us(request):
    return render(request, "reservations/about.html")


def faqs(request):
    return render(request, "reservations/faqs.html")
