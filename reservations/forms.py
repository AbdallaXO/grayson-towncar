# reservations/forms.py

from django import forms
from .models import Reservation, Customer, Leg, Flight
from django.utils import timezone
from django.db.models import Q
from typing import override
from .validator import validate_vehicle_constraints


class CustomerForm(forms.ModelForm):
    """Form for customer information"""

    class Meta:
        model = Customer
        fields = ["first_name", "last_name", "email", "phone_number", "zipcode"]
        widgets = {
            "first_name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "First Name",
                    "autocomplete": "given-name",
                    "autofocus": True,
                }
            ),
            "last_name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Last Name",
                    "autocomplete": "family-name",
                }
            ),
            "email": forms.EmailInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "you@example.com",
                    "type": "email",
                    "autocomplete": "email",
                }
            ),
            "phone_number": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "555-555-5555",
                    "required": True,
                    "type": "tel",
                    "autocomplete": "tel",
                }
            ),
            "zipcode": forms.TextInput(
                attrs={"class": "form-control", "autocomplete": "postal-code"}
            ),
        }
        labels = {
            "first_name": "First Name",
            "last_name": "Last Name",
        }

    @override
    def save(self, commit=True):
        obj, created = Customer.objects.filter(
            Q(email=self.instance.email),
            Q(phone_number=self.instance.phone_number),
        ).get_or_create(
            email=self.instance.email,
            phone_number=self.instance.phone_number,
            first_name=self.instance.first_name,
            last_name=self.instance.last_name,
            zipcode=self.instance.zipcode,
        )
        if not created:
            obj.is_returning = True
            obj.save()

        return obj


class ReservationForm(forms.ModelForm):
    """Form for reservation details"""

    class Meta:
        model = Reservation
        fields = [
            "passenger_count",
            "luggage_count",
            "store_stop",
            "special_requests",
            "need_carseats",
            "rf_carseats",
            "ff_carseats",
            "booster_seats",
        ]
        widgets = {
            "passenger_count": forms.NumberInput(
                attrs={"class": "form-control", "min": 1, "max": 10 ,"type":"number"}
            ),
            "luggage_count": forms.NumberInput(
                attrs={"class": "form-control", "min": 0, "max": 12 ,"type":"number"}
            ),
            "store_stop": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "special_requests": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 4,
                    "placeholder": "Special car seat needs, gratuity method preferance, or other notes…",
                }
            ),
            "need_carseats": forms.CheckboxInput(
                attrs={
                    "class": "form-check-input",
                    "id": "id_need_carseats",  # critical for JS
                }
            ),
            "rf_carseats": forms.NumberInput(attrs={"class": "form-control" ,"type":"number",}),
            "ff_carseats": forms.NumberInput(attrs={"class": "form-control" ,"type":"number"}),
            "booster_seats": forms.NumberInput(attrs={"class": "form-control" ,"type":"number"}),
        }
        help_texts = {
            "special_requests": "Optional. We’ll do our best to accommodate. ",
            "needs_carseat": "check this if you would like any carseats or boosters",
        }
        labels = {
            "rf_carseats": "Rear-Facing Carseats",
            "ff_carseats": "Forward-Facing Carseats",
            "booster_seats": "Boosters",
            "need_carseats": "Traveling with children?",
            "store_stop": "Need to stop at Publix on the way? (Optional)",
        }

    # Grab and store rate when the reservation is created.
    def __init__(self, *args, **kwargs):
        self.rate = kwargs.pop("rate", None)
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()
        if self.rate:
            vehicle = self.rate.vehicle
            validate_vehicle_constraints(vehicle, cleaned_data, self.add_error)

    def save(self, commit=True, **kwargs):
        self.instance.customer = kwargs.get("customer")
        self.instance.trip_type = kwargs.get("trip_type")
        self.instance.rate = kwargs.get("rate")
        self.instance.base_price = kwargs.get("base_price")
        self.instance.vehicle = kwargs.get("vehicle")
        return super().save(commit)


class LegForm(forms.ModelForm):
    """Form for trip leg details"""

    class Meta:
        model = Leg
        fields = ["pickup_date", "pickup_time", "pickup_location", "dropoff_location"]
        labels = {
            "pickup_date": "Pickup Date",
            "pickup_time": "Pickup Time",
            "pickup_location": "Pickup Location",
            "dropoff_location": "Drop-off Location",
        }
        widgets = {
            "pickup_date": forms.DateInput(
                attrs={"type": "date", "class": "form-control", "autocomplete": "off"}
            ),
            "pickup_time": forms.TimeInput(
                attrs={"type": "time", "class": "form-control", "autocomplete": "off"}
            ),
            "pickup_location": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "e.g. MCO Airport"}
            ),
            "dropoff_location": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "e.g. Disney All Star Music",
                }
            ),
        }
        error_messages = {
            "pickup_date": {
                "required": "Please enter a pickup date.",
            },
            "pickup_time": {
                "required": "Please enter a pickup time.",
            },
            "pickup_location": {
                "required": "Please enter a pickup location.",
            },
            "dropoff_location": {
                "required": "Please enter a drop-off location.",
            },
        }

    def clean_pickup_date(self):
        date = self.cleaned_data["pickup_date"]
        if date < timezone.now().date():
            raise forms.ValidationError("Please Enter a Valid Pickup Date")
        return date


class FlightForm(forms.ModelForm):
    """Form for flight information"""

    class Meta:
        model = Flight
        fields = ["airline", "flight_number"]
        widgets = {
            "airline": forms.TextInput(
                attrs={"class": "form-control", "list": "airlines"}
            ),
            "flight_number": forms.TextInput(attrs={"class": "form-control"}),
        }

