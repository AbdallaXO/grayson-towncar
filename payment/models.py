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


@receiver(post_save, sender=Payment)
def payment_saved(sender, instance, created, **kwargs):
    """Sync payment to HubSpot when saved"""
    if not instance.reservation:
        logger.warning(
            f"Payment #{instance.id} has no associated reservation - skipping HubSpot sync"
        )
        return

    from reservations.hubspot_service import (
        update_deal_payment_status,
        invalidate_deal_cache,
    )

    status_map = {
        "pending": "Pending",
        "card_saved": "Card On File",
        "paid": "Paid",
        "failed": "Failed",
    }
    hubspot_status = status_map.get(instance.status, "Unknown")

    # Get payment method if available
    payment_method = None
    if (
        instance.customer
        and hasattr(instance.customer, "card_brand")
        and instance.customer.card_brand
        and instance.customer.card_last4
    ):
        payment_method = f"{instance.customer.card_brand.title()} ending in {instance.customer.card_last4}"

    try:
        # Update HubSpot
        result = update_deal_payment_status(
            reservation_id=instance.reservation.id,
            payment_status=hubspot_status,
            payment_amount=instance.amount,
            payment_method=payment_method,
        )

        if result["success"]:
            logger.info(
                f"Updated payment status for reservation #{instance.reservation.id} in HubSpot"
            )
        else:
            logger.warning(
                f"Failed to update payment status in HubSpot: {result.get('error', 'unknown error')}"
            )

        # Invalidate the deal cache to ensure fresh data
        invalidate_deal_cache(instance.reservation.id)
    except Exception as e:
        logger.error(
            f"Error updating HubSpot payment for reservation #{instance.reservation.id}: {e}"
        )
