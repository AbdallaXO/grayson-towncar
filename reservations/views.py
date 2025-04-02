from django.shortcuts import render, get_object_or_404
from rates.models import Vehicle, Rate
from .forms import ReservationForm, CustomerForm, LegForm, FlightForm
from .utils import *
from django.forms import inlineformset_factory
from . models import Reservation, Leg, Customer

# Create your views here.
def index(request):
    """Returns the Landing Page"""
    return render(request, "reservations/index.html")


def reservation_form(request, pk):
    """Returns a Reservation Form either oneway or roundtrip with a car type & rate & route or 404"""
    rate = get_object_or_404(Rate.objects.select_related("route", "vehicle"), pk=pk)
    trip_type, price = get_form_details(request, rate)
    leg_count = 1 if trip_type == 'one_way' else 2
    LegFormSet = inlineformset_factory(
        Reservation,
        Leg,
        extra=leg_count,
        can_delete=False,
        form=LegForm
    ) 
    if request.method == 'POST':
        customer_form = ReservationForm(request.POST)
        reservation_form = ReservationForm(request.POST)
        reservation_instance = Reservation()
        leg_formset = LegFormSet(request.POST, instance=reservation_instance)
        flight_form = FlightForm(request.POST)
        print("Reservation valid:", reservation_form.is_valid())
        print("LegFormSet valid:", leg_formset.is_valid())
        
        print("Reservation errors:", reservation_form.errors)
        print("LegFormSet errors:", leg_formset.errors)
        forms = [reservation_form, customer_form, leg_formset]
            
        if reservation_form.is_valid() and leg_formset.is_valid():  # Changed leg_form to leg_formset
            reservation = reservation_form.save(commit=False)
            reservation.base_price = price
            reservation.route = rate.route
            reservation.total_price = price
            reservation.customer = Customer.objects.first()
            reservation.vehicle = Vehicle.objects.first()
            reservation.save()
            
            leg_formset.instance = reservation  # Changed leg_form to leg_formset
            leg_formset.save()  # Changed leg_form to leg_formset
            
            messages.success(request,'Reservation Has Been Submitted.')
    else:
        customer_form = CustomerForm()
        reservation_form = ReservationForm(initial={'route': rate.route, 'total_price': price, 'base_price': price})
        flight_form = FlightForm()
        leg_formset = LegFormSet()  # Changed leg_form to leg_formset
    
    return render(request, 'reservations/book_form.html', {
        'customer_form': customer_form,
        'reservation_form': reservation_form, 
        'flight_form':flight_form,
        'leg_form': leg_formset  # Changed variable name but kept the template key as 'leg_form'
    })

def about_us(request):
    return render(request, "reservations/about.html")


def faqs(request):
    return render(request, "reservations/faqs.html")
