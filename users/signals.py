import logging
from django.db.models.signals import post_save
from django.dispatch import receiver
from .emails import thankyou_email
from .models import PartnerForm, ContactUsForm

logger = logging.getLogger(__name__)


@receiver(post_save, sender=PartnerForm)
@receiver(post_save, sender=ContactUsForm)
def handle_form_submission(sender, instance, created, **kwargs):
    """
    Signal handler that sends email confirmation when a form is submitted
    Handles PartnerForm, ContactUsForm, and NewsLetter models
    """
    if created:
        try:
            logger.info(f"Attempting to Email {instance} from {sender.__name__}")
            thankyou_email(instance)
        except Exception as e:
            logger.error(f"Error sending email for {sender.__name__}: {e}")
