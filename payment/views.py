from django.shortcuts import render
from django.conf import settings
from django.shortcuts import get_object_or_404, redirect
from reservations.models import Reservation
from django.urls import reverse
import stripe

stripe.api_key = "sk_test_51R6ae8R0WxX20o0RNVnNeZNS1ndfJJX6fgNT7jElFtCHPoZX0f6669sZsDSaHE02aKOfBg3GFlNZw4eplDRcLDLw009YcMaEK0"


# Create your views here.
def create_checkout_session(request, reservation_id):
    # get the reservation user just submitted
    reservation = get_object_or_404(Reservation, pk=reservation_id)

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="payment",  # One Time Payment , can be instead subscription for an example
        line_items=[
            {
                "price_data": {
                    "currency": "usd",
                    "unit_amount": int(reservation.total_price * 100),
                    "product_data": {
                        "name": f"{reservation.rate.vehicle} {reservation.trip_type.replace('_', ' ').title()} Reservation",
                        "description": f" Route: {reservation.rate.route}",
                    },
                },
                "quantity": 1,
            }
        ],
        success_url=request.build_absolute_uri("/thank-you/"),
        cancel_url=request.build_absolute_uri(reverse("cancel")),
        metadata={"reservation_id": reservation.id},
    )
    return redirect(session.url, code=303)


def thank_you(request):
    return render(request, "stripe/thank_you.html")


def cancel(request):
    return render(request, "stripe/failed.html")
