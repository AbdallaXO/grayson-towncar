from django.conf import settings
from django.db import models
from django.db.models import Q
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
# Channel taxonomy is the single source of truth for booking_source labels,
# groups and choices (reservations/attribution.py has no model-level imports,
# so this is cycle-safe).
from reservations.attribution import BOOKING_SOURCE_CHOICES


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

    @property
    def display_number(self):
        """Customer/partner-facing reservation number: the PK with the standard
        '50' prefix (e.g. id 1234 -> '501234').

        Single source of truth for the "50..." number shown on confirmations,
        flight notices, payment reminders, the travel-agent dashboard, and
        commission payouts/statements. Use this everywhere instead of
        hardcoding '50' + id so the convention stays consistent in one place.
        """
        if self.id is None:
            return ""
        return f"50{self.id}"

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

    # VIP flag: gold-highlights this reservation's legs on the dispatch board.
    # Set from the board / reservation page for special clients or trips that need
    # extra attention (not only travel-agency VIPs). A leg is ALSO treated as VIP
    # when its travel agent is a VIP agency (Small World Big Fun) -- see
    # Leg.is_vip, which OR's this flag with the agency match.
    is_vip = models.BooleanField(default=False)

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
    # Manual exclusion: used for personal/discounted trips an agent books for
    # themselves. Set to True and the eligibility engine drops the reservation
    # into the Excluded bucket regardless of trip status. Reversible.
    commission_excluded = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Manually excluded from commission (e.g. personal trip with discount).",
    )
    commission_exclusion_reason = models.CharField(
        max_length=255,
        blank=True,
        help_text="Why this reservation was excluded from commission (shown in the Excluded bucket).",
    )
    commission_excluded_at = models.DateTimeField(null=True, blank=True)
    commission_excluded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="commission_exclusions",
        help_text="User who flagged this reservation as non-commissionable.",
    )
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

    # First-touch external referrer host (e.g. "chatgpt.com", "bing.com"),
    # captured client-side when a visitor arrives with NO utm_source. Lets
    # derive_booking_source attribute organic AI/search traffic that doesn't
    # tag itself. Same-origin referrers are dropped client-side, so this is
    # always an EXTERNAL host or blank.
    referrer_host = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        db_index=True,
        help_text="First-touch external referrer host (fallback attribution when no UTM source)",
    )

    # Canonical attribution channel — derived from utm_*/click IDs/referrer/
    # travel_agent in reservations.attribution.derive_booking_source(). Persisted
    # on save so KPI dashboards can GROUP BY it directly without re-deriving.
    # The channel taxonomy (labels, groups, choices) lives in that module so it
    # never drifts between the model and the dashboards.
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

    # Refund states that are "in-flight" — a refund is underway but not finalized.
    # Trips in these states must NOT be assigned to a driver by mistake; the
    # dispatch surfaces flag them visually.
    PENDING_REFUND_STATUSES = ("requested", "processing", "approved")

    @property
    def has_pending_refund(self):
        """True when a refund is in-flight (requested/processing/approved) and not
        yet completed or rejected — i.e. don't dispatch this trip."""
        return self.refund_status in self.PENDING_REFUND_STATUSES

    # ── Manual review flag for out-of-area / custom-quote stops ──
    # Set to True when a customer adds an "Other" extra stop, or when a stop's
    # location is outside the allowed LocationGroups. Routes the booking to
    # save_card mode in Stripe instead of immediate pay_now charge.
    requires_manual_review = models.BooleanField(
        default=False,
        db_index=True,
        help_text="True when this reservation has a stop or detail needing dispatcher pricing review before charging",
    )

    # ── Automated unpaid-reminder bookkeeping ──
    # Per-stage sent_at timestamps make the scheduler idempotent: the queryset
    # filters WHERE ..._sent_at IS NULL for each stage, so a re-run never
    # double-sends. See ops.unpaid_reminders.UnpaidReminderEngine.
    unpaid_first_reminder_sent_at = models.DateTimeField(
        null=True, blank=True, db_index=True,
    )
    unpaid_second_reminder_sent_at = models.DateTimeField(
        null=True, blank=True, db_index=True,
    )
    unpaid_three_day_warning_sent_at = models.DateTimeField(
        null=True, blank=True, db_index=True,
    )
    unpaid_final_warning_sent_at = models.DateTimeField(
        null=True, blank=True, db_index=True,
    )
    unpaid_auto_cancel_eligible_at = models.DateTimeField(
        null=True, blank=True, db_index=True,
        help_text="Set when the reservation crosses pickup-2h still unpaid. v1 surfaces this for manual review; future versions may auto-cancel from this signal.",
    )
    unpaid_auto_reminder_hold = models.BooleanField(
        default=False, db_index=True,
        help_text="Staff override — when True, the automated reminder pipeline skips this reservation entirely.",
    )
    unpaid_auto_reminder_hold_reason = models.CharField(
        max_length=255, blank=True, default="",
    )
    unpaid_duplicate_suspected = models.BooleanField(
        default=False, db_index=True,
        help_text="Set by the duplicate guard in the reminder engine. Clears when staff resolves via /duplicate-reservations/.",
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
            # Targets the affiliate_payments per-agent subqueries that filter
            # commission_paid=False AND status='completed' AND travel_agent=X.
            # Partial index stays tiny because rows leave once paid.
            models.Index(
                fields=["travel_agent", "created_at"],
                name="res_unpaid_completed_agent_idx",
                condition=Q(commission_paid=False, status="completed"),
            ),
            # Reservations list default path: exclude cancelled + order by
            # -created_at within a 90-day window (incident 2026-07-18).
            models.Index(fields=["status", "created_at"], name="res_status_created_idx"),
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

        # Auto-link a travel agent when the booking-contact email belongs to a
        # registered agent. Agents book for their clients under their own email,
        # so this lands the trip in the agent's portal (and triggers the
        # commission calc below) with no manual step. Creation-only + only when
        # unset, so a deliberate manual detach is never re-applied on a later edit.
        if self._state.adding and not self.travel_agent_id:
            from reservations.attribution import resolve_agent_by_customer_email
            agent = resolve_agent_by_customer_email(self)
            if agent is not None:
                self.travel_agent = agent

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
        Calculate total amount to be paid to drivers.

        Mirrors Leg.total_driver_pay (base+gratuity+additional, else legacy
        driver_pay_amount) as a single DB aggregate instead of summing the
        property per leg in Python — same pattern as
        Driver.get_total_unpaid_amount. Numerically identical: every component
        is a 2-decimal field, so the SQL sum equals the per-leg-quantize sum.
        """
        from django.db.models import Sum, Case, When, Q, Value, F, DecimalField
        from django.db.models.functions import Coalesce

        total = self.legs.aggregate(
            total=Sum(
                Case(
                    When(
                        Q(driver_base_pay__isnull=False)
                        | Q(driver_gratuity__isnull=False)
                        | Q(driver_additional__isnull=False),
                        then=(
                            Coalesce(F("driver_base_pay"), Value(Decimal("0.00")))
                            + Coalesce(F("driver_gratuity"), Value(Decimal("0.00")))
                            + Coalesce(F("driver_additional"), Value(Decimal("0.00")))
                        ),
                    ),
                    default=Coalesce(F("driver_pay_amount"), Value(Decimal("0.00"))),
                    output_field=DecimalField(max_digits=10, decimal_places=2),
                )
            )
        )["total"]
        return total or Decimal("0.00")

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
                leg.revenue_share = share  # set before calculate_profit() reads it
                leg.profit_estimate = leg.calculate_profit()
        else:
            # Equal split (original behavior)
            share = (self.total_price / Decimal(len(legs))).quantize(Decimal("0.01"))
            for leg in legs:
                leg.revenue_share = share
                leg.profit_estimate = leg.calculate_profit()

        # One bulk UPDATE for both columns instead of 2 UPDATEs per leg.
        # bulk_update bypasses save()/signals — same as the prior .update() calls,
        # and per-leg revenue_share/profit_estimate values are unchanged.
        Leg.objects.bulk_update(legs, ["revenue_share", "profit_estimate"], batch_size=500)

    @cached_property
    def all_payments(self):
        """
        Get all payments for this reservation
        Cached to avoid N+1 queries when accessed multiple times
        """
        return list(self.payments.all())

    @cached_property
    def first_pickup_dt(self):
        """
        Timezone-aware datetime of the first non-cancelled leg's pickup.
        Returns None if no active leg exists. Mirrors the ordering used by
        ops.tasks._scan_unpaid_reservations and dispatching.duplicate_reservations.
        """
        leg = (
            self.legs.exclude(status="cancelled")
            .order_by("pickup_date", "pickup_time")
            .first()
        )
        if not leg or not leg.pickup_date or not leg.pickup_time:
            return None
        naive = timezone.datetime.combine(leg.pickup_date, leg.pickup_time)
        return timezone.make_aware(naive, timezone.get_current_timezone())

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

        # Precedence across ALL payments, not first-row-wins: a reservation with
        # an old card_saved/pending row plus a later paid row IS paid. (The old
        # loop returned on the first row in arbitrary DB order, so the booking-time
        # card_saved row masked the real payment and the board showed it unpaid.)
        statuses = {payment.status for payment in payments}
        if "paid" in statuses:
            return "paid"
        if "card_saved" in statuses:
            return "card_saved"
        if "pending" in statuses:
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
        # Track original pickup_time/pickup_date so save() can stamp pickup
        # moves for the board's "time changed" badge. __dict__.get so a
        # deferred field (.only()/.defer() querysets) never fires a per-row
        # query here.
        self._original_pickup_time = self.__dict__.get("pickup_time")
        self._original_pickup_date = self.__dict__.get("pickup_date")
        # Same idiom for the addresses: save() re-matches the route only when
        # one of them actually changed, so an already-routed leg whose pickup
        # moved across town stops being priced off the old route.
        self._original_pickup_location = self.__dict__.get("pickup_location")
        self._original_dropoff_location = self.__dict__.get("dropoff_location")

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
    afterhours_fee = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=0,
        help_text=(
            "After-hours fee currently applied for this leg (pickup 10 PM-6 AM). "
            "Set at booking and reconciled when a flight delay shifts the pickup "
            "into/out of the window. 0 = none applied."
        ),
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

    # --- Farmed-out legs: who the OPERATOR actually put on the job ---
    # When `driver` is an operator (drivers.Driver.portal_role == 'operator'), the
    # assigned "driver" is a company, not a person. These carry the chauffeur THEY
    # dispatched, typed in their portal, so our dispatcher can call the man on the
    # job instead of relaying through the operator. Always optional — an operator
    # who never fills them in still works exactly as before.
    # Cleared automatically in save() when the leg changes hands (below).
    operator_driver_name = models.CharField(
        max_length=120, blank=True, default="",
        help_text="Name of the operator's own chauffeur on this leg. Typed by the operator; blank until they assign it.",
    )
    operator_driver_phone = models.CharField(
        max_length=25, blank=True, default="",
        help_text="Cell for the operator's chauffeur, so dispatch can reach the actual driver.",
    )
    operator_accepted_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When the operator accepted this farm-out in their portal.",
    )
    # Decline survives the unassign: `driver` is cleared so the leg returns to the
    # board needing coverage, so WHO declined has to live here or it is lost.
    operator_declined_by = models.ForeignKey(
        "drivers.Driver", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="declined_legs",
        help_text="Operator who gave this leg back. Kept after the unassign so the board can say who declined.",
    )
    operator_declined_at = models.DateTimeField(null=True, blank=True)
    operator_decline_reason = models.CharField(
        max_length=255, blank=True, default="",
        help_text="Why the operator turned the job down (no car, already booked, rate).",
    )

    # --- Samsara Phase 2: schedule-aware live ETA + late-risk (background-computed) ---
    # Written ONLY by the Samsara ETA sweep (dispatching/samsara_scheduler.sweep_eta);
    # read at render time. NEVER computed synchronously in a request path. Only the
    # driver's single "next stop" leg carries these; all others are cleared.
    DISPATCH_RISK_CHOICES = [
        ("on_time", "On time"),
        ("watch", "Watch"),
        ("at_risk", "At risk"),
        ("late", "Late"),
        ("unknown", "Unknown (telematics stale)"),
    ]
    dispatch_eta_minutes = models.IntegerField(
        null=True, blank=True,
        help_text="Live drive-time (min) from the assigned vehicle's GPS to this leg's relevant target.",
    )
    dispatch_eta_target = models.CharField(
        max_length=16, blank=True, default="",
        help_text="What the ETA is to: pickup / dropoff / next_pickup.",
    )
    dispatch_eta_target_time = models.DateTimeField(
        null=True, blank=True,
        help_text="Scheduled (flight-aware) time of the target, for the slack/late comparison.",
    )
    dispatch_risk_status = models.CharField(
        max_length=16, blank=True, default="", db_index=True,
        choices=DISPATCH_RISK_CHOICES,
        help_text="Will-he-make-it band driving the board badge color.",
    )
    dispatch_risk_reason = models.CharField(
        max_length=255, blank=True, default="",
        help_text="Human-readable reason shown on hover.",
    )
    dispatch_eta_evaluated_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When the sweep last computed this. Render only while fresh.",
    )
    dispatch_is_moving = models.BooleanField(
        null=True, blank=True,
        help_text="Snapshot of whether the assigned vehicle was moving at sweep time.",
    )
    dispatch_stationary_minutes = models.IntegerField(
        null=True, blank=True,
        help_text="How long the assigned vehicle had been stationary at sweep time.",
    )
    dispatch_vehicle_label = models.CharField(
        max_length=50, blank=True, default="",
        help_text="Assigned vehicle number snapshot, shown in the live-tracking panel.",
    )
    # Cost-control bookkeeping: the inputs the stored dispatch_eta_minutes was last
    # computed against. The sweep reuses the stored minutes (skipping the paid Google
    # Distance Matrix call) when the vehicle hasn't moved meaningfully since this GPS
    # and the target location is unchanged. Risk bands are always recomputed locally.
    dispatch_eta_origin_lat = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
        help_text="Vehicle GPS latitude the stored dispatch_eta_minutes was computed against.",
    )
    dispatch_eta_origin_lng = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
        help_text="Vehicle GPS longitude the stored dispatch_eta_minutes was computed against.",
    )
    dispatch_eta_origin_target = models.CharField(
        max_length=255, blank=True, default="",
        help_text="Target location string the stored ETA was computed against; a change forces a fresh lookup.",
    )

    # How long a computed ETA stays renderable before it's considered stale.
    DISPATCH_ETA_FRESH_MIN = 10

    @property
    def dispatch_eta_is_fresh(self) -> bool:
        """True when the background ETA sweep evaluated this leg recently."""
        if not self.dispatch_eta_evaluated_at:
            return False
        return (timezone.now() - self.dispatch_eta_evaluated_at) <= timedelta(
            minutes=self.DISPATCH_ETA_FRESH_MIN
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
    pay_manually_set = models.BooleanField(
        default=False,
        help_text=(
            "True when a person typed this leg's pay instead of the system computing it. "
            "Automatic recalculation (a pickup time move, an address edit) leaves these "
            "legs alone. Cleared when the driver changes, since the typed amount belonged "
            "to the previous driver."
        ),
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
    flight_verification_email_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When the self-service flight verification email was last sent "
            "to the guest. Cleared when the guest acts on the link so a fresh "
            "verification cycle can start if needed."
        ),
    )
    confirmation_sms_override = models.TextField(
        blank=True,
        default="",
        help_text="Custom confirmation SMS body for this leg. When set, it replaces the auto-generated message body. The standard footer is still appended.",
    )
    # ── Overnight arrival date confirmation (12 AM–6 AM pickups) ──
    # The same flight number lands just after midnight every night, so a lookup
    # alone can never tell which night the guest is on — only their takeoff date
    # does. These stamps track the "which night is it?" confirmation loop.
    overnight_confirm_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When the overnight date-confirmation email (one-tap takeoff-date "
            "question) was last sent to the guest."
        ),
    )
    overnight_confirmed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When the guest (or staff) confirmed which night this overnight "
            "arrival actually lands. Once set, the pickup date is trusted and "
            "no confirmation call/email is needed."
        ),
    )
    overnight_confirmed_source = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="How the overnight date was confirmed: booking_form | one_tap | staff.",
    )
    # ── Pickup-time change tracking (flight-change safety) ──
    # Stamped whenever pickup_time moves on an existing leg (save() hook +
    # the flight-match write path) so the board can flag "time changed" until
    # a dispatcher acknowledges it.
    pickup_time_changed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When pickup_time last changed on an existing leg.",
    )
    pickup_time_was = models.TimeField(
        null=True,
        blank=True,
        help_text="Pickup time before the earliest still-unacknowledged change.",
    )
    pickup_date_was = models.DateField(
        null=True,
        blank=True,
        help_text=(
            "Pickup DATE before the earliest still-unacknowledged change. Set "
            "only when a move crossed the calendar day — a day move is the "
            "dangerous one (the trip silently leaves the board it was on), so "
            "the badge has to say so, not just show a new time."
        ),
    )
    pickup_change_ack_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When a dispatcher acknowledged the pickup-time change on the board.",
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
    def hours_since_verify_email(self):
        """Hours since flight_verification_email_sent_at, or None if never sent."""
        if not self.flight_verification_email_sent_at:
            return None
        delta = timezone.now() - self.flight_verification_email_sent_at
        return delta.total_seconds() / 3600

    @property
    def has_unacked_time_change(self):
        """True while a pickup-time change is awaiting a dispatcher ack on the
        board (drives the purple "time changed" badge)."""
        return bool(self.pickup_time_changed_at and (self.pickup_change_ack_at is None or self.pickup_change_ack_at < self.pickup_time_changed_at))

    @property
    def pickup_day_moved(self):
        """True when the still-unacknowledged pickup move crossed the calendar
        day. Drives the loud variant of the board badge: a day move is not a
        retime, it is the trip leaving the day it was scheduled on, and it
        needs to read differently from "pickup slipped 20 minutes"."""
        return bool(self.has_unacked_time_change and self.pickup_date_was)

    @property
    def active_keoi(self):
        """Active KEOI ('Keep Eye On It') flag or None. Uses the board prefetch
        (Prefetch to_attr='active_keoi_list') when present, so no N+1."""
        if hasattr(self, "active_keoi_list"):       # set by Prefetch(to_attr=...)
            return self.active_keoi_list[0] if self.active_keoi_list else None
        return self.keoi_flags.filter(closed_at__isnull=True).first()

    @property
    def overnight_date_status(self):
        """Board badge state for the overnight-arrival ambiguity: None for legs
        outside the 12 AM-6 AM tracked-arrival window, else 'confirmed' /
        'unconfirmed' depending on whether the guest answered which night they
        land (same flight number lands every night — only their takeoff date
        disambiguates)."""
        try:
            from dispatching.overnight_arrival import leg_in_overnight_window
            if not leg_in_overnight_window(self):
                return None
        except Exception:
            return None
        return "confirmed" if self.overnight_confirmed_at else "unconfirmed"

    @property
    def overnight_prev_day(self):
        """pickup_date − 1 for the overnight badge's takeoff choices (Django
        templates can't do date arithmetic)."""
        return self.pickup_date - timedelta(days=1) if self.pickup_date else None

    @property
    def overnight_next_day(self):
        """pickup_date + 1 for the overnight badge's takeoff choices."""
        return self.pickup_date + timedelta(days=1) if self.pickup_date else None

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
    def display_carseats(self):
        """Car seats for THIS leg, as one human string, or None if there are none.

        The leg-level counterpart to Reservation.display_carseats, and the one
        every driver-facing surface should use. Two things it fixes over reading
        the reservation's version directly:
          * it honours leg overrides (seats live on the leg whenever a dispatcher
            edits one direction of a round trip — the reservation-level value is
            simply the wrong number for that leg);
          * it counts extra_carseats / extra_boosters, which the reservation
            version omits, so an "extra booster" no longer vanishes silently.

        A leg flagged as needing seats but carrying no counts returns an explicit
        "count not confirmed" rather than None — the driver still has to bring
        something, and a blank line reads as "no seats needed".
        """
        seats = [
            (self.effective_rf_carseats, "Rear-Facing"),
            (self.effective_ff_carseats, "Forward-Facing"),
            (self.effective_booster_seats, "Booster"),
            (self.effective_extra_carseats, "Extra Car Seat"),
            (self.effective_extra_boosters, "Extra Booster"),
        ]
        parts = [f"{n} {label}" for n, label in seats if n]
        if parts:
            return ", ".join(parts)
        return "Yes — count not confirmed" if self.effective_need_carseats else None

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

    def _resolve_location_endpoints(self, locations=None):
        """Return (origin Location, destination Location) for this leg's addresses.

        Either may be None when the text matches nothing we know. Cached per
        instance against the address pair, because both the route lookup and the
        zone lookup want the same answer within a single save.

        ``locations`` lets a caller checking many legs at once hand in the list
        rather than have every leg re-read the table. The Locations table is a
        dozen rows and rarely changes, so a page-lifetime list is safe; passing
        nothing keeps the original per-leg behaviour.
        """
        key = (self.pickup_location or "", self.dropoff_location or "")
        cached = getattr(self, "_loc_endpoints_cache", None)
        if cached is not None and cached[0] == key:
            return cached[1]
        if not (self.pickup_location and self.dropoff_location):
            result = (None, None)
        else:
            if locations is None:
                locations = list(Location.objects.all())
            result = (
                self._match_location(self.pickup_location, locations),
                self._match_location(self.dropoff_location, locations),
            )
        self._loc_endpoints_cache = (key, result)
        return result

    def _resolve_route_from_locations(self, locations=None, routes=None):
        """Return the explicit Route this leg's addresses text-match, or None.

        There is deliberately NO fallback to the reservation's booking rate.
        That fallback used to fire whenever the addresses matched nothing, and
        it wrote a confident wrong number: a Clermont → Port Canaveral run was
        priced off the booking's MCO → Disney rate at $25. A wrong number reads
        as correct on every future audit, which is worse than no number.

        Returning None here is not the end of pricing — a leg whose endpoints
        are both in a known pay zone is still priced from the zone rate (see
        drivers.pay_calc). A Route exists only to override its zone.

        ``locations`` and ``routes`` let a caller checking a page full of legs
        hand in both tables once. Both are a couple of dozen rows and change
        rarely; passing nothing keeps the original per-leg queries.
        """
        key = (self.pickup_location or "", self.dropoff_location or "")
        cached = getattr(self, "_route_match_cache", None)
        if cached is not None and cached[0] == key:
            return cached[1]

        origin, destination = self._resolve_location_endpoints(locations=locations)
        if not (origin and destination):
            match = None
        elif routes is not None:
            pair = {origin.id, destination.id}
            match = next(
                (r for r in routes if {r.origin_id, r.destination_id} == pair), None
            )
        else:
            match = (
                Route.objects.filter(origin=origin, destination=destination).first()
                # Reverse direction (return trips match origin↔destination)
                or Route.objects.filter(origin=destination, destination=origin).first()
            )
        self._route_match_cache = (key, match)
        return match

    def _assign_route_from_locations(self):
        if self.route:
            return
        route = self._resolve_route_from_locations()
        if route:
            self.route = route

    # Fields that require expensive calculations (route, pay, profit).
    # When save(update_fields=...) is called with only fields NOT in this set,
    # we skip all the expensive work (route lookup, pay calc, profit calc).
    _EXPENSIVE_FIELDS = frozenset({
        'driver', 'route', 'revenue_share', 'pickup_location', 'dropoff_location',
        'driver_base_pay', 'driver_gratuity', 'driver_additional',
        'driver_pay_amount', 'profit_estimate',
    })

    def refresh_from_db(self, *args, **kwargs):
        """
        Re-sync the change-tracking attributes alongside the field values.

        Without this they keep describing the row as it looked when the
        instance was first built, so an instance held across an external
        queryset .update() would re-stamp the pickup badge (or re-fire the
        driver reset) on its next unrelated save, backdating someone else's
        change onto that save.
        """
        super().refresh_from_db(*args, **kwargs)
        # Read through __dict__, exactly as __init__ does, and re-anchor only
        # what is actually loaded.
        #
        # These used to be plain attribute reads, which is fine until the
        # instance came from a .only() queryset missing one of them: reading a
        # deferred field makes Django call refresh_from_db to fetch it, which
        # re-enters this method, which reads the still-deferred field again.
        # It recurses until the stack gives out. Nothing hit it while the only
        # callers loaded every field; pricing a leg from the payroll screen's
        # .only() queryset did, instantly.
        #
        # A field that is not loaded keeps whatever anchor it had. That is the
        # honest answer — there is no fresh value to anchor to — and it is safer
        # than blanking it, which would disarm the driver-change clear and the
        # pickup-moved badge for the rest of that instance's life.
        loaded = self.__dict__
        for attr, field in (
            ("_original_pickup_time", "pickup_time"),
            ("_original_pickup_date", "pickup_date"),
            ("_original_driver_id", "driver_id"),
            ("_original_pickup_location", "pickup_location"),
            ("_original_dropoff_location", "dropoff_location"),
        ):
            if field in loaded:
                setattr(self, attr, loaded[field])

    def save(self, *args, **kwargs):
        # PERF TEMP START
        import time as _time; _t0 = _time.monotonic()
        # PERF TEMP END

        # Stamp pickup moves on EXISTING legs so the board can flag "time
        # changed" until a dispatcher acknowledges. Runs BEFORE the fast path
        # below so save(update_fields=['pickup_time']) also stamps.
        #
        # DATE moves stamp too. A flight match that lands 11:25 PM onto a leg
        # dated the following day shifts the pickup ~23h without changing the
        # time-of-day badge at all — that is exactly how a trip goes unrun. The
        # day move has to raise the same unacked flag the time move does.
        _uf = kwargs.get('update_fields')
        _orig_time = getattr(self, '_original_pickup_time', None)
        _orig_date = getattr(self, '_original_pickup_date', None)
        _time_moved = _orig_time is not None and _orig_time != self.pickup_time
        _date_moved = _orig_date is not None and _orig_date != self.pickup_date

        # Capture how much the night bonus MOVED, right here, while _orig_time is
        # still the old value — the re-sync a few lines below overwrites it, and a
        # delta re-derived after that point is always exactly zero: a silent no-op
        # that looks like it works.
        #
        # Gated on _time_moved so the common save never pays for this: a retime is
        # rare, everything else skips straight past. Deferred fields are read out of
        # __dict__ so touching them cannot trigger refresh_from_db(), which would
        # re-anchor the change-tracking attributes mid-save and disarm the
        # driver-change clear below.
        self._night_delta = None
        if (
            _time_moved
            and self.pk
            and self.driver_id
            and self.__dict__.get('payment_status') == 'unpaid'
            and not self.__dict__.get('pay_manually_set')
        ):
            from drivers.pay_calc import calculate_night_bonus as _calc_night
            _was_night = _calc_night(self.driver, _orig_time)
            _now_night = _calc_night(self.driver, self.pickup_time)
            if _was_night != _now_night:
                self._night_delta = _now_night - _was_night

        if self.pk and (_time_moved or _date_moved):
            _back_to_start = (
                self.has_unacked_time_change
                and self.pickup_time == self.pickup_time_was
                and self.pickup_date == (self.pickup_date_was or self.pickup_date)
            )
            if _back_to_start:
                # Net-zero revert (A→B→A before anyone acked): the pending
                # change just moved back to where it started — clear the badge
                # instead of stamping "was 10:00 → now 10:00".
                self.pickup_time_changed_at = None
                self.pickup_time_was = None
                self.pickup_date_was = None
                self.pickup_change_ack_at = None
            else:
                # Preserve the earliest "was" across successive moves: only capture
                # the pre-change values when no change is already awaiting an ack.
                if not self.has_unacked_time_change:
                    self.pickup_time_was = _orig_time
                    self.pickup_date_was = _orig_date if _date_moved else None
                elif _date_moved and self.pickup_date_was is None:
                    # An already-pending time move has now also crossed the day.
                    # Capture the original date the first time that happens so
                    # the badge stops understating what moved.
                    self.pickup_date_was = _orig_date
                self.pickup_time_changed_at = timezone.now()
                self.pickup_change_ack_at = None
            # Widen update_fields (same idiom as the driver-change reset below)
            # or the stamp is silently dropped.
            if _uf is not None:
                kwargs['update_fields'] = set(_uf) | {
                    'pickup_time_changed_at', 'pickup_time_was',
                    'pickup_date_was', 'pickup_change_ack_at',
                }
                _uf = kwargs['update_fields']
        # Re-sync so subsequent saves on the same instance don't re-stamp.
        self._original_pickup_time = self.pickup_time
        self._original_pickup_date = self.pickup_date

        # Fast path: if update_fields is specified and contains only simple
        # fields (e.g. driver assignment, status), skip expensive calculations.
        # pickup_time deliberately stays OUT of _EXPENSIVE_FIELDS — widening it
        # would make every retime redo route matching and profit. Instead only a
        # retime that actually changes the night bonus is pulled onto the slow path.
        if (
            _uf is not None
            and not (set(_uf) & self._EXPENSIVE_FIELDS)
            and self._night_delta is None
        ):
            super().save(*args, **kwargs)
            # PERF TEMP START
            import logging as _logging
            _logging.getLogger('perf').debug("Leg.save FAST path: %.1fms (fields=%s)", (_time.monotonic()-_t0)*1000, _uf)
            # PERF TEMP END
            return

        # Attempt to match a route from pickup/dropoff when not set
        self._assign_route_from_locations()

        # An address edit on an ALREADY-routed leg used to keep the old route
        # forever (_assign_route_from_locations returns early when route is set),
        # so a pickup that moved from Sanford to MCO kept being paid the Sanford
        # rate. Re-match — but into a local first, and only take the answer when
        # it resolves. Nulling the route and hoping is what makes the Recalculate
        # button destructive; a failed re-match must leave the leg exactly as it
        # was, not strip a route a dispatcher linked by hand.
        _addr_moved = (
            self.pk
            and (
                (getattr(self, '_original_pickup_location', None) or '') != (self.pickup_location or '')
                or (getattr(self, '_original_dropoff_location', None) or '') != (self.dropoff_location or '')
            )
        )
        if _addr_moved and not self.__dict__.get('pay_manually_set'):
            _origin, _dest = self._resolve_location_endpoints()
            _rematched = self._resolve_route_from_locations()
            if _rematched is not None:
                if _rematched.pk != self.route_id:
                    self.route = _rematched
            elif self.route_id and _origin and _dest:
                # The addresses now resolve to a pair with no Route row of its
                # own. The old route describes a trip this leg no longer is, and
                # the zone can price the new one, so drop it. Only when both
                # endpoints actually resolve — a leg we cannot place keeps
                # whatever it had rather than losing a hand-linked route.
                self.route = None

            # Re-price the base directly rather than nulling the pay fields and
            # hoping the auto-fill below picks it up — that guard needs all four
            # NULL, and a leg that already has pay has a gratuity of 0.00 in the
            # way. Gratuity and the additional bucket are not route-derived, so
            # they stay. Runs whether the price came from a Route or a zone.
            if (
                self.driver_base_pay is not None
                and self.driver_id
                and self.__dict__.get('payment_status') == 'unpaid'
            ):
                from drivers.pay_calc import calculate_driver_pay as _calc_base
                _new_base = _calc_base(self)
                if _new_base is not None and _new_base != self.driver_base_pay:
                    self.driver_base_pay = _new_base.quantize(Decimal("0.01"))
                    self.driver_pay_amount = (
                        self.driver_base_pay
                        + (self.driver_gratuity or Decimal("0.00"))
                        + (self.driver_additional or Decimal("0.00"))
                    ).quantize(Decimal("0.01"))
                    self.__dict__.pop('total_driver_pay', None)
        self._original_pickup_location = self.pickup_location
        self._original_dropoff_location = self.dropoff_location

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
            # The typed amount belonged to the previous driver.
            self.pay_manually_set = False

        # Reset leg status whenever the DRIVER CHANGES — unassign, reassign,
        # or fresh assign (founder rule 2026-06-11): progressed states like
        # confirmed/on-the-way/picked-up belong to the previous driver, so the
        # leg goes back to 'in-progress' (the new driver must re-accept).
        # Terminal 'completed'/'cancelled' stick, so e.g. a payroll-correction
        # reassignment of a finished trip never resurrects it.
        _driver_changed = (
            self.pk
            and hasattr(self, '_original_driver_id')
            and self._original_driver_id != self.driver_id
        )
        if _driver_changed and self.status not in ('in-progress', 'completed', 'cancelled'):
            self.status = 'in-progress'
            self.status_changed_by = getattr(self, '_status_change_user', None)
            self.status_changed_at = timezone.now()
            self._reset_status_on_unassign = True
            # Assignment saves (auto-assign apply, swaps) pass
            # update_fields=['driver', ...] — widen it or the reset is dropped.
            if _uf is not None:
                kwargs['update_fields'] = set(_uf) | {
                    'status', 'status_changed_by', 'status_changed_at'
                }

        # The operator's own chauffeur belongs to the operator who was holding the
        # leg. Once it changes hands the name/phone are stale — and leaving them
        # would show dispatch a driver from a company that no longer has the job.
        # The decline record (operator_declined_*) is deliberately NOT cleared: it
        # is written by the same save that unassigns, and it is the only remaining
        # trace of who gave the leg back.
        if _driver_changed and (
            self.operator_driver_name
            or self.operator_driver_phone
            or self.operator_accepted_at
        ):
            self.operator_driver_name = ""
            self.operator_driver_phone = ""
            self.operator_accepted_at = None
            _uf_now = kwargs.get('update_fields')
            if _uf_now is not None:
                kwargs['update_fields'] = set(_uf_now) | {
                    'operator_driver_name', 'operator_driver_phone', 'operator_accepted_at'
                }

        # Auto-fill driver pay when not set (inhouse and affiliate).
        #
        # The gate is BASE PAY ALONE. It used to require all four pay fields to be
        # NULL, which quietly created a class of permanently unpriceable legs: any
        # leg that got a gratuity before it got a rate — a tip charged to one leg
        # through the payment portal, a dispatcher's tip correction — had the gate
        # shut on it forever. Base pay could never be filled, so the trip read as
        # "needs a price" on every payroll run for the rest of its life, even on a
        # route the system prices hundreds of times a week. neuma's Sanford → Beach
        # Club leg (#26497) is one: $57.00 of tip, no rate, both endpoints known.
        #
        # Each field is now filled only if it is individually unset, so this can
        # add what is missing and can never overwrite what is already there.
        _autofill_set_bonus = False
        if (
            self.driver_base_pay is None
            and self.driver
            and not self.pay_manually_set
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
                        # Split only the UNATTRIBUTED remainder of the customer
                        # gratuity. Gratuity already pinned to specific legs
                        # (their driver_gratuity is set — e.g. a tip charged for
                        # one leg via the payment portal) must NOT be re-smeared
                        # across the other legs. Total is always conserved.
                        siblings = [
                            l for l in reservation.legs.all() if l.pk != self.pk
                        ]
                        already_attributed = sum(
                            (
                                l.driver_gratuity
                                for l in siblings
                                if l.driver_gratuity is not None
                            ),
                            Decimal("0.00"),
                        )
                        unattributed = gratuity_amount - already_attributed
                        if unattributed < 0:
                            unattributed = Decimal("0.00")
                        # Divide across legs that still need a share: siblings
                        # without a pinned gratuity, plus this leg.
                        share_count = (
                            sum(1 for l in siblings if l.driver_gratuity is None) + 1
                        )
                        if share_count <= 0:
                            share_count = 1
                        gratuity_share = (
                            unattributed / Decimal(share_count)
                        ).quantize(Decimal("0.01"))

                # Set base pay and gratuity separately
                self.driver_base_pay = base_pay.quantize(Decimal("0.01"))
                # Only when nothing is attributed yet. A share already sitting on
                # this leg was put there on purpose and the split above has
                # already excluded it from what the siblings divide.
                if self.driver_gratuity is None:
                    self.driver_gratuity = gratuity_share.quantize(Decimal("0.01"))

                # Night pickup bonus goes in additional (early/late fee). Only
                # into an EMPTY box: driver_additional is a mixed bucket, and
                # something already in it may or may not already be the bonus.
                # A night pickup whose box is occupied is left for the
                # night-bonus flag to raise rather than guessed at here.
                additional = self.driver_additional or Decimal("0.00")
                if self.driver_additional is None:
                    night_bonus = calculate_night_bonus(self.driver, self.pickup_time)
                    if night_bonus > 0:
                        additional = night_bonus.quantize(Decimal("0.01"))
                        self.driver_additional = additional
                        _autofill_set_bonus = True
                self.driver_pay_amount = (
                    self.driver_base_pay
                    + (self.driver_gratuity or Decimal("0.00"))
                    + additional
                ).quantize(Decimal("0.01"))

        # The pickup moved across the night-bonus boundary AFTER pay was already
        # computed. Apply the DIFFERENCE, never an overwrite: driver_additional is a
        # mixed bucket (night bonus + wait time + whatever a dispatcher typed for an
        # extra stop) and overwriting it would silently delete the rest.
        #
        # Skipped when the auto-fill just wrote the bonus itself (it used the new
        # time — adding the delta on top would double it), when the driver
        # changed in the same save (the delta is the NEW driver's rate applied to the
        # OLD driver's money), and when there is no computed pay to adjust (writing a
        # bonus onto an all-NULL leg leaves base pay NULL and shuts the guard above
        # forever, so payroll would pay the bonus alone).
        _night_delta = getattr(self, '_night_delta', None)
        if (
            _night_delta
            and not _autofill_set_bonus
            and not _driver_changed
            and self.driver_base_pay is not None
        ):
            _additional = (self.driver_additional or Decimal("0.00")) + _night_delta
            if _additional < 0:
                # Driver.night_bonus was edited between assignment and this move, so
                # we would claw back more than was ever paid. Never write negative pay.
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "Leg #%s: night-bonus delta %s would make driver_additional "
                    "negative (was %s); floored at 0 — check this leg by hand.",
                    self.pk, _night_delta, self.driver_additional,
                )
                _additional = Decimal("0.00")
            self.driver_additional = _additional.quantize(Decimal("0.01"))
            self.driver_pay_amount = (
                (self.driver_base_pay or Decimal("0.00"))
                + (self.driver_gratuity or Decimal("0.00"))
                + self.driver_additional
            ).quantize(Decimal("0.01"))
            # total_driver_pay is a cached_property and calculate_profit() reads it.
            self.__dict__.pop('total_driver_pay', None)
        self._night_delta = None

        # Calculate and store profit estimate if driver payment is set
        if self.driver_base_pay is not None or self.driver_gratuity is not None or self.driver_additional is not None or self.driver_pay_amount is not None:
            self.profit_estimate = self.calculate_profit()

        # When update_fields is specified but save() auto-filled pay or cleared
        # pay due to driver change, expand update_fields so those values persist.
        if _uf is not None:
            _pay_fields = {
                'driver_base_pay', 'driver_gratuity', 'driver_additional',
                'driver_pay_amount', 'profit_estimate', 'route', 'revenue_share',
                'pay_manually_set',
            }
            _uf_set = set(_uf)
            if not _uf_set.issuperset(_pay_fields):
                _uf_set |= _pay_fields
                kwargs['update_fields'] = list(_uf_set)
            if getattr(self, '_reset_status_on_unassign', False):
                _uf_set = set(kwargs.get('update_fields') or _uf)
                _uf_set |= {'status', 'status_changed_by', 'status_changed_at'}
                kwargs['update_fields'] = list(_uf_set)

        super().save(*args, **kwargs)

        # Audit trail: log the auto-reset to LegStatus history.
        if getattr(self, '_reset_status_on_unassign', False):
            LegStatus.objects.create(
                leg=self,
                status='in-progress',
                updated_by=getattr(self, '_status_change_user', None),
                timestamp=timezone.now(),
                notes='Auto-reset: driver unassigned',
            )
            self._reset_status_on_unassign = False
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

    @property
    def shows_store_stop(self):
        """True only on the leg the Publix grocery stop actually rides on.

        `store_stop` lives on the Reservation, but the grocery run happens on
        the way INTO town — the airport-pickup leg (a normal arrival, or an
        airport→cruise-port transfer). Rendering the reservation-level flag on
        every leg used to badge "Publix Stop" onto the departure/return leg of
        the same reservation, which drivers don't stop on. Every per-leg badge
        (dispatch boards, driver app, SMS payloads) should use this.

        For the rare reservation whose legs never start at an airport, the
        badge falls back to the first leg so the stop stays visible somewhere.
        """
        res = self.reservation if self.reservation_id else None
        if not res or not res.store_stop:
            return False
        if self.is_flight_tracked_arrival():
            return True
        trip = self.get_trip_type()
        if trip in ("return", "cruise"):
            # Heading TO the airport, or a cruise leg that doesn't start at
            # the airport — never the grocery leg.
            return False
        # 'other' leg: badge only when no sibling is the natural grocery leg
        # and this is the reservation's first leg. res.legs.all() rides an
        # existing prefetch when the view set one up.
        siblings = list(res.legs.all())
        for sib in siblings:
            if sib.pk != self.pk and sib.is_flight_tracked_arrival():
                return False
        first = min(siblings, key=lambda l: l.pk, default=None)
        return first is not None and first.pk == self.pk

    def is_flight_tracked_arrival(self):
        """True when this leg's pickup depends on an INBOUND flight we should track.

        That's a normal airport 'arrival', OR a cruise transfer that STARTS at the
        airport (e.g. MCO → Port Canaveral) — functionally an arrival with a tracked
        inbound flight, even though its display trip type stays 'cruise'. Hotel→port
        cruise legs (no inbound flight to track) are correctly excluded because their
        pickup isn't an airport.

        This is the single predicate every flight-tracking guard shares (background +
        bulk refresh, mismatch scan, tight-turn, board badge, match-to-flight), so a
        cruise guest's inbound flight is never silently left untracked.
        """
        trip_type = self.get_trip_type()
        if trip_type == "arrival":
            return True
        if trip_type == "cruise" and self.is_airport_pickup():
            return True
        return False

    def flight_tracking_trip_type(self):
        """Trip type to hand AeroAPI when refreshing this leg's flight. An airport→
        cruise transfer tracks an inbound ARRIVAL at the airport, so it refreshes with
        'arrival' semantics; every other leg keeps its natural trip type."""
        if self.is_flight_tracked_arrival():
            return "arrival"
        return self.get_trip_type()

    def _is_airport(self, location_lower):
        """Airport-terminal test.

        Delegates to the shared dispatching.analytics.is_airport_location detector
        so trip-type classification and route-timing location buckets always agree.
        Previously this used a narrow keyword list (mco/sfb/mlb/lal/"international
        airport") that missed pickups written as "Terminal B", an airline name, or
        "baggage claim" — they were mislabeled 'other' and lost their dwell
        allowance. Imported lazily to avoid a models <-> analytics import cycle.
        """
        if not location_lower:
            return False
        from dispatching.analytics import is_airport_location
        return is_airport_location(location_lower)

    def has_flight_time_mismatch(self, threshold_minutes=30):
        """
        For arrival legs with flight info: True if the flight's best available
        arrival time differs from the leg's pickup time by at least threshold_minutes.

        Uses best available time (actual/estimated if available, otherwise scheduled)
        so delayed flights don't incorrectly flag legs that have been updated to
        match the new arrival time.
        """
        if not self.is_flight_tracked_arrival() or not self.flight_information:
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
        if not self.is_flight_tracked_arrival() or not self.flight_information:
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

    @property
    def is_vip(self):
        """True if this leg should be flagged VIP on the dispatch board/planner:
        the reservation is manually marked VIP, OR its travel agent belongs to a
        VIP agency (e.g. Small World Big Fun). Single source of truth for the gold
        highlight.

        Query-safe by design: the agency check only consults relations that are
        ALREADY loaded, so it never fires a query in bulk/engine contexts (e.g.
        build_driver_schedules is shared with auto-assign). The board & planner
        querysets select_related travel_agent + agency, so the agency check is
        fully live there; where they aren't loaded, only the (cheap, always-loaded)
        manual flag applies."""
        reservation = self.reservation
        if reservation is None:
            return False
        if getattr(reservation, "is_vip", False):
            return True
        # Agency-based VIP -- guard every relation hop against an unloaded FK so
        # we never N+1. reservation is select_related in every slot/row context.
        res_state = getattr(reservation, "_state", None)
        agent = res_state.fields_cache.get("travel_agent") if res_state else None
        if agent is None:
            return False
        if agent.agency_id:
            agent_state = getattr(agent, "_state", None)
            if not agent_state or "agency" not in agent_state.fields_cache:
                return False  # agency FK not loaded -> skip rather than query
        # Local import: dispatching.confirmation_sms imports reservations.models,
        # so importing it at module load would be circular.
        from dispatching.confirmation_sms import is_vip_leg
        return is_vip_leg(self)

    def flight_timing_flag(self, early_watch_minutes=15, alert_minutes=20):
        """Board-facing flight-timing signal for an arrival leg, with two distinct,
        mutually-exclusive levels so the dispatcher board can render them differently
        and keep the noise down:

          * 'alert' (red)   — the flight is >= alert_minutes off the booked pickup in
                              either direction; the pickup likely needs matching.
          * 'watch' (amber) — the flight is landing EARLY by early_watch..alert_minutes;
                              still fine, but an early arrival can squeeze a driver's
                              turnaround, so it's worth an eye ("landing early").

        Uses the same best-available arrival chain as get_flight_time_mismatch_display.
        Returns {level, direction, minutes, label, arrival_label} or None. This is a
        separate, additive helper — get_flight_time_mismatch_display (and its push-alert
        / flight-refresh callers) are intentionally left on the original 30-min rule.
        """
        if not self.is_flight_tracked_arrival() or not self.flight_information:
            return None
        flight = self.flight_information
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

        # Pretty "now lands at" time for the badge.
        flight_local = (
            timezone.localtime(flight_dt) if timezone.is_aware(flight_dt) else flight_dt
        )
        try:
            arrival_label = flight_local.strftime("%I:%M %p").lstrip("0")
        except Exception:
            arrival_label = ""

        leg_dt = datetime.combine(self.pickup_date, self.pickup_time)
        flight_naive = flight_dt
        if timezone.is_aware(flight_naive):
            flight_naive = timezone.make_naive(flight_naive, timezone.get_current_timezone())
        total_seconds = int((flight_naive - leg_dt).total_seconds())
        total_minutes = abs(total_seconds) // 60
        direction = "late" if total_seconds >= 0 else "early"

        if total_minutes >= alert_minutes:
            level = "alert"
        elif direction == "early" and total_minutes >= early_watch_minutes:
            level = "watch"
        else:
            return None

        if total_minutes >= 60:
            hours, mins = divmod(total_minutes, 60)
            time_str = f"{hours} hr {mins} min" if mins else f"{hours} hr"
        else:
            time_str = f"{total_minutes} min"
        if level == "watch":
            label = f"Landing {time_str} early"
        else:
            label = f"Coming {time_str} {'late' if direction == 'late' else 'early'}"
        return {
            "level": level,
            "direction": direction,
            "minutes": total_minutes,
            "label": label,
            "arrival_label": arrival_label,
        }

    def flight_disruption_flag(self):
        """Board-facing RED badge for a tracked inbound flight that AeroAPI reports
        cancelled or diverted — the case flight_timing_flag (a minute-delta signal)
        cannot see, because a cancelled flight usually keeps its scheduled time.

        Returns {'level':'alert', 'kind':'cancelled'|'diverted', 'label':...} or None.
        """
        if not self.is_flight_tracked_arrival() or not self.flight_information:
            return None
        status = (self.flight_information.status or "").lower()
        if "cancel" in status:
            return {"level": "alert", "kind": "cancelled", "label": "Flight cancelled"}
        if "divert" in status:
            return {"level": "alert", "kind": "diverted", "label": "Flight diverted"}
        return None

    def effective_afterhours_time(self):
        """The local time-of-day used to decide the after-hours (10 PM-6 AM)
        window: for an arrival leg with flight info, the flight's best (possibly
        delayed) arrival when it lands on the pickup date — so a delay into the
        window is caught before the pickup is re-matched; otherwise the booked
        pickup_time. Returns a datetime.time or None."""
        if self.get_trip_type() == "arrival" and self.flight_information_id:
            arr = self.flight_information.best_arrival_local()
            if arr is not None:
                if timezone.is_aware(arr):
                    arr = timezone.make_naive(arr, timezone.get_current_timezone())
                # Trust only a same-day arrival; red-eye / different-date arrivals
                # are handled by the normal flight-verification flow.
                if self.pickup_date is None or arr.date() == self.pickup_date:
                    return arr.time()
        return self.pickup_time

    def afterhours_fee_outstanding(self):
        """Decimal after-hours fee OWED BUT NOT YET applied/charged for this leg,
        based on the effective (delay-aware) pickup time. Returns 0 when out of
        the window or already collected — a leg booked late already carries the
        fee (afterhours_fee == 20), so it returns 0 and shows no 'owed' flag."""
        from .utils import afterhours_fee_owed
        owed = afterhours_fee_owed(self.effective_afterhours_time())
        applied = self.afterhours_fee or Decimal("0.00")
        return owed - applied if owed > applied else Decimal("0.00")

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

    # ── Multi-stop / multi-flight accessors ──
    # See plan: extra stops are opt-in. If no LegStop rows exist, all_stops
    # collapses to [pickup, dropoff] so legacy templates render unchanged.
    # If no LegFlight rows exist, controlling_flight falls back to flight_information.

    @property
    def has_extra_stops(self):
        """True if this leg has one or more intermediate stops (any type)."""
        if self.pk is None:
            return False
        return self.legstop_set.exists()

    @cached_property
    def additional_dropoffs(self):
        """LegStop rows that represent additional drop-off destinations
        (stop_type='dropoff'). The leg's `dropoff_location` CharField is the
        PRIMARY drop-off; these are extras after that one (e.g., second resort)."""
        if self.pk is None:
            return []
        # Iterate the (typically prefetched) legstop_set in Python so list
        # views don't fire a query per leg. Meta.ordering = (leg_id, sequence)
        # keeps these in sequence order.
        return [s for s in self.legstop_set.all() if s.stop_type == 'dropoff']

    @cached_property
    def intermediate_stops(self):
        """LegStop rows that represent on-the-way stops with a duration
        (luggage drop, store stop, wait) — i.e., stops where everyone stays
        in the vehicle (or briefly disembarks then continues). Anything not
        flagged as a 'dropoff' falls here."""
        if self.pk is None:
            return []
        return [s for s in self.legstop_set.all() if s.stop_type != 'dropoff']

    @cached_property
    def has_additional_dropoffs(self):
        if self.pk is None:
            return False
        return any(s.stop_type == 'dropoff' for s in self.legstop_set.all())

    @cached_property
    def has_intermediate_stops(self):
        if self.pk is None:
            return False
        return any(s.stop_type != 'dropoff' for s in self.legstop_set.all())

    @cached_property
    def all_stops(self):
        """
        Return the ordered itinerary as a list of dicts:
          [{'kind': 'pickup',       'location': str, ...},
           {'kind': 'intermediate', 'location': str, 'stop_type': str, 'duration_minutes': int, 'notes': str, ...} *,
           {'kind': 'dropoff',      'location': str, ...}]

        For legs with no LegStop rows this returns exactly two entries (pickup + dropoff)
        — preserving legacy display behavior.
        """
        items = [{
            "kind": "pickup",
            "location": self.pickup_location,
            "stop_type": "pickup",
            "duration_minutes": 0,
            "notes": "",
        }]
        if self.pk is not None:
            for stop in self.legstop_set.all():
                items.append({
                    "kind": "intermediate",
                    "location": stop.display_location,
                    "stop_type": stop.stop_type,
                    "duration_minutes": stop.duration_minutes,
                    "notes": stop.notes,
                    "requires_manual_review": stop.requires_manual_review,
                    "stop": stop,
                })
        items.append({
            "kind": "dropoff",
            "location": self.dropoff_location,
            "stop_type": "dropoff",
            "duration_minutes": 0,
            "notes": "",
        })
        return items

    @property
    def controlling_flight(self):
        """
        Return the Flight that drives pickup timing for this leg.

        Resolution order:
          1. The LegFlight row marked is_controlling=True (post-Phase-2 path).
          2. The legacy OneToOne flight_information (pre-multi-flight legs).
          3. None.
        """
        if self.pk is not None:
            lf = self.legflight_set.filter(is_controlling=True).select_related("flight").first()
            if lf is not None:
                return lf.flight
        return self.flight_information

    # ── External deep links (Google Maps / flight trackers) ──
    # Thin wrappers over reservations.trip_links so templates can render a
    # "open this in Maps" button without knowing the URL contract. Imported
    # inside the properties to keep models.py import-time clean.

    @property
    def pickup_map_url(self):
        """Google Maps link for this leg's pickup address, or None if blank."""
        from .trip_links import maps_place_url

        return maps_place_url(self.pickup_location)

    @property
    def dropoff_map_url(self):
        """Google Maps link for this leg's drop-off address, or None if blank."""
        from .trip_links import maps_place_url

        return maps_place_url(self.dropoff_location)

    @cached_property
    def route_map_url(self):
        """Driving directions for the WHOLE leg — pickup, every on-the-way stop,
        then the drop-offs in order. None when either end is missing."""
        from .trip_links import leg_trip_links

        return leg_trip_links(self)["route_url"]

    @cached_property
    def flight_tracker_links(self):
        """FlightAware / FlightView links for every flight on this leg,
        controlling flight first. Empty list when there's nothing trackable."""
        from .trip_links import leg_trip_links

        return leg_trip_links(self)["flights"]

    class Meta:
        ordering = ["pickup_date", "pickup_time"]
        indexes = [
            models.Index(fields=["reservation"]),
            models.Index(fields=["flight_information"]),
            models.Index(fields=["pickup_date", "pickup_time"]),
            models.Index(fields=["driver"]),
            models.Index(fields=["status"]),
            # completed-trips filters driver + status='completed'; date/board views
            # filter pickup_date + status (incident 2026-07-18).
            models.Index(fields=["driver", "status"], name="leg_driver_status_idx"),
            models.Index(fields=["pickup_date", "status"], name="leg_pickup_status_idx"),
        ]


class LegStop(models.Model):
    """
    An intermediate stop within a Leg, sitting between the leg's pickup_location
    (anchor: first) and dropoff_location (anchor: last). Existence of LegStop
    rows is purely opt-in — legs without extra stops have zero rows here and
    render exactly as they did before this feature.

    Fees on a LegStop are SNAPSHOT at creation time so post-booking vehicle
    or pricing changes don't retroactively reprice old stops.
    """

    STOP_TYPE_CHOICES = [
        ("dropoff", "Additional drop-off"),
        ("stop", "Additional stop"),
        ("pickup", "Additional pickup"),
        ("charter", "Charter (hourly)"),
    ]

    leg = models.ForeignKey(
        Leg,
        on_delete=models.CASCADE,
        related_name="legstop_set",
    )
    sequence = models.PositiveSmallIntegerField(
        help_text="Order between the leg's pickup and dropoff anchors (0, 1, 2, ...)",
    )
    location_text = models.CharField(
        max_length=255,
        blank=True,
        help_text="Free-text address or venue name for this stop. Optional for charter stops (driver takes them anywhere).",
    )
    location = models.ForeignKey(
        "rates.Location",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="legstops",
        help_text="Structured Location match — populated when location_text matches a known Location",
    )
    stop_type = models.CharField(
        max_length=10,
        choices=STOP_TYPE_CHOICES,
        default="dropoff",
    )
    duration_minutes = models.PositiveSmallIntegerField(
        default=10,
        help_text="Estimated minutes the chauffeur will be stopped here. For charter, this stores hours × 60.",
    )
    start_time = models.TimeField(
        null=True,
        blank=True,
        help_text="When this stop begins. Required for charter (hourly) stops, optional otherwise.",
    )
    notes = models.TextField(blank=True)
    extra_fee = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Snapshot of the per-stop fee at creation. NULL = manual review pending or fee not yet quoted.",
    )
    requires_manual_review = models.BooleanField(
        default=False,
        help_text="True for out-of-area or 'Other' stops needing a custom quote before charging",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["leg_id", "sequence"]
        unique_together = (("leg", "sequence"),)
        indexes = [
            models.Index(fields=["leg", "sequence"]),
            models.Index(fields=["requires_manual_review"]),
        ]

    @property
    def display_location(self):
        """Return the structured Location name when matched, else free text.
        Charter stops with no destination fall back to a friendly hint."""
        if self.location_id and self.location:
            return self.location.name
        if self.location_text:
            return self.location_text
        if self.stop_type == "charter":
            return "Open destination — guest directs the driver"
        return ""

    @property
    def map_url(self):
        """Google Maps link for this stop, or None when it has no address.

        Deliberately reads the raw location rather than `display_location`: a
        charter stop's friendly "guest directs the driver" sentence is not
        somewhere Google can take anyone.
        """
        from .trip_links import maps_place_url, stop_address

        return maps_place_url(stop_address(self))

    @property
    def charter_hours(self):
        """For charter stops, expose duration as whole/half hours for display."""
        mins = self.duration_minutes or 0
        hours = mins / 60.0
        return int(hours) if hours.is_integer() else round(hours, 1)

    def __str__(self):
        return f"Stop {self.sequence} on Leg #{self.leg_id}: {self.display_location}"


class LegFlight(models.Model):
    """
    Through-model linking a Leg to one or more Flights. Exactly one LegFlight
    per leg may have is_controlling=True — that flight drives pickup timing.

    For legacy legs that still use Leg.flight_information (OneToOne), the
    controlling row is created by the backfill migration. New code reads
    Leg.controlling_flight which falls back to flight_information when no
    LegFlight rows exist (e.g., during partial deploys).
    """

    leg = models.ForeignKey(
        Leg,
        on_delete=models.CASCADE,
        related_name="legflight_set",
    )
    flight = models.ForeignKey(
        "Flight",
        on_delete=models.CASCADE,
        related_name="legflights",
    )
    is_controlling = models.BooleanField(
        default=False,
        help_text="True for the flight that determines pickup timing (the later-arriving flight, by convention)",
    )
    sequence = models.PositiveSmallIntegerField(
        default=0,
        help_text="Display order — lower values shown first",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["leg_id", "sequence", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["leg", "flight"],
                name="legflight_unique_leg_flight",
            ),
            models.UniqueConstraint(
                fields=["leg"],
                condition=models.Q(is_controlling=True),
                name="legflight_one_controlling_per_leg",
            ),
        ]
        indexes = [
            models.Index(fields=["leg", "is_controlling"]),
        ]

    def __str__(self):
        marker = " [controlling]" if self.is_controlling else ""
        return f"LegFlight leg #{self.leg_id} ↔ flight #{self.flight_id}{marker}"


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
    departure_date = models.DateField(
        null=True,
        blank=True,
        help_text=(
            "Takeoff date (Eastern) for this flight — guest-confirmed or staff-set. "
            "Disambiguates overnight red-eyes: the same flight number lands just "
            "after midnight every night, so only the departure date pins which "
            "instance the guest is on. When set, AeroAPI lookups anchor on this "
            "date instead of the leg's pickup date."
        ),
    )

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

    def best_arrival_local(self):
        """Best available arrival time (single source of truth for the priority
        chain): actual gate > estimated gate > actual runway > estimated runway >
        scheduled gate > scheduled runway. Returns a datetime or None."""
        return (
            self.actual_gate_arrival_local
            or self.estimated_gate_arrival_local
            or self.actual_arrival_local
            or self.estimated_arrival_local
            or self.scheduled_gate_arrival_local
            or self.scheduled_arrival_local
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
            # Guard: if normalize_airline didn't recognize the input, it returns the
            # bare uppercased string. Sending that to AeroAPI produces malformed idents
            # like "ALLIEGANT2942" → 400 Bad Request. Real IATA codes are 2-3
            # alphanumeric chars; anything else means we don't know the airline.
            if not iata_code or len(iata_code) > 3 or not iata_code.isalnum():
                return self.flight_iata or None
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

    # First-touch external referrer host (e.g. "chatgpt.com", "bing.com"),
    # captured client-side when a visitor arrives with NO utm_source. Lets the
    # lead-analytics source breakdown attribute organic AI/search traffic that
    # doesn't tag itself — same fallback signal Reservation.referrer_host gives
    # the booking dashboards. Same-origin referrers are dropped client-side, so
    # this is always an EXTERNAL host or blank.
    referrer_host = models.CharField(
        max_length=255, blank=True, null=True, db_index=True,
        help_text="First-touch external referrer host (fallback attribution when no UTM source)",
    )

    # Follow-Up Automation Fields
    segment = models.CharField(
        max_length=30, choices=SegmentChoices.choices, default=SegmentChoices.GENERAL, blank=True
    )
    sequence_active = models.BooleanField(default=False, help_text="Whether follow-up automation is running")
    sequence_completed_at = models.DateTimeField(null=True, blank=True)
    needs_human_follow_up = models.BooleanField(default=False, help_text="Flagged for human closer after lead replied")

    # SMS opt-out (TCPA). Set True when the contact replies STOP/UNSUBSCRIBE/etc.
    # Propagated across ALL leads sharing this phone (normalized_phone) so a
    # round-trip customer's duplicate leads are all suppressed. The shared GHL
    # send path (GoHighLevelService.send_sms) hard-blocks any opted-out number,
    # so this protects the initial SMS, the 5-step sequence, and the nudge alike.
    sms_opt_out = models.BooleanField(
        default=False, db_index=True,
        help_text="Contact opted out of SMS (replied STOP/UNSUBSCRIBE/etc). Blocks all outbound SMS.",
    )

    # Pre-pickup nudge: a deliberate, backend-set discount (default 0) for the
    # one-touch SMS fired ~3 days before pickup. Nothing sets this automatically
    # — staff set it per lead/date. When > 0 the nudge routes the lead to a human
    # to book with the discount applied manually, because the booking flow has no
    # coupon/price-override mechanism (only customer-entered Stripe promo codes).
    pre_pickup_discount = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="Deliberate discount ($) for the pre-pickup nudge. Default 0. "
                  "When > 0, the lead is routed to a human to book with the discount applied.",
    )

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


class LegKeoi(models.Model):
    """'Keep Eye On It' — dispatcher-raised watch flag on ONE leg.

    Active while closed_at IS NULL. Auto-closes when the leg reaches a terminal
    status; auto-reactivates if the leg leaves it (unless admin-removed).
    operational_status is workflow color only — it NEVER hides the flag.
    """

    class Category(models.TextChoices):
        TIGHT_SCHEDULE       = "tight_schedule", "Tight Schedule"
        DRIVER_CONFLICT      = "driver_conflict", "Possible Driver Conflict"
        FLIGHT_DELAY         = "flight_delay", "Flight Delay Risk"
        PASSENGER_READINESS  = "passenger_readiness", "Passenger Readiness Risk"
        TRAFFIC              = "traffic", "Traffic Risk"
        WAITING_INFO         = "waiting_info", "Waiting on Information"
        OTHER                = "other", "Other"

    class OperationalStatus(models.TextChoices):
        NEEDS_ATTENTION = "needs_attention", "Needs Attention"
        BEING_MONITORED = "being_monitored", "Being Monitored"
        BACKUP_ARRANGED = "backup_arranged", "Backup Arranged"

    class ClosedReason(models.TextChoices):
        LEG_COMPLETED = "leg_completed", "Leg Completed"
        LEG_CANCELLED = "leg_cancelled", "Leg Cancelled"
        ADMIN_REMOVED = "admin_removed", "Removed by Admin"
        CONFLICT_RESOLVED = "conflict_resolved", "Conflict Resolved"

    leg = models.ForeignKey("Leg", on_delete=models.CASCADE, related_name="keoi_flags")
    category = models.CharField(max_length=30, choices=Category.choices)
    description = models.TextField()  # required; enforced in views (codebase uses no forms for AJAX)
    operational_status = models.CharField(
        max_length=20,
        choices=OperationalStatus.choices,
        default=OperationalStatus.NEEDS_ATTENTION,
    )
    created_by = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="keoi_created",
    )
    created_at = models.DateTimeField(default=timezone.now)   # not auto_now_add (tests can backdate; matches LegStatus)
    updated_by = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="keoi_updated",
    )
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_reason = models.CharField(
        max_length=20, choices=ClosedReason.choices, null=True, blank=True,
    )
    closed_by = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="keoi_closed",
    )
    removal_reason = models.TextField(blank=True, default="")  # required iff admin_removed

    TERMINAL_LEG_STATUSES = ("completed", "cancelled")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["leg", "-created_at"])]
        permissions = [("remove_keoi", "Can remove KEOI flags (with reason)")]
        constraints = [
            models.UniqueConstraint(
                fields=["leg"],
                condition=models.Q(closed_at__isnull=True),
                name="uniq_active_keoi_per_leg",
            ),
            models.CheckConstraint(
                check=(
                    models.Q(closed_at__isnull=True, closed_reason__isnull=True)
                    | models.Q(closed_at__isnull=False, closed_reason__isnull=False)
                ),
                name="keoi_closed_fields_paired",
            ),
        ]

    @property
    def is_active(self):
        return self.closed_at is None

    def __str__(self):
        state = "active" if self.is_active else f"closed:{self.closed_reason}"
        return f"KEOI Leg #{self.leg_id} - {self.get_category_display()} ({state})"


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
        ('conflict_advisor', 'Auto-save Before Advisor Apply'),
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


