from django.db import models
from django.contrib.auth.models import User
from django.utils.text import slugify
from .constants import CARSEAT_CHOICES, FLIGHT_TYPE_CHOICES, TRIP_CHOICES, VEHICLE_TYPES


class Customer(models.Model):
    """
    Stores basic customer information, including name, contact details, and
    reservation history. It is related to the Reservation model via a ForeignKey.
    """
    first_name = models.CharField(max_length=50)
    last_name = models.CharField(max_length=50, blank=True)
    email = models.EmailField()
    phone_number = models.CharField(max_length=20)
    zipcode = models.CharField(max_length=20)
    # For future reference
    is_returning = models.BooleanField(default=False)
    reservation_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        """
        Automatically increments the reservation_count if this customer is being saved.
        (Note: This approach increments on every save call if self.created_at is set.)
        """
        if self.created_at:
            self.reservation_count += 1
        super().save(*args, **kwargs)

    def __str__(self):
        """
        Returns the customer's first name for easy identification.
        """
        return self.first_name


class Reservation(models.Model):
    """
    Represents a core reservation in the system. It keeps track of trip details,
    pricing, and ties a customer, route, and vehicle together.
    """
    trip_type = models.CharField(max_length=20, choices=TRIP_CHOICES)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
    route = models.ForeignKey("Route", on_delete=models.PROTECT)
    vehicle = models.ForeignKey("Vehicle", on_delete=models.PROTECT) # when a reservation is created

    passenger_count = models.PositiveIntegerField(default=1)
    has_children = models.BooleanField(default=False)
    luggage_count = models.PositiveIntegerField(default=1)
    carseat_type = models.CharField(max_length=20, choices=CARSEAT_CHOICES, blank=True, null=True)

    # Special Requests
    store_stop = models.BooleanField(default=False)
    special_requests = models.TextField(blank=True)

    # Price and Payment Details
    base_price = models.DecimalField(max_digits=10, decimal_places=2)
    additional_charges = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    total_price = models.DecimalField(max_digits=10, decimal_places=2)
    payment_status = models.CharField(max_length=20, default="PENDING")  # corrected field name
    stripe_payment_id = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=20, choices=[('pending', 'pending')], default='PENDING')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        """
        Ensures total_price is always base_price + additional_charges before saving.
        """
        self.total_price = self.base_price + self.additional_charges
        super().save(*args, **kwargs)

    def __str__(self):
        """
        Returns a simple string representation, showing the reservation's ID
        and its customer for clarity.
        """
        return f"Reservation #{self.id} - Customer: {self.customer}"


class Leg(models.Model):
    """
    Represents an individual leg of a trip within a Reservation.
    For example, a single pickup/dropoff or a one-way airport transfer. 
    Multiple legs can be tied to a single Reservation.
    """
    reservation = models.ForeignKey(
        Reservation, on_delete=models.CASCADE, related_name="legs"
    )
    flight_information = models.OneToOneField('Flight', on_delete=models.CASCADE, null=True, blank=True)
    pickup_date = models.DateField()
    pickup_time = models.TimeField()
    pickup_location = models.CharField(max_length=255)
    dropoff_location = models.CharField(max_length=255)

    def __str__(self):
        """
        Returns a string identifying the leg by pickup and dropoff locations.
        """
        return f"Leg #{self.id} from {self.pickup_location} to {self.dropoff_location}"


class Flight(models.Model):
    """
    Stores specific flight details, including airline, flight number, date, and time.
    Ties into a Leg model via a OneToOneField.
    """
    flight_type = models.CharField(max_length=10, choices=FLIGHT_TYPE_CHOICES)
    airline = models.CharField(max_length=50)
    flight_number = models.CharField(max_length=50)
    date = models.DateField()
    time = models.TimeField()

    def __str__(self):
        """
        Display flight type (e.g., 'Arrival' or 'Departure'), airline,
        and flight number for quick reference.
        """
        return f"{self.get_flight_type_display()} - {self.airline} {self.flight_number}"


class Vehicle(models.Model):
    """
    Specifies details about a vehicle, such as its type, passenger capacity,
    and luggage capacity. Can be related to a Reservation or used for pricing via Rate.
    """
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
    vehicle = models.ForeignKey("Vehicle", on_delete=models.CASCADE)
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
