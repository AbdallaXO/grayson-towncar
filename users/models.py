from django.db import models
from django.contrib.auth.models import User
from django.db.models import Sum, Count, Q
from decimal import Decimal
from reservations.models import Reservation
import logging

# Create your models here.


class UserProfile(models.Model):
    """
    Extended profile for all users of the system
    """

    user = models.OneToOneField(User, on_delete=models.PROTECT, related_name="profile")
    phone_number = models.CharField(max_length=25)
    is_driver = models.BooleanField(default=False)
    is_travel_agent = models.BooleanField(default=False)

    def __str__(self):
        return self.user.email


class PartnerForm(models.Model):
    CONTACT_METHODS = [
        ("email", "Email"),
        ("phone", "Phone Call"),
        ("text", "Text Message"),
    ]
    REFERRAL_SOURCES = [
        ("google", "Google Search"),
        ("social", "Social Media"),
        ("referral", "Referral from another Agent"),
        ("client", "Client Recommendation"),
        ("conference", "Industry Conference"),
        ("other", "Other"),
    ]
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("contacted", "Contacted"),
        ("converted", "Converted"),
        ("closed", "Closed"),
    ]
    AGENCY_SIZE_CHOICES = [
        ("solo", "Solo advisor"),
        ("2-5", "2–5 agents"),
        ("6-20", "6–20 agents"),
        ("21-50", "21–50 agents"),
        ("50+", "50+ agents"),
    ]
    name = models.CharField(max_length=100)
    email = models.EmailField()
    phone_number = models.CharField(max_length=15)
    preferred_contact = models.CharField(
        max_length=10, choices=CONTACT_METHODS, default="email"
    )
    agency_name = models.CharField(max_length=200)
    agency_website = models.CharField(max_length=200, blank=True, null=True)
    agency_size = models.CharField(
        max_length=10, choices=AGENCY_SIZE_CHOICES, blank=True,
        help_text="Roughly how many travel agents work at this agency.",
    )
    referral_source = models.CharField(
        max_length=60, choices=REFERRAL_SOURCES, default="other"
    )
    additional_info = models.TextField(null=True, blank=True)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default="pending", db_index=True,
    )
    contacted_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Timestamp when this inquiry was marked as contacted.",
    )
    notes = models.TextField(
        blank=True,
        help_text="Internal notes from staff about this partner inquiry.",
    )
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} - {self.agency_name}"


class ContactUsForm(models.Model):
    CONTACT_METHODS = [
        ("email", "Email"),
        ("phone", "Phone Call"),
        ("text", "Text Message"),
    ]
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("contacted", "Contacted"),
        ("closed", "Closed"),
    ]
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField()
    phone_number = models.CharField(max_length=15)
    contact_method = models.CharField(
        max_length=10, choices=CONTACT_METHODS, default="email"
    )
    about = models.TextField()
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default="pending"
    )
    contacted_at = models.DateTimeField(null=True, blank=True, help_text="Timestamp when form was marked as contacted")
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    def __str__(self):
        return f"{self.first_name} - {self.last_name}"


class NewsLetter(models.Model):
    name = models.CharField(max_length=60, null=True, blank=True)
    email = models.EmailField(unique=True)

    def __str__(self):
        return self.email


class NewsletterSubscriptionAttempt(models.Model):
    ip_address = models.GenericIPAddressField()
    email = models.EmailField()
    timestamp = models.DateTimeField(auto_now_add=True)
    success = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=["ip_address", "timestamp"]),
        ]

    def __str__(self):
        return f"{self.ip_address} - {self.email} - {self.timestamp}"


