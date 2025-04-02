from django import forms
from .models import Reservation, Customer, Leg, Flight
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Submit


class CustomerForm(forms.ModelForm):
    """A Form that contians all the customers informations such as
    first_name last_name etc..."""

    class Meta:
        model = Customer
        fields = "__all__"
        exclude = ["is_returning", "reservation_count"]
        labels = {"first_name": "First Name", "last_name": "Last Name"}


class ReservationForm(forms.ModelForm):
    """A Form that contains everything related to a reservation such as rates/routes/trip_types"""

    class Meta:
        model = Reservation
        fields = ['passenger_count', 'has_children', 'luggage_count', 'carseat_type', 'store_stop', 'special_requests']
        labels = {
            "passenger_count": "Number Of Passengers",
            "has_children": "Children",
            "carseat_type": "Carseat Type",
            "luggage_count": "Luggage Count",
            "carseat_type": "Carseat Choices",
        }  #! what if need more than 1 carseat? TODO
    def clean_passenger_count(self):
        data = self.cleaned_data['passenger_count']

        vehicle_capacity = self.initial.get('vehicle').capacity
        if data > vehicle_capacity:
            raise forms.ValidationError(f"Maximum Capacity for this Vehicle is {vehicle_capacity}")
        return data

class FlightForm(forms.ModelForm):
    class Meta:
        model = Flight
        fields = "__all__"
        exclude = ["date", "time"]


class LegForm(forms.ModelForm):
    class Meta:
        model = Leg
        fields = "__all__"
        exclude = ["reservation", "flight_information"]
        widgets = {
            "pickup_date": forms.DateInput(
                attrs={"class": "form-control", "type": "date"}
            ),
            "pickup_time": forms.TimeInput(
                attrs={"class": "form-control", "type": "time"}
            ),
        }
