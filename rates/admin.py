from django.contrib import admin
from rates.models import Vehicle, Route, Rate, Location, Lead

# Register your models here.


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = ["vehicle_type", "capacity", "luggage_capacity"]


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    pass


@admin.register(Route)
class RouteAdmin(admin.ModelAdmin):
    pass


@admin.register(Rate)
class RateAdmin(admin.ModelAdmin):
    pass


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ['first_name', 'last_name', 'email', 'phone', 'trip_type', 'created_at']
    list_filter = ['trip_type', 'created_at']
    search_fields = ['first_name', 'last_name', 'email', 'phone']
    readonly_fields = ['created_at', 'updated_at']