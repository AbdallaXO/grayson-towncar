from django.contrib import admin
from rates.models import Vehicle, Route, Rate, Location

# Register your models here.


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = [
        "vehicle_type", "capacity", "luggage_capacity",
        "included_carseats", "included_boosters",
    ]
    fieldsets = (
        (None, {
            "fields": ("vehicle_type", "image"),
        }),
        ("Passenger & Luggage", {
            "fields": ("capacity", "luggage_capacity"),
        }),
        ("Car Seat Limits (physical max per type)", {
            "fields": ("ff_carseats_max", "rf_carseats_max", "boosters_max", "carseats_capacity"),
            "description": "How many of each seat type can physically fit in this vehicle.",
        }),
        ("Included Seats (before extra fees)", {
            "fields": ("included_carseats", "included_boosters"),
            "description": (
                "How many seats are included in the base price. "
                "Customers can add more for an extra fee."
            ),
        }),
        ("Extra Seat Fees", {
            "fields": ("extra_carseat_fee", "extra_booster_fee"),
        }),
        ("Display", {
            "fields": ("carseats_display",),
        }),
    )


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    pass


@admin.register(Route)
class RouteAdmin(admin.ModelAdmin):
    pass


@admin.register(Rate)
class RateAdmin(admin.ModelAdmin):
    pass
