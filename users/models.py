from django.db import models
from django.contrib.auth.models import User
from django.db.models import Sum, Count, Q
from decimal import Decimal
from reservations.models import Reservation

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

from django.db import models
from django.contrib.auth.models import User
from django.db.models import Sum, Count, Q
from decimal import Decimal
from reservations.models import Reservation


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
        'Agency', 
        null=True, 
        blank=True,
        related_name='agents',
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

        # Calculate the sum of unpaid commissions from completed reservations
        # This applies the agent's commission rate to each reservation total_price
        unpaid_commissions = Reservation.objects.filter(
            travel_agent=self, commission_paid=False, status="completed"
        ).annotate(
            calculated_commission=ExpressionWrapper(
                F('total_price') * (self.commission_rate / 100),
                output_field=DecimalField(max_digits=10, decimal_places=2)
            )
        ).aggregate(
            total=Sum('calculated_commission')
        )['total'] or 0
        
        return unpaid_commissions

    def calculate_pending_commissions(self):
        """Calculate pending commissions based on commission_rate without saving."""
        from django.db.models import Sum, F, ExpressionWrapper, DecimalField
        from reservations.models import Reservation
        
        # Calculate pending commissions (confirmed but not completed)
        pending_commissions = Reservation.objects.filter(
            travel_agent=self, status="confirmed"
        ).annotate(
            calculated_commission=ExpressionWrapper(
                F('total_price') * (self.commission_rate / 100),
                output_field=DecimalField(max_digits=10, decimal_places=2)
            )
        ).aggregate(
            total=Sum('calculated_commission')
        )['total'] or 0
        
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
        total_from_payouts = CommissionPayout.objects.filter(
            agent=self
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')

        # Update if different
        if self.total_paid_commission != total_from_payouts:
            self.total_paid_commission = total_from_payouts
            self.save(update_fields=['total_paid_commission'])
            return True
        return False

    def process_commission_payment(self):
        """Process payment for all unpaid commissions."""
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
                        F('total_price') * (self.commission_rate / 100),
                        output_field=DecimalField(max_digits=10, decimal_places=2)
                    )
                )

                # Calculate total commission
                commission_total = sum(
                    r.calculated_commission for r in reservations_with_commission
                )

                # Create payout record
                from .models import CommissionPayout

                payout = CommissionPayout.objects.create(
                    agent=self,
                    total_amount=commission_total,
                    payout_period_start=unpaid_reservations.earliest(
                        "created_at"
                    ).created_at.date(),
                    payout_period_end=unpaid_reservations.latest(
                        "created_at"
                    ).created_at.date(),
                )

                # Add reservations to payout
                payout.reservations.set(unpaid_reservations)

                # Mark reservations as paid and store the calculated commission
                for reservation in reservations_with_commission:
                    reservation.commission_amount = reservation.calculated_commission
                    reservation.commission_paid = True
                    reservation.commission_paid_at = timezone.now()
                    reservation.save(update_fields=['commission_amount', 'commission_paid', 'commission_paid_at'])

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

                # Double-check that total_paid_commission matches payouts
                self.sync_paid_commission()

                return payout, commission_total

            return None, 0

    def __str__(self):
        return f"{self.agency_name or self.agent_name or self.user.username}"

    class Meta:
        verbose_name = "Travel Agent"
        verbose_name_plural = "Travel Agents"


class CommissionPayout(models.Model):
    agent = models.ForeignKey(TravelAgent, on_delete=models.CASCADE)
    reservations = models.ManyToManyField("reservations.Reservation")

    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    payout_period_start = models.DateField()
    payout_period_end = models.DateField()
    paid_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True)
    
    # Non-persistent field to track signal processing
    _skip_signal_handler = False

    def __str__(self):
        return f"{self.agent} – {self.payout_period_start.strftime('%b %Y')} – ${self.total_amount}"


