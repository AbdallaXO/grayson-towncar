import stripe
import stripe.error
from django.utils import timezone
import logging

logger = logging.getLogger(__name__)


def get_or_create_stripe_customer(reservation):
    """Creates a Stripe customer for this reservation if one doesn't exist."""
    customer = reservation.customer
    if customer.stripe_customer_id:
        try:
            stripe_customer = stripe.Customer.retrieve(
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
    customer.stripe_customer_id = stripe_customer.id
    customer.save()
    return stripe_customer
