from django.contrib import admin

from .models import SamsaraVehicleSnapshot, SamsaraMaintenanceIssue


class _ReadOnlyModelAdmin(admin.ModelAdmin):
    """Sync products — humans should never edit these directly."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SamsaraVehicleSnapshot)
class SamsaraVehicleSnapshotAdmin(_ReadOnlyModelAdmin):
    list_display = [
        "fleet_vehicle",
        "engine_state",
        "speed_mph",
        "fuel_percent",
        "formatted_address",
        "location_recorded_at",
        "fetched_at",
    ]
    search_fields = ["fleet_vehicle__vehicle_number", "formatted_address"]
    list_filter = ["engine_state"]


@admin.register(SamsaraMaintenanceIssue)
class SamsaraMaintenanceIssueAdmin(_ReadOnlyModelAdmin):
    list_display = [
        "fleet_vehicle",
        "severity",
        "summary",
        "code",
        "opened_at",
        "resolved_at",
    ]
    list_filter = ["severity", "resolved_at"]
    search_fields = ["fleet_vehicle__vehicle_number", "summary", "code"]
