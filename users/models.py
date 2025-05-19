from django.db import models
from django.contrib.auth.models import User
from django.db.models import Sum, Count, Q

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
        unpaid_commissions = (
            Reservation.objects.filter(
                travel_agent=self, commission_paid=False, status="completed"
            )
            .annotate(
                calculated_commission=ExpressionWrapper(
                    F("total_price") * (self.commission_rate / 100),
                    output_field=DecimalField(max_digits=10, decimal_places=2),
                )
            )
            .aggregate(total=Sum("calculated_commission"))["total"]
            or 0
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
                # Calculate total commission amount using the agent's commission rate
                commission_total = (
                    unpaid_reservations.annotate(
                        calculated_commission=ExpressionWrapper(
                            F("total_price") * (self.commission_rate / 100),
                            output_field=DecimalField(max_digits=10, decimal_places=2),
                        )
                    ).aggregate(total=Sum("calculated_commission"))["total"]
                    or 0
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

                # Mark reservations as paid and record the actual commission amount
                for reservation in unpaid_reservations:
                    # Calculate the exact commission amount for this reservation
                    reservation.commission_amount = reservation.total_price * (
                        self.commission_rate / 100
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

    def __str__(self):
        return f"{self.agent} – {self.payout_period_start.strftime('%b %Y')} – ${self.total_amount}"
