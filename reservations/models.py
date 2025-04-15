from django.db import models
from .constants import (
    FLIGHT_TYPE_CHOICES,
    TRIP_CHOICES,
    RESERVTION_STATUS,
)
import uuid


class Customer(models.Model):
    """
    Stores basic customer information, including name, contact details, and
    reservation history. It is related to the Reservation model via a ForeignKey.
    """

    first_name = models.CharField(max_length=50, db_index=True)
    last_name = models.CharField(max_length=50, blank=True)
    email = models.EmailField(db_index=True)
    phone_number = models.CharField(max_length=20)
    zipcode = models.CharField(max_length=20)
    # For future reference
    is_returning = models.BooleanField(default=False)
    reservation_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Stripe Related Data and for future dashboard implementation
    stripe_customer_id = models.CharField(max_length=255, null=True, blank=True)
    card_brand = models.CharField(max_length=50, blank=True)
    card_last4 = models.CharField(max_length=4, blank=True)
    card_exp_month = models.IntegerField(null=True, blank=True)
    card_exp_year = models.IntegerField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["email"]),
            models.Index(fields=["phone_number"]),
        ]

    def __str__(self):
        """
        Returns the customer's first name for easy identification.
        """
        return self.first_name

    def get_full_name(self):
        return f"{self.first_name.title()} {self.last_name.title()}"


class Reservation(models.Model):
    """
    Represents a core reservation in the system. It keeps track of trip details,
    pricing, and ties a customer, route, and vehicle together.
    """

    trip_type = models.CharField(max_length=20, choices=TRIP_CHOICES)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
    rate = models.ForeignKey("rates.Rate", on_delete=models.PROTECT)
    vehicle = models.ForeignKey(
        "rates.vehicle", on_delete=models.PROTECT, null=True, blank=True
    )
    passenger_count = models.PositiveIntegerField(default=1)
    luggage_count = models.PositiveIntegerField(default=1)
    need_carseats = models.BooleanField(default=False)  #
    rf_carseats = models.PositiveIntegerField(default=0)
    ff_carseats = models.PositiveBigIntegerField(default=0)
    booster_seats = models.PositiveIntegerField(default=0)
    uuid = models.UUIDField(blank=True, unique=True, default=uuid.uuid4, editable=False)

    # Special Requests
    store_stop = models.BooleanField(default=False)
    special_requests = models.TextField(blank=True)

    # Price and Payment Details
    base_price = models.DecimalField(max_digits=10, decimal_places=2)
    additional_charges = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_price = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(
        max_length=20, choices=RESERVTION_STATUS, default="pending"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["customer"]),
            models.Index(fields=["trip_type"]),
            models.Index(fields=["rate"]),
            models.Index(fields=["uuid"]),
        ]

    def save(self, *args, **kwargs):
        self.total_price = self.base_price
        # or base_price + carseats, etc.
        super().save(*args, **kwargs)

    def display_carseats(self):
        carseats = []
        if not self.need_carseats:
            return None
        if self.rf_carseats:
            carseats.append(f"{self.rf_carseats} Rear Facing")
        if self.ff_carseats:
            carseats.append(f"{self.ff_carseats} Forward-Facing")
        if self.booster_seats:
            carseats.append(f"{self.booster_seats} Booster")
        return "".join(carseats) if carseats else None

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
    flight_information = models.OneToOneField(
        "Flight", on_delete=models.CASCADE, null=True, blank=True
    )
    pickup_date = models.DateField()
    pickup_time = models.TimeField()
    pickup_location = models.CharField(max_length=255)
    dropoff_location = models.CharField(max_length=255)

    class Meta:
        indexes = [
            models.Index(fields=["reservation"]),
            models.Index(fields=["flight_information"]),
        ]

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
    flight_type = models.CharField(max_length=10, choices=FLIGHT_TYPE_CHOICES, blank=True)
    airline = models.CharField(max_length=50, blank=True)
    flight_number = models.CharField(max_length=50, blank=True)

    def __str__(self):
        """
        Display flight type (e.g., 'Arrival' or 'Departure'), airline,
        and flight number for quick reference.
        """
        return f"{self.airline} {self.flight_number}"
