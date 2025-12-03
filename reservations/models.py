from django.db import models
from .constants import (
    FLIGHT_TYPE_CHOICES,
    TRIP_CHOICES,
    RESERVTION_STATUS,
    DRIVER_STATUS,
)
import uuid
from decimal import Decimal
from django.utils import timezone
from rates.models import Vehicle, Rate
from datetime import timedelta


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
    gratuity_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    gratuity_percentage = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
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
    total_driver_payments = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Sum of all driver payments",
    )
    profit_estimate = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Estimated profit (total price - driver payments)",
    )

    # UTM Parameters for Google Ads Attribution
    gclid = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Google Click ID for conversion tracking",
    )
    utm_source = models.CharField(
        max_length=100, blank=True, null=True, help_text="UTM source parameter"
    )
    utm_medium = models.CharField(
        max_length=100, blank=True, null=True, help_text="UTM medium parameter"
    )
    utm_campaign = models.CharField(
        max_length=100, blank=True, null=True, help_text="UTM campaign parameter"
    )
    utm_term = models.CharField(
        max_length=100, blank=True, null=True, help_text="UTM term parameter"
    )
    utm_content = models.CharField(
        max_length=100, blank=True, null=True, help_text="UTM content parameter"
    )

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
        if self.travel_agent and self.commission_amount is None:
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

    def calculate_total_driver_payments(self):
        """
        Calculate total amount to be paid to drivers
        """
        return sum(leg.driver_pay_amount or 0 for leg in self.legs.all())

    def calculate_profit(self):
        """
        Calculate profit (total price minus driver payments)
        """
        driver_payments = (
            self.total_driver_payments or self.calculate_total_driver_payments()
        )
        return (self.total_price - driver_payments).quantize(Decimal("0.01"))

    def update_profit_calculations(self):
        """
        Update stored profit calculations
        """
        self.total_driver_payments = self.calculate_total_driver_payments()
        self.profit_estimate = self.calculate_profit()
        self.save(update_fields=["total_driver_payments", "profit_estimate"])

    @property
    def all_payments(self):
        """
        Get all payments for this reservation
        """
        return self.payments.all()

    @property
    def payment_status(self):
        """
        Get the payment status for this reservation
        Uses prefetched payments to avoid N+1 queries
        """
        # Use prefetched payments if available, otherwise fall back to query
        if hasattr(self, '_prefetched_objects_cache') and 'payments' in self._prefetched_objects_cache:
            payments = self._prefetched_objects_cache['payments']
        else:
            payments = self.payments.all()
            
        if not payments:
            return "unpaid"
        
        # Check if any payment is marked as paid
        for payment in payments:
            if payment.status == "paid":
                return "paid"
            elif payment.status == "card_saved":
                return "card_saved"
            elif payment.status == "pending":
                return "pending"
        
        return "failed"

    @property
    def detailed_payment_status(self):
        """
        Get detailed payment status including payment type and status
        Uses prefetched payments to avoid N+1 queries
        Always returns the LATEST payment (most recent)
        """
        # Use prefetched payments if available, otherwise fall back to query
        if hasattr(self, '_prefetched_objects_cache') and 'payments' in self._prefetched_objects_cache:
            payments = list(self._prefetched_objects_cache['payments'])
            # Ensure prefetched payments are ordered by created_at desc (most recent first)
            payments.sort(key=lambda p: p.created_at, reverse=True)
        else:
            # Explicitly order by created_at desc to get the latest payment
            payments = list(self.payments.all().order_by('-created_at'))
            
        if not payments:
            return {"status": "unpaid", "type": None, "display": "Unpaid"}
        
        # Get the most recent payment (first in the ordered list)
        latest_payment = payments[0] if payments else None
        
        if not latest_payment:
            return {"status": "unpaid", "type": None, "display": "Unpaid"}
        
        if latest_payment.status == "paid":
            if latest_payment.payment_type == "pay_now":
                return {
                    "status": "paid",
                    "type": "pay_now",
                    "display": "Pre-Pay & Paid"
                }
            else:
                return {
                    "status": "paid", 
                    "type": "pay_later",
                    "display": "Saved Card & Paid"
                }
        elif latest_payment.status == "card_saved":
            return {
                "status": "card_saved",
                "type": "pay_later", 
                "display": "Card Saved"
            }
        elif latest_payment.status == "pending":
            if latest_payment.payment_type == "pay_now":
                return {
                    "status": "pending",
                    "type": "pay_now",
                    "display": "Pre-Pay Pending"
                }
            else:
                return {
                    "status": "pending",
                    "type": "pay_later", 
                    "display": "Save Card Pending"
                }
        elif latest_payment.status == "failed":
            if latest_payment.payment_type == "pay_now":
                return {
                    "status": "failed",
                    "type": "pay_now",
                    "display": "Pre-Pay Failed"
                }
            else:
                return {
                    "status": "failed",
                    "type": "pay_later",
                    "display": "Save Card Failed"
                }
        else:
            return {"status": "unknown", "type": None, "display": "Unknown"}

    def check_and_update_completion_status(self):
        """
        Check if all legs in this reservation are completed and update 
        reservation status to 'completed' if so.
        
        Returns:
            bool: True if reservation was updated to completed, False otherwise
        """
        # Skip if already completed or cancelled
        if self.status in ['completed', 'cancelled']:
            return False
            
        # Get all legs for this reservation
        legs = self.legs.all()
        
        # If no legs exist, don't auto-complete
        if not legs.exists():
            return False
            
        # Check if all legs are completed
        all_completed = all(leg.status == 'completed' for leg in legs)
        
        if all_completed:
            self.status = 'completed'
            self.save(update_fields=['status'])
            return True
            
        return False

    def get_completion_date(self):
        """
        Get the completion date of the last completed leg.
        Returns the pickup_date of the last completed leg, or None if no legs are completed.
        """
        completed_legs = self.legs.filter(status='completed').order_by('-pickup_date', '-pickup_time')
        if completed_legs.exists():
            last_leg = completed_legs.first()
            return last_leg.pickup_date
        return None

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
    driver_notes = models.TextField(
        null=True, blank=True, help_text="Notes added by the driver about this trip"
    )
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
    driver_pay_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Amount to pay the driver (set by admin)",
    )
    revenue_share = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="This leg's portion of the reservation revenue",
    )
    profit_estimate = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Estimated profit (revenue share - driver payment)",
    )

    payment_status = models.CharField(
        max_length=20,
        choices=[
            ("unpaid", "Unpaid"),
            ("paid", "Paid"),
            ("canceled", "Canceled"),
        ],
        default="unpaid",
    )

    def calculate_revenue_share(self):
        """
        Calculate this leg's portion of the reservation total price.
        """
        if not self.reservation:
            return Decimal("0.00")

        # Get total number of legs in this reservation
        total_legs = self.reservation.legs.count()

        if total_legs == 0:  # Safety check
            return Decimal("0.00")

        # Calculate leg's share of revenue (total price divided by number of legs)
        revenue_share = self.reservation.total_price / Decimal(total_legs)

        # Round to 2 decimal places
        return revenue_share.quantize(Decimal("0.01"))

    def calculate_profit(self):
        """
        Calculate profit (leg's revenue share minus driver payment)
        """
        revenue = self.revenue_share or self.calculate_revenue_share()
        driver_payment = self.driver_pay_amount or Decimal("0.00")

        return (revenue - driver_payment).quantize(Decimal("0.01"))

    def save(self, *args, **kwargs):
        # Calculate and store revenue share if not set
        if self.revenue_share is None:
            self.revenue_share = self.calculate_revenue_share()

        # Calculate and store profit estimate if driver payment is set
        if self.driver_pay_amount is not None:
            self.profit_estimate = self.calculate_profit()

        super().save(*args, **kwargs)

    def __str__(self):
        """
        Returns a string identifying the leg by pickup and dropoff locations.
        """
        return f"Leg #{self.id} from {self.pickup_location} to {self.dropoff_location}"

    def get_trip_type(self):
        """
        Determine if this leg is an arrival or return based on pickup/dropoff locations.
        Returns: 'arrival', 'return', or 'other'
        """
        import re

        # Keywords that indicate airport locations
        airport_keywords = ["mco", "airport", "terminal", "gate", "international"]

        # Convert to lowercase for case-insensitive matching
        pickup_lower = self.pickup_location.lower()
        dropoff_lower = self.dropoff_location.lower()

        # Check if pickup location contains airport keywords
        pickup_is_airport = any(keyword in pickup_lower for keyword in airport_keywords)

        # Check if dropoff location contains airport keywords
        dropoff_is_airport = any(
            keyword in dropoff_lower for keyword in airport_keywords
        )

        # Determine trip type
        if pickup_is_airport and not dropoff_is_airport:
            return "arrival"  # From airport to destination
        elif dropoff_is_airport and not pickup_is_airport:
            return "return"  # From destination to airport
        else:
            return "other"  # Neither pickup nor dropoff is airport, or both are

    def get_trip_type_display(self):
        """
        Get a human-readable display for the trip type with appropriate icons.
        """
        trip_type = self.get_trip_type()
        if trip_type == "arrival":
            return {
                "type": "arrival",
                "label": "Arrival",
                "icon": "bi-airplane-engines",
                "color": "dark",
                "description": "Airport to Destination",
            }
        elif trip_type == "return":
            return {
                "type": "return",
                "label": "Return",
                "icon": "bi-airplane",
                "color": "success",
                "description": "Destination to Airport",
            }
        else:
            return {
                "type": "other",
                "label": "Other",
                "icon": "bi-arrow-left-right",
                "color": "secondary",
                "description": "Non-Airport Transfer",
            }

    class Meta:
        ordering = ["pickup_date", "pickup_time"]
        indexes = [
            models.Index(fields=["reservation"]),
            models.Index(fields=["flight_information"]),
        ]


