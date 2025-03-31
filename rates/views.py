from django.views import View
from django.shortcuts import render, redirect, get_object_or_404
from reservations.models import Vehicle, Rate, Route, Reservation


# Create your views here.
def index(request):
    """a View that returns a table of Vehicles and their prices matching Oneway or Roundtrip,
    if you choose to book it will forward you to a view in reservations /book"""  
    return render(request, "rates/index.html")
