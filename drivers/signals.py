import logging
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from reservations.models import Leg
from reservations.utils import send_driver_status_notification, _run_in_background

logger = logging.getLogger(__name__)


# Fields whose changes the driver should hear about (push) or that the NTFY
# status notification needs. The pre_save fetch is skipped entirely when
# update_fields names none of them (e.g. confirmation_sms_sent_at saves).
_WATCHED_FIELDS = {"status", "driver", "driver_id", "pickup_date", "pickup_time"}


@receiver(pre_save, sender=Leg)
def leg_pre_save(sender, instance, **kwargs):
    """
    Store the old status/driver/pickup before saving to detect changes.
    Uses a lightweight values_list query instead of fetching the full object.
    Skips the DB fetch when update_fields doesn't include a watched field.
    """
    instance._old_status = None
    instance._old_driver_id = None
    instance._old_pickup_date = None
    instance._old_pickup_time = None
    instance._push_old_loaded = False
    if not instance.pk:
        return
    update_fields = kwargs.get('update_fields')
    if update_fields is not None and not (_WATCHED_FIELDS & set(update_fields)):
        return
    # Lightweight: only fetch the watched columns, not the full row
    row = (
        Leg.objects.filter(pk=instance.pk)
        .values_list("status", "driver_id", "pickup_date", "pickup_time")
        .first()
    )
    if row:
        (instance._old_status, instance._old_driver_id,
         instance._old_pickup_date, instance._old_pickup_time) = row
        instance._push_old_loaded = True


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


@receiver(post_save, sender=Leg)
def leg_push_notifications(sender, instance, created, **kwargs):
    """
    Web Push to drivers when DISPATCH changes their schedule: new trip,
    trip taken away, retime, or cancellation. A driver's own status taps
    never match these conditions (drivers can't reassign, retime, or set
    'cancelled'), so nobody gets buzzed about their own action. Bulk apply
    actions coalesce into one notification via the per-driver debounce.
    Caveat: queryset.update() bypasses signals — standard saves only.
    """
    from django.utils import timezone as _tz
    from drivers.push import push_enabled, queue_schedule_notice

    if not push_enabled():
        return
    try:
        if instance.pickup_date and instance.pickup_date < _tz.localdate():
            return  # changes to past trips don't matter

        if created:
            if instance.driver_id and instance.status != "cancelled":
                queue_schedule_notice(instance.driver_id, "new", instance)
            return

        if not getattr(instance, "_push_old_loaded", False):
            return

        old_driver = instance._old_driver_id
        new_driver = instance.driver_id

        if old_driver != new_driver:
            if old_driver:
                queue_schedule_notice(old_driver, "removed", instance)
            if new_driver and instance.status != "cancelled":
                queue_schedule_notice(new_driver, "new", instance)
            return

        if not new_driver:
            return

        if (instance._old_status and instance._old_status != "cancelled"
                and instance.status == "cancelled"):
            queue_schedule_notice(new_driver, "cancelled", instance)
            return

        if (instance._old_pickup_date and instance._old_pickup_time
                and (instance._old_pickup_date != instance.pickup_date
                     or instance._old_pickup_time != instance.pickup_time)):
            queue_schedule_notice(new_driver, "retimed", instance)
    except Exception as e:
        logger.error(f"Error queuing push notification for leg {instance.id}: {e}")
