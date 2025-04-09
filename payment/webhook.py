import stripe
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
import logging
from reservations.models import Reservation, Customer
from .models import Payment
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY


@csrf_exempt
def stripe_webhook(request):
    payload = request.body
    signature = request.META.get("HTTP_STRIPE_SIGNATURE")
    try:
        event = stripe.Webhook.construct_event(
            payload,
            signature,
           settings.STRIPE_WEBHOOK_SECRET,
        )
    except ValueError as e:
        logger.error(f"Invalid Payload : {e}")
        return HttpResponse(status=400)
    except stripe.error.SignatureVerificationError as e:
        logger.error(f"Invalid Signature : {e}")
        return HttpResponse(status=400)

    event_type = event["type"]
    event_object = event["data"]["object"]
    logger.info(f"Received Webhook {event_type}")

    if event_type == "checkout.session.completed":
        handle_checkout_session(event_object)

    return HttpResponse(status=200)


def handle_checkout_session(session):
    reservation_id = session.get("metadata", {}).get("reservation_id")
    logger.info(f"Processing checkout for reservation: {reservation_id}")
    logger.info(f"Session details: {session}")

    reservation_id = session.get("metadata", {}).get("reservation_id")
    if not reservation_id:
        logger.error("No Reservation ID in session metadata")
        return
    try:
        reservation = Reservation.objects.select_related("customer").get(
            uuid=reservation_id
        )
        customer = reservation.customer
        # creating a record
        payment, created = Payment.objects.get_or_create(
            reservation=reservation,
            customer=customer,
            stripe_checkout_id=session.get("id"),
            amount=reservation.total_price,
            payment_type="pay_later",
        )

        if session.get("mode") == "setup":
            setup_intent_id = session.get("setup_intent")
            if setup_intent_id:
                setup_intent = stripe.SetupIntent.retrieve(setup_intent_id)
                payment_method_id = setup_intent.payment_method

                # save card to customer in stripe, and database
                save_card_to_customer(customer.stripe_customer_id, payment_method_id)

                # update payment details
                payment.stripe_customer_id = customer.stripe_customer_id
                payment.stripe_payment_method_id = payment_method_id
                payment.stripe_checkout_id = session.get("id")
                payment.status = "card_saved"
                payment.save()

                # update reservation status
                reservation.status = "Confirmed"
                reservation.save()
                # Can send an email confirmation here
                logger.info(f"Card Saved for Reservation {reservation_id}")

        elif session.get("payment_status") == "paid":
            payment_intent = session.get("payment_intent")
            if payment_intent:
                full_payment_intent = stripe.PaymentIntent.retrieve(payment_intent)
                payment_method_id = full_payment_intent.payment_method

            save_card_to_customer(customer.stripe_customer_id, payment_method_id)
            payment.stripe_payment_intent_id = payment_intent
            payment.status = "paid"
            payment.save()

        # update reservation status and email here
    except Reservation.DoesNotExist:
        logger.error(f"Reservation {reservation_id} Not Found")
    except Exception as e:
        logger.exception(f"Error processing checkout session: {e}")


# Fix for utils.py save_card_to_customer function
def save_card_to_customer(customer_id: str, payment_method_id: str):
    """
    Given a Stripe customer ID and a payment method ID,
    retrieve card details and save them Customer model.
    """
    try:
        logger.info(f"Attempting to save card for Stripe customer ID: {customer_id}")
        logger.info(f"Payment method ID: {payment_method_id}")

        # First, attach the payment method to the customer in Stripe
        stripe.PaymentMethod.attach(
            payment_method_id,
            customer=customer_id,
        )
        logger.info("Payment method attached successfully")

        # Retrieve the payment method to get card details
        payment_method = stripe.PaymentMethod.retrieve(payment_method_id)
        card = payment_method.card

        logger.info(f"Retrieved card details: {card}")
        logger.info(f"Card brand: {card.brand}")
        logger.info(f"Card last4: {card.last4}")
        logger.info(f"Card exp month: {card.exp_month}")
        logger.info(f"Card exp year: {card.exp_year}")

        # Find the customer in your database
        try:
            customer = Customer.objects.get(stripe_customer_id=customer_id)
            logger.info(f"Found customer: {customer}")

            # Update customer card information
            customer.stripe_payment_method_id = payment_method.id
            customer.card_brand = card.brand
            customer.card_last4 = card.last4
            customer.card_exp_month = card.exp_month
            customer.card_exp_year = card.exp_year

            try:
                customer.save()
                logger.info("Customer card details saved successfully")
                logger.info(f"Updated customer details: {customer.__dict__}")
            except Exception as save_error:
                logger.error(f"Error saving customer: {save_error}")
                return False

            return True
        except Customer.DoesNotExist:
            logger.error(
                f"Customer not found in database with Stripe ID: {customer_id}"
            )
            return False

    except stripe.error.StripeError as e:
        logger.error(f"Stripe error saving card: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error saving card to customer: {e}")
        return False
