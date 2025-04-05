import stripe
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from reservations.models import Customer
from .utils import save_card_to_customer


stripe.api_key = settings.STRIPE_SECRET_KEY


@csrf_exempt
def stripe_webhook(request):
    # strip sends a post request with the event as raw bytes in the body
    payload = request.body
    # stripes signature/authentication
    signature = request.META.get("HTTP_STRIPE_SIGNATURE")

    try:
        # we construct an event, pass the payload signature and our secret webhook,if everything is valid we get a verified event object
        event = stripe.Webhook.construct_event(
            payload, signature, settings.STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.error.SignatureVerificationError):
        return HttpResponse(status=400)
    if event["type"] == "checkout.session.completed":
        session = event["data"][
            "object"
        ]  # gives access to stripe custoemr, session mode, setupintent

        if (
            session.get("mode") == "setup"
        ):  # checking if customer  paying now or later setup=later, payment=now
            customer_id = session.get("customer")  # customer id
            setup_intent_id = session.get(
                "setup_intent"
            )  # setup_intent points to the card

            if customer_id and setup_intent_id:
                setup_intent = stripe.SetupIntent.retrieve(
                    setup_intent_id
                )  # setup_intent points to the card customer is using
                payment_method_id = setup_intent.payment_method

                save_card_to_customer(customer_id, payment_method_id)
