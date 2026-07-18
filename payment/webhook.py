import stripe
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from django.db import transaction  # Added for atomicity
import logging
import time
import threading
from reservations.models import Reservation, Customer
from reservations.utils import _run_in_background
from .models import Payment
from users.emails import send_reservation_confirmation  # Added import
from decimal import Decimal  # Added import
# HubSpot integration removed
from reservations.conversions import send_purchase_event

logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY
# Bound every Stripe call (default SDK timeout is ~80s with retries) so a Stripe
# slowdown can't hang a worker. See payment/views.py (incident 2026-07-18).
stripe.max_network_retries = 1
stripe.default_http_client = stripe.RequestsClient(timeout=20)


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
        # Fingerprint of the LOADED secret (length + last 4, never the full value) so a
        # Railway-var vs Stripe-dashboard mismatch is a one-line diagnosis instead of a
        # guessing game: compare last4 against the destination's revealed signing secret.
        secret = settings.STRIPE_WEBHOOK_SECRET or ""
        logger.error(
            "Invalid Signature: %s (Stripe-Signature header: %s; loaded secret: len=%d last4=%s)",
            e,
            "present" if signature else "MISSING",
            len(secret),
            secret[-4:] if len(secret) >= 4 else "<unset>",
        )
        return HttpResponse(status=400)

    event_type = event["type"]
    event_object = event["data"]["object"]
    logger.info(f"Received Webhook {event_type}")

    if event_type == "checkout.session.completed":
        try:
            payment_result = handle_checkout_session(event_object)
        except Exception as e:
            # An unexpected error here means Stripe moved the money but we may NOT have
            # recorded it. Return 500 so Stripe retries (backoff, up to ~3 days) instead
            # of swallowing the failure behind a 200 and losing the payment silently.
            # Genuinely unprocessable cases (missing/unknown reservation) return None
            # below and still answer 200 — Stripe should not retry those.
            logger.exception(f"Webhook processing failed, asking Stripe to retry: {e}")
            return HttpResponse(status=500)

        if payment_result:
            reservation_id = payment_result.get("reservation_id")
            status_map = {
                "paid": "Paid",
                "card_saved": "Card On File",
                "pending": "Pending",
                "failed": "Failed",
            }
            payment_status = status_map.get(payment_result.get("status", ""), "Unknown")
            # Simple enhancement for non-card payment methods
            payment_method = None
            if payment_result.get("payment_method_type") == "card":
                card_brand = payment_result.get("card_brand", "")
                card_last4 = payment_result.get("card_last4", "")
                if card_brand and card_last4:
                    payment_method = f"{card_brand.title()} ending in {card_last4}"
            elif payment_result.get("payment_method_type"):
                # Just use the payment method type as is
                payment_method = (
                    payment_result.get("payment_method_type").replace("_", " ").title()
                )

            # HubSpot integration removed - no longer updating HubSpot deals

    elif event_type == "charge.refunded":
        # A refund issued directly in the Stripe dashboard (the fallback whenever the
        # in-app refund flow errors) — sync it back so the books don't show returned
        # money as revenue and the customer isn't dunned/recharged for a refunded trip.
        try:
            handle_charge_refunded(event_object)
        except Exception as e:
            logger.exception(f"charge.refunded failed, asking Stripe to retry: {e}")
            return HttpResponse(status=500)

    elif event_type == "charge.dispute.created":
        # A chargeback was opened — surface it loudly so staff respond before the
        # Stripe evidence deadline instead of finding out when the money is clawed back.
        try:
            handle_charge_dispute(event_object)
        except Exception as e:
            logger.exception(f"charge.dispute.created failed, asking Stripe to retry: {e}")
            return HttpResponse(status=500)

    return HttpResponse(status=200)


