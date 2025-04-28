from django.contrib import admin
from django import forms
from .models import Customer, Reservation, Leg, Flight


# Create a custom form for the Leg model
class LegAdminForm(forms.ModelForm):
    class Meta:
        model = Leg
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "pickup_time" in self.fields:
            self.fields["pickup_time"].widget.format = "%I:%M %p"
            self.fields["pickup_time"].input_formats = ["%I:%M %p", "%H:%M:%S", "%H:%M"]


class LegInline(admin.TabularInline):
    """
    Allows editing Leg objects directly on the Reservation
    admin detail page.
    """

    model = Leg
    extra = 1
    form = LegAdminForm


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    """
    Admin panel configuration for the Customer model.
    """

    list_display = (
        "first_name",
        "last_name",
        "email",
        "phone_number",
        "reservation_count",
        "is_returning",
        "created_at",
    )
    search_fields = ("first_name", "last_name", "email", "phone_number")
    list_filter = ("is_returning", "created_at")
    ordering = ("-created_at",)


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    """
    Admin panel configuration for the Reservation model.
    """

    list_display = (
        "id",
        "customer",
        "trip_type",
        "base_price",
        "additional_charges",
        "total_price",
        # "payment_status",
        "status",
        "created_at",
    )
    search_fields = ("customer__first_name", "customer__last_name", "stripe_payment_id")
    list_filter = ("trip_type", "status", "created_at")
    ordering = ("-created_at",)
    inlines = [LegInline]


@admin.register(Leg)
class LegAdmin(admin.ModelAdmin):
    """
    Admin panel configuration for the Leg model.
    Useful if you want to view/edit Legs directly rather than via Reservation.
    """

    form = LegAdminForm
    list_display = (
        "id",
        "reservation",
        "pickup_location",
        "dropoff_location",
        "pickup_date",
        "pickup_time",
    )
    search_fields = (
        "pickup_location",
        "dropoff_location",
        "reservation__customer__first_name",
        "reservation__customer__last_name",
    )
    list_filter = ("pickup_date",)
    ordering = ("-pickup_date", "-pickup_time")


@admin.register(Flight)
class FlightAdmin(admin.ModelAdmin):
    """
    Admin panel configuration for the Flight model.
    """

    list_display = ("id", "flight_type", "airline", "flight_number")
    search_fields = ("airline", "flight_number")
    list_filter = ("flight_type",)