class PayoutDetails(models.Model):
    """Structured payout details, shared by agents and agencies.

    Replaces a single free-text handle. Each rail gets its own validated field so
    a payout run knows what it is holding -- a PayPal email and a Venmo handle
    are not interchangeable, and a mistyped handle pays a stranger irreversibly.
    Account numbers are encrypted; see users.payout_crypto.
    """

    # Rails a new partner may choose. Retired rails stay valid for existing rows
    # so history and admin display keep working -- they are simply not offered.
    SELECTABLE_PAYMENT_METHODS = ["paypal", "venmo", "bank"]
    RETIRED_PAYMENT_METHODS = ["zelle", "cashapp", "check", "other"]

    ACCOUNT_TYPE_CHOICES = [("checking", "Checking"), ("savings", "Savings")]

    paypal_email = models.EmailField(blank=True, default="", help_text="The email on the PayPal account.")
    venmo_handle = models.CharField(
        max_length=64, blank=True, default="", help_text="Venmo username, without the @."
    )
    bank_account_name = models.CharField(max_length=120, blank=True, default="")
    bank_routing_number = models.CharField(max_length=9, blank=True, default="")
    bank_account_type = models.CharField(
        max_length=10, blank=True, default="", choices=ACCOUNT_TYPE_CHOICES
    )
    # Ciphertext, plus the last four kept in clear so screens can identify an
    # account without decrypting anything.
    bank_account_encrypted = models.TextField(blank=True, default="")
    bank_account_last4 = models.CharField(max_length=4, blank=True, default="")

    class Meta:
        abstract = True

    def set_bank_account(self, number):
        """Store an account number encrypted, keeping the last four readable."""
        from .payout_crypto import encrypt, last4

        number = "".join(c for c in (number or "") if c.isdigit())
        self.bank_account_encrypted = encrypt(number)
        self.bank_account_last4 = last4(number)

    @property
    def bank_account_number(self):
        """The real account number. Decrypts on demand; '' if unreadable."""
        from .payout_crypto import decrypt

        return decrypt(self.bank_account_encrypted)

    @property
    def bank_account_masked(self):
        return f"••••{self.bank_account_last4}" if self.bank_account_last4 else ""

    def payout_target(self, method):
        """What this payee's chosen rail actually pays to, or '' if not set up."""
        if method == "paypal":
            return self.paypal_email or ""
        if method == "venmo":
            return self.venmo_handle or ""
        if method == "bank":
            complete = self.bank_account_last4 and self.bank_routing_number and self.bank_account_name
            return self.bank_account_masked if complete else ""
        # Retired rails were never structured; they still carry free text.
        return (getattr(self, "payment_info", "") or "").strip()


