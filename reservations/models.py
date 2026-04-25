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
from django.utils.functional import cached_property
from rates.models import Vehicle, Rate, Route, Location
from datetime import datetime, timedelta, time
from simple_history.models import HistoricalRecords


class Customer(models.Model):
    """
    Stores basic customer information, including name, contact details, and
    reservation history. It is related to the Reservation model via a ForeignKey.
    """

    first_name = models.CharField(max_length=50, db_index=True)
    last_name = models.CharField(max_length=50, blank=True)
    email = models.EmailField(db_index=True)
    phone_number = models.CharField(max_length=20)
    zipcode = models.CharField(max_length=20, blank=True, default="")
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

    history = HistoricalRecords()

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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Track original status so save() can detect changes without a DB query
        self._original_status = self.status if self.pk else None

    trip_type = models.CharField(max_length=20, choices=TRIP_CHOICES)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT)
    rate = models.ForeignKey("rates.Rate", on_delete=models.PROTECT)
    vehicle = models.ForeignKey(
        "rates.vehicle", on_delete=models.PROTECT, null=True, blank=True
    )
    passenger_count = models.PositiveIntegerField(default=1)

    LUGGAGE_TYPE_CHOICES = [
        ("carry_on", "Carry-On Only"),
        ("checked", "Checked Bags"),
        ("mixed", "Mixed / Not Sure"),
    ]
    luggage_count = models.PositiveIntegerField(default=1)
    luggage_type = models.CharField(
        max_length=10, choices=LUGGAGE_TYPE_CHOICES, blank=True, default="",
    )
    need_carseats = models.BooleanField(default=False)  #
    rf_carseats = models.PositiveIntegerField("RF-Seat", default=0)
    ff_carseats = models.PositiveBigIntegerField("FF-Seat", default=0)
    booster_seats = models.PositiveIntegerField("Booster", default=0)
    extra_carseats = models.PositiveIntegerField("Extra Car Seats", default=0)
    extra_boosters = models.PositiveIntegerField("Extra Boosters", default=0)
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
        db_index=True,
    )
    fbclid = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Facebook Click ID for conversion tracking",
        db_index=True,
    )
    utm_source = models.CharField(
        max_length=100, blank=True, null=True, help_text="UTM source parameter", db_index=True
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

    # Canonical attribution channel — derived from utm_*/click IDs/travel_agent
    # in reservations.attribution.derive_booking_source(). Persisted on save so
    # KPI dashboards can GROUP BY it directly without re-deriving.
    BOOKING_SOURCE_CHOICES = [
        ("google_ads", "Google Ads"),
        ("google_organic", "Google Organic"),
        ("meta_ads", "Meta Ads"),
        ("meta_organic", "Meta Organic"),
        ("travel_agent", "Travel Agent"),
        ("referral", "Referral"),
        ("direct", "Direct"),
        ("phone", "Phone / Dispatcher"),
        ("other", "Other"),
    ]
    booking_source = models.CharField(
        max_length=32,
        choices=BOOKING_SOURCE_CHOICES,
        default="direct",
        db_index=True,
        help_text="Normalized acquisition channel for KPI reporting",
    )
    is_repeat_booking = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True if this customer's email has a prior reservation. Independent of booking_source.",
    )

    # Persisted paid state — maintained by payment.signals.sync_reservation_paid_state.
    # These exist so revenue KPIs can be queried with .filter(is_paid=True)
    # instead of looping in Python over the @cached_property payment_status.
    is_paid = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True when at least one Payment with status='paid' exists (net of refunds > 0)",
    )
    paid_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text="Net collected revenue (paid payments minus refunded amounts)",
    )
    gross_paid = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text="Sum of paid Payment amounts before refunds",
    )
    total_refunded = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text="Sum of refunded_amount across all paid payments",
    )
    first_paid_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Timestamp of the first successful payment — used for revenue trends",
    )

    # Audit fields - track who created/modified and when
    created_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reservations_created",
        help_text="User who created this reservation",
    )
    modified_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reservations_modified",
        help_text="User who last modified this reservation",
    )
    last_modified_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of last modification",
    )

    # Refund Request Fields
    REFUND_STATUS_CHOICES = [
        ('none', 'No Refund Requested'),
        ('requested', 'Refund Requested'),
        ('processing', 'Processing Refund'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('completed', 'Refund Completed'),
    ]
    
    refund_status = models.CharField(
        max_length=20,
        choices=REFUND_STATUS_CHOICES,
        default='none',
        help_text="Current status of refund request",
    )
    refund_requested_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="refund_requests_made",
        help_text="Staff member who requested the refund",
    )
    refund_requested_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the refund was requested",
    )
    refund_reason = models.TextField(
        null=True,
        blank=True,
        help_text="Reason for refund request from staff",
    )
    refund_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Amount to refund (can be partial or full)",
    )
    refund_processed_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="refunds_processed",
        help_text="Admin who processed the refund",
    )
    refund_processed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the refund was processed",
    )
    refund_notes = models.TextField(
        null=True,
        blank=True,
        help_text="Admin notes about the refund processing",
    )

    class Meta:
        indexes = [
            models.Index(fields=["customer"]),
            models.Index(fields=["trip_type"]),
            models.Index(fields=["rate"]),
            models.Index(fields=["uuid"]),
            models.Index(fields=["travel_agent"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["status"]),
            models.Index(fields=["refund_status"]),
            models.Index(fields=["travel_agent", "status"]),
        ]

    def save(self, *args, **kwargs):
        """
        Override save method to calculate prices and track changed fields
        """
        # Initialize changed fields list
        self._changed_fields = []
        
        # Auto-set modified_by if not set and user is available
        if self.pk:  # Only for existing instances (updates)
            try:
                from reservations.middleware import get_current_user
                current_user = get_current_user()
                if current_user and not self.modified_by:
                    self.modified_by = current_user
                if not self.last_modified_at:
                    self.last_modified_at = timezone.now()
            except:
                pass  # If middleware not available, skip

        # Track changed fields using values captured at __init__ (no extra DB query)
        if self.pk and hasattr(self, '_original_status') and self._original_status != self.status:
            self._changed_fields.append("status")

        # Business logic for pricing
        if not self.base_price:
            self.base_price = (
                (self.total_price - self.additional_charges) if self.total_price else 0
            )

        if not self.total_price:
            self.total_price = self.base_price + (self.additional_charges or 0)

        # Calculate commission if this is a travel agent reservation
        # Commission is calculated on base_price only, not additional fees or gratuity
        if self.travel_agent and self.commission_amount is None:
            commission_rate = (self.travel_agent.commission_rate / Decimal("100")) if hasattr(self.travel_agent, 'commission_rate') and self.travel_agent.commission_rate else Decimal("0.10")
            self.commission_amount = self.base_price * commission_rate

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
        return sum(leg.total_driver_pay for leg in self.legs.all())

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
        Reservation.objects.filter(pk=self.pk).update(
            total_driver_payments=self.total_driver_payments,
            profit_estimate=self.profit_estimate,
        )

    def recalculate_leg_revenue_shares(self):
        """
        Recalculate revenue_share and profit_estimate for all legs in this reservation.
        Call this whenever legs are added or removed so the split stays correct.

        When legs have per-leg base prices (leg_base_price), revenue is split
        proportionally by base price. Otherwise, equal split (current behavior).
        """
        legs = list(self.legs.all())
        if not legs or not self.total_price:
            return

        has_per_leg_pricing = any(l.leg_base_price is not None for l in legs)

        if has_per_leg_pricing:
            # Weighted split: legs with leg_base_price get proportional share;
            # legs without it get an equal portion of the remainder.
            num_legs = len(legs)
            default_share = self.base_price / Decimal(num_legs) if self.base_price else Decimal("0.00")
            total_base = sum(
                l.leg_base_price if l.leg_base_price is not None else default_share
                for l in legs
            )
            for leg in legs:
                leg_base = leg.leg_base_price if leg.leg_base_price is not None else default_share
                if total_base > 0:
                    weight = leg_base / total_base
                    share = (self.total_price * weight).quantize(Decimal("0.01"))
                else:
                    share = (self.total_price / Decimal(num_legs)).quantize(Decimal("0.01"))
                Leg.objects.filter(pk=leg.pk).update(revenue_share=share)
                leg.revenue_share = share  # keep in-memory copy current
                profit = leg.calculate_profit()
                Leg.objects.filter(pk=leg.pk).update(profit_estimate=profit)
        else:
            # Equal split (original behavior)
            share = (self.total_price / Decimal(len(legs))).quantize(Decimal("0.01"))
            self.legs.update(revenue_share=share)
            for leg in legs:
                leg.revenue_share = share
                profit = leg.calculate_profit()
                Leg.objects.filter(pk=leg.pk).update(profit_estimate=profit)

    @cached_property
    def all_payments(self):
        """
        Get all payments for this reservation
        Cached to avoid N+1 queries when accessed multiple times
        """
        return list(self.payments.all())

    @cached_property
    def total_paid(self):
        """
        Calculate total amount paid (sum of all successful payments, excluding refunded amounts)
        Cached to avoid N+1 queries when accessed multiple times in templates
        """
        from django.db.models import Sum
        # Calculate total paid (excluding refunded payments)
        paid_sum = self.payments.filter(status="paid").aggregate(total=Sum("amount"))["total"] or Decimal('0.00')
        # Subtract partial refunds from paid payments
        partial_refunded_sum = self.payments.filter(status="paid", refunded_amount__isnull=False).aggregate(
            total=Sum("refunded_amount")
        )["total"] or Decimal('0.00')
        return (paid_sum - partial_refunded_sum).quantize(Decimal("0.01"))

    @cached_property
    def amount_owed(self):
        """
        Calculate remaining amount owed (total price - total paid)
        Cached to avoid N+1 queries when accessed multiple times in templates
        """
        return (self.total_price - self.total_paid).quantize(Decimal("0.01"))

    @cached_property
    def payment_status(self):
        """
        Get the payment status for this reservation
        Cached to avoid N+1 queries when accessed multiple times
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

    @cached_property
    def detailed_payment_status(self):
        """
        Get detailed payment status including payment type and status
        Cached to avoid N+1 queries when accessed multiple times
        Always returns the LATEST payment (most recent by created_at)
        """
        # Use prefetched payments if available, otherwise fall back to query
        if hasattr(self, '_prefetched_objects_cache') and 'payments' in self._prefetched_objects_cache:
            payments = list(self._prefetched_objects_cache['payments'])
            # Sort by created_at desc, then by id desc (most recent first)
            # This ensures we get the truly latest payment even if timestamps are identical
            # Use id as secondary sort since higher id = more recent payment
            payments.sort(key=lambda p: (
                p.created_at if p.created_at else timezone.make_aware(timezone.datetime.min),
                p.id if p.id else 0
            ), reverse=True)
        else:
            # Explicitly order by created_at desc, then id desc to get the latest payment
            payments = list(self.payments.all().order_by('-created_at', '-id'))
            
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
        elif latest_payment.status == "refunded":
            # Check if it's a full or partial refund
            if latest_payment.refunded_amount and latest_payment.refunded_amount < latest_payment.amount:
                return {
                    "status": "refunded",
                    "type": latest_payment.payment_type,
                    "display": f"Partially Refunded (${latest_payment.refunded_amount} of ${latest_payment.amount})"
                }
            else:
                return {
                    "status": "refunded",
                    "type": latest_payment.payment_type,
                    "display": "Refunded"
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
            
        # Get all legs for this reservation, excluding cancelled ones
        legs = self.legs.all()
        active_legs = [leg for leg in legs if leg.status != 'cancelled']

        # If no active legs exist, don't auto-complete
        if not active_legs:
            return False

        # Check if all active legs are completed
        all_completed = all(leg.status == 'completed' for leg in active_legs)
        
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

    history = HistoricalRecords()

    def __str__(self):
        """
        Returns a simple string representation, showing the reservation's ID
        and its customer for clarity.
        """
        return f"Reservation #{self.id} - {self.customer.get_full_name()}"


class RefundRequest(models.Model):
    """
    Tracks a refund request against a reservation, with optional per-leg granularity.
    Supports three refund types: price adjustment, partial cancellation, full cancellation.
    """
    REFUND_TYPE_CHOICES = [
        ('price_adjustment', 'Price Adjustment'),
        ('partial_cancellation', 'Partial Cancellation'),
        ('full_cancellation', 'Full Cancellation'),
    ]
    STATUS_CHOICES = [
        ('requested', 'Requested'),
        ('processing', 'Processing'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('completed', 'Completed'),
    ]

    reservation = models.ForeignKey(
        Reservation,
        on_delete=models.CASCADE,
        related_name='refund_requests',
    )
    legs = models.ManyToManyField(
        'Leg',
        blank=True,
        related_name='refund_requests',
        help_text='Specific legs included in this refund (empty = all legs for full cancellation)',
    )
    refund_type = models.CharField(
        max_length=30,
        choices=REFUND_TYPE_CHOICES,
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='requested',
    )
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text='Final approved refund amount',
    )
    suggested_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text='Policy-calculated suggested refund amount',
    )
    policy_override = models.BooleanField(
        default=False,
        help_text='True if admin overrode the policy-suggested amount',
    )
    reason = models.TextField(
        help_text='Reason for refund request',
    )
    notes = models.TextField(
        null=True,
        blank=True,
        help_text='Admin notes about processing',
    )

    # Audit fields
    requested_by = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True,
        related_name='refund_requests_created',
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    processed_by = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='refund_requests_processed',
    )
    processed_at = models.DateTimeField(null=True, blank=True)

    # Stripe tracking
    stripe_refund_ids = models.JSONField(
        default=list,
        blank=True,
        help_text='List of Stripe refund IDs created for this request',
    )

    class Meta:
        ordering = ['-requested_at']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['reservation', 'status']),
        ]

    def __str__(self):
        return f"RefundRequest #{self.pk} - {self.get_refund_type_display()} ({self.get_status_display()})"


class Leg(models.Model):
    """
    Represents an individual leg of a trip within a Reservation.
    For example, a single pickup/dropoff or a one-way airport transfer.
    Multiple legs can be tied to a single Reservation.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Track original driver_id so save() can detect reassignment without a DB query
        self._original_driver_id = self.driver_id

    reservation = models.ForeignKey(
        Reservation, on_delete=models.CASCADE, related_name="legs"
    )
    route = models.ForeignKey(
        "rates.Route",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="legs",
        help_text="Matched route for this leg (auto-filled when possible)",
    )

    # ── Per-leg trip detail overrides ──
    # NULL = inherit from reservation (backward-compatible default).
    # Set a value to override for this specific leg.
    vehicle = models.ForeignKey(
        "rates.Vehicle",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="legs",
        help_text="Override vehicle for this leg. NULL = use reservation vehicle.",
    )
    leg_rate = models.ForeignKey(
        "rates.Rate",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="legs",
        help_text="Rate for this leg's vehicle+route. NULL = use reservation rate.",
    )
    leg_base_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Base price for this leg. NULL = equal split of reservation base_price.",
    )
    passenger_count = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Override passenger count. NULL = use reservation value.",
    )
    luggage_count = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Override luggage count. NULL = use reservation value.",
    )
    luggage_type = models.CharField(
        max_length=10,
        blank=True,
        default="",
        help_text="Override luggage type. Empty = use reservation value.",
    )
    need_carseats = models.BooleanField(
        null=True,
        blank=True,
        help_text="Override car seat requirement. NULL = use reservation value.",
    )
    rf_carseats = models.PositiveIntegerField(
        "RF-Seat",
        null=True,
        blank=True,
        help_text="Override rear-facing car seats. NULL = use reservation value.",
    )
    ff_carseats = models.PositiveIntegerField(
        "FF-Seat",
        null=True,
        blank=True,
        help_text="Override forward-facing car seats. NULL = use reservation value.",
    )
    booster_seats = models.PositiveIntegerField(
        "Booster",
        null=True,
        blank=True,
        help_text="Override booster seats. NULL = use reservation value.",
    )
    extra_carseats = models.PositiveIntegerField(
        "Extra Car Seats",
        null=True,
        blank=True,
        help_text="Override extra car seats. NULL = use reservation value.",
    )
    extra_boosters = models.PositiveIntegerField(
        "Extra Boosters",
        null=True,
        blank=True,
        help_text="Override extra boosters. NULL = use reservation value.",
    )

    flight_information = models.OneToOneField(
        "Flight", on_delete=models.CASCADE, null=True, blank=True
    )
    cruise_information = models.OneToOneField(
        "Cruise", on_delete=models.CASCADE, null=True, blank=True
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
        help_text="Amount to pay the driver (set by admin) - DEPRECATED: Use driver_base_pay + driver_gratuity",
    )
    driver_base_pay = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Base pay amount for the driver (excluding gratuity)",
    )
    driver_gratuity = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Gratuity amount for the driver",
    )
    driver_additional = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Additional pay amount (e.g., wait time, early morning bonus, etc.)",
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

    # Audit fields - track driver assignments and status changes
    driver_assigned_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="legs_driver_assigned",
        help_text="User who last assigned/changed the driver",
    )
    driver_assigned_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when driver was last assigned/changed",
    )
    status_changed_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="legs_status_changed",
        help_text="User who last changed the leg status",
    )
    status_changed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when leg status was last changed",
    )
    confirmation_sms_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When next-day confirmation SMS was sent (Twilio)",
    )
    confirmation_sms_override = models.TextField(
        blank=True,
        default="",
        help_text="Custom confirmation SMS body for this leg. When set, it replaces the auto-generated message body. The standard footer is still appended.",
    )
    exclude_from_analytics = models.BooleanField(
        default=False,
        help_text="Exclude this leg from route timing analytics (bad data)",
    )

    # ── Effective-value resolution properties ──
    # These check leg-level overrides first, then fall back to reservation.
    # Use these everywhere instead of accessing reservation fields directly.

    @property
    def effective_vehicle(self):
        """Return this leg's vehicle override, or fall back to reservation vehicle."""
        return self.vehicle if self.vehicle_id else (
            self.reservation.vehicle if self.reservation_id else None
        )

    @property
    def effective_vehicle_type(self):
        """Return vehicle_type string for scheduler/template use."""
        v = self.effective_vehicle
        return v.vehicle_type if v else None

    @property
    def effective_rate(self):
        """Return this leg's rate override, or fall back to reservation rate."""
        return self.leg_rate if self.leg_rate_id else (
            self.reservation.rate if self.reservation_id else None
        )

    @property
    def effective_passenger_count(self):
        if self.passenger_count is not None:
            return self.passenger_count
        return self.reservation.passenger_count if self.reservation_id else 1

    @property
    def effective_luggage_count(self):
        if self.luggage_count is not None:
            return self.luggage_count
        return self.reservation.luggage_count if self.reservation_id else 1

    @property
    def effective_luggage_type(self):
        if self.luggage_type:
            return self.luggage_type
        return self.reservation.luggage_type if self.reservation_id else ""

    @property
    def effective_need_carseats(self):
        if self.need_carseats is not None:
            return self.need_carseats
        return self.reservation.need_carseats if self.reservation_id else False

    @property
    def effective_rf_carseats(self):
        if self.rf_carseats is not None:
            return self.rf_carseats
        return self.reservation.rf_carseats if self.reservation_id else 0

    @property
    def effective_ff_carseats(self):
        if self.ff_carseats is not None:
            return self.ff_carseats
        return self.reservation.ff_carseats if self.reservation_id else 0

    @property
    def effective_booster_seats(self):
        if self.booster_seats is not None:
            return self.booster_seats
        return self.reservation.booster_seats if self.reservation_id else 0

    @property
    def effective_extra_carseats(self):
        if self.extra_carseats is not None:
            return self.extra_carseats
        return self.reservation.extra_carseats if self.reservation_id else 0

    @property
    def effective_extra_boosters(self):
        if self.extra_boosters is not None:
            return self.extra_boosters
        return self.reservation.extra_boosters if self.reservation_id else 0

    @property
    def has_overrides(self):
        """Return True if this leg has any trip-detail overrides."""
        return (
            self.vehicle_id is not None
            or self.passenger_count is not None
            or self.luggage_count is not None
            or bool(self.luggage_type)
            or self.need_carseats is not None
            or self.rf_carseats is not None
            or self.ff_carseats is not None
            or self.booster_seats is not None
            or self.extra_carseats is not None
            or self.extra_boosters is not None
        )

    def auto_set_rate(self):
        """
        Look up the Rate for this leg's vehicle+route combination and set
        leg_rate and leg_base_price. Called when a leg-level vehicle override
        is set. Returns True if a rate was found, False otherwise.
        """
        from rates.models import Rate

        if not self.vehicle_id or not self.route_id:
            return False

        rate = Rate.objects.filter(
            vehicle=self.vehicle, route=self.route
        ).first()
        if rate:
            self.leg_rate = rate
            self.leg_base_price = rate.oneway_price
            return True
        return False

    def calculate_revenue_share(self):
        """
        Calculate this leg's portion of the reservation total price.
        """
        if not self.reservation:
            return Decimal("0.00")

        total_price = self.reservation.total_price
        if not total_price:
            return Decimal("0.00")

        # Get total number of legs in this reservation
        total_legs = self.reservation.legs.count()

        if total_legs == 0:  # Safety check
            return Decimal("0.00")

        # Calculate leg's share of revenue (total price divided by number of legs)
        revenue_share = total_price / Decimal(total_legs)

        # Round to 2 decimal places
        return revenue_share.quantize(Decimal("0.01"))

    @cached_property
    def total_driver_pay(self):
        """
        Calculate total driver pay from base_pay + gratuity + additional, or fallback to driver_pay_amount
        """
        if self.driver_base_pay is not None or self.driver_gratuity is not None or self.driver_additional is not None:
            base = self.driver_base_pay or Decimal("0.00")
            gratuity = self.driver_gratuity or Decimal("0.00")
            additional = self.driver_additional or Decimal("0.00")
            return (base + gratuity + additional).quantize(Decimal("0.01"))
        # Fallback to legacy field
        return self.driver_pay_amount or Decimal("0.00")

    def calculate_profit(self):
        """
        Calculate profit (leg's revenue share minus driver payment)
        """
        revenue = self.revenue_share if self.revenue_share is not None else self.calculate_revenue_share()
        driver_payment = self.total_driver_pay
        if revenue is None:
            return None

        return (revenue - driver_payment).quantize(Decimal("0.01"))

    def _match_location(self, text, locations):
        if not text:
            return None
        text_lower = text.lower()
        best_match = None
        best_length = 0

        for location in locations:
            candidates = []
            if location.name:
                candidates.append(location.name)
            if location.aliases:
                candidates.extend(
                    alias.strip()
                    for alias in location.aliases.split(",")
                    if alias.strip()
                )

            for candidate in candidates:
                candidate_lower = candidate.lower()
                if candidate_lower in text_lower:
                    candidate_length = len(candidate_lower)
                    if candidate_length > best_length:
                        best_length = candidate_length
                        best_match = location

        return best_match

    def _assign_route_from_locations(self):
        if self.route:
            return

        # 1. Try text matching from actual pickup/dropoff locations (most accurate)
        if self.pickup_location and self.dropoff_location:
            locations = list(Location.objects.all())
            origin = self._match_location(self.pickup_location, locations)
            destination = self._match_location(self.dropoff_location, locations)
            if origin and destination:
                route = Route.objects.filter(origin=origin, destination=destination).first()
                if not route:
                    # Check reverse direction (return trips match origin↔destination)
                    route = Route.objects.filter(origin=destination, destination=origin).first()
                if route:
                    self.route = route
                    return

        # 2. Fall back to reservation's rate route (may be a default from booking)
        if self.reservation_id:
            try:
                res = self.reservation
                if res and res.rate_id and res.rate.route_id:
                    self.route = res.rate.route
                    return
            except Exception:
                pass

    # Fields that require expensive calculations (route, pay, profit).
    # When save(update_fields=...) is called with only fields NOT in this set,
    # we skip all the expensive work (route lookup, pay calc, profit calc).
    _EXPENSIVE_FIELDS = frozenset({
        'driver', 'route', 'revenue_share', 'pickup_location', 'dropoff_location',
        'driver_base_pay', 'driver_gratuity', 'driver_additional',
        'driver_pay_amount', 'profit_estimate',
    })

    def save(self, *args, **kwargs):
        # PERF TEMP START
        import time as _time; _t0 = _time.monotonic()
        # PERF TEMP END

        # Fast path: if update_fields is specified and contains only simple
        # fields (e.g. driver assignment, status), skip expensive calculations.
        _uf = kwargs.get('update_fields')
        if _uf is not None and not (set(_uf) & self._EXPENSIVE_FIELDS):
            super().save(*args, **kwargs)
            # PERF TEMP START
            import logging as _logging
            _logging.getLogger('perf').debug("Leg.save FAST path: %.1fms (fields=%s)", (_time.monotonic()-_t0)*1000, _uf)
            # PERF TEMP END
            return

        # Attempt to match a route from pickup/dropoff when not set
        self._assign_route_from_locations()

        # Calculate and store revenue share if not set
        if self.revenue_share is None:
            self.revenue_share = self.calculate_revenue_share()

        # Clear pay fields when driver changes (recalculate for new driver)
        if (
            self.pk
            and hasattr(self, '_original_driver_id')
            and self._original_driver_id is not None
            and self._original_driver_id != self.driver_id
        ):
            self.driver_base_pay = None
            self.driver_gratuity = None
            self.driver_additional = None
            self.driver_pay_amount = None

        # Auto-fill driver pay when not set (inhouse and affiliate)
        if (
            self.driver_base_pay is None
            and self.driver_gratuity is None
            and self.driver_additional is None
            and self.driver_pay_amount is None
            and self.driver
        ):
            from drivers.pay_calc import calculate_driver_pay, calculate_night_bonus

            base_pay = calculate_driver_pay(self)

            if base_pay is not None:
                # Gratuity split — divide customer gratuity across all legs
                gratuity_share = Decimal("0.00")
                reservation = self.reservation
                if reservation:
                    gratuity_amount = reservation.gratuity_amount
                    if (
                        gratuity_amount is None
                        and reservation.gratuity_percentage
                        and reservation.base_price
                    ):
                        gratuity_amount = (
                            reservation.base_price
                            * reservation.gratuity_percentage
                            / Decimal("100")
                        )
                    if gratuity_amount:
                        leg_count = reservation.legs.count()
                        if self.pk is None:
                            leg_count += 1
                        if leg_count <= 0:
                            leg_count = 1
                        gratuity_share = (gratuity_amount / Decimal(leg_count)).quantize(
                            Decimal("0.01")
                        )

                # Set base pay and gratuity separately
                self.driver_base_pay = base_pay.quantize(Decimal("0.01"))
                self.driver_gratuity = gratuity_share.quantize(Decimal("0.01"))

                # Night pickup bonus goes in additional (early/late fee)
                night_bonus = calculate_night_bonus(self.driver, self.pickup_time)
                additional = self.driver_additional or Decimal("0.00")
                if night_bonus > 0:
                    additional = (additional + night_bonus).quantize(Decimal("0.01"))
                    self.driver_additional = additional
                self.driver_pay_amount = (self.driver_base_pay + self.driver_gratuity + additional).quantize(
                    Decimal("0.01")
                )

        # Calculate and store profit estimate if driver payment is set
        if self.driver_base_pay is not None or self.driver_gratuity is not None or self.driver_additional is not None or self.driver_pay_amount is not None:
            self.profit_estimate = self.calculate_profit()

        # When update_fields is specified but save() auto-filled pay or cleared
        # pay due to driver change, expand update_fields so those values persist.
        if _uf is not None:
            _pay_fields = {
                'driver_base_pay', 'driver_gratuity', 'driver_additional',
                'driver_pay_amount', 'profit_estimate', 'route', 'revenue_share',
            }
            _uf_set = set(_uf)
            if not _uf_set.issuperset(_pay_fields):
                _uf_set |= _pay_fields
                kwargs['update_fields'] = list(_uf_set)

        super().save(*args, **kwargs)
        # Keep _original_driver_id in sync so subsequent saves on the same
        # instance don't re-trigger the driver-change clear.
        self._original_driver_id = self.driver_id
        # PERF TEMP START
        import logging as _logging
        _logging.getLogger('perf').debug("Leg.save FULL path: %.1fms (leg #%s)", (_time.monotonic()-_t0)*1000, self.pk)
        # PERF TEMP END

    history = HistoricalRecords()

    def __str__(self):
        """
        Returns a string identifying the leg by pickup and dropoff locations.
        """
        return f"Leg #{self.id} from {self.pickup_location} to {self.dropoff_location}"

    CRUISE_PORT_KEYWORDS = [
        "port canaveral", "canaveral", "cruise port", "cruise terminal",
        "cruise termina", "cruise ship", "port canaveral terminal",
    ]
    AIRPORT_KEYWORDS = ["mco", "sfb", "mlb", "lal", "international airport"]
    # Hotel brands / keywords — if present, the location is a hotel, not an airport
    HOTEL_INDICATORS = [
        "hotel", "inn", "resort", "suites", "suite", "lodge", "motel",
        "hyatt", "hilton", "marriott", "sheraton", "westin", "doubletree",
        "hampton", "fairfield", "courtyard", "comfort", "holiday",
        "radisson", "wyndham", "embassy", "omni", "ritz", "waldorf",
        "four seasons", "loews", "renaissance", "springhill", "aloft",
        "best western", "la quinta", "homewood", "residence",
    ]

    def get_trip_type(self):
        """
        Determine trip type from pickup/dropoff locations.
        Returns: 'arrival', 'return', 'cruise', or 'other'
        """
        pickup_lower = (self.pickup_location or "").lower()
        dropoff_lower = (self.dropoff_location or "").lower()

        pickup_is_cruise = any(kw in pickup_lower for kw in self.CRUISE_PORT_KEYWORDS)
        dropoff_is_cruise = any(kw in dropoff_lower for kw in self.CRUISE_PORT_KEYWORDS)

        # Any leg involving a cruise port is "cruise"
        if pickup_is_cruise or dropoff_is_cruise:
            return "cruise"

        pickup_is_airport = self._is_airport(pickup_lower)
        dropoff_is_airport = self._is_airport(dropoff_lower)

        if pickup_is_airport and not dropoff_is_airport:
            return "arrival"
        elif dropoff_is_airport and not pickup_is_airport:
            return "return"
        else:
            return "other"

    def get_cruise_direction(self):
        """
        For cruise legs, determine the direction:
          'to_cruise'   — Airport/Hotel → Cruise Port (pickup at airport or hotel)
          'from_cruise' — Cruise Port → Hotel/Airport (pickup at cruise terminal)
        Returns None if not a cruise leg.
        """
        if self.get_trip_type() != "cruise":
            return None

        pickup_lower = (self.pickup_location or "").lower()
        pickup_is_cruise = any(kw in pickup_lower for kw in self.CRUISE_PORT_KEYWORDS)
        return "from_cruise" if pickup_is_cruise else "to_cruise"

    def is_airport_pickup(self):
        """True if this leg's pickup is at an airport (for cruise legs going airport→cruise port)."""
        pickup_lower = (self.pickup_location or "").lower()
        return self._is_airport(pickup_lower)

    def _is_airport(self, location_lower):
        if not location_lower:
            return False
        if any(kw in location_lower for kw in self.CRUISE_PORT_KEYWORDS):
            return False
        if any(kw in location_lower for kw in self.HOTEL_INDICATORS):
            return False
        return any(kw in location_lower for kw in self.AIRPORT_KEYWORDS)

    def has_flight_time_mismatch(self, threshold_minutes=30):
        """
        For arrival legs with flight info: True if the flight's best available
        arrival time differs from the leg's pickup time by at least threshold_minutes.

        Uses best available time (actual/estimated if available, otherwise scheduled)
        so delayed flights don't incorrectly flag legs that have been updated to
        match the new arrival time.
        """
        if self.get_trip_type() != "arrival" or not self.flight_information:
            return False
        flight = self.flight_information

        # Use best available arrival time - same priority as flight refresh
        # Priority: actual gate > estimated gate > actual runway > estimated runway > scheduled gate > scheduled runway
        flight_dt = (
            flight.actual_gate_arrival_local
            or flight.estimated_gate_arrival_local
            or flight.actual_arrival_local
            or flight.estimated_arrival_local
            or flight.scheduled_gate_arrival_local
            or flight.scheduled_arrival_local
        )

        if not flight_dt:
            return False
        leg_dt = datetime.combine(self.pickup_date, self.pickup_time)
        if timezone.is_aware(flight_dt):
            flight_dt = timezone.make_naive(flight_dt, timezone.get_current_timezone())
        delta = abs(flight_dt - leg_dt)
        return delta >= timedelta(minutes=threshold_minutes)

    def get_flight_time_mismatch_display(self, threshold_minutes=30):
        """
        For arrival legs with flight info: if best available flight arrival time
        differs from leg pickup by at least threshold_minutes, return a dict with
        direction ('early'|'late'), minutes (int), and label.

        Uses best available time (actual/estimated if available, otherwise scheduled)
        so that delayed flights don't incorrectly flag legs that have been updated
        to match the new arrival time.
        """
        if self.get_trip_type() != "arrival" or not self.flight_information:
            return None
        flight = self.flight_information

        # Use best available arrival time - same priority as flight refresh
        # Priority: actual gate > estimated gate > actual runway > estimated runway > scheduled gate > scheduled runway
        flight_dt = (
            flight.actual_gate_arrival_local
            or flight.estimated_gate_arrival_local
            or flight.actual_arrival_local
            or flight.estimated_arrival_local
            or flight.scheduled_gate_arrival_local
            or flight.scheduled_arrival_local
        )

        if not flight_dt:
            return None
        leg_dt = datetime.combine(self.pickup_date, self.pickup_time)
        if timezone.is_aware(flight_dt):
            flight_dt = timezone.make_naive(flight_dt, timezone.get_current_timezone())
        delta = flight_dt - leg_dt
        total_seconds = int(delta.total_seconds())
        total_minutes = abs(total_seconds) // 60
        if total_minutes < threshold_minutes:
            return None
        if total_seconds >= 0:
            direction = "late"
        else:
            direction = "early"
        if total_minutes >= 60:
            hours, mins = divmod(total_minutes, 60)
            if mins:
                time_str = f"{hours} hr {mins} min"
            else:
                time_str = f"{hours} hr"
        else:
            time_str = f"{total_minutes} min"
        label = f"Coming {time_str} {'late' if direction == 'late' else 'early'}"
        return {"direction": direction, "minutes": total_minutes, "label": label}

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
        elif trip_type == "cruise":
            return {
                "type": "cruise",
                "label": "Cruise Transfer",
                "icon": "bi-ship",
                "color": "info",
                "description": "Cruise port transfer",
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
            models.Index(fields=["pickup_date", "pickup_time"]),
            models.Index(fields=["driver"]),
            models.Index(fields=["status"]),
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
    airline = models.CharField(max_length=50, blank=True, help_text="IATA code (e.g., DL, WN, B6) for API calls")
    airline_display_name = models.CharField(
        max_length=100, blank=True,
        help_text="Full airline name for display (e.g., Delta Airlines, Southwest Airlines)"
    )
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
    actual_arrival_local = models.DateTimeField(
        null=True, blank=True,
        help_text="Actual runway arrival time in local timezone (what actually happened)"
    )
    actual_gate_arrival_local = models.DateTimeField(
        null=True, blank=True,
        help_text="Actual gate arrival time in local timezone (what actually happened)"
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
        Override save to normalize airline and flight_number fields before saving.
        This ensures consistent airline formatting regardless of how it's entered.
        Also extracts airline codes from flight numbers if present.
        """
        # Import here to avoid circular import
        from .utils import normalize_airline, normalize_flight_number, extract_airline_from_flight_number, get_airline_display_name
        
        # Normalize airline field first
        original_airline = self.airline
        if self.airline:
            self.airline = normalize_airline(self.airline)
        
        # Update display name whenever airline code changes
        # Check if airline was changed (compare normalized versions)
        if self.airline:
            new_display_name = get_airline_display_name(self.airline)
            # Update display name if:
            # 1. It's not set yet, OR
            # 2. The airline code has changed (normalized airline doesn't match current display name's code)
            if not self.airline_display_name or new_display_name != self.airline_display_name:
                # Only update if we got a proper display name (not just the code itself)
                if new_display_name != self.airline:
                    self.airline_display_name = new_display_name
                elif not self.airline_display_name:
                    # If display name lookup returned the code (unknown airline), set it anyway if empty
                    self.airline_display_name = new_display_name
        
        # Handle flight_number
        if self.flight_number:
            # If airline is already set, check if flight_number starts with that airline code
            if self.airline and len(self.airline) == 2:
                # Check if flight_number starts with the airline code
                flight_upper = str(self.flight_number).strip().upper()
                if flight_upper.startswith(self.airline):
                    # Remove the airline code prefix from flight_number
                    self.flight_number = flight_upper[len(self.airline):]
            
            # If airline is empty, try to extract it from flight_number
            if not self.airline or self.airline.strip() == "":
                extracted_airline = extract_airline_from_flight_number(self.flight_number)
                if extracted_airline:
                    self.airline = extracted_airline
                    # Set display name when extracting airline
                    if not self.airline_display_name:
                        self.airline_display_name = get_airline_display_name(self.airline)
                    # Remove the airline code from flight_number
                    flight_upper = str(self.flight_number).strip().upper()
                    if flight_upper.startswith(extracted_airline):
                        self.flight_number = flight_upper[len(extracted_airline):]
            
            # Clean the flight number (remove all letters, keep only digits)
            self.flight_number = normalize_flight_number(self.flight_number)
        
        super().save(*args, **kwargs)

    def __str__(self):
        """
        Display flight type (e.g., 'Arrival' or 'Departure'), airline,
        and flight number for quick reference.
        Uses display name if available, falls back to IATA code.
        """
        airline_display = self.airline_display_name or self.airline or ""
        return f"{airline_display} {self.flight_number}".strip()
    
    def get_flight_ident(self):
        """
        Get the flight identifier for AeroAPI (combines airline and flight number)
        Returns FlightAware format like 'DL1691' or 'JBU123' (converts IATA to FlightAware codes)
        
        Prioritizes current airline/flight_number over stored flight_iata to ensure
        we use the most up-to-date flight information.
        """
        # Prioritize current airline/flight_number over stored flight_iata
        # This ensures we use the updated flight info if the user changed it
        if self.airline and self.flight_number:
            # Import here to avoid circular import
            from .utils import normalize_airline, get_flightaware_code
            # Normalize airline to IATA code (already normalized in save, but double-check)
            iata_code = normalize_airline(self.airline)
            # Convert IATA code to FlightAware code for API calls
            flightaware_code = get_flightaware_code(iata_code)
            # Remove non-alphanumeric from flight number
            flight_num = ''.join(c for c in self.flight_number if c.isalnum())
            return f"{flightaware_code}{flight_num}"
        
        # Fallback to stored flight_iata if airline/flight_number not available
        if self.flight_iata:
            return self.flight_iata
        
        return None


class Cruise(models.Model):
    """
    Stores specific cruise details, including cruise line and ship name.
    Ties into a Leg model via a OneToOneField.
    Similar to Flight model but for cruise information.
    """

    cruise_line = models.CharField(
        max_length=100, 
        blank=True,
        help_text="Cruise line name (e.g., Disney Cruise Line, Royal Caribbean, Carnival)"
    )
    ship_name = models.CharField(
        max_length=100, 
        blank=True,
        help_text="Ship name (e.g., Disney Wish, Wonder of the Seas, Mardi Gras)"
    )

    def __str__(self):
        """
        Display cruise line and ship name for quick reference.
        """
        if self.cruise_line and self.ship_name:
            return f"{self.cruise_line} - {self.ship_name}"
        elif self.cruise_line:
            return self.cruise_line
        elif self.ship_name:
            return self.ship_name
        return "Cruise Information"


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
        COLD = "cold", "Cold"

    class SegmentChoices(models.TextChoices):
        GENERAL = "general", "General"
        AIRPORT_TRANSFER = "airport_transfer", "Airport Transfer"
        CRUISE_TRANSFER = "cruise_transfer", "Cruise Transfer"
        THEME_PARK = "theme_park", "Theme Park"
        LARGE_GROUP = "large_group", "Large Group"
        REPEAT_CUSTOMER = "repeat_customer", "Repeat Customer"
        ABANDONED_QUOTE = "abandoned_quote", "Abandoned Quote"

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
    normalized_phone = models.CharField(
        max_length=10, blank=True, db_index=True,
        help_text="Last 10 digits of phone (auto-populated on save). Used for fast dedup lookups.",
    )

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

    # GoHighLevel Integration Fields
    ghl_contact_id = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    ghl_synced_at = models.DateTimeField(null=True, blank=True)
    initial_sms_sent = models.BooleanField(default=False)
    initial_sms_sent_at = models.DateTimeField(null=True, blank=True)
    initial_email_sent = models.BooleanField(default=False, help_text="Fallback email sent when SMS failed")
    initial_email_sent_at = models.DateTimeField(null=True, blank=True)
    last_reply_at = models.DateTimeField(null=True, blank=True)
    has_replied = models.BooleanField(default=False)

    # UTM Parameters for Lead Source Tracking
    gclid = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Google Click ID for conversion tracking",
        db_index=True,
    )
    fbclid = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Facebook Click ID for conversion tracking",
        db_index=True,
    )
    utm_source = models.CharField(
        max_length=100, blank=True, null=True, help_text="UTM source parameter", db_index=True
    )
    utm_medium = models.CharField(
        max_length=100, blank=True, null=True, help_text="UTM medium parameter", db_index=True
    )
    utm_campaign = models.CharField(
        max_length=100, blank=True, null=True, help_text="UTM campaign parameter", db_index=True
    )
    utm_term = models.CharField(
        max_length=100, blank=True, null=True, help_text="UTM term parameter"
    )
    utm_content = models.CharField(
        max_length=100, blank=True, null=True, help_text="UTM content parameter"
    )

    # Follow-Up Automation Fields
    segment = models.CharField(
        max_length=30, choices=SegmentChoices.choices, default=SegmentChoices.GENERAL, blank=True
    )
    sequence_active = models.BooleanField(default=False, help_text="Whether follow-up automation is running")
    sequence_completed_at = models.DateTimeField(null=True, blank=True)
    needs_human_follow_up = models.BooleanField(default=False, help_text="Flagged for human closer after lead replied")

    # Revenue Attribution
    converted_reservation = models.ForeignKey(
        'Reservation', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='converted_leads', help_text="Reservation created from this lead"
    )

    # GHL Pipeline
    ghl_opportunity_id = models.CharField(max_length=100, blank=True, null=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Lead"
        verbose_name_plural = "Leads"
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["pickup_date"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["status", "pickup_date"]),
        ]

    @staticmethod
    def normalize_phone(raw):
        """Extract last 10 digits from a phone string."""
        if not raw:
            return ""
        digits = "".join(filter(str.isdigit, raw))
        return digits[-10:] if len(digits) >= 10 else ""

    def save(self, *args, **kwargs):
        self.normalized_phone = self.normalize_phone(self.phone)
        super().save(*args, **kwargs)

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


class BlockedTimeSlot(models.Model):
    """
    Blocks specific time windows from online reservations.
    Used to prevent overbooking when the company is fully booked or unavailable.
    """
    date = models.DateField(
        help_text="Date when the time slot is blocked"
    )
    start_time = models.TimeField(
        help_text="Start time of the blocked window (e.g., 12:00 AM)"
    )
    end_time = models.TimeField(
        help_text="End time of the blocked window (e.g., 8:00 AM)"
    )
    reason = models.CharField(
        max_length=255,
        blank=True,
        help_text="Optional reason for blocking (e.g., 'Fully booked', 'Maintenance')"
    )
    notes = models.TextField(
        blank=True,
        help_text="Additional notes about this blocked time slot"
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Uncheck to temporarily disable this block without deleting it"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_blocked_time_slots",
        help_text="User who created this blocked time slot"
    )

    class Meta:
        ordering = ["date", "start_time"]
        indexes = [
            models.Index(fields=["date", "is_active"]),
        ]
        verbose_name = "Blocked Time Slot"
        verbose_name_plural = "Blocked Time Slots"

    def __str__(self):
        date_str = self.date.strftime("%Y-%m-%d")
        time_str = f"{self.start_time.strftime('%I:%M %p')} - {self.end_time.strftime('%I:%M %p')}"
        reason_str = f" ({self.reason})" if self.reason else ""
        status = "Active" if self.is_active else "Inactive"
        return f"{date_str} {time_str}{reason_str} [{status}]"

    def is_time_blocked(self, check_date, check_time):
        """
        Check if a specific date and time falls within this blocked slot.
        
        Args:
            check_date: Date to check
            check_time: Time to check
            
        Returns:
            bool: True if the time is blocked, False otherwise
        """
        if not self.is_active:
            return False
            
        if check_date != self.date:
            return False
        
        # Handle time ranges that span midnight (e.g., 10 PM to 2 AM)
        if self.start_time <= self.end_time:
            # Normal case: start_time < end_time (e.g., 12:00 AM to 8:00 AM)
            return self.start_time <= check_time < self.end_time
        else:
            # Spans midnight: start_time > end_time (e.g., 10:00 PM to 2:00 AM)
            return check_time >= self.start_time or check_time < self.end_time

    @classmethod
    def is_time_slot_available(cls, check_date, check_time):
        """
        Check if a time slot is available (not blocked).
        
        Args:
            check_date: Date to check
            check_time: Time to check
            
        Returns:
            tuple: (is_available: bool, blocked_slot: BlockedTimeSlot or None)
        """
        blocked_slots = cls.objects.filter(
            date=check_date,
            is_active=True
        )
        
        for slot in blocked_slots:
            if slot.is_time_blocked(check_date, check_time):
                return False, slot
        
        return True, None


class AuditLog(models.Model):
    """
    Comprehensive audit log for tracking all changes to important models.
    Provides full history for compliance, debugging, and accountability.
    """
    
    ACTION_CHOICES = [
        ('created', 'Created'),
        ('updated', 'Updated'),
        ('deleted', 'Deleted'),
        ('driver_assigned', 'Driver Assigned'),
        ('driver_unassigned', 'Driver Unassigned'),
        ('status_changed', 'Status Changed'),
        ('payment_processed', 'Payment Processed'),
        ('commission_processed', 'Commission Processed'),
    ]
    
    # What was changed
    model_name = models.CharField(
        max_length=100,
        help_text="Name of the model that was changed (e.g., 'Reservation', 'Leg')"
    )
    object_id = models.PositiveIntegerField(
        help_text="ID of the object that was changed"
    )
    action = models.CharField(
        max_length=50,
        choices=ACTION_CHOICES,
        help_text="Type of action performed"
    )
    
    # What field changed (if applicable)
    field_name = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="Name of the field that changed (if specific field change)"
    )
    old_value = models.TextField(
        null=True,
        blank=True,
        help_text="Previous value (before change)"
    )
    new_value = models.TextField(
        null=True,
        blank=True,
        help_text="New value (after change)"
    )
    
    # Who made the change
    user = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
        help_text="User who made the change"
    )
    username = models.CharField(
        max_length=150,
        null=True,
        blank=True,
        help_text="Username at time of change (for historical reference)"
    )
    
    # When it happened
    timestamp = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        help_text="When the change occurred"
    )
    
    # Additional context
    ip_address = models.GenericIPAddressField(
        null=True,
        blank=True,
        help_text="IP address of the user who made the change"
    )
    user_agent = models.TextField(
        null=True,
        blank=True,
        help_text="User agent string from the request"
    )
    notes = models.TextField(
        null=True,
        blank=True,
        help_text="Additional notes or context about the change"
    )
    
    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['model_name', 'object_id']),
            models.Index(fields=['user', '-timestamp']),
            models.Index(fields=['action', '-timestamp']),
            models.Index(fields=['-timestamp']),
        ]
        verbose_name = "Audit Log"
        verbose_name_plural = "Audit Logs"
    
    def __str__(self):
        user_str = self.username or (self.user.username if self.user else "System")
        return f"{self.action} {self.model_name}#{self.object_id} by {user_str} at {self.timestamp}"


class LegStatus(models.Model):
    """
    Tracks status changes for legs with timestamps.
    Provides a history of when drivers accept jobs, arrive on location, pick up passengers, etc.
    """

    STATUS_CHOICES = [
        ('assigned', 'Job Assigned'),
        ('in-progress', 'In Progress'),
        ('confirmed', 'Confirmed'),
        ('on-the-way', 'On the Way'),
        ('on-location', 'On Location'),
        ('picked-up', 'Picked Up'),
        ('completed', 'Completed'),
    ]

    leg = models.ForeignKey(
        'Leg',
        on_delete=models.CASCADE,
        related_name='status_history',
        help_text="The leg this status update belongs to"
    )
    status = models.CharField(
        max_length=30,
        choices=STATUS_CHOICES,
        help_text="The status at this point in time"
    )
    timestamp = models.DateTimeField(
        default=timezone.now,
        db_index=True,
        help_text="When this status was set"
    )
    updated_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="leg_status_updates",
        help_text="User who updated the status (admin, driver, or system)"
    )
    notes = models.TextField(
        null=True,
        blank=True,
        help_text="Optional notes about this status change"
    )

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['leg', '-timestamp']),
            models.Index(fields=['-timestamp']),
        ]
        verbose_name = "Leg Status Update"
        verbose_name_plural = "Leg Status Updates"

    def __str__(self):
        return f"Leg #{self.leg.id} - {self.get_status_display()} at {self.timestamp}"


class DriverLocation(models.Model):
    """
    GPS snapshot captured when a driver changes status (on-the-way, on-location, picked-up).
    Used to compute live ETA from the driver's position to pickup/dropoff.
    """
    leg = models.ForeignKey(
        'Leg', on_delete=models.CASCADE, related_name='driver_locations',
    )
    driver = models.ForeignKey(
        'drivers.Driver', on_delete=models.CASCADE, related_name='location_history',
    )
    status = models.CharField(
        max_length=30, blank=True,
        help_text="The status when this location was captured",
    )
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    accuracy_meters = models.FloatField(null=True, blank=True)
    heading = models.FloatField(null=True, blank=True)
    speed_mps = models.FloatField(null=True, blank=True, help_text="Speed in meters/second")
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    # ETA computed server-side via Google Maps Distance Matrix
    eta_minutes = models.IntegerField(
        null=True, blank=True,
        help_text="Estimated minutes to destination (computed via Google Maps)",
    )
    eta_destination = models.CharField(
        max_length=255, blank=True,
        help_text="The destination address used for ETA calculation",
    )

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['leg', '-timestamp']),
            models.Index(fields=['driver', '-timestamp']),
        ]

    def __str__(self):
        return f"Driver {self.driver_id} @ ({self.latitude},{self.longitude}) - {self.status} - {self.timestamp}"


class ScheduleSnapshot(models.Model):
    """A saved snapshot of driver assignments for a specific date."""
    TRIGGER_CHOICES = [
        ('manual', 'Manual Save'),
        ('before_reset', 'Auto-save Before Reset'),
        ('before_auto_assign', 'Auto-save Before Auto-Assign'),
    ]

    schedule_date = models.DateField(help_text="The date whose schedule was saved")
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="schedule_snapshots",
    )
    trigger = models.CharField(max_length=30, choices=TRIGGER_CHOICES, default='manual')
    label = models.CharField(max_length=100, blank=True, default='')
    notes = models.TextField(blank=True, default='', help_text="Optional notes about why this snapshot was saved")
    assigned_count = models.IntegerField(default=0, help_text="Number of legs with drivers at snapshot time")

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['schedule_date', '-created_at'])]

    def __str__(self):
        return f"Snapshot for {self.schedule_date} ({self.get_trigger_display()}) - {self.assigned_count} legs"


class ScheduleSnapshotEntry(models.Model):
    """One leg's driver assignment within a snapshot."""
    snapshot = models.ForeignKey(
        ScheduleSnapshot, on_delete=models.CASCADE, related_name='entries',
    )
    leg = models.ForeignKey('Leg', on_delete=models.CASCADE, related_name='snapshot_entries')
    driver = models.ForeignKey(
        'drivers.Driver', on_delete=models.SET_NULL, null=True,
    )
    driver_assigned_by = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True,
    )
    driver_assigned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=['snapshot', 'leg'])]

    def __str__(self):
        return f"Snapshot entry: Leg #{self.leg_id} → Driver {self.driver_id}"


