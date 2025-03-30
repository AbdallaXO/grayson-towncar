from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.contrib import messages
from django.http import HttpResponseBadRequest
import logging
from .models import Vehicle, Route, Reservation, Rate
from .forms import ReservationForm, CustomerForm
# Create your views here.
def index(request):
    return render(request, "reservations/index.html")



    
def reservation_form(request):
    vehicle = request.GET.get('vehicle')
    route = request.GET.get('route')
    trip_type = request.GET.get('trip')
    vehicle = Vehicle.objects.get(vehicle_type = vehicle)
    route = Route.objects.get(pk=route)
    
    trip = Reservation.objects.get(trip_type='round_trip')
    print(vehicle, route, trip)
    reservation_form = ReservationForm()
    customer_form = CustomerForm()
    context = {
        'reservation_form': reservation_form,
        'customer_form': customer_form,
    }
    return render(request, 'reservations/book_form.html', context)
    


def about_us(request):
    return render(request, 'reservations/about.html')
    
def faqs(request):
    return render(request, "reservations/faqs.html")
