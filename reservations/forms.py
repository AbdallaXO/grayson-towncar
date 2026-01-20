# reservations/forms.py

from django import forms
from .models import Reservation, Customer, Leg, Flight, Cruise, Lead
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
            "gratuity_percentage",
        ]
        widgets = {
            "passenger_count": forms.NumberInput(
                attrs={"class": "form-control", "min": 1, "max": 14, "type": "number"}
            ),
            "luggage_count": forms.NumberInput(
                attrs={"class": "form-control", "min": 0, "max": 14, "type": "number"}
            ),
            "store_stop": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "special_requests": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 4,
                    "placeholder": "Special requests, accessibility needs, surprise arrangements, or other notes…",
                }
            ),
            "need_carseats": forms.CheckboxInput(
                attrs={
                    "class": "form-check-input",
                    "id": "id_need_carseats",  # critical for JS
                }
            ),
            "rf_carseats": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "type": "number",
                }
            ),
            "ff_carseats": forms.NumberInput(
                attrs={"class": "form-control", "type": "number"}
            ),
            "booster_seats": forms.NumberInput(
                attrs={"class": "form-control", "type": "number"}
            ),
            "gratuity_percentage": forms.NumberInput(
                attrs={"class": "form-control", "type": "number", "step": "0.01", "min": "0", "max": "100"}
            ),
        }
        help_texts = {
            "special_requests": "Optional. We'll do our best to accommodate. ",
            "needs_carseat": "check this if you would like any carseats or boosters",
        }
        labels = {
            "rf_carseats": "Rear-Facing Carseats",
            "ff_carseats": "Forward-Facing Carseats",
            "booster_seats": "Boosters",
            "need_carseats": "Traveling with children?",
            "store_stop": "Need to stop at Publix on the way? (Optional)",
            "gratuity_percentage": "Gratuity Percentage",
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
        gratuity_percentage = cleaned_data.get("gratuity_percentage")
        if gratuity_percentage is not None:
            try:
                gratuity_value = float(gratuity_percentage)
            except (TypeError, ValueError):
                gratuity_value = None
            if gratuity_value is not None:
                if gratuity_value == 0 or gratuity_value == 20 or gratuity_value >= 21:
                    return cleaned_data
                self.add_error(
                    "gratuity_percentage",
                    "Online gratuity is available starting at 20%. If you prefer a different amount, gratuity may be provided directly to your chauffeur in cash.",
                )

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


class CruiseForm(forms.ModelForm):
    """Form for cruise information"""

    class Meta:
        model = Cruise
        fields = ["cruise_line", "ship_name"]
        widgets = {
            "cruise_line": forms.TextInput(
                attrs={"class": "form-control", "list": "cruise_lines", "placeholder": "e.g. Disney Cruise Line"}
            ),
            "ship_name": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "e.g. Disney Wish"}
            ),
        }
        labels = {
            "cruise_line": "Cruise Line",
            "ship_name": "Ship Name",
        }


class ReservationAdminForm(forms.ModelForm):
    """Form for reservation details"""

    class Meta:
        model = Reservation
        fields = "__all__"


class LeadForm(forms.ModelForm):
    class Meta:
        model = Lead
        fields = ["first_name", "last_name", "email", "phone", "pickup_date"]
        widgets = {
            "first_name": forms.TextInput(
                attrs={
                    "class": "form-control form-control-lg bg-white",
                    "placeholder": "First Name",
                    "required": True,
                    "minlength": "2",
                    "maxlength": "50",
                    "autocomplete": "given-name",
                }
            ),
            "last_name": forms.TextInput(
                attrs={
                    "class": "form-control form-control-lg bg-white",
                    "placeholder": "Last Name",
                    "required": True,
                    "minlength": "2",
                    "maxlength": "50",
                    "autocomplete": "family-name",
                }
            ),
            "email": forms.EmailInput(
                attrs={
                    "class": "form-control form-control-lg bg-white",
                    "placeholder": "Email Address",
                    "required": True,
                    "autocomplete": "email",
                }
            ),
            "phone": forms.TextInput(
                attrs={
                    "class": "form-control form-control-lg bg-white",
                    "placeholder": "Phone Number",
                    "required": True,
                    "minlength": "10",
                    "maxlength": "20",
                    "autocomplete": "tel",
                }
            ),
            "pickup_date": forms.DateInput(
                attrs={
                    "class": "form-control form-control-lg bg-white pickup-date",
                    "placeholder": "Date of Service",
                    "type": "date",
                    "required": False,
                }
            ),
        }
        error_messages = {
            "first_name": {
                "required": "Please enter your first name.",
                "min_length": "First name must be at least 2 characters.",
                "max_length": "First name cannot exceed 50 characters.",
            },
            "last_name": {
                "required": "Please enter your last name.",
                "min_length": "Last name must be at least 2 characters.",
                "max_length": "Last name cannot exceed 50 characters.",
            },
            "email": {
                "required": "Please enter your email address.",
                "invalid": "Please enter a valid email address.",
            },
            "phone": {
                "required": "Please enter your phone number.",
                "min_length": "Phone number must be at least 10 digits.",
                "max_length": "Phone number cannot exceed 20 digits.",
            },
            "pickup_date": {"invalid": "Please enter a valid date."},
        }
