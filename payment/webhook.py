import stripe
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from django.db import transaction  # Added for atomicity
import logging
from reservations.models import Reservation, Customer
from .models import Payment
from users.emails import send_reservation_confirmation  # Added import
from decimal import Decimal  # Added import

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY


@csrf_exempt
def stripe_webhook(request):
    logger.info(f"⚠️ Webhook received")
    payload = request.body
    signature = request.META.get("HTTP_STRIPE_SIGNATURE")
    try:
        event = stripe.Webhook.construct_event(
            payload,
            signature,
            settings.STRIPE_WEBHOOK_SECRET,
        )
    except ValueError as e:
        logger.error(f"Invalid Payload: {e}")
        return HttpResponse(status=400)
    except stripe.error.SignatureVerificationError as e:
        logger.error(f"Invalid Signature: {e}")
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

    if not reservation_id:
        logger.error("No Reservation ID in session metadata")
        return
    try:
        reservation = Reservation.objects.select_related("customer").get(
            id=reservation_id
        )
        customer = reservation.customer

        amount_total = session.get("amount_total")
        session_total_amount = (
            Decimal(amount_total if amount_total is not None else 0) / 100
        )

        payment, created = Payment.objects.get_or_create(
            reservation=reservation,
            customer=customer,
            stripe_checkout_id=session.get("id"),
            defaults={
                "amount": session_total_amount,
                "payment_type": "pay_now",
                "status": "pending",
            },
        )

        if session.get("mode") == "setup":
            setup_intent_id = session.get("setup_intent")
            if setup_intent_id:
                setup_intent = stripe.SetupIntent.retrieve(setup_intent_id)
                payment_method_id = setup_intent.payment_method

                if save_card_to_customer(
                    customer.stripe_customer_id, payment_method_id
                ):
                    payment.stripe_customer_id = customer.stripe_customer_id
                    payment.stripe_payment_method_id = payment_method_id
                    payment.stripe_checkout_id = session.get("id")
                    payment.payment_type = "pay_later"
                    payment.amount = reservation.total_price
                    payment.status = "card_saved"
                    reservation.status = "confirmed"

                    with transaction.atomic():
                        payment.save()
                        reservation.save()

                    send_reservation_confirmation(reservation)
                    logger.info(f"Card Saved for Reservation {reservation_id}")
                else:
                    logger.error("Failed to save card to customer")

        elif session.get("payment_status") == "paid":
            payment_intent = session.get("payment_intent")
            if payment_intent:
                full_payment_intent = stripe.PaymentIntent.retrieve(payment_intent)
                payment_method_id = full_payment_intent.payment_method
                payment_method_type = None

                if payment_method_id:
                    try:
                        payment_method = stripe.PaymentMethod.retrieve(payment_method_id)
                        payment_method_type = payment_method.type
                        logger.info(f"Payment method type: {payment_method_type}")
                    except Exception as e:
                        logger.warning(f"Could not retrieve payment method details: {e}")

                final_amount = Decimal(full_payment_intent.amount) / 100
                
                # Handle card payments - try to save card details
                if payment_method_type == 'card':
                    card_saved = save_card_to_customer(
                        customer.stripe_customer_id, payment_method_id
                    )
                    if not card_saved:
                        logger.warning("Could not save card details, but continuing with payment processing")
                else:
                    # Non-card payment method (Link, SEPA, etc.) - just log it
                    logger.info(f"Non-card payment method used: {payment_method_type}")
                
                # Process the payment regardless of card saving success
                payment.stripe_payment_intent_id = payment_intent
                payment.payment_type = "pay_now"
                payment.status = "paid"
                payment.amount = final_amount
                
                # If we have payment method info, still record it
                if payment_method_id:
                    payment.stripe_payment_method_id = payment_method_id
                
                reservation.status = "confirmed"
                reservation.base_price = final_amount
                reservation.total_price = final_amount
                
                with transaction.atomic():
                    payment.save()
                    reservation.save()

                send_reservation_confirmation(reservation)
                logger.info(f"Payment processed for reservation {reservation_id}")
            else:
                logger.error("No payment_intent in session")

    except Reservation.DoesNotExist:
        logger.error(f"Reservation {reservation_id} Not Found")
    except Exception as e:
        logger.exception(f"Error processing checkout session: {e}")

def save_card_to_customer(customer_id: str, payment_method_id: str):
    """
    Given a Stripe customer ID and a payment method ID,
    retrieve card details and save them to Customer model.
    """
    try:
        logger.info(f"Attempting to save card for Stripe customer ID: {customer_id}")
        logger.info(f"Payment method ID: {payment_method_id}")

        # attach the payment method to the customer in Stripe
        stripe.PaymentMethod.attach(
            payment_method_id,
            customer=customer_id,
        )
        logger.info("Payment method attached successfully")

        payment_method = stripe.PaymentMethod.retrieve(payment_method_id)
        
        # Check if this is a card payment method
        if payment_method.type != 'card':
            logger.info(f"Payment method is not a card, it's: {payment_method.type}")
            return False
            
        card = payment_method.card

        logger.info(f"Retrieved card details: {card}")
        logger.info(f"Card brand: {card.brand}")
        logger.info(f"Card last4: {card.last4}")

        try:
            customer = Customer.objects.get(stripe_customer_id=customer_id)
            logger.info(f"Found customer: {customer}")
            customer.stripe_payment_method_id = payment_method.id
            customer.card_brand = card.brand
            customer.card_last4 = card.last4
            customer.card_exp_month = card.exp_month
            customer.card_exp_year = card.exp_year

            try:
                customer.save()
                logger.info(
                    f"Customer card details saved successfully for {customer.get_full_name()}"
                )
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