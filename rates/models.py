from django.db import models
from django.utils.text import slugify
from django.utils import timezone


class Vehicle(models.Model):
    """
    Specifies details about a vehicle, such as its type, passenger capacity,
    and luggage capacity. Can be related to a Reservation or used for pricing via Rate.
    """

    VEHICLE_TYPES = [
        ("towncar", "Towncar"),
        ("suv", "SUV"),
        ("mini_van", "Mini Van"),
        ("van", "Van"),
    ]

    vehicle_type = models.CharField(max_length=20, choices=VEHICLE_TYPES)
    capacity = models.PositiveIntegerField()
    luggage_capacity = models.PositiveIntegerField()
    image = models.ImageField(upload_to="vehicles/", blank=True)
    ff_carseats_max = models.PositiveIntegerField(default=1)
    rf_carseats_max = models.PositiveIntegerField(default=1)
    boosters_max = models.PositiveIntegerField(default=1)
    carseats_capacity = models.PositiveIntegerField(default=1)
    carseats_display = models.CharField(max_length=100, blank=True)

    def __str__(self):
        """
        Returns the vehicle type in uppercase (e.g., 'SUV', 'VAN').
        """
        return str(self.vehicle_type.title())

    class Meta:
        ordering = ["capacity"]


class Location(models.Model):
    name = models.CharField(max_length=70)

    def __str__(self):
        return self.name


class Route(models.Model):
    """
    Defines a specific route (e.g., 'Disney Property <--> MCO').
    """

    origin = models.ForeignKey(
        "Location", related_name="origin", on_delete=models.CASCADE
    )
    destination = models.ForeignKey(
        "Location", related_name="destination", on_delete=models.CASCADE
    )
    slug = models.SlugField(max_length=200, blank=True, null=True)
    description = models.TextField(blank=True)

    class Meta:
        unique_together = (
            "origin",
            "destination",
        )
        # Ensure unique origin-destination pairs

    def save(self, *args, **kwargs):
        """
        Automatically generates a slug based on the route name if not provided.
        """
        if not self.slug:
            self.slug = slugify(f"{self.origin} to {self.destination}")
        super().save(*args, **kwargs)

    def __str__(self):
        """
        Returns the route name.
        """
        return f"{self.origin.name} ⇄ {self.destination.name}"


from django.db import models
from django.db.models import UniqueConstraint


class Rate(models.Model):
    """
    Associates a specific Vehicle with a specific Route, defining
    one-way and round-trip prices for that combination.
    """

    vehicle = models.ForeignKey(
        "Vehicle",
        on_delete=models.CASCADE,
        related_name="rates",
        db_index=True,  # Additional index on this foreign key
    )
    route = models.ForeignKey(
        "Route",
        on_delete=models.CASCADE,
        db_index=True,  # Additional index on this foreign key
    )
    oneway_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        db_index=True,  # Optional: if you frequently filter by price
    )
    round_trip_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        db_index=True,  # Optional: if you frequently filter by price
    )

    class Meta:
        # Use UniqueConstraint for more flexibility
        constraints = [
            UniqueConstraint(fields=["vehicle", "route"], name="unique_vehicle_route")
        ]

        # Multiple indexes for different query patterns
        indexes = [
            # Composite index for vehicle and route
            models.Index(fields=["vehicle"]),
            # Optional: additional indexes for common query patterns
            models.Index(fields=["oneway_price"]),
            models.Index(fields=["round_trip_price"]),
        ]

    def __str__(self):
        """
        Returns a combination of Vehicle and Route for pricing details.
        """
        return f"{self.vehicle} - {self.route}"

from django.db import models
from django.utils import timezone

class Lead(models.Model):
    """
    Lead model to capture customer interest in transportation services.
    Uses proper relationships to relevant models instead of string fields.
    """
    TRIP_TYPES = [
        ('oneway', 'One-Way Trip'),
        ('round', 'Round Trip'),
    ]
    
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField()
    phone = models.CharField(max_length=20)
    
    # Use proper relationships to existing models
    vehicle = models.ForeignKey('Vehicle', on_delete=models.SET_NULL, null=True)
    origin = models.ForeignKey('Location', related_name='lead_origins', on_delete=models.SET_NULL, null=True)
    destination = models.ForeignKey('Location', related_name='lead_destinations', on_delete=models.SET_NULL, null=True)
    route = models.ForeignKey('Route', on_delete=models.SET_NULL, null=True, blank=True)
    
    trip_type = models.CharField(max_length=20, choices=TRIP_TYPES)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Store the quoted price for reference
    quoted_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    
    def __str__(self):
        return f"{self.first_name} {self.last_name} - {self.created_at.strftime('%Y-%m-%d')}"
    
    def save(self, *args, **kwargs):
        """
        If origin and destination are set but route is not,
        try to find the matching route automatically.
        """
        if self.origin and self.destination and not self.route:
            try:
                self.route = Route.objects.get(origin=self.origin, destination=self.destination)
            except Route.DoesNotExist:
                pass
        super().save(*args, **kwargs)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['email']),
            models.Index(fields=['created_at']),
        ]