from django.contrib import admin
from .models import Customer, Reservation, Leg, Flight


class LegInline(admin.TabularInline):
    """
    Allows editing Leg objects directly on the Reservation
    admin detail page.
    """

    model = Leg
    extra = 1  # Number of empty inline forms to display by default
    # If you want to allow editing Flight within the Leg inline,
    # consider adding a FlightInline or a custom approach. With
    # OneToOneField, an inline approach can be more complex.


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

    # If you want to ensure 'reservation_count' doesn't keep incrementing
    # on every save, consider customizing the save logic in models.py.


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    """
    Admin panel configuration for the Reservation model.
    """

    list_display = (
        "id",
        "customer",
        "trip_type",
        "vehicle",
        "base_price",
        "additional_charges",
        "total_price",
        "payment_status",
        "status",
        "created_at",
    )
    search_fields = ("customer__first_name", "customer__last_name", "stripe_payment_id")
    list_filter = ("trip_type", "status", "created_at")
    ordering = ("-created_at",)

    inlines = [LegInline]

    # If you'd like to show a custom label for the customer or other fields,
    # you can define a method here, e.g.:
    #
    # def customer_name(self, obj):
    #     return f"{obj.customer.first_name} {obj.customer.last_name}"
    # customer_name.short_description = 'Customer Name'


@admin.register(Leg)
class LegAdmin(admin.ModelAdmin):
    """
    Admin panel configuration for the Leg model.
    Useful if you want to view/edit Legs directly rather than via Reservation.
    """

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
