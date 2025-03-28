from django.views import View
from django.shortcuts import render, redirect, get_object_or_404
from reservations.models import Vehicle, Route, Rate, Reservation, Customer
from .forms import ReservationForm

# Create your views here.
def index(request):
    return render(request, "rates/index.html")

class BookRideView(View):
    def get(self, request):
        vehicle_type = request.GET.get('vehicle')
        route_id = request.GET.get('route')
        trip_type = request.GET.get('trip', 'one_way')  # default to 'one_way'

        route = get_object_or_404(Route, id=route_id)
        vehicle = get_object_or_404(Vehicle, vehicle_type=vehicle_type)
        rate = get_object_or_404(Rate, vehicle__vehicle_type=vehicle_type, route=route)

        base_price = rate.oneway_price if trip_type == 'one_way' else 0  # No round_trip_price yet

        form = ReservationForm()

        return render(request, 'rates/book_form.html', {
            'form': form,
            'vehicle': vehicle,
            'route': route,
            'trip_type': trip_type,
            'base_price': base_price,
        })

    def post(self, request):
        vehicle_type = request.GET.get('vehicle')
        route_id = request.GET.get('route')
        trip_type = request.GET.get('trip', 'one_way')

        route = get_object_or_404(Route, id=route_id)
        vehicle = get_object_or_404(Vehicle, vehicle_type=vehicle_type)
        rate = get_object_or_404(Rate, vehicle__vehicle_type=vehicle_type, route=route)

        base_price = rate.oneway_price if trip_type == 'one_way' else 0  # Again, no round_trip_price

        form = ReservationForm(request.POST)
        if form.is_valid():
            reservation = form.save(commit=False)
            reservation.customer = Customer.objects.first()  # Replace with logged-in logic
            reservation.route = route
            reservation.vehicle_type = vehicle
            reservation.base_price = base_price
            reservation.additional_charges = 0
            reservation.status = "PENDING"
            reservation.save()

            return redirect("home")

        return render(request, 'rates/book_form.html', {
            'form': form,
            'vehicle': vehicle,
            'route': route,
            'trip_type': trip_type,
            'base_price': base_price,
        })
