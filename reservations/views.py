from django.shortcuts import render
from django.views import View
from django.shortcuts import render, redirect, get_object_or_404
from .models import Vehicle, Route, Reservation
from .forms import ReservationForm



# Create your views here.
def index(request):
    return render(request, "reservations/index.html")
def BookRideView(View):
    """
    This Function Renders/Handles reservations
    """
    def get(self, request):
        vehicle_type = request.GET.get('vehicle')
        route_id = request.GET.get('route')
        trip_type = request.GET.get('trip', 'one_way')
        print(vehicle_type, route_id, trip_type)

        return render(request, 'reservations/book_form.html')

def faqs(request):
    return render(request, "reservations/faqs.html")
