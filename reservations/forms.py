from django import forms
from .models import Reservation


class ReservationForm(forms.ModelForm):
    """
    This represents a reservation form that is displayed to the user, as a roundtrip
    Can have conditionals to only render the oneway or roundtrip.
    """

    class Meta:
        model = Reservation
        fields = [
            "first_name",
            "last_name",
            "email",
            "phone_number",
            "pickup_date",
            "pickup_time",
            "pickup_location",
            "dropoff_location",
            "passenger_count",
            "luggage_count",
            "has_children",
            "carseat_type",
            "store_stop",
            "return_date",
            "return_time",
            "return_pickup_location",
            "return_dropoff_location",
            "special_requests",
        ]
        widgets = {
            "pickup_date": forms.DateInput(
                attrs={"type": "date", "class": "form-control"}
            ),
            "pickup_time": forms.TimeInput(
                attrs={"type": "time", "class": "form-control"}
            ),
            "return_date": forms.DateInput(
                attrs={"type": "date", "class": "form-control"}
            ),
            "return_time": forms.TimeInput(
                attrs={"type": "time", "class": "form-control"}
            ),
        }

        def __init__(self, *args, **kwargs):
            """
            Adds a Class of form-control to all form Fields.
            """
            super().__init__(*args, **kwargs)
            for visible_field in self.visible.fields():
                visible_field.field.widget.attrs["class"] = "form-control"
