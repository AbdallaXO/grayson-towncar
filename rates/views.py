from django.views import View
from django.shortcuts import render, redirect, get_object_or_404
from .models import Rate, Vehicle


# Create your views here.
def index(request):
    """a View that returns a table of Vehicles and their prices matching Oneway or Roundtrip,
    if you choose to book it will forward you to a view in reservations /book""" 
    rates = Rate.objects.select_related('vehicle', 'route').distinct().all()
    vehicles = Vehicle.objects.all()
    print(vehicles[0].rates)
    context = {
        "rates": rates,
        "vehicles": vehicles
    }
    return render(request, "rates/index.html", context)
