"""
Admin surface for the pricing engine. This is the single place a non-engineer
changes any quote number — no code change or redeploy needed.

  • Pricing configuration  — gratuity %, peak multiplier, rounding, display copy
  • Vehicle classes        — the 5 classes + display names + capacities
  • Hourly rates           — per-class $/hr and minimum hours
  • Fallback formulas       — per-class base + per-mile + floor (unlisted routes)
  • City routes            — named routes; edit per-class prices inline
  • Peak dates             — date ranges that trigger the peak multiplier
  • Route distances (cache)— precomputed/cached miles for unlisted routes
  • Instant quotes         — read-only log of quotes customers generated
"""

from django.contrib import admin

from .models import (
    CityRoute,
    CityRoutePrice,
    FallbackFormula,
    HourlyRate,
    InstantQuote,
    PeakDate,
    PricingConfig,
    RouteDistanceCache,
    VehicleClass,
)


@admin.register(PricingConfig)
class PricingConfigAdmin(admin.ModelAdmin):
    list_display = ("__str__", "gratuity_percentage", "peak_multiplier", "round_to_whole_dollars", "updated_at")

    def has_add_permission(self, request):
        # Singleton — never offer "add another".
        return not PricingConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(VehicleClass)
class VehicleClassAdmin(admin.ModelAdmin):
    list_display = ("display_name", "key", "vehicle_type", "passenger_capacity", "luggage_capacity", "sort_order", "is_active")
    list_editable = ("passenger_capacity", "luggage_capacity", "sort_order", "is_active")
    prepopulated_fields = {"key": ("display_name",)}
    search_fields = ("display_name", "key")


@admin.register(HourlyRate)
class HourlyRateAdmin(admin.ModelAdmin):
    list_display = ("vehicle_class", "hourly_rate", "minimum_hours", "peak_minimum_hours")
    list_editable = ("hourly_rate", "minimum_hours", "peak_minimum_hours")
    list_select_related = ("vehicle_class",)


@admin.register(FallbackFormula)
class FallbackFormulaAdmin(admin.ModelAdmin):
    list_display = ("vehicle_class", "base", "per_mile", "minimum")
    list_editable = ("base", "per_mile", "minimum")
    list_select_related = ("vehicle_class",)


class CityRoutePriceInline(admin.TabularInline):
    model = CityRoutePrice
    extra = 0
    autocomplete_fields = ("vehicle_class",)


@admin.register(CityRoute)
class CityRouteAdmin(admin.ModelAdmin):
    list_display = ("name", "origin_label", "approx_miles", "sort_order", "is_active")
    list_editable = ("approx_miles", "sort_order", "is_active")
    search_fields = ("name", "aliases")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [CityRoutePriceInline]


@admin.register(PeakDate)
class PeakDateAdmin(admin.ModelAdmin):
    list_display = ("label", "start_date", "end_date", "multiplier", "is_active")
    list_editable = ("start_date", "end_date", "multiplier", "is_active")
    list_filter = ("is_active",)
    ordering = ("start_date",)


@admin.register(RouteDistanceCache)
class RouteDistanceCacheAdmin(admin.ModelAdmin):
    list_display = ("origin_text", "destination_text", "miles", "source", "updated_at")
    search_fields = ("origin_text", "destination_text", "origin_key", "destination_key")
    list_filter = ("source",)


@admin.register(InstantQuote)
class InstantQuoteAdmin(admin.ModelAdmin):
    list_display = ("created_at", "service_type", "vehicle_class", "trip_label", "total", "price_source", "converted_reservation")
    list_filter = ("service_type", "price_source", "all_inclusive")
    search_fields = ("origin", "destination", "token")
    readonly_fields = [f.name for f in InstantQuote._meta.fields]
    list_select_related = ("vehicle_class", "converted_reservation")

    def has_add_permission(self, request):
        return False
