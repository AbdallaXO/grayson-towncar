from django.contrib import messages
from django.shortcuts import redirect


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