class Flight(models.Model):
    """
    Stores specific flight details, including airline, flight number, date, and time.
    Ties into a Leg model via a OneToOneField.
    Includes AeroAPI tracking data for real-time flight status.
    """

    flight_type = models.CharField(
        max_length=10, choices=FLIGHT_TYPE_CHOICES, blank=True
    )
    airline = models.CharField(max_length=50, blank=True)
    flight_number = models.CharField(max_length=50, blank=True)
    
    # AeroAPI Tracking Fields
    flight_iata = models.CharField(
        max_length=20, blank=True, 
        help_text="IATA flight code (e.g., DL1691)"
    )
    origin = models.CharField(
        max_length=255, blank=True,
        help_text="Origin airport (e.g., SLC - Salt Lake City Intl)"
    )
    destination = models.CharField(
        max_length=255, blank=True,
        help_text="Destination airport (e.g., MCO - Orlando Intl)"
    )
    status = models.CharField(
        max_length=100, blank=True,
        help_text="Flight status (e.g., En Route, Landed, Scheduled)"
    )
    scheduled_arrival_local = models.DateTimeField(
        null=True, blank=True,
        help_text="Scheduled runway arrival time in local timezone"
    )
    estimated_arrival_local = models.DateTimeField(
        null=True, blank=True,
        help_text="Estimated runway arrival time in local timezone"
    )
    scheduled_gate_arrival_local = models.DateTimeField(
        null=True, blank=True,
        help_text="Scheduled gate arrival time in local timezone"
    )
    estimated_gate_arrival_local = models.DateTimeField(
        null=True, blank=True,
        help_text="Estimated gate arrival time in local timezone"
    )
    terminal = models.CharField(
        max_length=10, blank=True,
        help_text="Arrival terminal (e.g., B)"
    )
    gate = models.CharField(
        max_length=20, blank=True,
        help_text="Arrival gate (e.g., 76)"
    )
    baggage_claim = models.CharField(
        max_length=20, blank=True,
        help_text="Baggage claim area (e.g., 31)"
    )
    last_updated = models.DateTimeField(
        null=True, blank=True,
        help_text="When flight data was last fetched from AeroAPI"
    )

    def save(self, *args, **kwargs):
        """
        Override save to normalize airline field before saving.
        This ensures consistent airline formatting regardless of how it's entered.
        """
        if self.airline:
            # Import here to avoid circular import
            from .utils import normalize_airline
            self.airline = normalize_airline(self.airline)
        super().save(*args, **kwargs)

    def __str__(self):
        """
        Display flight type (e.g., 'Arrival' or 'Departure'), airline,
        and flight number for quick reference.
        """
        return f"{self.airline} {self.flight_number}"
    
    def get_flight_ident(self):
        """
        Get the flight identifier for AeroAPI (combines airline and flight number)
        Returns IATA format like 'DL1691' or falls back to airline + flight_number
        
        Prioritizes current airline/flight_number over stored flight_iata to ensure
        we use the most up-to-date flight information.
        """
        # Prioritize current airline/flight_number over stored flight_iata
        # This ensures we use the updated flight info if the user changed it
        if self.airline and self.flight_number:
            # Import here to avoid circular import
            from .utils import normalize_airline
            # Normalize airline to IATA code (already normalized in save, but double-check)
            airline_code = normalize_airline(self.airline)
            # Remove non-alphanumeric from flight number
            flight_num = ''.join(c for c in self.flight_number if c.isalnum())
            return f"{airline_code}{flight_num}"
        
        # Fallback to stored flight_iata if airline/flight_number not available
        if self.flight_iata:
            return self.flight_iata
        
        return None