class TravelAgent(PayoutDetails):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    agent_name = models.CharField(
        max_length=100, help_text="Your full name", null=True, blank=True
    )
    include_in_confirmation_sms = models.BooleanField(
        default=False,
        help_text=(
            "If checked, confirmation texts to the guest will mention that the "
            "trip was booked through this agent/agency"
        ),
    )
    agency_name = models.CharField(max_length=100, null=True, blank=True)
    phone = models.CharField(max_length=20)
    commission_rate = models.DecimalField(max_digits=5, decimal_places=2, default=10.00)
    is_active = models.BooleanField(default=True)
    agency = models.ForeignKey(
        "Agency",
        null=True,
        blank=True,
        related_name="agents",
        on_delete=models.SET_NULL,
    )
    payment_info = models.CharField(
        max_length=200,
        help_text="Preferred Way to Get Paid & Information - Paypal/Zelle/CashApp/Bank Info etc.",
        null=True,
        blank=True,
    )
    last_payment_date = models.DateTimeField(null=True, blank=True)

    # Commission tracking fields
    total_paid_commission = models.DecimalField(
        max_digits=10, decimal_places=2, default=0.00
    )
    unpaid_commissions = models.DecimalField(
        max_digits=10, decimal_places=2, default=0.00
    )
    pending_commissions = models.DecimalField(
        max_digits=10, decimal_places=2, default=0.00
    )
    agency_handles_payment = models.BooleanField(
        default=False,
        help_text="If checked, commission payments will be made to the agency instead of directly to the agent",
    )

    PAYMENT_METHOD_CHOICES = [
        ("agency", "Agency"),
        ("paypal", "PayPal"),
        ("venmo", "Venmo"),
        ("zelle", "Zelle"),
        ("cashapp", "Cash App"),
        ("bank", "Bank Transfer"),
        ("check", "Check"),
        ("other", "Other"),
    ]
    payment_method = models.CharField(
        max_length=20,
        choices=PAYMENT_METHOD_CHOICES,
        null=True,
        blank=True,
        help_text="Select your preferred payment method",
    )
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    # ---------- Dashboard helper properties (no DB hits) ----------
    @property
    def routes_through_agency(self):
        """True if commissions for this agent are paid to their agency."""
        return bool(self.agency_handles_payment and self.agency_id)

    @property
    def effective_payment_method(self):
        """The method actually used for this agent — agency's if routed through agency, else own."""
        if self.routes_through_agency and self.agency:
            return self.agency.payment_method or ""
        return self.payment_method or ""

    @property
    def effective_payment_info(self):
        """The handle/info actually used — agency's if routed through agency, else own."""
        payee = self.agency if (self.routes_through_agency and self.agency) else self
        method = self.effective_payment_method
        # Structured detail for the chosen rail, falling back to legacy free text.
        return payee.payout_target(method) or (payee.payment_info or "")

    @property
    def payment_info_complete(self):
        """True only when both method and handle are populated on the effective payee."""
        return bool(self.effective_payment_method) and bool(self.effective_payment_info)

    def calculate_unpaid_commissions(self):
        """Sum of commission amounts that are currently SAFE TO PAY (Ready bucket).

        Delegates to users.eligibility -- so the queue, the preview, and the
        actual pay action all agree on what counts as "owed". A reservation only
        contributes if eligibility says READY: customer paid us, no refunds, not
        cancelled, and either status=completed OR the final leg date + grace
        period has passed.
        """
        from users.eligibility import sum_ready
        return sum_ready(self)

    def calculate_pending_commissions(self):
        """Sum of commission amounts currently in the PENDING bucket.

        Pending = future trips and trips still inside the post-leg grace window.
        Does NOT include trips that are stuck in review or excluded -- those are
        surfaced separately so they don't quietly inflate the "pending" KPI.
        """
        from users.eligibility import sum_pending
        return sum_pending(self)

    def update_unpaid_commissions(self):
        """Calculate and update the unpaid commissions for this agent."""
        unpaid_amount = self.calculate_unpaid_commissions()
        self.unpaid_commissions = unpaid_amount
        self.save(update_fields=["unpaid_commissions"])
        return unpaid_amount

    def update_commission_stats(self):
        """Calculate and update all commission statistics for this agent."""
        # Reuse the calculation methods
        pending_amount = self.calculate_pending_commissions()
        unpaid_amount = self.calculate_unpaid_commissions()

        # Update the fields
        self.pending_commissions = pending_amount
        self.unpaid_commissions = unpaid_amount
        self.save(update_fields=["pending_commissions", "unpaid_commissions"])

        return {"pending": pending_amount, "unpaid": unpaid_amount}

    def sync_paid_commission(self):
        """
        Sync the agent's total_paid_commission with the sum of their actual payouts.
        This ensures the total_paid_commission matches the actual payout records.
        """
        from django.db.models import Sum
        from decimal import Decimal

        # Get sum of all payouts
        total_from_payouts = CommissionPayout.objects.filter(agent=self).aggregate(
            total=Sum("total_amount")
        )["total"] or Decimal("0")

        # Update if different
        if self.total_paid_commission != total_from_payouts:
            self.total_paid_commission = total_from_payouts
            self.save(update_fields=["total_paid_commission"])
            return True
        return False

    def sync_total_paid_commission(self):
        """
        Force sync the agent's total_paid_commission with their actual payouts.
        This is used to fix any discrepancies in the total_paid_commission field.
        """
        from django.db.models import Sum
        from decimal import Decimal

        # Get sum of all payouts
        total_from_payouts = CommissionPayout.objects.filter(agent=self).aggregate(
            total=Sum("total_amount")
        )["total"] or Decimal("0")

        # Always update to match payouts
        self.total_paid_commission = total_from_payouts
        self.save(update_fields=["total_paid_commission"])
        return total_from_payouts

    def process_commission_payment(self, create_agency_payout=True):
        from .partner_services import process_recorded_payout
        # Agent entry points pay only the direct-to-agent group. Agency groups
        # have their own entry point, including when the agent has since left.
        return process_recorded_payout(self, agency=None, create_agency_payout=create_agency_payout)

    def __str__(self):
        return f"{self.agent_name} - {self.agency}"

    class Meta:
        verbose_name = "Travel Agent"
        verbose_name_plural = "Travel Agents"


class CommissionPayout(models.Model):
    agent = models.ForeignKey(TravelAgent, on_delete=models.CASCADE)
    agency = models.ForeignKey(
        "Agency",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="agent_payouts",
    )
    reservations = models.ManyToManyField("reservations.Reservation")

    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    payout_period_start = models.DateField()
    payout_period_end = models.DateField()
    paid_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True)

    # Captured at mark-paid time: external transaction ID and the method actually used.
    payment_reference = models.CharField(max_length=120, blank=True, default="")
    payment_method_used = models.CharField(
        max_length=20,
        blank=True,
        default="",
        choices=TravelAgent.PAYMENT_METHOD_CHOICES,
    )

    # Non-persistent field to track signal processing
    _skip_signal_handler = False

    def __str__(self):
        if self.agency:
            return f"{self.agency.name} (Agent: {self.agent}) – {self.payout_period_start.strftime('%b %Y')} – ${self.total_amount}"
        return f"{self.agent} – {self.payout_period_start.strftime('%b %Y')} – ${self.total_amount}"



