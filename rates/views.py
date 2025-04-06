from django.shortcuts import render
from .models import Rate, Vehicle
from django.db import connection
from django.db.models import F
from django.db.models import Prefetch


# Create your views here.
def index(request):
    vehicles = Vehicle.objects.prefetch_related(
        Prefetch(
            "rates",
            queryset=Rate.objects.select_related(
                "route", "route__origin", "route__destination"
            ),
        )
    ).all()

    context = {"vehicles": vehicles}
    return render(request, "rates/index.html", context)
