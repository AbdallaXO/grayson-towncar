from django.http.response import HttpResponse, HttpResponsePermanentRedirect, HttpResponseRedirect


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


def reservation_form(request, pk) -> HttpResponsePermanentRedirect | HttpResponseRedirect | HttpResponse :
    """Returns a Reservation Form either oneway or roundtrip with a car type & rate & route or 404"""
    rate = get_object_or_404(Rate.objects.select_related("route", "vehicle"), pk=pk)
    trip_type, price = get_form_details(request, rate)

    if request.method == "POST":
        customer_form = CustomerForm(request.POST)
        reservation_form = ReservationForm(request.POST)
        flight1_form = FlightForm(request.POST, prefix="flight1")
        flight2_form = (
            FlightForm(request.POST, prefix="flight2")
            if trip_type == "round_trip"
            else None
        )
        leg1_form = LegForm(request.POST, prefix="leg1")
        leg2_form = (
            LegForm(request.POST, prefix="leg2") if trip_type == "round_trip" else None
        )

        customer_valid = customer_form.is_valid()
        reservation_valid = reservation_form.is_valid()
        leg1_valid = leg1_form.is_valid()
        leg2_valid = trip_type == "one_way" or leg2_form.is_valid()
        flight1_valid = flight1_form.is_valid()
        flight2_valid = trip_type == "one_way" or flight2_form.is_valid()
        forms_valid = customer_valid and reservation_valid and leg1_valid and leg2_valid

        # we validate all forms, and validate leg2 form only  if the trip_type is not = one_way
        if forms_valid:
            customer = customer_form.save()
            reservation = reservation_form.save(commit=False)
            reservation.customer = customer
            reservation.trip_type = trip_type
            reservation.rate = rate
            reservation.base_price = price
            reservation.save()

            leg1 = leg1_form.save(commit=False)
            leg1.reservation = reservation

            if flight1_valid and any(flight1_form.cleaned_data.values()):
                flight1 = flight1_form.save()
                leg1.flight_information = flight1
            else:
                leg1.flight_information = None
            leg1.save()

            if trip_type == "round_trip":
                leg2 = leg2_form.save(commit=False)
                leg2.reservation = reservation

                if flight2_valid and any(flight2_form.cleaned_data.values()):
                    flight2 = flight2_form.save()
                    leg2.flight_information = flight2
                else:
                    leg2.flight_information = None
                leg2.save()

            return redirect("checkout_session", reservation_id=reservation.id)

    else:
        customer_form = CustomerForm()
        reservation_form = ReservationForm(
            initial={
                "vehicle": rate.vehicle,
                "base_price": price,
                "total_price": price,
                "route": rate.route,
            },
        )
        flight1_form = FlightForm(prefix="flight1")
        leg1_form = LegForm(prefix="leg1")
        # conditional forms if its a roundtrip
        flight2_form = (
            FlightForm(prefix="flight2") if trip_type == "round_trip" else None
        )

        leg2_form = LegForm(prefix="leg2") if trip_type == "round_trip" else None

    context = {
        "customer_form": customer_form,
        "reservation_form": reservation_form,
        "flight1_form": flight1_form,
        "flight2_form": flight2_form,
        "leg1_form": leg1_form,
        "leg2_form": leg2_form,
        "route": rate.route,
        "price": price,
        "trip_type": trip_type.replace("_", " "),
        "vehicle": rate.vehicle,
    }
    return render(request, "reservations/book_form.html", context)


def about_us(request):
    return render(request, "reservations/about.html")


def faqs(request):
    return render(request, "reservations/faqs.html")


def contact(request):
    return render(request, "reservations/contact_us.html")
