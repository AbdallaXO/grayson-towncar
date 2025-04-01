from django.shortcuts import render, redirect, get_object_or_404
from rates.models import Vehicle, Route, Rate
from .forms import ReservationForm, CustomerForm
from . utils import *


# Create your views here.
def index(request):
    """Returns the Landing Page"""
    return render(request, "reservations/index.html")


def reservation_form(request, pk):
    """Returns a Reservation Form either oneway or roundtrip with a car type & rate & route or 404"""

    rate = get_object_or_404(Rate.objects.select_related("route", "vehicle"), pk=pk)
    trip_type, price = get_form_details(request, rate)
    reservation_form = ReservationForm()
    customer_form = CustomerForm()
    context = {
        "reservation_form": reservation_form,
        "customer_form": customer_form,
        "trip": rate,
        "price": price,
        "trip_type": trip_type,
    }
    return render(request, "reservations/book_form.html", context)


def about_us(request):
    return render(request, "reservations/about.html")


def faqs(request):
    return render(request, "reservations/faqs.html")
