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
        exclude = (
            "base_price",
            "payment_status",
            "stripe_payment_id",
            "status",
            "additional_charges",
            "trip_type",
            "customer",
            "route",
            "vehicle",
            "total_price",
            "special_requests",
        )
        labels = {
            "passenger_count": "Number Of Passengers",
            "has_children": "Children",
            "carseat_type": "Carseat Type",
            "luggage_count": "Luggage Count",
            "carseat_type": "Carseat Choices",
        }  #! what if need more than 1 carseat? TODO

    # def __init__(self, *args, **kwargs):
    #     super().__init__(*args, **kwargs)
    #     if self.fields['special_requests'] is not None:
    #         self.fields["special_requests"].widget.attrs.update(
    #         {"placeholder": "Any Special Requests E.G store stop, surprise for someone"}
    #         )


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
