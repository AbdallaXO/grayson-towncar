import stripe
import stripe.error
import logging

logger = logging.getLogger(__name__)


def get_or_create_stripe_customer(reservation):
    customer = reservation.customer
    logger.info(f"Attempting to get/create Stripe customer for {customer.email}")

    if hasattr(customer, "stripe_customer_id") and customer.stripe_customer_id:
        try:
            stripe_customer = stripe.Customer.retrieve(customer.stripe_customer_id)

            if stripe_customer.get("deleted", False):
                logger.warning(
                    f"Stripe customer {stripe_customer.id} was deleted. Creating new customer."
                )
                stripe_customer = create_stripe_customer(customer, reservation)
                customer.stripe_customer_id = stripe_customer.id
                customer.save()
            else:
                logger.info(f"Retrieved existing Stripe customer: {stripe_customer.id}")

            return stripe_customer

        except stripe.error.StripeError as e:
            logger.warning(
                f"Failed to retrieve Stripe customer {customer.stripe_customer_id}: {e}"
            )
            customer.stripe_customer_id = None
            customer.save()

    try:
        stripe_customer = create_stripe_customer(customer, reservation)
        logger.info(f"Created new Stripe customer: {stripe_customer.id}")
        customer.stripe_customer_id = stripe_customer.id
        customer.save()

        return stripe_customer
    except Exception as e:
        logger.error(f"Error creating Stripe customer: {e}")
        raise


def create_stripe_customer(customer, reservation):
    stripe_customer = stripe.Customer.create(
        email=customer.email,
        name=customer.get_full_name(),
        metadata={
            "Reservation ID": str(reservation.id),
            "Trip Type": reservation.trip_type,
            "Phone Number": customer.phone_number,
        },
    )
    logger.debug(f"Stripe customer created inside helper: {stripe_customer.id}")
    return stripe_customer
