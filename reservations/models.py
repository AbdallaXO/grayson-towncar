from django.db import models
from .constants import (
    FLIGHT_TYPE_CHOICES,
    TRIP_CHOICES,
    RESERVTION_STATUS,
    DRIVER_STATUS,
)
import uuid
from decimal import Decimal


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
        return f"{self.first_name.title()} {self.last_name.title()}"

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
    rf_carseats = models.PositiveIntegerField("RF-Seat", default=0)
    ff_carseats = models.PositiveBigIntegerField("FF-Seat", default=0)
    booster_seats = models.PositiveIntegerField("Booster", default=0)
    uuid = models.UUIDField(blank=True, unique=True, default=uuid.uuid4, editable=False)

    # Special Requests
    store_stop = models.BooleanField(default=False)
    special_requests = models.TextField(blank=True)

    # Price and Payment Details
    base_price = models.DecimalField(max_digits=10, decimal_places=2)
    additional_charges = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_price = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(
        max_length=20, choices=RESERVTION_STATUS, default="confirmed"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    private_notes = models.TextField(null=True, blank=True)

    # Travel Agent fields
    travel_agent = models.ForeignKey(
        "users.TravelAgent",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reservations",
    )
    commission_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    commission_paid = models.BooleanField(default=False)
    commission_paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["customer"]),
            models.Index(fields=["trip_type"]),
            models.Index(fields=["rate"]),
            models.Index(fields=["uuid"]),
            models.Index(fields=["travel_agent"]),
        ]

    def save(self, *args, **kwargs):
        """
        Override save method to calculate prices and track changed fields
        """
        # Initialize changed fields list
        self._changed_fields = []

        # Check for changes if this is an existing instance
        if self.pk:
            try:
                # Get the current state from the database
                old_instance = Reservation.objects.get(pk=self.pk)

                # Compare all fields and track which ones changed
                for field in self._meta.fields:
                    old_value = getattr(old_instance, field.name)
                    new_value = getattr(self, field.name)

                    # Check if the field has changed
                    if old_value != new_value:
                        self._changed_fields.append(field.name)

            except Reservation.DoesNotExist:
                # This is technically a new instance
                pass

        # Business logic for pricing
        if not self.base_price:
            self.base_price = (
                (self.total_price - self.additional_charges) if self.total_price else 0
            )

        if not self.total_price:
            self.total_price = self.base_price + (self.additional_charges or 0)

        # Calculate commission if this is a travel agent reservation
        if self.travel_agent and not self.commission_amount:
            self.commission_amount = self.total_price * Decimal(
                "0.10"
            )  # 10% commission

        # Call the original save() method
        super().save(*args, **kwargs)

    def display_carseats(self):
        carseats = []
        if not self.need_carseats:
            return None
        if self.rf_carseats:
            carseats.append(f"{self.rf_carseats} Rear-Facing")
        if self.ff_carseats:
            carseats.append(f"{self.ff_carseats} Forward-Facing")
        if self.booster_seats:
            carseats.append(f"{self.booster_seats} Booster")
        return ", ".join(carseats) if carseats else None

    def __str__(self):
        """
        Returns a simple string representation, showing the reservation's ID
        and its customer for clarity.
        """
        return f"Reservation #{self.id} - {self.customer.get_full_name()}"


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
    private_notes = models.TextField(null=True, blank=True)
    driver = models.ForeignKey(
        "drivers.Driver",
        null=True,
        on_delete=models.SET_NULL,
        blank=True,
        related_name="legs",
    )
    status = models.CharField(
        choices=DRIVER_STATUS,
        null=True,
        blank=True,
        max_length=255,
        default="in-progress",
    )

    class Meta:
        ordering = ["pickup_date", "pickup_time"]
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

    flight_type = models.CharField(
        max_length=10, choices=FLIGHT_TYPE_CHOICES, blank=True
    )
    airline = models.CharField(max_length=50, blank=True)
    flight_number = models.CharField(max_length=50, blank=True)

    def __str__(self):
        """
        Display flight type (e.g., 'Arrival' or 'Departure'), airline,
        and flight number for quick reference.
        """
        return f"{self.airline} {self.flight_number}"
