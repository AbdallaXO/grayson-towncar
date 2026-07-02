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


class TravelAgent(models.Model):
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
        if self.routes_through_agency and self.agency:
            return self.agency.payment_info or ""
        return self.payment_info or ""

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
        """
        Process payment for all currently-Ready commissions.

        Uses users.eligibility.ready_reservations as the single source of truth
        for what to include -- the same helper that the queue UI and previews
        use. This guarantees the operator can never accidentally pay something
        that the queue marked as Needs Review or Excluded (refunded, unpaid,
        cancelled, etc.) by clicking Pay Now.

        Args:
            create_agency_payout (bool): Whether to create an agency payout if agent belongs to an agency
        """
        from django.utils import timezone
        from django.db import transaction
        from users.eligibility import ready_reservations

        with transaction.atomic():
            # Pull every Ready reservation + its computed commission in one pass.
            # Keep the (reservation, EligibilityResult) tuples so we can use the
            # already-computed commission decimal instead of recalculating.
            ready_items = list(ready_reservations(self))

            if ready_items:
                commission_total = sum((r.commission for _, r in ready_items), Decimal("0"))
                ready_reservation_objs = [res for res, _ in ready_items]
                ready_reservation_ids = [res.id for res in ready_reservation_objs]

                # Period start = earliest pickup_date across legs of all paid reservations.
                # Period end = today (the day this payout was processed).
                earliest_pickup_date = None
                for reservation in ready_reservation_objs:
                    for leg in reservation.legs.all():
                        if leg.pickup_date and (earliest_pickup_date is None or leg.pickup_date < earliest_pickup_date):
                            earliest_pickup_date = leg.pickup_date

                period_start = earliest_pickup_date if earliest_pickup_date else timezone.localtime(timezone.now()).date()
                period_end = timezone.localtime(timezone.now()).date()

                # Build detailed reservation summary for notes.
                reservation_details = []
                for res, result in ready_items:
                    reservation_details.append(
                        f"#{res.id} - {res.customer} (Base: ${res.base_price:.2f}, Total: ${res.total_price:.2f} -> Commission: ${result.commission:.2f})"
                    )

                agent_payout_notes = [
                    f"DIRECT AGENT PAYOUT",
                    f"Agent: {self.agent_name or self.user.username} ({self.user.email})",
                    f"Agency: {self.agency.name if self.agency else 'Independent'}",
                    f"Commission Rate: {self.commission_rate}%",
                    f"Period: {period_start} to {period_end}",
                    f"Processed: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    f"Total Reservations: {len(reservation_details)}",
                    f"Total Commission: ${commission_total:.2f}",
                    "",
                    "RESERVATION BREAKDOWN:",
                    "-" * 40,
                ]

                display_reservations = reservation_details[:15]
                agent_payout_notes.extend(display_reservations)

                if len(reservation_details) > 15:
                    agent_payout_notes.append(
                        f"... and {len(reservation_details) - 15} more reservations"
                    )

                notes_text = "\n".join(agent_payout_notes)

                agent_payout = CommissionPayout.objects.create(
                    agent=self,
                    agency=self.agency,
                    total_amount=commission_total,
                    payout_period_start=period_start,
                    payout_period_end=period_end,
                    notes=notes_text,
                )

                agent_payout.reservations.set(ready_reservation_ids)

                # Mark each paid reservation: persist the actually-paid commission
                # (could differ from stored commission_amount if rate changed since booking).
                for res, result in ready_items:
                    res.commission_amount = result.commission
                    res.commission_paid = True
                    res.commission_paid_at = timezone.now()
                    res.save(
                        update_fields=[
                            "commission_amount",
                            "commission_paid",
                            "commission_paid_at",
                        ]
                    )

                # Update agent totals
                self.total_paid_commission += commission_total
                self.last_payment_date = timezone.now()
                self.unpaid_commissions = 0
                self.save(
                    update_fields=[
                        "total_paid_commission",
                        "last_payment_date",
                        "unpaid_commissions",
                    ]
                )

                # Create agency payout if agent belongs to an agency and flag is set
                agency_payout = None
                if self.agency and self.agency_handles_payment and create_agency_payout:
                    # Create detailed agency payout notes
                    agency_payout_notes = [
                        f"AGENCY PAYOUT - {self.agency.name}",
                        f"Single Agent Commission Payment",
                        f"Agent: {self.agent_name or self.user.username} ({self.user.email})",
                        f"Commission Rate: {self.commission_rate}%",
                        f"Period: {period_start} to {period_end}",
                        f"Processed: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}",
                        f"Reservations: {len(reservation_details)}",
                        f"Amount: ${commission_total:.2f}",
                        "",
                        "LINKED AGENT PAYOUT:",
                        f"Agent Payout ID: #{agent_payout.id}",
                        "",
                        "SAMPLE RESERVATIONS:",
                        "-" * 30,
                    ]

                    # Add sample reservations (first 5)
                    sample_reservations = reservation_details[:5]
                    agency_payout_notes.extend(sample_reservations)
                    if len(reservation_details) > 5:
                        agency_payout_notes.append(
                            f"... and {len(reservation_details) - 5} more"
                        )

                    agency_notes_text = "\n".join(agency_payout_notes)

                    # Create corresponding agency payout
                    agency_payout = AgencyCommissionPayout.objects.create(
                        agency=self.agency,
                        total_amount=commission_total,
                        payout_period_start=agent_payout.payout_period_start,
                        payout_period_end=agent_payout.payout_period_end,
                        notes=agency_notes_text,
                    )

                    # Link the agent payout to the agency payout
                    agency_payout.agent_payouts.add(agent_payout)

                    # Update agency's total paid commission
                    self.agency.total_paid_commission += commission_total
                    self.agency.save(update_fields=["total_paid_commission"])

                # Double-check that total_paid_commission matches payouts
                self.sync_paid_commission()

                return agent_payout, commission_total, agency_payout

            return None, 0, None

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

    def save(self, *args, **kwargs):
        # If agent has an agency, set it automatically
        if not self.agency and self.agent.agency:
            self.agency = self.agent.agency
        super().save(*args, **kwargs)


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


