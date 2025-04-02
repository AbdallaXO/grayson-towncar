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

    leg_count = 1 if trip_type == "one_way" else 2

    if request.method == "POST":
        customer_form = CustomerForm(request.POST)
        reservation_form = ReservationForm(request.POST)
        flight_form = FlightForm(request.POST)

        # handle leg forms with prefixes to seperate them
        leg_forms = []
        for i in range(leg_count):
            prefix = f"leg-{i}"
            leg_forms.append(LegForm(request.POST, prefix=prefix))

        # validate if necessery forms are valid
        customer_valid = customer_form.is_valid()
        reservation_valid = reservation_form.is_valid()
        legs_valid = all(leg_form.is_valid() for leg_form in leg_forms)

        if customer_valid and reservation_valid and legs_valid:
            customer = customer_form.save()
            reservation = reservation_form.save(commit=False)
            reservation.customer = customer
            reservation.total_price = price
            reservation.base_price = price
            reservation.route = rate.route
            reservation.trip_type = trip_type

            # Save Legs
            for i, leg_form in enumerate(leg_forms):
                leg = leg_form.save(commit=False)
                leg.reservation = reservation


def about_us(request):
    return render(request, "reservations/about.html")


def faqs(request):
    return render(request, "reservations/faqs.html")