class AgencyCommissionPayout(models.Model):
    agency = models.ForeignKey('Agency', on_delete=models.CASCADE, related_name='commission_payouts')
    agent_payouts = models.ManyToManyField(CommissionPayout, related_name='agency_payouts')
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
    head = models.ForeignKey(
        User, 
        on_delete=models.SET_NULL, 
        related_name="managed_agency",
        help_text="User who manages this agency and can see all agents' data",
        null=True
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
    logo = models.ImageField(upload_to='agency_logos/', blank=True, null=True)
    # Agency-level commission tracking
    total_paid_commission = models.DecimalField(
        max_digits=10, decimal_places=2, default=0.00,
        help_text="Total commission paid to this agency across all agents"
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
        return self.agents.aggregate(
            total=Sum('pending_commissions')
        )['total'] or 0
    
    def get_total_unpaid_commissions(self):
        """Calculate total unpaid commissions across all agents"""
        return self.agents.aggregate(
            total=Sum('unpaid_commissions')
        )['total'] or 0
        
    def get_total_paid_commissions(self):
        """Calculate total paid commissions across all agents"""
        return self.agents.aggregate(
            total=Sum('total_paid_commission')
        )['total'] or 0
    
    def update_commission_stats(self):
        """Update commission statistics for the agency"""
        # Calculate total unpaid and pending commissions
        total_unpaid = self.get_total_unpaid_commissions()
        total_pending = self.get_total_pending_commissions()
        total_paid = self.get_total_paid_commissions()
        
        # Return stats
        return {
            "unpaid": total_unpaid,
            "pending": total_pending,
            "paid": total_paid,
            "agents_count": self.agents.count()
        }

    def process_agency_commission_payment(self):
        """Process payment for all unpaid commissions from all agents in the agency."""
        from django.db.models import Sum, F, ExpressionWrapper, DecimalField
        from django.utils import timezone
        from django.db import transaction
        from reservations.models import Reservation

        with transaction.atomic():
            # Get all agents in the agency
            agents = self.agents.all()
            if not agents.exists():
                return None, 0

            # Track all processed payouts and total amount
            processed_payouts = []
            total_amount = Decimal('0')

            # Process each agent's commissions
            for agent in agents:
                # Get all unpaid completed reservations for this agent
                unpaid_reservations = Reservation.objects.filter(
                    travel_agent=agent, 
                    commission_paid=False, 
                    status="completed"
                )

                if unpaid_reservations.exists():
                    # Calculate commission amounts for each reservation
                    reservations_with_commission = unpaid_reservations.annotate(
                        calculated_commission=ExpressionWrapper(
                            F('total_price') * (agent.commission_rate / 100),
                            output_field=DecimalField(max_digits=10, decimal_places=2)
                        )
                    )

                    # Calculate total commission for this agent
                    agent_commission_total = sum(
                        r.calculated_commission for r in reservations_with_commission
                    )

                    if agent_commission_total > 0:
                        # Create individual agent payout
                        agent_payout = CommissionPayout.objects.create(
                            agent=agent,
                            total_amount=agent_commission_total,
                            payout_period_start=unpaid_reservations.earliest("created_at").created_at.date(),
                            payout_period_end=unpaid_reservations.latest("created_at").created_at.date(),
                        )

                        # Add reservations to payout
                        agent_payout.reservations.set(unpaid_reservations)

                        # Mark reservations as paid
                        for reservation in reservations_with_commission:
                            reservation.commission_amount = reservation.calculated_commission
                            reservation.commission_paid = True
                            reservation.commission_paid_at = timezone.now()
                            reservation.save(update_fields=['commission_amount', 'commission_paid', 'commission_paid_at'])

                        # Update agent totals
                        agent.total_paid_commission += agent_commission_total
                        agent.last_payment_date = timezone.now()
                        agent.unpaid_commissions = 0
                        agent.save(update_fields=['total_paid_commission', 'last_payment_date', 'unpaid_commissions'])

                        processed_payouts.append(agent_payout)
                        total_amount += agent_commission_total

            if processed_payouts:
                # Create agency payout record
                agency_payout = AgencyCommissionPayout.objects.create(
                    agency=self,
                    total_amount=total_amount,
                    payout_period_start=min(p.payout_period_start for p in processed_payouts),
                    payout_period_end=max(p.payout_period_end for p in processed_payouts),
                )

                # Link all agent payouts to this agency payout
                agency_payout.agent_payouts.set(processed_payouts)

                # Update agency's total paid commission
                self.total_paid_commission += total_amount
                self.save(update_fields=['total_paid_commission'])

                return agency_payout, total_amount

            return None, 0