class ScheduleDraft(models.Model):
    """
    A per-date sandbox draft of driver assignments.

    Drivers see the LIVE schedule via Leg.driver. While a date has an *active*
    (non-terminal) ScheduleDraft, dispatcher edits for that date are routed into
    the DraftAssignment overlay instead of Leg.driver — so nothing reaches drivers
    until a manager publishes. Publishing applies the overlay onto Leg.driver.

    State machine:
        draft → in_review → published          (approve + publish)
        in_review → changes_requested → in_review (reject with notes, resubmit)
        draft / changes_requested → discarded
    `published` and `discarded` are terminal. The partial unique constraint allows
    a NEW draft to be opened over a previously published/discarded one for the same
    date (publish, then re-hold).
    """

    class State(models.TextChoices):
        DRAFT = "draft", "Draft (building)"
        IN_REVIEW = "in_review", "Submitted for review"
        CHANGES_REQUESTED = "changes_requested", "Changes requested"
        PUBLISHED = "published", "Published"
        DISCARDED = "discarded", "Discarded"

    # States in which a draft is "active" (the date is held; edits go to overlay).
    ACTIVE_STATES = (State.DRAFT, State.IN_REVIEW, State.CHANGES_REQUESTED)

    schedule_date = models.DateField(db_index=True, help_text="The date this draft holds")
    state = models.CharField(max_length=20, choices=State.choices, default=State.DRAFT)

    # Baseline captured at hold-time, used for the review diff and conflict detection.
    base_snapshot = models.ForeignKey(
        ScheduleSnapshot, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="drafts",
    )
    # Set of leg IDs present on the date when the draft was opened. Any leg on the
    # date NOT in this set is "new since draft started" → needs attention (live-merge).
    baseline_leg_ids = models.JSONField(default=list, blank=True)
    # Per-leg snapshot of schedule-critical fields at hold time, keyed by str(leg.id):
    # {driver_id, pickup_time "HH:MM", pickup_date "YYYY-MM-DD", pickup_location,
    # dropoff_location}. Used to detect what a non-sandbox user changed LIVE while
    # the draft was open (driver, time, date moves) and surface it in the summary.
    baseline_legs = models.JSONField(default=dict, blank=True)

    created_by = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="drafts_created",
    )
    created_at = models.DateTimeField(default=timezone.now)
    submitted_by = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="drafts_submitted",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="drafts_reviewed",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="drafts_published",
    )
    published_at = models.DateTimeField(null=True, blank=True)
    notified_by = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="drafts_notified",
    )
    notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["schedule_date", "state"])]
        permissions = [
            # Grant to the few dispatchers allowed to build/hold sandbox schedules.
            # Superusers (managers) always have it. Everyone else edits live as before.
            ("use_schedule_sandbox", "Can build/hold sandbox schedules"),
        ]
        constraints = [
            # At most one ACTIVE (non-terminal) draft per date. Terminal drafts
            # (published/discarded) may coexist for the same date as history.
            models.UniqueConstraint(
                fields=["schedule_date"],
                condition=models.Q(
                    state__in=["draft", "in_review", "changes_requested"]
                ),
                name="uniq_active_draft_per_date",
            ),
        ]

    def __str__(self):
        return f"Draft for {self.schedule_date} ({self.get_state_display()})"

    @property
    def is_active(self):
        return self.state in self.ACTIVE_STATES


