from django.db import models
from django.utils.text import slugify


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

    def __str__(self):
        """
        Returns the vehicle type in uppercase (e.g., 'SUV', 'VAN').
        """
        return str(self.vehicle_type.upper())


class Route(models.Model):
    """
    Defines a specific route (e.g., 'Disney Property <--> MCO').
    """
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, blank=True, null=True)

    def save(self, *args, **kwargs):
        """
        Automatically generates a slug based on the route name if not provided.
        """
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        """
        Returns the route name.
        """
        return self.name


class Rate(models.Model):
    """
    Associates a specific Vehicle with a specific Route, defining
    one-way and round-trip prices for that combination.
    """
    vehicle = models.ForeignKey("Vehicle", on_delete=models.CASCADE, related_name='rates')
    route = models.ForeignKey("Route", on_delete=models.CASCADE)
    oneway_price = models.DecimalField(max_digits=10, decimal_places=2)
    round_trip_price = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        unique_together = ("vehicle", "route")

    def __str__(self):
        """
        Returns a combination of Vehicle and Route for pricing details.
        """
        return f"{self.vehicle} - {self.route}"