def handle_checkout_session(session):
    reservation_id = session.get("metadata", {}).get("reservation_id")
    logger.info(f"Processing checkout for reservation: {reservation_id}")
    # Do NOT log the full session object — it contains customer PII (name, email,
    # postal code) and live Stripe IDs. Log only a safe, non-sensitive summary.
    logger.info(
        "Checkout session summary: id=%s payment_status=%s mode=%s",
        session.get("id"),
        session.get("payment_status"),
        session.get("mode"),
    )

    if not reservation_id:
        logger.error("No Reservation ID in session metadata")
        return
    
    # Check if this payment was initiated by a dispatcher
    metadata = session.get("metadata", {})
    initiated_by = metadata.get("initiated_by", "")
    is_dispatcher_payment = initiated_by == "dispatcher"
    
    try:
        reservation = Reservation.objects.select_related("customer").get(
            id=reservation_id
        )
        customer = reservation.customer

        amount_total = session.get("amount_total")
        session_total_amount = (
            Decimal(amount_total if amount_total is not None else 0) / 100
        )

        # Extract description from metadata if available
        payment_description = metadata.get("payment_description", "")
        if not payment_description:
            # Fallback to default description
            payment_description = f"Payment for Reservation #{reservation.id}"

        payment, created = Payment.objects.get_or_create(
            reservation=reservation,
            customer=customer,
            stripe_checkout_id=session.get("id"),
            defaults={
                "amount": session_total_amount,
                "description": payment_description,
                "payment_type": "pay_now",
                "status": "pending",
            },
        )

        # Idempotency guard. Stripe redelivers events, and on the single worker our own
        # webhook can time out and be retried — so the SAME checkout session arrives more
        # than once. If we already fully processed this session, do NOT re-run the
        # paid/setup branch: re-running would add final_amount to total_price a second
        # time (line ~219) and manufacture a phantom balance the customer doesn't owe.
        if not created and payment.status in ("paid", "card_saved"):
            logger.info(
                "Duplicate checkout.session.completed for %s (payment already %s) — "
                "skipping reprocessing",
                session.get("id"),
                payment.status,
            )
            payment_result = {
                "reservation_id": reservation_id,
                "status": payment.status,
                "amount": payment.amount,
                "payment_method_type": None,
                "card_brand": None,
                "card_last4": None,
            }
            if hasattr(customer, "card_brand") and customer.card_brand:
                payment_result["payment_method_type"] = "card"
                payment_result["card_brand"] = customer.card_brand
                payment_result["card_last4"] = customer.card_last4
            return payment_result

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

                    # Card on file, no charge yet — but send the confirmation now so the
                    # customer has written proof of the booking. A card_saved Payment row
                    # now exists, so the email's "complete payment" block is skipped and
                    # it reads as a plain booking confirmation.
                    _run_in_background(send_reservation_confirmation, reservation)
                    logger.info(f"Card Saved for Reservation {reservation_id} (initiated by: {initiated_by})")
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
                        payment_method = stripe.PaymentMethod.retrieve(
                            payment_method_id
                        )
                        payment_method_type = payment_method.type
                        logger.info(f"Payment method type: {payment_method_type}")
                    except Exception as e:
                        logger.warning(
                            f"Could not retrieve payment method details: {e}"
                        )

                final_amount = Decimal(full_payment_intent.amount) / 100

                # Calculate amount owed BEFORE this payment
                amount_owed_before = reservation.amount_owed

                # Handle card payments - try to save card details
                if payment_method_type == "card":
                    card_saved = save_card_to_customer(
                        customer.stripe_customer_id, payment_method_id
                    )
                    if not card_saved:
                        logger.warning(
                            "Could not save card details, but continuing with payment processing"
                        )
                else:
                    # Non-card payment method (Link, SEPA, etc.) - just log it
                    logger.info(f"Non-card payment method used: {payment_method_type}")

                # Process the payment regardless of card saving success
                payment.stripe_payment_intent_id = payment_intent
                payment.payment_type = "pay_now"
                payment.status = "paid"
                payment.amount = final_amount

                # Update description if it wasn't set during payment creation
                if not payment.description or payment.description == f"Payment for Reservation #{reservation.id}":
                    # Try to get description from payment intent metadata
                    pi_description = full_payment_intent.metadata.get("payment_description", "")
                    if pi_description:
                        payment.description = pi_description

                # If we have payment method info, still record it
                if payment_method_id:
                    payment.stripe_payment_method_id = payment_method_id

                reservation.status = "confirmed"

                # Automatic total_price adjustment logic:
                # If amount owed was $0 (or nearly $0), this is a NEW charge, so add to total_price
                # Otherwise, this is a payment toward existing balance, don't add
                # Special case: if total_price is 0, this is the initial setup, set it directly
                if reservation.total_price == 0:
                    # First payment setup
                    reservation.base_price = final_amount
                    reservation.total_price = final_amount
                    logger.info(f"Initial payment setup: total_price set to ${final_amount}")
                elif amount_owed_before <= Decimal("0.01"):
                    # Amount owed was nearly zero, this is a new charge
                    reservation.total_price += final_amount
                    logger.info(
                        f"Auto-added ${final_amount} to reservation total (was ${reservation.total_price - final_amount}, "
                        f"now ${reservation.total_price}) - detected as new charge via webhook"
                    )

                with transaction.atomic():
                    payment.save()
                    reservation.save()

                # Send confirmation email in background — must not block the 200 response to Stripe
                _run_in_background(send_reservation_confirmation, reservation)
                logger.info(f"Confirmation email queued for reservation {reservation_id} (initiated by: {initiated_by})")
                
                # Send purchase event to Meta in background thread to avoid blocking webhook response.
                # Stable event_id (Stripe payment-intent id) — matches the success page + browser
                # pixel so Meta dedupes all three to ONE Purchase. No timestamp (breaks dedup).
                event_id = str(payment_intent) if payment_intent else None
                # _run_in_background (used just above for the confirmation email)
                # closes the thread's DB connection when done (connection
                # saturation 2026-07-18).
                _run_in_background(send_purchase_event, reservation, value=None, event_id=event_id)
            else:
                logger.error("No payment_intent in session")

        payment_result = {
            "reservation_id": reservation_id,
            "status": payment.status,
            "amount": payment.amount,
            "payment_method_type": None,
            "card_brand": None,
            "card_last4": None,
        }
        if hasattr(customer, "card_brand") and customer.card_brand:
            payment_result["payment_method_type"] = "card"
            payment_result["card_brand"] = customer.card_brand
            payment_result["card_last4"] = customer.card_last4

        return payment_result
    except Reservation.DoesNotExist:
        # Unprocessable, not transient — do not ask Stripe to retry. Answers 200.
        logger.error(f"Reservation {reservation_id} Not Found")
        return None
    # Any OTHER exception is intentionally NOT caught here: it propagates to
    # stripe_webhook, which returns 500 so Stripe retries the delivery.