class DraftAssignment(models.Model):
    """
    The delta overlay: one row per leg a dispatcher TOUCHED inside a draft.

    Effective draft driver for a leg = proposed_driver if a row exists, else the
    live Leg.driver. Three meaningful states:
        row with proposed_driver  → "draft assigns this driver"
        row with proposed_driver=NULL → "draft says unassigned"
        no row                    → "no draft opinion; show live Leg.driver"
    """
    draft = models.ForeignKey(
        ScheduleDraft, on_delete=models.CASCADE, related_name="assignments",
    )
    leg = models.ForeignKey("Leg", on_delete=models.CASCADE, related_name="draft_assignments")
    proposed_driver = models.ForeignKey(
        "drivers.Driver", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="draft_assignments",
    )
    assigned_by = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True,
    )
    assigned_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["draft", "leg"], name="uniq_draft_leg"),
        ]
        indexes = [models.Index(fields=["draft", "leg"])]

    def __str__(self):
        return f"Draft #{self.draft_id}: Leg #{self.leg_id} → Driver {self.proposed_driver_id}"


class ScheduleDraftEvent(models.Model):
    """Unified timeline for a draft: audit trail (who did what) + review feedback."""

    class EventType(models.TextChoices):
        CREATED = "created", "Draft opened"
        EDITED = "edited", "Assignment edited"
        SUBMITTED = "submitted", "Submitted for review"
        APPROVED = "approved", "Approved & published"
        REJECTED = "rejected", "Changes requested"
        PUBLISHED = "published", "Published to live"
        NOTIFIED = "notified", "Drivers notified (SMS)"
        DISCARDED = "discarded", "Draft discarded"
        CONFLICT = "conflict", "Conflict detected"

    draft = models.ForeignKey(ScheduleDraft, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=20, choices=EventType.choices)
    actor = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="draft_events",
    )
    note = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["draft", "created_at"])]

    def __str__(self):
        return f"Draft #{self.draft_id} {self.event_type} @ {self.created_at}"


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


