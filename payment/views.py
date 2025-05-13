import stripe
from django.shortcuts import render, redirect, get_object_or_404
import stripe.error
from reservations.models import Reservation
from .utils import get_or_create_stripe_customer
from django.conf import settings
from django.urls import reverse
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
stripe.api_key = settings.STRIPE_SECRET_KEY


def create_checkout_session(request, reservation_id):
    logger.info(f"Starting checkout session for reservation {reservation_id}")
    logger.info(f"POST data: {request.POST}")

    reservation = get_object_or_404(Reservation, uuid=reservation_id)
    logger.info(f"Found Reservation For Customer Email={reservation.customer.email}")

    stripe_customer = get_or_create_stripe_customer(reservation)
    logger.info(f"Stripe Customer ID: {stripe_customer.id}")

    success_url = request.build_absolute_uri(
        reverse("payment_success") + f"?q={reservation.uuid}"
    )
    cancel_url = request.build_absolute_uri(
        reverse("payment_cancel") + f"?q={reservation.uuid}"
    )
    if request.method == "POST":
        action = request.POST.get("action")
        logger.info(f"Action selected: {action}")

        if action == "pay_now":
            try:
                checkout_session = stripe.checkout.Session.create(
                    customer=stripe_customer.id,
                    line_items=[
                        {
                            "price_data": {
                                "currency": "usd",
                                "product_data": {
                                    "name": f"Grayson Town Car {reservation.trip_type.replace('_', ' ').title()} Booking",
                                    "description": (f"{reservation.rate.route}"),
                                },
                                "unit_amount": int(reservation.total_price * 100),
                            },
                            "quantity": 1,
                        }
                    ],
                    allow_promotion_codes=True,
                    mode="payment",
                    billing_address_collection="auto",  # zip code autofill
                    success_url=success_url,
                    cancel_url=cancel_url,
                    metadata={
                        "reservation_uuid": reservation.uuid,
                        "reservation_id": reservation.id,
                        "customer_id": reservation.customer.id,
                        "mode": "pay_now",
                        "route": f"Roundtrip Between {reservation.rate.route}",
                        "vehicle": str(reservation.rate.vehicle),
                    },
                    payment_intent_data={"setup_future_usage": "off_session"},
                    client_reference_id=reservation.id,
                )
                logger.info(f"Checkout session created: {checkout_session.id}")
            except Exception as e:
                logger.error(f"Stripe error creating checkout: {e}")
                return render(request, "stripe/error.html", {"error": e})

            return redirect(checkout_session.url, code=303)
        elif action == "save_card":
            logger.info(f"Redirecting to save card for reservation {reservation_id}")
            return redirect("save_card_checkout", reservation_id=reservation.uuid)

    return render(request, "stripe/payment.html", {"reservation": reservation})


def save_card(request, reservation_id):
    reservation = get_object_or_404(Reservation, uuid=reservation_id)
    success_url = request.build_absolute_uri(
        reverse("payment_success") + f"?q={reservation.uuid}"
    )
    cancel_url = request.build_absolute_uri(
        reverse("payment_cancel") + f"?q={reservation.uuid}"
    )
    try:
        stripe_customer = get_or_create_stripe_customer(reservation)
    except Exception as e:
        logger.error(f"Unexpected error in checkout session: {e}")
    try:
        checkout_session = stripe.checkout.Session.create(
            customer=stripe_customer.id,
            payment_method_types=["card"],
            mode="setup",
            billing_address_collection="auto",
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={
                "reservation_id": reservation.uuid,
                "customer_id": reservation.customer.id,
                "mode": "pay_now",
                "route": f"Roundtrip Between {reservation.rate.route}",
                "vehicle": str(reservation.rate.vehicle),
            },
            client_reference_id=reservation.uuid,
        )
    except stripe.error.StripeError as e:
        return render(request, "stripe/error.html", {"error": e})

    return redirect(checkout_session.url, code=303)


def payment_success(request):
    return render(request, "stripe/success.html")


def payment_cancel(request):
    reservation_uuid = request.GET.get("q")
    source = request.GET.get("source")
    return render(
        request,
        "stripe/cancel.html",
        {
            "reservation_uuid": str(reservation_uuid),
            "source": source,
        },
    )