def handle_charge_refunded(charge):
    """Sync a Stripe-side refund (dashboard or API) back onto our Payment row.

    Matches the Payment by payment_intent and sets refunded_amount to the CUMULATIVE
    amount Stripe reports (so redelivery is naturally idempotent). The Payment post_save
    signal then recomputes the reservation's paid-state columns.
    """
    pi = charge.get("payment_intent")
    if not pi:
        logger.info("charge.refunded with no payment_intent — skipping")
        return None

    payment = (
        Payment.objects.filter(stripe_payment_intent_id=pi)
        .select_related("reservation")
        .first()
    )
    if not payment:
        logger.info("charge.refunded for unknown payment_intent %s — skipping", pi)
        return None

    amount_refunded = Decimal(charge.get("amount_refunded", 0) or 0) / 100
    payment.refunded_amount = amount_refunded

    refunds = (charge.get("refunds") or {})
    refund_rows = refunds.get("data") if hasattr(refunds, "get") else None
    if refund_rows:
        latest = refund_rows[0].get("id") if hasattr(refund_rows[0], "get") else None
        if latest:
            payment.stripe_refund_id = latest

    charged = Decimal(charge.get("amount", 0) or 0) / 100
    if charge.get("refunded") or (charged and amount_refunded >= charged):
        payment.status = "refunded"

    payment.save()  # post_save signal recomputes reservation paid-state
    logger.info(
        "Synced Stripe refund $%s onto payment %s (reservation %s)",
        amount_refunded, payment.id, payment.reservation_id,
    )
    return {"reservation_id": payment.reservation_id, "status": "refunded"}


def handle_charge_dispute(dispute):
    """A card dispute/chargeback was opened. File a HIGH ops task on the reservation so
    staff respond in the Stripe dashboard before the evidence deadline."""
    pi = dispute.get("payment_intent")
    charge_id = dispute.get("charge")
    payment = None
    if pi:
        payment = (
            Payment.objects.filter(stripe_payment_intent_id=pi)
            .select_related("reservation")
            .first()
        )
    if not payment:
        logger.info(
            "charge.dispute.created with no matching payment (pi=%s charge=%s) — skipping",
            pi, charge_id,
        )
        return None

    reservation = payment.reservation
    try:
        from ops.services import create_task
        from ops.models import OperationalTask

        amount = Decimal(dispute.get("amount", 0) or 0) / 100
        res_no = getattr(reservation, "display_number", None) or reservation.id
        create_task(
            task_type=OperationalTask.TaskType.MANUAL,
            title=f"Stripe DISPUTE on Res #{res_no} — ${amount}"[:200],
            description=(
                "A card dispute/chargeback was opened. Respond in the Stripe dashboard "
                "before the evidence deadline.\n\n"
                f"Reason: {dispute.get('reason', '—')}\n"
                f"Status: {dispute.get('status', '—')}\n"
                f"Amount: ${amount}\n"
                f"Charge: {charge_id}"
            ),
            priority=OperationalTask.Priority.HIGH,
            reservation=reservation,
            metadata={
                "source": "stripe_dispute",
                "dispute_id": dispute.get("id"),
                "charge": charge_id,
            },
        )
    except Exception as e:
        logger.exception("Failed to create dispute ops task: %s", e)

    return {"reservation_id": reservation.id, "status": "disputed"}


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
        if payment_method.type != "card":
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
