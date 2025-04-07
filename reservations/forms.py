# reservations/forms.py

from django import forms
from .models import Reservation, Customer, Leg, Flight, ContactUsForm
from django.utils import timezone
from .validators import validate_carseat_limits
class CustomerForm(forms.ModelForm):
    """Form for customer information"""

    class Meta:
        model = Customer
        fields = ["first_name", "last_name", "email", "phone_number", "zipcode"]
        widgets = {
            "first_name": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "First Name"}
            ),
            "last_name": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "Last Name"}
            ),
            "email": forms.EmailInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "you@example.com",
                    "type": "email",
                }
            ),
            "phone_number": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "555-555-5555"}
            ),
            "zipcode": forms.TextInput(attrs={"class": "form-control"}),
        }


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
            "need_carseats",
            "ff_carseat",
            "rf_carseat",
            "booster_seats",
        ]
        widgets = {
            "passenger_count": forms.NumberInput(
                attrs={"class": "form-control", "min": 1, "max": 10}
            ),
            "luggage_count": forms.NumberInput(
                attrs={"class": "form-control", "min": 0, "max": "12"}
            ),
            "has_children": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "carseat_type": forms.Select(attrs={"class": "form-select"}),
            "store_stop": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "special_requests": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 4,
                    "placeholder": "Any special requests?",
                }
            ),
            "need_carseats": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "rf_carseat": forms.NumberInput(
                attrs={"class": "form-control" ,'id':'rf-carseat'}
            ),
            "ff_carseat": forms.NumberInput(
                attrs={"class": "form-control",'id':'ff-carseat'}
            ),
            "booster_seats": forms.NumberInput(
                attrs={"class": "form-control", 'id':'booster'}
            ),


        }
        help_texts = {
            "special_requests": "Optional. We’ll do our best to accommodate. "
        }

    def __init__(self, *args, **kwargs):
        self.vehicle = kwargs.pop("vehicle", None)
        super().__init__(*args, **kwargs)
        if self.vehicle:
            limits = {
                "towncar": {"carseats": 1, "boosters": 1},
                "suv": {"carseats": 2, "boosters": 2},
                "van": {"carseats": 2, "boosters": 2},
            }
            name = self.vehicle
            limit = limits.get(name, {"carseats": 0, "boosters": 0})

            self.fields["rf_carseat"].widget.attrs["max"] = limit["carseats"]
            self.fields["ff_carseat"].widget.attrs["max"] = limit["carseats"]
            self.fields["booster_seats"].widget.attrs["max"] = limit["boosters"]

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("need_carseats") and self.vehicle:
            validate_carseat_limits(
                vehicle=self.vehicle,
                rear=cleaned.get("rear_facing_seats", 0),
                forward=cleaned.get("forward_facing_seats", 0),
                booster=cleaned.get("booster_seats", 0),
            )
        return cleaned
            
                


class LegForm(forms.ModelForm):
    """Form for trip leg details"""

    class Meta:
        model = Leg
        fields = ["pickup_date", "pickup_time", "pickup_location", "dropoff_location"]
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
                    "placeholder": "e.g Disney All Star Music",
                }
            ),
        }

    def clean_pickup_date(self):
        date = self.cleaned_data["pickup_date"]
        if date < timezone.now().date():
            raise forms.ValidationError("Pick Up Date Cannot Be In The Past")
        return date


class FlightForm(forms.ModelForm):
    """Form for flight information"""

    class Meta:
        model = Flight
        fields = ["airline", "flight_number"]
        widgets = {
            "airline": forms.TextInput(attrs={"class": "form-control"}),
            "flight_number": forms.TextInput(attrs={"class": "form-control"}),
        }


class ContactUsFormSubmission(forms.ModelForm):
    class Meta:
        model = ContactUsForm
        fields = [
            "first_name",
            "last_name",
            "phone_number",
            "email",
            "contact_method",
            "about",
        ]
        widgets = {
            "first_name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "id": "firstName",
                    "placeholder": "First Name",
                }
            ),
            "last_name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "id": "lastName",
                    "placeholder": "Last Name",
                }
            ),
            "email": forms.EmailInput(
                attrs={
                    "class": "form-control",
                    "id": "email",
                    "placeholder": "Your Email",
                }
            ),
            "phone_number": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "id": "phone",
                    "placeholder": "Your Phone Number",
                }
            ),
            "contact_method": forms.RadioSelect(
                attrs={
                    "class": "form-check-input",
                }
            ),
            "about": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "id": "tripDetails",
                    "rows": "5",
                    "placeholder": "Tell us about your dream destination, travel dates, number of travelers, and any special requirements...",
                }
            ),
        }
        labels = {
            "first_name": "First Name",
            "last_name": "Last Name",
            "email": "Email",
            "phone_number": "Phone Number",
            "contact_method": "",
            "about": "About Your Trip",
        }
