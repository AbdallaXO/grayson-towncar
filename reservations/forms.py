from django import forms
from .models import Reservation, Vehicle, Location


class ReservationForm(forms.ModelForm):
    """
    Form for creating a transportation reservation
    """

    vehicle_type = forms.ModelChoiceField(
        queryset=Vehicle.objects.filter(available=True), label="Vehicle Type"
    )

    pickup_location = forms.ModelChoiceField(
        queryset=Location.objects.filter(
            category__in=[
                "mco",
                "sanford",
                "disney",
                "port",
                "universal",
                "hotel",
                "custom",
            ]
        ),
        label="Pickup Location",
    )

    dropoff_location = forms.ModelChoiceField(
        queryset=Location.objects.filter(
            category__in=[
                "mco",
                "sanford",
                "disney",
                "port",
                "universal",
                "hotel",
                "custom",
            ]
        ),
        label="Dropoff Location",
    )

    class Meta:
        model = Reservation
        fields = [
            "guest_name",
            "guest_email",
            "guest_phone",
            "pickup_date",
            "pickup_time",
            "vehicle_type",
            "pickup_location",
            "dropoff_location",
            "passenger_count",
            "children_count",
            "carseat_needed",
            "carseat_type",
            "luggage_count",
            "airline",
            "flight_number",
            "special_requests",
        ]

    def clean(self):
        """
        Custom validation
        """
        cleaned_data = super().clean()

        # Validate vehicle capacity
        vehicle = cleaned_data.get("vehicle_type")
        passenger_count = cleaned_data.get("passenger_count")

        if vehicle and passenger_count:
            if passenger_count > vehicle.capacity:
                raise forms.ValidationError(
                    f"Selected vehicle can only accommodate {vehicle.capacity} passengers."
                )

        # Validate car seat requirements
        children_count = cleaned_data.get("children_count", 0)
        carseat_needed = cleaned_data.get("carseat_needed")
        carseat_type = cleaned_data.get("carseat_type")

        if children_count > 0 and not carseat_needed:
            raise forms.ValidationError(
                "Car seat is required when traveling with children."
            )

        if carseat_needed and not carseat_type:
            raise forms.ValidationError("Please specify the type of car seat needed.")

        return cleaned_data
