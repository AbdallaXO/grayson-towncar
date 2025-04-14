import stripe
import stripe.error
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_or_create_stripe_customer(reservation):
    customer = reservation.customer
    logger.info(f"Attempting to get/create Stripe customer for {customer.email}")
    try:
        stripe_customer =  stripe.Customer.create(
             email=customer.email,
            name=customer.get_full_name(),
            metadata={
                 'Reservation ID':str(reservation.id),
                 'Trip Type':reservation.trip_type,
                 'Phone Number':customer.phone_number,
                       
            }
            )
        logger.info(f"Created new Stripe customer: {stripe_customer.id}")

        # Save the new Stripe customer ID
        customer.stripe_customer_id = stripe_customer.id
        customer.save()
        return stripe_customer

    except Exception as e:
        logger.error(f"Error creating Stripe customer: {e}")
        raise
        
