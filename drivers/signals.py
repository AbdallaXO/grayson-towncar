import logging
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.db import transaction

from reservations.models import Leg
from reservations.utils import send_driver_status_notification

logger = logging.getLogger(__name__)


@receiver(pre_save, sender=Leg)
def leg_pre_save(sender, instance, **kwargs):
    """
    Store the old status before saving to detect changes
    """
    if instance.pk:
        try:
            old_instance = Leg.objects.get(pk=instance.pk)
            instance._old_status = old_instance.status
        except Leg.DoesNotExist:
            instance._old_status = None
    else:
        instance._old_status = None


@receiver(post_save, sender=Leg)
def leg_status_changed(sender, instance, created, **kwargs):
    """
    Send NTFY notification when leg status changes
    """
    # Skip if this is a new leg (created=True) or no driver assigned
    if created or not instance.driver:
        return

    # Check if status actually changed
    old_status = getattr(instance, '_old_status', None)
    new_status = instance.status

    if old_status != new_status and new_status:
        try:
            logger.info(
                f"Leg {instance.id} status changed from '{old_status}' to '{new_status}'"
            )
            
            # Send notification in a separate transaction to avoid issues
            with transaction.atomic():
                send_driver_status_notification(
                    leg=instance,
                    old_status=old_status,
                    new_status=new_status
                )
                
        except Exception as e:
            logger.error(
                f"Error sending driver status notification for leg {instance.id}: {str(e)}"
            )