# yourapp/models.py
from django.db import models
from django.utils import timezone
from datetime import timedelta


class Lead(models.Model):
    class StatusChoices(models.TextChoices):
        NEW = "new", "New"
        CONTACTED = "contacted", "Contacted"
        INTERESTED = "interested", "Interested"
        FUTURE_CONTACT = "future_contact", "Future Contact"
        CONVERTED = "converted", "Converted"
        LOST = "lost", "Lost"

    class PriorityChoices(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"
        URGENT = "urgent", "Urgent"

    class TripTypeChoices(models.TextChoices):
        ONEWAY = "oneway", "One Way"
        ROUNDTRIP = "roundtrip", "Round Trip"

    # Contact Info
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)

    # Trip Details
    vehicle = models.ForeignKey(
        Vehicle, on_delete=models.SET_NULL, null=True, blank=True
    )
    pickup_location = models.CharField(max_length=200, blank=True)
    dropoff_location = models.CharField(max_length=200, blank=True)
    pickup_date = models.DateField(null=True, blank=True)
    trip_type = models.CharField(
        max_length=20, choices=TripTypeChoices.choices, blank=True
    )
    estimated_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )

    # Lead Management
    status = models.CharField(
        max_length=20, choices=StatusChoices.choices, default=StatusChoices.NEW
    )
    priority = models.CharField(
        max_length=20, choices=PriorityChoices.choices, default=PriorityChoices.MEDIUM
    )
    next_follow_up = models.DateTimeField(null=True, blank=True)
    contact_attempts = models.PositiveIntegerField(default=0)
    last_contact_date = models.DateTimeField(null=True, blank=True)

    # Conversion Tracking
    converted = models.BooleanField(default=False)
    converted_at = models.DateTimeField(null=True, blank=True)

    # Notes and Timestamps
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Lead"
        verbose_name_plural = "Leads"

    def __str__(self):
        name = f"{self.first_name} {self.last_name}".strip()
        return name or f"Lead #{self.id}"

    @property
    def get_full_name(self):
        return f"{self.first_name} {self.last_name}".strip() or "No Name"

    @property
    def latest_quote(self):
        """Get the most recent quote for this lead"""
        return self.quotes.order_by("-created_at").first()

    @property
    def quote_count(self):
        """Get the number of quotes for this lead"""
        return self.quotes.count()