class RouteTimingMetric(models.Model):
    """
    Stores calculated average timing metrics for specific route patterns.
    Updated periodically (daily/weekly) via management command.
    Used for scheduling optimization and capacity planning.
    """

    TRIP_TYPE_CHOICES = [
        ('arrival', 'Arrival (Airport → Destination)'),
        ('return', 'Return (Destination → Airport)'),
        ('cruise', 'Cruise Transfer'),
        ('other', 'Other'),
    ]

    TIME_OF_DAY_CHOICES = [
        ('early_morning', 'Early Morning (4-7 AM)'),
        ('morning_rush', 'Morning Rush (7-10 AM)'),
        ('midday', 'Midday (10 AM - 2 PM)'),
        ('afternoon', 'Afternoon (2-6 PM)'),
        ('evening', 'Evening (6-10 PM)'),
        ('night', 'Night (10 PM - 4 AM)'),
    ]

    DAY_TYPE_CHOICES = [
        ('weekday', 'Weekday'),
        ('weekend', 'Weekend'),
        ('holiday', 'Holiday'),
    ]

    # Route identification
    trip_type = models.CharField(
        max_length=20,
        choices=TRIP_TYPE_CHOICES,
        help_text="Type of trip (arrival, return, cruise, other)"
    )
    pickup_location_category = models.CharField(
        max_length=100,
        help_text="Categorized pickup location (e.g., 'MCO', 'Disney Resort', 'Universal Resort')"
    )
    dropoff_location_category = models.CharField(
        max_length=100,
        help_text="Categorized dropoff location"
    )

    # Time-based segmentation
    time_of_day_category = models.CharField(
        max_length=20,
        choices=TIME_OF_DAY_CHOICES,
        help_text="Time of day category for this metric"
    )
    day_type = models.CharField(
        max_length=20,
        choices=DAY_TYPE_CHOICES,
        help_text="Day type (weekday, weekend, holiday)"
    )

    # Calculated metrics (in minutes)
    avg_airport_dwell_time = models.IntegerField(
        null=True,
        blank=True,
        help_text="Average time from gate arrival to picked up (arrivals only)"
    )
    median_airport_dwell_time = models.IntegerField(
        null=True,
        blank=True,
        help_text="Median dwell time for more accurate estimates"
    )
    p90_airport_dwell_time = models.IntegerField(
        null=True,
        blank=True,
        help_text="90th percentile for conservative scheduling"
    )
    p75_airport_dwell_time = models.IntegerField(
        null=True,
        blank=True,
        help_text="75th percentile dwell time for balanced scheduling"
    )

    avg_drive_time = models.IntegerField(
        null=True,
        blank=True,
        help_text="Average time from picked up to completed"
    )
    median_drive_time = models.IntegerField(
        null=True,
        blank=True,
        help_text="Median drive time"
    )
    p90_drive_time = models.IntegerField(
        null=True,
        blank=True,
        help_text="90th percentile drive time"
    )
    p75_drive_time = models.IntegerField(
        null=True,
        blank=True,
        help_text="75th percentile drive time for balanced scheduling"
    )

    avg_total_time = models.IntegerField(
        null=True,
        blank=True,
        help_text="Average total time from gate arrival/scheduled pickup to completed"
    )
    median_total_time = models.IntegerField(
        null=True,
        blank=True,
        help_text="Median total time"
    )
    p75_total_time = models.IntegerField(
        null=True,
        blank=True,
        help_text="75th percentile total time"
    )
    p90_total_time = models.IntegerField(
        null=True,
        blank=True,
        help_text="90th percentile total time"
    )

    # Sample size for confidence
    sample_count = models.IntegerField(
        default=0,
        help_text="Number of historical legs used to calculate this metric"
    )
    last_calculated = models.DateTimeField(
        auto_now=True,
        help_text="When this metric was last recalculated"
    )

    class Meta:
        indexes = [
            models.Index(fields=['trip_type', 'pickup_location_category', 'dropoff_location_category']),
            models.Index(fields=['time_of_day_category']),
            models.Index(fields=['-last_calculated']),
        ]
        unique_together = [
            ['trip_type', 'pickup_location_category', 'dropoff_location_category',
             'time_of_day_category', 'day_type']
        ]
        verbose_name = "Route Timing Metric"
        verbose_name_plural = "Route Timing Metrics"

    def __str__(self):
        return f"{self.get_trip_type_display()}: {self.pickup_location_category} → {self.dropoff_location_category} ({self.get_time_of_day_category_display()}, {self.get_day_type_display()})"


