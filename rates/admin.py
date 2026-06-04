from django.contrib import admin
from rates.models import Vehicle, Route, Rate, Location, LocationGroup

# Register your models here.


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = [
        "vehicle_type", "capacity", "luggage_capacity",
        "included_carseats", "included_boosters", "requires_certification",
    ]
    list_editable = ["requires_certification"]
    fieldsets = (
        (None, {
            "fields": ("vehicle_type", "image", "requires_certification"),
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
            "fields": ("extra_carseat_fee", "extra_booster_fee", "extra_stop_fee"),
        }),
        ("Display", {
            "fields": ("carseats_display",),
        }),
    )


@admin.register(LocationGroup)
class LocationGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "location_count")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}

    def location_count(self, obj):
        return obj.locations.count()
    location_count.short_description = "# Locations"


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ("name", "group", "aliases")
    list_filter = ("group",)
    search_fields = ("name", "aliases")
    autocomplete_fields = ("group",)


@admin.register(Route)
class RouteAdmin(admin.ModelAdmin):
    list_display = ("origin", "destination", "extra_stop_groups_summary")
    search_fields = ("origin__name", "destination__name", "slug")
    autocomplete_fields = ("origin", "destination")
    filter_horizontal = ("allowed_extra_stop_groups",)

    def extra_stop_groups_summary(self, obj):
        names = list(obj.allowed_extra_stop_groups.values_list("name", flat=True)[:3])
        return ", ".join(names) if names else "—"
    extra_stop_groups_summary.short_description = "Extra-stop groups"


@admin.register(Rate)
class RateAdmin(admin.ModelAdmin):
    pass
