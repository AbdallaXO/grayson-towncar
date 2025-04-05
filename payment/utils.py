import stripe
from django.shortcuts import render, redirect, get_object_or_404
import stripe.error
from reservations.models import Reservation
from logging import Logger


def get_or_create_stripe_customer(reservation):
    """Takes the Reservation and Returns an existing stripe object or creates one for the customer
    also updates the reservation.customers stripe_customer_id to his stripe_customer_id after creating it."""
    if not reservation.customer.stripe_customer_id:
        stripe_customer = stripe.Customer.create(
            email=reservation.customer.email,
            metadata={
                "reservation_id": reservation.id,
                "trip_type": reservation.trip_type,
            },
        )
        reservation.customer.stripe_customer_id = stripe_customer.id
        reservation.customer.save()
    else:
        stripe_customer = stripe.Customer.retrieve(
            id=reservation.customer.stripe_customer_id
        )
    return stripe_customer
