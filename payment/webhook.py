import stripe
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from reservations.models import Customer
from django.utils.timezone import now

stripe.api_key = settings.STRIPE_SECRET_KEY


@csrf_exempt
def stripe_webhook(request):

    payload = request.body
    signature = request.META.get("HTTP_STRIPE_SIGNATURE")

    try:
        # Json that contains everything about the event
        event = stripe.Webhook.construct_event(
            payload, signature, settings.STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.error.SignatureVerificationError):
        return HttpResponse(status=400)

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"] # gives access to id customer mode, set up etc..

        if session.get("mode") == "setup": # checking if customer  paying now or later
            customer_id = session.get("customer") #customer id
            setup_intent_id = session.get("setup_intent") # saves card here

            if customer_id and setup_intent_id:
                setup_intent = stripe.SetupIntent.retrieve(setup_intent_id)
                payment_method_id = setup_intent.payment_method

                stripe.PaymentMethod.attach(payment_method_id, customer=customer_id)

                try:
                    customer = Customer.objects.get(stripe_customer_id=customer_id)
                    customer.stripe_payment_method_id = payment_method_id
                    customer.card_saved_at = now()
                    customer.save()
                except Customer.DoesNotExist:
                    pass

    return HttpResponse(status=200)
