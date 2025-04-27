from django.shortcuts import render
from django.http import HttpResponse
from django.contrib.auth.decorators import login_required
from reservations.models import Reservation, Leg
from django.shortcuts import get_object_or_404
from django.db.models import Prefetch
from reservations.forms import ReservationForm, CustomerForm

# Create your views here.
@login_required(login_url='login')
def index(request):
    legs = Leg.objects.select_related('reservation', 'flight_information', 'reservation__customer', 'reservation__vehicle')
    context = {'legs':legs}
    return render(request, 'dispatching/index.html', context)


@login_required(login_url='login')
def reservation_details(request, id):
    reservation = get_object_or_404(Reservation.objects.select_related('customer', 'vehicle'), uuid = id)
    context = {'reservation':reservation}
    return render(request, 'dispatching/reservation_view.html', context)

    
def modify_reservation(request, id):
    reservation = get_object_or_404(Reservation.objects.select_related('customer', 'vehicle'), uuid = id)
    customer_form = CustomerForm(instance = reservation.customer)
    reservation_form = ReservationForm(instance = reservation)

    context = {'reservation_form':reservation_form, 'customer_form':customer_form}
    return render(request, 'dispatching/modify_reservation.html', context)
