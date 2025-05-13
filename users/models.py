from django.db import models
from django.contrib.auth.models import User
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
    created_at = models.DateTimeField(auto_now_add=True)

    payment_info = models.CharField(
        max_length=200,
        help_text="Preferred Way to Get Paid & Information - Paypal/Zelle/CashApp/Bank Info etc.",
        null=True,
        blank=True,
    )
    last_payment_date = models.DateTimeField(null=True, blank=True)
    total_earned_commission = models.DecimalField(
        max_digits=10, decimal_places=2, default=0.00
    )
    total_paid_commission = models.DecimalField(
        max_digits=10, decimal_places=2, default=0.00
    )
    PAYMENT_METHOD_CHOICES = [
        ("paypal", "PayPal"),
        ("venmo", "Venmo"),
        ("zelle", "Zelle"),
        ("cashapp", "Cash App"),
        ("bank", "Bank Transfer"),
        ("check", "Check"),
        ("other", "Other"),  # Added this option
    ]
    payment_method = models.CharField(
        max_length=20,
        choices=PAYMENT_METHOD_CHOICES,
        null=True,
        blank=True,
        help_text="Select your preferred payment method",
    )
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    def __str__(self):
        return f"{self.agency_name} - {self.user.username}"

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
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    def __str__(self):
        return f"{self.agent.agency_name or self.agent.agent_name or self.agent.user.username} – {self.payout_period_start.strftime('%b %Y')} – ${self.total_amount}"
