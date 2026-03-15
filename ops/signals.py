"""
Signal receivers for automatic ops task creation and closure.
Imported by ops.apps.OpsConfig.ready().

These provide immediate task closure for events that shouldn't
wait for the 30-minute scheduler cycle:
- Payment received → close payment_chase
- Driver assigned → close driver_assign
- Reservation cancelled → cancel all linked tasks
"""

import logging
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

logger = logging.getLogger(__name__)

# Store old values for change detection (same pattern as reservations/signals.py)
_leg_old_driver = {}
_res_old_status = {}
_NOT_TRACKED = object()  # Sentinel: pre_save didn't store this leg


# ── Reservation signals ──

@receiver(pre_save, sender="reservations.Reservation")
def _ops_store_res_old_status(sender, instance, **kwargs):
    """Store old reservation status for change detection."""
    if instance.pk:
        try:
            from reservations.models import Reservation
            old = Reservation.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
            _res_old_status[instance.pk] = old
        except Exception:
            pass


@receiver(post_save, sender="reservations.Reservation")
def _ops_reservation_task_handler(sender, instance, created, **kwargs):
    """Cancel all tasks when reservation is cancelled."""
    from .models import OperationalTask
    from .services import cancel_task

    if not created:
        old_status = _res_old_status.pop(instance.pk, None)
        if old_status != instance.status and instance.status == "cancelled":
            # Cancel all open tasks for this reservation
            from .services import models_Q_reservation_or_legs
            open_tasks = OperationalTask.objects.filter(
                status__in=list(OperationalTask.OPEN_STATUSES),
            ).filter(models_Q_reservation_or_legs(instance))

            for task in open_tasks:
                cancel_task(task, reason="Reservation cancelled")


# ── Leg signals ──

@receiver(pre_save, sender="reservations.Leg")
def _ops_store_leg_old_driver(sender, instance, **kwargs):
    """Store old driver_id for change detection.
    Skips DB fetch when update_fields is specified and driver not in it
    (same guard as reservations/signals.py:store_leg_old_values).
    """
    if not instance.pk:
        return
    update_fields = kwargs.get("update_fields")
    if update_fields is not None and "driver" not in update_fields:
        return
    try:
        from reservations.models import Leg
        old = Leg.objects.filter(pk=instance.pk).values_list("driver_id", flat=True).first()
        _leg_old_driver[instance.pk] = old
    except Exception:
        pass


@receiver(post_save, sender="reservations.Leg")
def _ops_leg_task_handler(sender, instance, created, **kwargs):
    """On driver assignment → close driver_assign tasks."""
    from .models import OperationalTask
    from .services import close_task

    if not created:
        old_driver = _leg_old_driver.pop(instance.pk, _NOT_TRACKED)
        if old_driver is _NOT_TRACKED:
            # pre_save didn't track this save (update_fields without "driver")
            # — skip, driver didn't change
            return
        if not old_driver and instance.driver_id:
            # Driver went from None → assigned: close driver_assign tasks
            tasks = OperationalTask.objects.filter(
                task_type=OperationalTask.TaskType.DRIVER_ASSIGNMENT,
                leg=instance,
                status__in=list(OperationalTask.OPEN_STATUSES),
            )
            for task in tasks:
                close_task(
                    task,
                    resolution_notes=f"Auto-closed: driver {instance.driver} assigned",
                    auto=True,
                )


# ── Contact form signals ──

@receiver(post_save, sender="users.ContactUsForm")
def _ops_contact_form_handler(sender, instance, created, **kwargs):
    """Close contact_form tasks when form is marked as contacted or closed."""
    if instance.status in ("contacted", "closed"):
        from .models import OperationalTask
        from .services import close_task

        tasks = OperationalTask.objects.filter(
            task_type=OperationalTask.TaskType.CONTACT_FORM,
            contact_form=instance,
            status__in=list(OperationalTask.OPEN_STATUSES),
        )
        for task in tasks:
            close_task(
                task,
                resolution_notes=f"Auto-closed: form marked {instance.status}",
                auto=True,
            )


# ── Payment signals ──

@receiver(post_save, sender="payment.Payment")
def _ops_payment_task_handler(sender, instance, created, **kwargs):
    """Close payment_chase tasks when a payment is marked as paid."""
    from .models import OperationalTask
    from .services import close_task

    if instance.status == "paid" and instance.reservation_id:
        tasks = OperationalTask.objects.filter(
            task_type=OperationalTask.TaskType.PAYMENT_CHASE,
            reservation_id=instance.reservation_id,
            status__in=list(OperationalTask.OPEN_STATUSES),
        )
        for task in tasks:
            close_task(
                task,
                resolution_notes=f"Auto-closed: payment received (${instance.amount})",
                auto=True,
            )
