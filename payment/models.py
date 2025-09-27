from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
import logging

logger = logging.getLogger(__name__)
# Create your models here.


class Payment(models.Model):
    reservation = models.ForeignKey(
        "reservations.Reservation",
        on_delete=models.PROTECT,
        related_name="payments",
        null=True,
    )
    customer = models.ForeignKey(
        "reservations.Customer",
        on_delete=models.PROTECT,
        related_name="payments",
        null=True,
    )
    stripe_customer_id = models.CharField(max_length=255, blank=True, null=True)
    stripe_payment_method_id = models.CharField(max_length=255, blank=True, null=True)
    stripe_checkout_id = models.CharField(max_length=255, blank=True, null=True)
    stripe_payment_intent_id = models.CharField(max_length=255, blank=True, null=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_type = models.CharField(
        max_length=20,
        choices=[
            ("pay_now", "Pre-Pay"),
            ("pay_later", "Save Card & Pay Later"),
        ],
        default="pay_later",
    )
    status = models.CharField(
        max_length=20,
        choices=[
            ("pending", "Pending"),
            ("card_saved", "Card Saved On File"),
            ("paid", "Paid"),
            ("failed", "Failed"),
        ],
        default="pending",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.payment_type.replace('_', ' ').title()} - {self.status.title()}"


# HubSpot integration removed - no longer syncing payments to HubSpot
