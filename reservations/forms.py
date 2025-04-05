# reservations/forms.py

from django import forms
from .models import Reservation, Customer, Leg, Flight, ContactUsForm
from .constants import CARSEAT_CHOICES, TRIP_CHOICES


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
        }
        help_texts = {
            "special_requests": "Optional. We’ll do our best to accommodate. "
        }


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
            'first_name', 'last_name', 'phone_number', 'email', 'contact_method', 'about'
        ]
        widgets = {
            'first_name': forms.TextInput(attrs={
                'class': 'form-control', 
                'id': 'firstName', 
                'placeholder': 'First Name'
            }),
            'last_name': forms.TextInput(attrs={
                'class': 'form-control', 
                'id': 'lastName', 
                'placeholder': 'Last Name'
            }),
            'email': forms.EmailInput(attrs={
                'class': 'form-control', 
                'id': 'email', 
                'placeholder': 'Your Email'
            }),
            'phone_number': forms.TextInput(attrs={
                'class': 'form-control', 
                'id': 'phone', 
                'placeholder': 'Your Phone Number'
            }),
            'contact_method': forms.RadioSelect(attrs={
                'class': 'form-check-input',
            }),
            'about': forms.Textarea(attrs={
                'class': 'form-control', 
                'id': 'tripDetails', 
                'rows': '5',
                'placeholder': 'Tell us about your dream destination, travel dates, number of travelers, and any special requirements...'
            }),
        }
        labels = {
            'first_name': 'First Name',
            'last_name': 'Last Name',
            'email': 'Email',
            'phone_number': 'Phone Number',
            'contact_method': '',
            'about': 'About Your Trip',
        }