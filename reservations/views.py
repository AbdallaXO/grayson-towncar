from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.contrib import messages
from django.http import HttpResponseBadRequest
import logging
from .models import Reservation
from rates.models import Vehicle, Route , Rate
from .forms import ReservationForm, CustomerForm
from django.contrib import messages


# Create your views here.
def index(request):
    return render(request, "reservations/index.html")


def reservation_form(request, pk):
    rate = get_object_or_404(Rate.objects.select_related('route', 'vehicle'),pk=pk)
    trip_type = request.GET.get('round')
    print(str(trip_type))
    if trip_type == 'ow':
        price = rate.oneway_price
        trip_type = 'One Way'
    elif trip_type == 'rt':
        price = rate.round_trip_price
        trip_type = 'Round Trip'
    else:
        messages.error(request , f"{trip_type} Is not a Valid URL")
        #! FIX ERROR MESSAGE.
        return redirect('rates')
    
   
        
    reservation_form = ReservationForm()
    customer_form = CustomerForm()
    context = {
        "reservation_form": reservation_form,
        "customer_form": customer_form,
        "trip":rate,
        "price":price,
        "trip_type":trip_type,
    }
    return render(request, "reservations/book_form.html", context)


def about_us(request):
    return render(request, "reservations/about.html")


def faqs(request):
    return render(request, "reservations/faqs.html")
