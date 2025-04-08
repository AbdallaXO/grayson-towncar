import stripe
import stripe.error
from reservations.models import Customer
from django.utils import timezone


def get_or_create_stripe_customer(reservation):
    """Takes the Reservation and Returns an existing stripe object or creates one for the customer
    also updates the reservation.customers stripe_customer_id to his stripe_customer_id after creating it."""
    customer = reservation.customer
    if not customer.stripe_customer_id:
        # check customer email against db to see if they already exist with an ID
        stripe_customer = stripe.Customer.create(
            email=reservation.customer.email,
            metadata={
                "reservation_id": reservation.id,
                "trip_type": reservation.trip_type,
            },
        )
        reservation.customer.stripe_customer_id = stripe_customer.id
        reservation.customer.save()
    else:
        stripe_customer = stripe.Customer.retrieve(
            id=reservation.customer.stripe_customer_id
        )
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