class AgencyCommissionPayout(models.Model):
    agency = models.ForeignKey(
        "Agency", on_delete=models.CASCADE, related_name="commission_payouts"
    )
    agent_payouts = models.ManyToManyField(
        CommissionPayout, related_name="agency_payouts"
    )
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    payout_period_start = models.DateField()
    payout_period_end = models.DateField()
    paid_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True)

    payment_reference = models.CharField(max_length=120, blank=True, default="")
    payment_method_used = models.CharField(
        max_length=20,
        blank=True,
        default="",
        choices=TravelAgent.PAYMENT_METHOD_CHOICES,
    )

    def __str__(self):
        return f"{self.agency.name} – {self.payout_period_start.strftime('%b %Y')} – ${self.total_amount}"

    class Meta:
        verbose_name = "Agency Commission Payout"
        verbose_name_plural = "Agency Commission Payouts"


class Agency(PayoutDetails):
    """
    Represents a travel agency with multiple travel agents
    """

    # Some agencies pay their own agents and are always the payee. Stating that
    # once stops every new agent being adjudicated by hand -- and stops an agent
    # believing they arranged direct payment when the agency never allows it.
    PAYOUT_POLICY_CHOICES = [
        ("either", "Agents may ask to be paid directly"),
        ("agency", "Grayson always pays the agency, never the agent"),
    ]
    payout_policy = models.CharField(
        max_length=10,
        choices=PAYOUT_POLICY_CHOICES,
        default="either",
        help_text="Whether this agency's agents may be paid directly by Grayson.",
    )

    name = models.CharField(max_length=100)
    address = models.TextField(blank=True, null=True)
    phone = models.CharField(max_length=20, blank=True, null=True)
    website = models.URLField(blank=True, null=True)

    # Agency head/admin user account
    heads = models.ManyToManyField(
        User,
        related_name="managed_agency",
        help_text="User who manages this agency and can see all agents' data",
        blank=True,
    )
    # Payment information for the agency as a whole
    payment_info = models.TextField(blank=True, null=True)
    payment_method = models.CharField(
        max_length=20,
        choices=TravelAgent.PAYMENT_METHOD_CHOICES,
        null=True,
        blank=True,
    )

    # Logo and branding (optional)
    logo = models.ImageField(upload_to="agency_logos/", blank=True, null=True)
    # Agency-level commission tracking
    total_paid_commission = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0.00,
        help_text="Total commission paid to this agency across all agents",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name

    @property
    def payment_info_complete(self):
        return bool(self.payment_method) and bool(
            self.payout_target(self.payment_method) or self.payment_info
        )

    class Meta:
        verbose_name = "Agency"
        verbose_name_plural = "Agencies"

    def get_all_agents(self):
        """Return all travel agents associated with this agency"""
        return TravelAgent.objects.filter(agency=self)

    def get_total_pending_commissions(self):
        """Calculate total pending commissions across all agents"""
        return self.agents.aggregate(total=Sum("pending_commissions"))["total"] or 0

    def get_total_unpaid_commissions(self):
        """Calculate total unpaid commissions across all agents"""
        return self.agents.aggregate(total=Sum("unpaid_commissions"))["total"] or 0

    def get_total_paid_commissions(self):
        """Calculate total paid commissions across all agents"""
        return self.agents.aggregate(total=Sum("total_paid_commission"))["total"] or 0

    def sync_paid_commission(self):
        """
        Sync the agency's total_paid_commission with the sum of their actual payouts.
        This ensures the total_paid_commission matches the actual payout records.
        """
        from django.db.models import Sum
        from decimal import Decimal

        # Get sum of all agency payouts
        total_from_payouts = AgencyCommissionPayout.objects.filter(
            agency=self
        ).aggregate(total=Sum("total_amount"))["total"] or Decimal("0")

        # Update if different
        if self.total_paid_commission != total_from_payouts:
            self.total_paid_commission = total_from_payouts
            self.save(update_fields=["total_paid_commission"])
            return True
        return False

    def update_commission_stats(self):
        """Update commission statistics for the agency"""
        # First sync the paid commission to ensure accuracy
        self.sync_paid_commission()

        # Calculate total unpaid and pending commissions
        total_unpaid = self.get_total_unpaid_commissions()
        total_pending = self.get_total_pending_commissions()
        total_paid = self.total_paid_commission  # Use the synced value

        # Return stats
        return {
            "unpaid": total_unpaid,
            "pending": total_pending,
            "paid": total_paid,
            "agents_count": self.agents.count(),
        }

    def process_agency_commission_payment(self):
        from .partner_services import process_recorded_agency_payout
        return process_recorded_agency_payout(self)


# Registered here so Django discovers the additive onboarding models.
from .partner_models import (PartnerIdentity, PartnerToken, AgencyAlias,
    AgencyApplication, AffiliationClaim, AgencyMembership, PartnerBooking,
    PartnerEvent, PartnerOutbox, PartnerBackfill, PartnerNameLock, PartnerPayoutAdjustment)
