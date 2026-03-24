import logging
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from reservations.models import Leg
from reservations.utils import send_driver_status_notification, _run_in_background

logger = logging.getLogger(__name__)


@receiver(pre_save, sender=Leg)
def leg_pre_save(sender, instance, **kwargs):
    """
    Store the old status before saving to detect changes.
    Uses a lightweight values_list query instead of fetching the full object.
    Skips the DB fetch when update_fields doesn't include 'status'.
    """
    if not instance.pk:
        instance._old_status = None
        return
    update_fields = kwargs.get('update_fields')
    if update_fields is not None and 'status' not in update_fields:
        instance._old_status = None
        return
    # Lightweight: only fetch the status column, not the full row
    old_status = Leg.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    instance._old_status = old_status


@receiver(post_save, sender=Leg)
def leg_status_changed(sender, instance, created, **kwargs):
    """
    Send NTFY notification when leg status changes.
    Runs in background thread to avoid blocking the driver's request.
    """
    if created or not instance.driver:
        return

    old_status = getattr(instance, '_old_status', None)
    new_status = instance.status

    if old_status is not None and old_status != new_status and new_status:
        logger.info(f"Leg {instance.id} status changed from '{old_status}' to '{new_status}'")
        try:
            _run_in_background(
                send_driver_status_notification,
                leg=instance,
                old_status=old_status,
                new_status=new_status,
            )
        except Exception as e:
            logger.error(f"Error queuing driver status notification for leg {instance.id}: {e}")
