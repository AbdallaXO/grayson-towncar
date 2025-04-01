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

    # ? Idea
    # boosters = models.PositiveIntegerField()
    # carseats = models.PositiveIntegerField()

    def __str__(self):
        """
        Returns the vehicle type in uppercase (e.g., 'SUV', 'VAN').
        """
        return str(self.vehicle_type.title())


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
        )  # Ensure unique origin-destination pairs

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


class Rate(models.Model):
    """
    Associates a specific Vehicle with a specific Route, defining
    one-way and round-trip prices for that combination.
    """

    vehicle = models.ForeignKey(
        "Vehicle", on_delete=models.CASCADE, related_name="rates"
    )
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

    # def get_absolute_url(self):
    #     return reverse("Test_detail", kwargs={"pk": self.pk})
