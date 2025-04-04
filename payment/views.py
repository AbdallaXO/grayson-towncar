import stripe
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.conf import settings
from reservations.models import Reservation

# Ensure you're using environment variables in production
stripe.api_key = "sk_test_51R6ae8R0WxX20o0RNVnNeZNS1ndfJJX6fgNT7jElFtCHPoZX0f6669sZsDSaHE02aKOfBg3GFlNZw4eplDRcLDLw009YcMaEK0"

def create_checkout_session(request, reservation_id):
    # Retrieve the specific reservation
    reservation = get_object_or_404(Reservation, pk=reservation_id)

    try:
        # Create Stripe Checkout Session
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            mode='setup',  # setup - saves payment method
            
           
            success_url=request.build_absolute_uri('/payment-method-saved/') + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=request.build_absolute_uri(reverse('cancel')),
            
            # Metadata to track the reservation
            metadata={
                'reservation_id': str(reservation.id),
                #can add more stuff here optionally
            }
        )
        
        # stripe checkout
        return redirect(session.url, code=303)
    
    except stripe.error.StripeError as e:
        # Handle any Stripe-related errors
        return render(request, 'stripe/error.html', {'error': str(e)})

def payment_method_saved(request):
    # Retrieve the Checkout Session ID from the URL
    checkout_session_id = request.GET.get('session_id')
    
    if not checkout_session_id:
        return render(request, 'stripe/error.html', {'error': 'No session ID provided'})
    
    try:
        # Retrieve the Checkout Session
        checkout_session = stripe.checkout.Session.retrieve(checkout_session_id)
        
        # Retrieve the SetupIntent
        setup_intent = stripe.SetupIntent.retrieve(checkout_session.setup_intent)
        
        # Get the Payment Method ID
        payment_method_id = setup_intent.payment_method
        
        # Context to pass to the template
        context = {
            'payment_method_id': payment_method_id,
            'reservation_id': checkout_session.metadata.get('reservation_id')
        }
        
        return render(request, 'stripe/payment_method_saved.html', context)
    
    except stripe.error.StripeError as e:
        # Handle any Stripe-related errors
        return render(request, 'stripe/error.html', {'error': str(e)})

def cancel(request):
    return render(request, 'stripe/failed.html')