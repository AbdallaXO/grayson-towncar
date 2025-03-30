from django.views import View
from django.shortcuts import render, redirect, get_object_or_404
from reservations.models import Vehicle, Rate, Route


# Create your views here.
def index(request):
    """a View that returns a table of Vehicles and their prices matching Oneway or Roundtrip,
    if you choose to book it will forward you to a view in reservations /book"""
    towncar = Vehicle.objects.get(vehicle_type='towncar')
    towncar_rate = Rate.objects.get(vehicle = towncar)
    towncar = Vehicle.objects.get(vehicle_type='suv')
    suv_rate = Rate.objects.get(vehicle = suv)


    print(towncar_rate.oneway_price, suv_rate)
    return render(request, "rates/index.html")
