import stripe
from django.shortcuts import render, redirect, get_object_or_404
import stripe.error
from reservations.models import Reservation
from .utils import get_or_create_stripe_customer
from django.conf import settings
from django.urls import reverse

# Ensure you're using environment variables in production
stripe.api_key = settings.STRIPE_SECRET_KEY


def create_checkout_session(request, reservation_id):
    """
    Creates a Checkout Session for a User and a stripe customer object
    and gives user the option to pay now or later then takes payment here or re-directs user
    to save_card if they decide to save card for later"""
    reservation = get_object_or_404(Reservation, pk=reservation_id)
    stripe_customer = get_or_create_stripe_customer(reservation)

    if request.method == "POST":
        #this will determine if its pay_now or save_card
        action = request.POST.get("action")
        # if user is pre-paying
        if action == "pay_now":
            try:
                checkout_session = stripe.checkout.Session.create(
                    customer=stripe_customer.id,
                    line_items=[
                        {
                            "price_data": {
                                "currency": "usd",
                                "product_data": {
                                    "name": f"{reservation.rate.vehicle} {reservation.trip_type.replace('_', '').title()} Reservation Res ID#{reservation.id}",
                                },
                                "unit_amount": int(reservation.total_price * 100),
                            },
                            "quantity": 1,
                        }
                    ],
                    mode="payment",
                    success_url=request.build_absolute_uri(reverse('payment_success')),
                    cancel_url=request.build_absolute_uri(reverse('payment_cancel')),
                    metadata={
                        "reservation_id": reservation.id,
                        "mode": "pay_now",
                        "route":f"Roundtrip Between {reservation.rate.route}",
                        "vehicle":{reservation.rate.vehicle},
                        
                    },
                )
            except stripe.error.StripeError as e:
                return render(request, "stripe/error.html", {"error": e})

            return redirect(checkout_session.url, code=303)
        elif action == "save_card":
            return redirect("save_card_checkout", reservation_id=reservation.id)

    return render(request, "stripe/payment.html", {"reservation": reservation})


def save_card(request, reservation_id):
    success_url = request.build_absolute_uri(reverse("payment_success"))
    cancel_url = request.build_absolute_uri(reverse("payment_cancel"))
    reservation = get_object_or_404(Reservation, id=reservation_id)
    stripe_customer = get_or_create_stripe_customer(reservation)
    try:
        checkout_session = stripe.checkout.Session.create(
            customer=stripe_customer.id,
            payment_method_types=["card"],
            mode="setup",
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={"reservation_id": reservation.id, "mode": "save_card"},
        )
    except stripe.error.StripeError as e:
        return render(request, "stripe/error.html", {"error": e})

    return redirect(checkout_session.url, code=303)


def payment_success(request):
    return render(request, "stripe/success.html")


def payment_cancel(request):
    return render(request, "stripe/cancel.html")
