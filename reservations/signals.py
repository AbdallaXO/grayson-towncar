import os
from django.core.mail import send_mail
from django.db.models.signals import post_save
from django.dispatch import receiver
import logging
from reservations.models import Reservation
from .email import send_reservation_confirmation


logger = logging.getLogger(__name__)  # Get a logger instance

@receiver(post_save, sender=Reservation)
def reservationCreated(sender, instance, created, **kwargs):
    reservation = instance
    subject = 'Thank you for Choosing Grayson Towncar we are happy to have you'
    message = f"Hello {reservation.customer.first_name} Thank you for Choosing Grayson Towncar!"
    if created:
        try:
                send_reservation_confirmation(instance)
                logger.info(f"Email sent successfully to {reservation.customer.email}")
        except Exception as e:
                logger.error(f"Error sending email to {reservation.customer.email}: {e}")