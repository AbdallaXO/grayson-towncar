import stripe
import stripe.error
from reservations.models import Customer
from django.utils import timezone
import logging

logger = logging.getLogger(__name__)


def get_or_create_stripe_customer(reservation):
    """Creates a Stripe customer for this reservation if one doesn't exist."""
    customer = reservation.customer
    if customer.stripe_customer_id:
        try:
            stripe_customer = stripe.Customer.create(
                id=customer.stripe_customer_id,
            )
            return stripe_customer
        except stripe.error.InvalidRequestError:
            logger.warning("Stripe customer ID is invalid...")
            stripe_customer = stripe.Customer.create(
                email=customer.email,
                name=customer.get_full_name(),
                metadata={
                    "reservation_id": reservation.id,
                    "customer_db_id": customer.id,
                    "creation_date": timezone.now().isoformat(),
                },
            )

    stripe_customer = stripe.Customer.create(
        email=customer.email,
        name=customer.get_full_name(),
        metadata={
            "reservation_id": reservation.id,
            "customer_db_id": customer.id,
            "creation_date": timezone.now().isoformat(),
        },
    )
    customer.stripe_customer_id = stripe_customer.id
    customer.save()
    return stripe_customer


def save_card_to_customer(customer_id: str, payment_method_id: str):
    """
    Given a Stripe customer ID and a payment method ID,
    retrieve card details and save them Customer model.
    """
    try:
        stripe.PaymentMethod.attach(
            payment_method_id,
            customer=customer_id,
        )

        payment_method = stripe.PaymentMethod.retrieve(payment_method_id)
        card = payment_method.card
        customer = Customer.objects.get(stripe_customer_id=customer_id)
        customer.stripe_payment_method_id = payment_method.id
        customer.card_brand = card.brand
        customer.card_last4 = card.last4
        customer.card_exp_month = card.exp_month
        customer.card_exp_year = card.exp_year
        customer.card_saved_at = timezone.now()
        customer.save()
        return True

    except Exception as e:
        print(f"Error saving card to customer: {e}")
        return False
