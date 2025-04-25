from django.shortcuts import render
from django.http import HttpResponse
from django.contrib.auth.decorators import login_required
from reservations.models import Reservation, Leg
from django.shortcuts import get_object_or_404
from django.db.models import Prefetch

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

    
