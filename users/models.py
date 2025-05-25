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
    name = models.CharField(max_length=100)
    email = models.EmailField()
    phone_number = models.CharField(max_length=15)
    preferred_contact = models.CharField(
        max_length=10, choices=CONTACT_METHODS, default="email"
    )
    agency_name = models.CharField(max_length=200)
    agency_website = models.CharField(max_length=200, blank=True, null=True)
    referral_source = models.CharField(
        max_length=60, choices=REFERRAL_SOURCES, default="other"
    )
    additional_info = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    def __str__(self):
        return f"{self.name} - {self.agency_name}"


class ContactUsForm(models.Model):
    CONTACT_METHODS = [
        ("email", "Email"),
        ("phone", "Phone Call"),
        ("text", "Text Message"),
    ]
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField()
    phone_number = models.CharField(max_length=15)
    contact_method = models.CharField(
        max_length=10, choices=CONTACT_METHODS, default="email"
    )
    about = models.TextField()
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

    def calculate_unpaid_commissions(self):
        """Calculate unpaid commissions based on commission_rate without saving."""
        from django.db.models import Sum, F, ExpressionWrapper, DecimalField
        from reservations.models import Reservation
        import logging

        logger = logging.getLogger(__name__)

        # Calculate the sum of unpaid commissions from completed reservations
        # This applies the agent's commission rate to each reservation total_price
        unpaid_reservations = Reservation.objects.filter(
            travel_agent=self, commission_paid=False, status="completed"
        )

        logger.info(
            f"Found {unpaid_reservations.count()} unpaid completed reservations for agent {self}"
        )

        unpaid_commissions = (
            unpaid_reservations.annotate(
                calculated_commission=ExpressionWrapper(
                    F("total_price") * (self.commission_rate / 100),
                    output_field=DecimalField(max_digits=10, decimal_places=2),
                )
            ).aggregate(total=Sum("calculated_commission"))["total"]
            or 0
        )

        logger.info(
            f"Calculated unpaid commissions for agent {self}: ${unpaid_commissions}"
        )

        return unpaid_commissions

    def calculate_pending_commissions(self):
        """Calculate pending commissions based on commission_rate without saving."""
        from django.db.models import Sum, F, ExpressionWrapper, DecimalField
        from reservations.models import Reservation

        # Calculate pending commissions (confirmed but not completed)
        pending_commissions = (
            Reservation.objects.filter(travel_agent=self, status="confirmed")
            .annotate(
                calculated_commission=ExpressionWrapper(
                    F("total_price") * (self.commission_rate / 100),
                    output_field=DecimalField(max_digits=10, decimal_places=2),
                )
            )
            .aggregate(total=Sum("calculated_commission"))["total"]
            or 0
        )

        return pending_commissions

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
        Process payment for all unpaid commissions.
        Enhanced with better notes generation.

        Args:
            create_agency_payout (bool): Whether to create an agency payout if agent belongs to an agency
        """
        from django.db.models import Sum, F, ExpressionWrapper, DecimalField
        from django.utils import timezone
        from django.db import transaction
        from reservations.models import Reservation

        with transaction.atomic():
            # Get all unpaid completed reservations
            unpaid_reservations = Reservation.objects.filter(
                travel_agent=self, commission_paid=False, status="completed"
            )

            if unpaid_reservations.exists():
                # Calculate commission amounts for each reservation
                reservations_with_commission = unpaid_reservations.annotate(
                    calculated_commission=ExpressionWrapper(
                        F("total_price") * (self.commission_rate / 100),
                        output_field=DecimalField(max_digits=10, decimal_places=2),
                    )
                )

                # Calculate total commission
                commission_total = sum(
                    r.calculated_commission for r in reservations_with_commission
                )

                # Get date range
                period_start = unpaid_reservations.earliest(
                    "created_at"
                ).created_at.date()
                period_end = unpaid_reservations.latest("created_at").created_at.date()

                # Build detailed reservation summary for notes
                reservation_details = []
                for res in reservations_with_commission:
                    reservation_details.append(
                        f"#{res.id} - {res.customer} (${res.total_price:.2f} -> ${res.calculated_commission:.2f})"
                    )

                # Create comprehensive notes
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

                # Add reservation details (limit to first 15 to avoid overly long notes)
                display_reservations = reservation_details[:15]
                agent_payout_notes.extend(display_reservations)

                if len(reservation_details) > 15:
                    agent_payout_notes.append(
                        f"... and {len(reservation_details) - 15} more reservations"
                    )

                notes_text = "\n".join(agent_payout_notes)

                # Create agent payout record with detailed notes
                agent_payout = CommissionPayout.objects.create(
                    agent=self,
                    agency=self.agency,  # This will be set automatically if agent has agency
                    total_amount=commission_total,
                    payout_period_start=period_start,
                    payout_period_end=period_end,
                    notes=notes_text,
                )

                # Add reservations to payout
                agent_payout.reservations.set(unpaid_reservations)

                # Mark reservations as paid and store the calculated commission
                for reservation in reservations_with_commission:
                    reservation.commission_amount = reservation.calculated_commission
                    reservation.commission_paid = True
                    reservation.commission_paid_at = timezone.now()
                    reservation.save(
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
        return f"{self.agency_name or self.agent_name or self.user.username}"

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
        """Process payment for all unpaid commissions from all agents in the agency."""
        from django.db.models import Sum, F, ExpressionWrapper, DecimalField
        from django.utils import timezone
        from django.db import transaction
        from reservations.models import Reservation
        from decimal import Decimal

        with transaction.atomic():
            # Get all agents in the agency with unpaid commissions
            agents = self.agents.filter(
                unpaid_commissions__gt=0, agency_handles_payment=True
            )

            if not agents.exists():
                return None, 0

            # Track all processed payouts and total amount
            processed_payouts = []
            total_amount = Decimal("0")
            earliest_date = None
            latest_date = None

            # Track details for comprehensive notes
            agent_details = []
            total_reservations = 0

            # Process each agent's commissions
            for agent in agents:
                # Get all unpaid completed reservations for this agent
                unpaid_reservations = Reservation.objects.filter(
                    travel_agent=agent, commission_paid=False, status="completed"
                )

                if unpaid_reservations.exists():
                    # Calculate commission amounts for each reservation
                    reservations_with_commission = unpaid_reservations.annotate(
                        calculated_commission=ExpressionWrapper(
                            F("total_price") * (agent.commission_rate / 100),
                            output_field=DecimalField(max_digits=10, decimal_places=2),
                        )
                    )

                    # Calculate total commission for this agent
                    agent_commission_total = sum(
                        r.calculated_commission for r in reservations_with_commission
                    )

                    if agent_commission_total > 0:
                        # Get date range for this agent's reservations
                        agent_earliest_date = unpaid_reservations.earliest(
                            "created_at"
                        ).created_at.date()
                        agent_latest_date = unpaid_reservations.latest(
                            "created_at"
                        ).created_at.date()

                        # Update overall date range
                        if earliest_date is None or agent_earliest_date < earliest_date:
                            earliest_date = agent_earliest_date
                        if latest_date is None or agent_latest_date > latest_date:
                            latest_date = agent_latest_date

                        # Collect reservation details for notes
                        reservation_ids = list(
                            unpaid_reservations.values_list("id", flat=True)
                        )
                        reservation_count = len(reservation_ids)
                        total_reservations += reservation_count

                        # Build detailed notes for individual agent payout
                        agent_reservation_details = []
                        for res in reservations_with_commission:
                            agent_reservation_details.append(
                                f"#{res.id} (${res.total_price:.2f} -> ${res.calculated_commission:.2f})"
                            )

                        individual_agent_notes = (
                            f"Agent: {agent.agent_name or agent.user.username} ({agent.user.email})\n"
                            f"Commission Rate: {agent.commission_rate}%\n"
                            f"Reservations ({reservation_count}): {', '.join(agent_reservation_details[:10])}"
                            f"{'...' if reservation_count > 10 else ''}\n"
                            f"Period: {agent_earliest_date} to {agent_latest_date}\n"
                            f"Total Commission: ${agent_commission_total:.2f}\n"
                            f"Processed as part of agency batch payout to {self.name}"
                        )

                        # Create individual agent payout with detailed notes
                        agent_payout = CommissionPayout.objects.create(
                            agent=agent,
                            agency=self,
                            total_amount=agent_commission_total,
                            payout_period_start=agent_earliest_date,
                            payout_period_end=agent_latest_date,
                            notes=individual_agent_notes,
                        )

                        # Add reservations to payout
                        agent_payout.reservations.set(unpaid_reservations)

                        # Mark reservations as paid
                        for reservation in reservations_with_commission:
                            reservation.commission_amount = (
                                reservation.calculated_commission
                            )
                            reservation.commission_paid = True
                            reservation.commission_paid_at = timezone.now()
                            reservation.save(
                                update_fields=[
                                    "commission_amount",
                                    "commission_paid",
                                    "commission_paid_at",
                                ]
                            )

                        # Update agent totals
                        agent.unpaid_commissions = 0
                        agent.last_payment_date = timezone.now()
                        agent.save(
                            update_fields=["unpaid_commissions", "last_payment_date"]
                        )

                        # Sync the agent's total_paid_commission with their actual payouts
                        agent.sync_paid_commission()

                        processed_payouts.append(agent_payout)
                        total_amount += agent_commission_total

                        # Store agent details for agency payout notes
                        agent_details.append(
                            {
                                "name": agent.agent_name or agent.user.username,
                                "email": agent.user.email,
                                "commission_rate": agent.commission_rate,
                                "reservation_count": reservation_count,
                                "reservation_ids": reservation_ids[
                                    :5
                                ],  # First 5 IDs for summary
                                "total_reservations": reservation_count,
                                "amount": agent_commission_total,
                                "period_start": agent_earliest_date,
                                "period_end": agent_latest_date,
                            }
                        )

            if processed_payouts:
                # Build comprehensive agency payout notes
                agency_notes_lines = [
                    f"AGENCY BATCH PAYOUT - {self.name}",
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
                    agency_notes_lines.extend(
                        [
                            f"{i}. {agent_detail['name']} ({agent_detail['email']})",
                            f"   Rate: {agent_detail['commission_rate']}% | Reservations: {agent_detail['reservation_count']} | Amount: ${agent_detail['amount']:.2f}",
                            f"   Period: {agent_detail['period_start']} to {agent_detail['period_end']}",
                            f"   Sample Reservation IDs: {', '.join(map(str, agent_detail['reservation_ids']))}{'...' if agent_detail['total_reservations'] > 5 else ''}",
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