class Agency(models.Model):
    """
    Represents a travel agency with multiple travel agents
    """

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
        return bool(self.payment_method) and bool(self.payment_info)

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
        """Process payment for all currently-Ready commissions from every agent in the agency.

        Delegates per-agent work to TravelAgent.process_commission_payment so the
        eligibility logic lives in exactly one place (users.eligibility). This
        function only does the agency-level orchestration: aggregating per-agent
        payouts into one AgencyCommissionPayout, building the summary notes, and
        bumping the agency-level total_paid_commission once.
        """
        from django.utils import timezone
        from django.db import transaction
        from reservations.models import Reservation
        from decimal import Decimal

        with transaction.atomic():
            # All agents who route through the agency. We can't pre-filter on
            # unpaid_commissions__gt=0 because that stat may be stale relative
            # to the live eligibility helper -- let the helper decide per agent.
            agents = self.agents.filter(agency_handles_payment=True).select_related("user")

            processed_payouts = []
            total_amount = Decimal("0")
            earliest_date = None
            latest_date = None
            total_reservations = 0
            agent_details = []

            for agent in agents:
                # Run the per-agent processor with create_agency_payout=False so it
                # doesn't try to spawn its own AgencyCommissionPayout -- we make
                # the combined one ourselves below.
                agent_payout, agent_commission_total, _ = agent.process_commission_payment(
                    create_agency_payout=False
                )
                if not agent_payout or agent_commission_total <= 0:
                    continue

                processed_payouts.append(agent_payout)
                total_amount += agent_commission_total
                total_reservations += agent_payout.reservations.count()

                # Pull the period range from the agent payout the per-agent
                # processor just stamped (uses the same pickup-date logic).
                if earliest_date is None or agent_payout.payout_period_start < earliest_date:
                    earliest_date = agent_payout.payout_period_start
                if latest_date is None or agent_payout.payout_period_end > latest_date:
                    latest_date = agent_payout.payout_period_end

                agent_details.append({
                    "name": agent.agent_name or agent.user.username,
                    "email": agent.user.email,
                    "commission_rate": agent.commission_rate,
                    "reservation_count": agent_payout.reservations.count(),
                    "reservation_ids": list(
                        agent_payout.reservations.values_list("id", flat=True)[:5]
                    ),
                    "total_reservations": agent_payout.reservations.count(),
                    "amount": agent_commission_total,
                    "period_start": agent_payout.payout_period_start,
                    "period_end": agent_payout.payout_period_end,
                })

            if processed_payouts:
                # Build comprehensive agency payout notes
                agency_notes_lines = [
                    f"AGENCY PAYOUT - {self.name}",
                    f"Processed: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    f"Period: {earliest_date} to {latest_date}",
                    f"Total Agents: {len(agent_details)}",
                    f"Total Reservations: {total_reservations}",
                    f"Total Amount: ${total_amount:.2f}",
                    "",
                    "AGENT BREAKDOWN:",
                    "-" * 50,
                ]

                for i, agent_detail in enumerate(agent_details, 1):
                    # Get detailed reservation information for this agent
                    agent_reservations = Reservation.objects.filter(
                        id__in=agent_detail["reservation_ids"]
                    ).select_related(
                        "customer",
                        "rate",
                        "rate__route",
                        "rate__route__origin",
                        "rate__route__destination",
                        "vehicle",
                    )

                    agency_notes_lines.extend(
                        [
                            f"{i}. {agent_detail['name']}",
                            f"   Rate: {agent_detail['commission_rate']}% | Reservations: {agent_detail['reservation_count']} | Amount: ${agent_detail['amount']:.2f}",
                            f"   Period: {agent_detail['period_start']} ⇄ {agent_detail['period_end']}",
                            "",
                            "   RESERVATION DETAILS:",
                            "   " + "-" * 40,
                        ]
                    )

                    # Add detailed reservation information
                    for res in agent_reservations:
                        agency_notes_lines.extend(
                            [
                                f"   - Reservation #{res.id}",
                                f"     Customer: {res.customer.get_full_name()}",
                                f"     Route: {res.route_label}",
                                f"     Vehicle: {res.vehicle.vehicle_type.title() if res.vehicle else 'N/A'}",
                                f"     Trip Type: {res.trip_type.replace('_', ' ').title()}",
                                f"     Amount: ${res.total_price:.2f}",
                                f"     Commission: ${res.commission_amount:.2f}",
                                f"     Date: {res.created_at.strftime('%Y-%m-%d')}",
                                "",
                            ]
                        )

                    agency_notes_lines.extend(
                        [
                            "   " + "-" * 40,
                            "",
                        ]
                    )

                agency_notes_lines.extend(
                    [
                        "-" * 50,
                        f"Individual agent payouts created: {len(processed_payouts)}",
                    ]
                )

                comprehensive_notes = "\n".join(agency_notes_lines)

                # Create agency payout record with detailed notes
                agency_payout = AgencyCommissionPayout.objects.create(
                    agency=self,
                    total_amount=total_amount,
                    payout_period_start=earliest_date,
                    payout_period_end=latest_date,
                    notes=comprehensive_notes,
                )

                # Link all agent payouts to this agency payout
                agency_payout.agent_payouts.set(processed_payouts)

                # Update agency's total paid commission
                self.total_paid_commission += total_amount
                self.save(update_fields=["total_paid_commission"])

                return agency_payout, total_amount

            return None, 0
