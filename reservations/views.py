from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.contrib import messages
from django.http import HttpResponseBadRequest
import logging
from .models import Vehicle, Route, Reservation, Rate



# Create your views here.
def index(request):
    return render(request, "reservations/index.html")


def about_us(request):
    return render(request, 'reservations/about.html')
    
def faqs(request):
    return render(request, "reservations/faqs.html")
