# reservations/forms.py

from django import forms
from reservations.models import Reservation

# reservations/forms.py


class ReservationForm(forms.ModelForm):
    class Meta:
        model = Reservation
        fields = [
            "route",
            "pickup_date",
            "pickup_time",
            "passenger_count",
            "children_count",
            "carseat_needed",
            "carseat_type",
            "luggage_count",
            "special_requests",
            "email",
            "phone_number",
        ]
        widgets = {
            "pickup_date": forms.DateInput(attrs={"type": "date"}),
            "pickup_time": forms.TimeInput(attrs={"type": "time"}),
        }
