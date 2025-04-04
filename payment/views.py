import stripe
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.conf import settings
from reservations.models import Reservation
from django.http import JsonResponse


# Ensure you're using environment variables in production
stripe.api_key = "sk_test_51R6ae8R0WxX20o0RNVnNeZNS1ndfJJX6fgNT7jElFtCHPoZX0f6669sZsDSaHE02aKOfBg3GFlNZw4eplDRcLDLw009YcMaEK0"

def create_checkout_session(request, reservation_id):
    # Retrieve the specific reservation
    reservation = get_object_or_404(Reservation, pk=reservation_id)
    customer = reservation.customer
    if not customer.stripe_customer_id:
        stripe_customer = stripe.Customer.create(
            email=customer.email,
            metadata={'reservation_id':reservation.id},
        )
        customer.stripe_customer_id = stripe_customer.id
        customer.save()
    else:
        stripe_customer = stripe.Customer.retrieve(customer.stripe_customer_id)
        
    setup_intent = stripe.SetupIntent.create(
        customer=stripe_customer.id,
        automatic_payment_methods={'enabled':True},
    )
    return JsonResponse({'client_secret': setup_intent.client_secret})

    
        
def save_card_page(request, reservation_id):
    return render(request, "save_card.html", {"reservation_id": reservation_id})
    


def cancel(request):
    return render(request, 'stripe/failed.html')