import stripe
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.conf import settings
from reservations.models import Reservation
from django.http import JsonResponse


# Ensure you're using environment variables in production
stripe.api_key = "sk_test_51R6ae8R0WxX20o0RNVnNeZNS1ndfJJX6fgNT7jElFtCHPoZX0f6669sZsDSaHE02aKOfBg3GFlNZw4eplDRcLDLw009YcMaEK0"

def create_checkout_session(request, reservation_id):
    reservation = get_object_or_404(Reservation, pk=reservation_id)
    customer = reservation.customer

    if not customer.stripe_customer_id:
        stripe_customer = stripe.Customer.create(
            email=customer.email,
            metadata={'reservation': reservation.id},
        )
        customer.stripe_customer_id = stripe_customer.id
        customer.save()
    else:
        stripe_customer = stripe.Customer.retrieve(customer.stripe_customer_id)
    setup_intent = stripe.SetupIntent(
        customer = stripe_customer.id
    )

    print(f"Stripe Customer : {stripe_customer}")

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'pay_now':
            try:
                checkout_session = stripe.checkout.Session.create(
                    customer=stripe_customer.id,
                    line_items=[{
                        'price_data': {
                            'currency': 'usd',
                            'product_data': {
                                'name': f'Reservation #{reservation.id}',
                            },
                            'unit_amount': int(reservation.total_price * 100),
                        },
                        'quantity': 1,
                    }],
                    mode='payment',
                    success_url=request.build_absolute_uri('/'),
                    cancel_url=request.build_absolute_uri('/rates/'),
                )
            except Exception as e:
                return str(e)
            

            return redirect(checkout_session.url, code=303)
        elif action=="save_card":
            return redirect('save_card_checkout', reservation_id=reservation.id)

    return render(request, "stripe/payment.html", {"reservation": reservation})

def save_card(request, reservation_id):
    reservation = get_object_or_404(Reservation, id=reservation_id)
    customer = reservation.customer

    if not customer.stripe_customer_id:
        stripe_customer = stripe.Customer.create(
            email=customer.email,
            metadata={'reservation_id':reservation.id}
        ),
        customer.stripe_customer_id = stripe_customer.id
        customer.save()
    else:
        stripe_customer = stripe.Customer.retrieve(customer.stripe_customer_id)
    checkout_session = stripe.checkout.Session.create(
        customer = stripe_customer.id,
        payment_method_types=['card'],
        mode='setup',
        success_url=request.build_absolute_uri('/'),
        cancel_url=request.build_absolute_uri('/rates/'),
    )

    return redirect(checkout_session.url, code=303)

def stripe_webhook(request):
    ...