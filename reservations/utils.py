from django.contrib import messages
from django.shortcuts import redirect
from .forms import (
    ReservationForm,
    CustomerForm,
    LegForm,
    FlightForm,
    ContactUsFormSubmission,
)


def get_form_details(request, rate):
    """returns a trip type and returns a price based on the trip_type, if trip_type not valid
    redirects to the rates page"""
    trip_type = request.GET.get("round")
    if trip_type == "ow":
        price = rate.oneway_price
        trip_type = "one_way"
    elif trip_type == "rt":
        price = rate.round_trip_price
        trip_type = "round_trip"
    else:
        #! FIX ERROR MESSAGE.
        messages.error(request, f"{trip_type} Is not a Valid URL")
        return redirect("rates")
    return trip_type, price


def _initalize_form(trip_type, rate, price):
    """Initializes the forms for the GET request and returns forms for customer, reservation,
    flight1,leg1, and if trip_type is round_trip, it returns a flight2 form and a leg2 form"""
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
    flight2_form = FlightForm(prefix="flight2") if trip_type == "round_trip" else None
    leg2_form = LegForm(prefix="leg2") if trip_type == "round_trip" else None

    return (
        customer_form,
        reservation_form,
        flight1_form,
        leg1_form,
        flight2_form,
        leg2_form,
    )


def returns_post_form(request):
    """Returns Forms with Posted Data, just to Avoid Redundancy of repeating everything in the view
    returns customer,reservatiom,flight1,leg1, flight 2 and leg 2 if trip_type == 2, else oneway"""
    customer_form = CustomerForm(request.POST)
    reservation_form = ReservationForm(request.POST)
    flight1_form = FlightForm(request.POST, prefix="flight1")
    leg1_form = LegForm(request.POST, prefix="leg1")
    flight2_form = (
        FlightForm(request.POST, prefix="flight2")
        if trip_type == "round_trip"
        else None
    )
    leg2_form = (
        LegForm(request.POST, prefix="leg2") if trip_type == "round_trip" else None
    )
    return (
        customer_form,
        reservation_form,
        flight1_form,
        leg1_form,
        flight2_form,
        leg2_form,
    )
