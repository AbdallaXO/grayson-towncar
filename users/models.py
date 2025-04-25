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

    def __str__(self):
        return f"{self.first_name} - {self.last_name}"