class DriverDailyCapacity(models.Model):
    """
    Tracks historical driver performance to understand realistic daily capacity.
    Helps with scheduling optimization and driver utilization analysis.
    """

    driver = models.ForeignKey(
        'drivers.Driver',
        on_delete=models.CASCADE,
        related_name='daily_capacity_records',
        help_text="The driver this capacity record belongs to"
    )
    date = models.DateField(
        db_index=True,
        help_text="Date of this capacity record"
    )

    # Actual performance
    total_legs = models.IntegerField(
        default=0,
        help_text="Total number of legs completed this day"
    )
    total_revenue = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text="Total revenue generated this day"
    )
    total_active_hours = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Hours from first pickup to last dropoff"
    )

    # Efficiency metrics
    avg_turnaround_time = models.IntegerField(
        null=True,
        blank=True,
        help_text="Average minutes between jobs"
    )
    longest_gap_minutes = models.IntegerField(
        null=True,
        blank=True,
        help_text="Longest idle period between jobs"
    )

    # Trip composition
    arrival_count = models.IntegerField(
        default=0,
        help_text="Number of arrival legs (airport → destination)"
    )
    return_count = models.IntegerField(
        default=0,
        help_text="Number of return legs (destination → airport)"
    )
    cruise_count = models.IntegerField(
        default=0,
        help_text="Number of cruise transfer legs"
    )
    other_count = models.IntegerField(
        default=0,
        help_text="Number of other legs"
    )

    calculated_at = models.DateTimeField(
        auto_now=True,
        help_text="When this record was last calculated"
    )

    class Meta:
        indexes = [
            models.Index(fields=['driver', '-date']),
            models.Index(fields=['-date']),
            models.Index(fields=['-calculated_at']),
        ]
        unique_together = ['driver', 'date']
        ordering = ['-date']
        verbose_name = "Driver Daily Capacity"
        verbose_name_plural = "Driver Daily Capacities"

    def __str__(self):
        return f"{self.driver} - {self.date} ({self.total_legs} legs)"