class Quote(models.Model):
    class TripTypeChoices(models.TextChoices):
        ONEWAY = "oneway", "One Way"
        ROUNDTRIP = "roundtrip", "Round Trip"

    class StatusChoices(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"
        EXPIRED = "expired", "Expired"

    # Relationship to Lead
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="quotes")

    # Trip Details
    pickup_location = models.CharField(max_length=200, blank=True)
    dropoff_location = models.CharField(max_length=200, blank=True)
    pickup_date = models.DateField(null=True, blank=True)
    trip_type = models.CharField(
        max_length=20, choices=TripTypeChoices.choices, blank=True
    )
    vehicle = models.ForeignKey(
        Vehicle, on_delete=models.SET_NULL, null=True, blank=True
    )
    estimated_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )

    # Quote Management
    status = models.CharField(
        max_length=20, choices=StatusChoices.choices, default=StatusChoices.PENDING
    )
    is_current = models.BooleanField(
        default=True, help_text="Mark this as the most recent quote"
    )

    # Timestamps
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Quote"
        verbose_name_plural = "Quotes"

    def __str__(self):
        return f"Quote for {self.lead} - {self.pickup_date or 'No date'}"

    def save(self, *args, **kwargs):
        # If this quote is marked as current, unmark all other quotes for this lead
        if self.is_current:
            Quote.objects.filter(lead=self.lead).exclude(id=self.id).update(
                is_current=False
            )
        super().save(*args, **kwargs)
