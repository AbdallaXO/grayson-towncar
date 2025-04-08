import stripe
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from .utils import save_card_to_customer
import json
import pprint


stripe.api_key = settings.STRIPE_SECRET_KEY


@csrf_exempt
def stripe_webhook(request):
    payload = request.body
    signature = request.META.get("HTTP_STRIPE_SIGNATURE")
    print(payload)

    try:
        event = stripe.Webhook.construct_event(
            payload, signature, settings.STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.error.SignatureVerificationError):
        return HttpResponse(status=400)
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        print("\n🔔 Stripe Webhook Event Received:")
        # import pprint
        # pp = pprint.PrettyPrinter(indent=2)
        # print(session)
        # print(json.dumps(event, indent=2))
        # print(json.dumps(request.session, indent=2))

        if session.get("mode") == "setup":
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