class DemandPattern(models.Model):
    """
    Aggregated demand patterns for capacity planning.
    Tracks hourly demand by trip type to predict busy periods and optimize staffing.
    """

    date = models.DateField(
        db_index=True,
        help_text="Date for this demand pattern"
    )
    hour = models.IntegerField(
        help_text="Hour of day (0-23)"
    )
    day_of_week = models.IntegerField(
        help_text="Day of week (0=Monday, 6=Sunday)"
    )

    # Volume by trip type
    arrival_legs = models.IntegerField(
        default=0,
        help_text="Number of arrival legs in this hour"
    )
    return_legs = models.IntegerField(
        default=0,
        help_text="Number of return legs in this hour"
    )
    cruise_legs = models.IntegerField(
        default=0,
        help_text="Number of cruise transfer legs in this hour"
    )
    other_legs = models.IntegerField(
        default=0,
        help_text="Number of other legs in this hour"
    )

    # Total metrics
    total_legs = models.IntegerField(
        default=0,
        help_text="Total legs in this hour"
    )
    total_drivers_needed = models.IntegerField(
        null=True,
        blank=True,
        help_text="Estimated drivers needed based on timing constraints"
    )
    inhouse_drivers_used = models.IntegerField(
        default=0,
        help_text="Number of in-house drivers used"
    )
    affiliate_drivers_used = models.IntegerField(
        default=0,
        help_text="Number of affiliate drivers used"
    )

    # Revenue
    total_revenue = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text="Total revenue for this hour"
    )

    calculated_at = models.DateTimeField(
        auto_now=True,
        help_text="When this demand pattern was last calculated"
    )

    class Meta:
        indexes = [
            models.Index(fields=['-date', 'hour']),
            models.Index(fields=['day_of_week', 'hour']),
            models.Index(fields=['-calculated_at']),
        ]
        unique_together = ['date', 'hour']
        ordering = ['-date', 'hour']
        verbose_name = "Demand Pattern"
        verbose_name_plural = "Demand Patterns"

    def __str__(self):
        return f"{self.date} {self.hour}:00 - {self.total_legs} legs ({self.total_drivers_needed or '?'} drivers needed)"
