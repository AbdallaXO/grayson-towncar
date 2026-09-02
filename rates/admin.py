from django.contrib import admin
from rates.models import Vehicle, Route, Rate, Location, LocationGroup, Zone, ZoneRate

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


@admin.register(Zone)
class ZoneAdmin(admin.ModelAdmin):
    """Pay zones. Add one here, put places in it on the Locations page, then
    price it against the other zones under Zone rates."""

    list_display = ("name", "sort_order", "location_count", "description")
    list_editable = ("sort_order",)
    search_fields = ("name", "description")

    def location_count(self, obj):
        return obj.locations.count()
    location_count.short_description = "# Places"


@admin.register(ZoneRate)
class ZoneRateAdmin(admin.ModelAdmin):
    """What a driver is paid between two zones. These set the price for every
    trip that has no Route row of its own — which is most of them. Change a
    number here and every future trip between those zones follows it.

    A pair with no row here is not priced at all: those trips show up on the
    driver pay page as needing a price, rather than being guessed at."""

    list_display = ("__str__", "zone_a", "zone_b", "inhouse_base_pay")
    list_editable = ("inhouse_base_pay",)
    list_filter = ("zone_a", "zone_b")
    autocomplete_fields = ("zone_a", "zone_b")


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ("name", "pay_zone", "group", "aliases")
    list_filter = ("pay_zone", "group")
    autocomplete_fields = ("group", "pay_zone")
    list_editable = ("pay_zone",)
    search_fields = ("name", "aliases")


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
