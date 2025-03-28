from django.db import models
from django.contrib.auth.models import User
from django.utils.text import slugify


# Create your models here
class Reservation(models.Model):
    """
    Reservation Model, Has  Choice of One Way/Roundtrip
    Linked to a Customer Model & a Route and a Vehicle Type as ManyToOne, Since 1 one customer and 1 route and 1 vehicle can have many reservations.
    """

    CARSEAT_CHOICES = [
        ("booster", "Booster Seat"),
        ("rear_facing", "Rear-Facing Car Seat"),
        ("forward_facing", "Forward-Facing Car Seat"),
    ]
    TRIP_CHOICES = [
        ("one_way", "One Way"),
        ("round_trip", "Round Trip"),
    ]
    trip_type = models.CharField(max_length=20, choices=TRIP_CHOICES)
    # Customer Information
    first_name = models.CharField(max_length=50)
    last_name = models.CharField(max_length=50, blank=True)
    email = models.EmailField()
    phone_number = models.CharField(max_length=20)
    zipcode = models.CharField(max_length=20)

    # Trip Information
    route = models.ForeignKey("Route", on_delete=models.PROTECT)
    vehicle = models.ForeignKey("Vehicle", on_delete=models.PROTECT)
    passenger_count = models.PositiveIntegerField(default=1)
    luggage_count = models.PositiveIntegerField(default=1)
    # Trip First Leg- can add
    pickup_date = models.DateField()
    pickup_time = models.TimeField()
    pickup_location = models.CharField(max_length=255)
    dropoff_location = models.CharField(max_length=255)
    has_children = models.BooleanField(default=False)

    # (for round trip only)
    return_date = models.DateField(blank=True, null=True)
    return_time = models.TimeField(blank=True, null=True)
    return_pickup_location = models.CharField(max_length=255, blank=True)
    return_dropoff_location = models.CharField(max_length=255, blank=True)

    # Special Requests
    carseat_type = models.CharField(
        max_length=20, choices=CARSEAT_CHOICES, blank=True, null=True
    )
    store_stop = models.BooleanField(default=False)
    special_requests = models.TextField(blank=True)

    # Reservation Price Details + Linking Stripe payment_ID
    base_price = models.DecimalField(max_digits=10, decimal_places=2)
    additional_charges = models.DecimalField(
        max_digits=10, decimal_places=2, default=0.00
    )
    total_price = models.DecimalField(max_digits=10, decimal_places=2)
    payemnt_status = models.CharField(max_length=20, default="PENDING")
    stripe_payment_id = models.CharField(max_length=100, blank=True)

    def save(self, *args, **kwargs):
        """
        Automatically Calculate total_price
        """
        self.total_price = self.base_price + self.additional_charges
        super().save(*args, **kwargs)


class FlightInformation(models.Model):
    reservation = models.ForeignKey(
        "Reservation", on_delete=models.CASCADE, related_name="flights"
    )
    FLIGHT_TYPE_CHOICES = [
        ("arrival", "Arrival"),
        ("departure", "Departure"),
    ]
    flight_type = models.CharField(max_length=10, choices=FLIGHT_TYPE_CHOICES)

    airline = models.CharField(max_length=50)
    flight_number = models.CharField(max_length=50)
    date = models.DateField()
    time = models.TimeField()

    def __str__(self):
        return f"{self.get_flight_type_display()}: {self.airline} {self.flight_number}"


class Vehicle(models.Model):
    """
    a Model for choosing a vehicle/capacity
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
        return str(self.vehicle_type.upper())


class Route(models.Model):
    """
    Model for Route for Example  ( Disney Property <--> MCO )
    """

    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, blank=True, null=True)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)


from django.db import models


class Rate(models.Model):
    vehicle = models.ForeignKey("Vehicle", on_delete=models.CASCADE)
    route = models.ForeignKey("Route", on_delete=models.CASCADE)
    oneway_price = models.DecimalField(max_digits=10, decimal_places=2)
    round_trip_price = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        unique_together = ("vehicle", "route")

    def __str__(self):
        return f"{self.vehicle} - {self.route}"
