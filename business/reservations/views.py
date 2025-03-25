from django.shortcuts import render
from .forms import ReservationForm, CustomerForm

# Create your views here.
def index(request):
    return render(request, 'reservations/index.html')

def make_reservation(request):
    reservation_form = ReservationForm()
    customer_form = CustomerForm()
    context = {'customer':customer_form, 'reservation':ReservationForm}
    if request.method =='POST':
        customer_form = CustomerForm(request.POST)
        reservation_form = ReservationForm(request.POST)

        if customer_form.is_valid() and reservation_form.is_valid():
            reservation = reservation_form.save(commit=False)
            customer = customer_form.save()
            reservation.customer = customer
            reservation.save()


    return render(request, 'reservations/reservation.html', context)