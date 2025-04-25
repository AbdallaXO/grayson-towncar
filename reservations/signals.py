import logging
from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Reservation
from users.emails import send_reservation_confirmation
logger = logging.getLogger(__name__)  # Get a logger instance


@receiver(post_save, sender=Reservation)
def reservation_updated(sender, instance, created, **kwargs):
    """
    Signal handler that sends email confirmation when a reservation is updated.
    Only sends email for updates, not for newly created reservations.
    """
    if not created:  
        try:
            send_reservation_confirmation(instance)
            logger.info(f"Confirmation email sent to {instance.customer.email} for updated reservation")
        except Exception as e:
            logger.error(f"Failed to send confirmation email to {instance.customer.email}: {e}")