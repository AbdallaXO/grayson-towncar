from django.db import models


class SamsaraVehicleSnapshot(models.Model):
    """Latest known telemetry for a FleetVehicle. One row per vehicle, upserted on every poll."""

    fleet_vehicle = models.OneToOneField(
        "drivers.FleetVehicle",
        on_delete=models.CASCADE,
        related_name="samsara_snapshot",
    )
    latitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    longitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    speed_mph = models.FloatField(null=True, blank=True)
    heading_degrees = models.FloatField(null=True, blank=True)
    fuel_percent = models.IntegerField(null=True, blank=True)
    odometer_miles = models.IntegerField(null=True, blank=True)
    engine_state = models.CharField(max_length=20, blank=True)
    formatted_address = models.CharField(max_length=255, blank=True)
    location_recorded_at = models.DateTimeField(null=True, blank=True)
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["fetched_at"])]

    def __str__(self):
        return f"snapshot of {self.fleet_vehicle} @ {self.fetched_at:%Y-%m-%d %H:%M}"


class SamsaraMaintenanceIssue(models.Model):
    """Active maintenance issue for a FleetVehicle. Mirrors Samsara's issue list."""

    SEVERITY_CHOICES = [
        ("critical", "Critical"),
        ("warning", "Warning"),
        ("info", "Info"),
    ]

    fleet_vehicle = models.ForeignKey(
        "drivers.FleetVehicle",
        on_delete=models.CASCADE,
        related_name="maintenance_issues",
    )
    samsara_issue_id = models.CharField(max_length=64, unique=True)
    severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES)
    summary = models.CharField(max_length=255)
    code = models.CharField(max_length=64, blank=True)
    opened_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)
    samsara_url = models.URLField(blank=True)

    class Meta:
        indexes = [models.Index(fields=["fleet_vehicle", "resolved_at"])]
        ordering = ["-opened_at"]

    def __str__(self):
        return f"{self.severity}: {self.summary} ({self.fleet_vehicle})"
