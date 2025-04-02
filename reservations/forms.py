# reservations/forms.py

from django import forms
from .models import Reservation, Customer, Leg, Flight
from .constants import CARSEAT_CHOICES, TRIP_CHOICES


class CustomerForm(forms.ModelForm):
    """Form for customer information"""

    class Meta:
        model = Customer
        fields = ["first_name", "last_name", "email", "phone_number", "zipcode"]


class ReservationForm(forms.ModelForm):
    """Form for reservation details"""

    class Meta:
        model = Reservation
        fields = [
            "passenger_count",
            "luggage_count",
            "has_children",
            "carseat_type",
            "store_stop",
            "special_requests",
        ]


class LegForm(forms.ModelForm):
    """Form for trip leg details"""

    class Meta:
        model = Leg
        fields = ["pickup_date", "pickup_time", "pickup_location", "dropoff_location"]
        widgets = {
            "pickup_date": forms.DateInput(attrs={"type": "date"}),
            "pickup_time": forms.TimeInput(attrs={"type": "time"}),
        }


class FlightForm(forms.ModelForm):
    """Form for flight information"""

    class Meta:
        model = Flight
        fields = ["airline", "flight_number"]