class RouteDistanceCache(models.Model):
    """
    Persistent, precomputed Google Distance Matrix drive time for a specific
    pickup→dropoff ADDRESS pair (not a category bucket).

    This is the "offline-cached matrix (no in-request network)" that
    dispatching/scheduler.py's DEFAULT_DRIVE_TIME comment asks for. The category
    table (DRIVE_TIME_ESTIMATES) can't tell a residential address 10 min out from
    one an hour out (e.g. Umatilla → MCO), so for routes the category map can't
    place we look the real drive time up here.

    Read path (resolve_drive_minutes) only ever READS this table — a single indexed
    lookup, never a network call. Rows are filled by the background resolver
    (management command `resolve_route_distances`, run via cron / the schedulers
    process), exactly like the Samsara `Leg.dispatch_eta_*` precompute pattern.
    A brand-new pair returns None until the resolver fills it, so the render path
    falls back to the category estimate in the meantime.
    """

    STATUS_PENDING = "pending"
    STATUS_OK = "ok"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending resolution"),
        (STATUS_OK, "Resolved"),
        (STATUS_FAILED, "Failed (address unresolvable)"),
    ]

    # md5 of the normalized "pickup||dropoff" text — stable, order-sensitive key.
    pair_hash = models.CharField(max_length=32, unique=True, db_index=True)
    pickup_text = models.CharField(max_length=500)
    dropoff_text = models.CharField(max_length=500)

    drive_minutes = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Google Distance Matrix traffic-aware drive time, rounded to minutes.",
    )
    distance_text = models.CharField(max_length=50, blank=True, default="")

    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True,
    )
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=255, blank=True, default="")

    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['status', 'resolved_at']),
        ]
        verbose_name = "Route Distance Cache"
        verbose_name_plural = "Route Distance Cache"

    def __str__(self):
        mins = f"{self.drive_minutes} min" if self.drive_minutes is not None else self.status
        return f"{self.pickup_text} → {self.dropoff_text} ({mins})"


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
