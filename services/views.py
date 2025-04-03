from django.shortcuts import render
from reservations.models import *


# Create your views here.d
def index(request):
    return render(request, "services/index.html")

