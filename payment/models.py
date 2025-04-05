from django.db import models


# Create your models here.


class Payment(models.Model):
        # Customer and payment method tracking
    stripe_customer_id = models.CharField(max_length=255, blank=True)
    stripe_payment_method_id = models.CharField(max_length=255, blank=True)

    # Session tracking
    stripe_checkout_id = models.CharField(max_length=255, blank=True)
    stripe_payment_intent_id = models.CharField(max_length=255, blank=True)

    amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_mode = models.CharField(
        max_length=20,
        choices=[
            ("pay_now", "Pay Now"),
            ("pay_later", "Pay Later"),
        ],
        default="pay_now",
    )
    status = models.CharField(
        max_length=20,
        choices=[
            ("pending", "Pending"),
            ("card_saved", "Card Saved"),
            ("paid", "Paid"),
            ("failed", "Failed"),
        ],
        default="pending",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
