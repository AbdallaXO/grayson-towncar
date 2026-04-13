"""
Signal receivers for automatic ops task creation and closure.
Imported by ops.apps.OpsConfig.ready().

These provide immediate task closure for events that shouldn't
wait for the 30-minute scheduler cycle:
- Payment received → close payment_chase
- Driver assigned → close driver_assign
- Driver changed/removed → close driver_conflict (this leg + cross-leg)
- Leg completed/cancelled → close driver_conflict (this leg + cross-leg)
- Reservation cancelled → cancel all linked tasks
"""

import logging
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

logger = logging.getLogger(__name__)

# Store old values for change detection (same pattern as reservations/signals.py)
_leg_old_driver = {}
_leg_old_status = {}
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
def _ops_store_leg_old_values(sender, instance, **kwargs):
    """Store old driver_id and status for change detection.
    Skips DB fetch when update_fields is specified and neither field is in it
    (same guard as reservations/signals.py:store_leg_old_values).
    """
    if not instance.pk:
        return
    update_fields = kwargs.get("update_fields")
    track_driver = update_fields is None or "driver" in update_fields
    track_status = update_fields is None or "status" in update_fields
    if not track_driver and not track_status:
        return
    try:
        from reservations.models import Leg
        old = Leg.objects.filter(pk=instance.pk).values_list("driver_id", "status").first()
        if old:
            if track_driver:
                _leg_old_driver[instance.pk] = old[0]
            if track_status:
                _leg_old_status[instance.pk] = old[1]
    except Exception:
        pass


@receiver(post_save, sender="reservations.Leg")
def _ops_leg_task_handler(sender, instance, created, **kwargs):
    """On driver or status change → close driver_assign and driver_conflict tasks."""
    if created:
        return

    from .models import OperationalTask
    from .services import close_task

    old_driver = _leg_old_driver.pop(instance.pk, _NOT_TRACKED)
    old_status = _leg_old_status.pop(instance.pk, _NOT_TRACKED)
    driver_changed = old_driver is not _NOT_TRACKED and old_driver != instance.driver_id
    status_changed = old_status is not _NOT_TRACKED and old_status != instance.status

    # ── A. Driver change handling ───────────────────────────────────────
    if driver_changed:

        # 1. Driver assigned (None → someone): close driver_assign tasks
        if not old_driver and instance.driver_id:
            for task in OperationalTask.objects.filter(
                task_type=OperationalTask.TaskType.DRIVER_ASSIGNMENT,
                leg=instance,
                status__in=list(OperationalTask.OPEN_STATUSES),
            ):
                close_task(
                    task,
                    resolution_notes=f"Auto-closed: driver {instance.driver} assigned",
                    auto=True,
                )

        # 2. Driver conflict tasks on THIS leg
        # If the driver changed (reassigned or removed), the original
        # conflict recorded in the task metadata is no longer valid.
        for task in OperationalTask.objects.filter(
            task_type=OperationalTask.TaskType.DRIVER_CONFLICT,
            leg=instance,
            status__in=list(OperationalTask.OPEN_STATUSES),
        ):
            meta_driver = (task.metadata or {}).get("driver_id")
            if not instance.driver_id:
                # Driver removed — no conflict possible
                close_task(
                    task,
                    resolution_notes="Auto-closed: driver unassigned",
                    auto=True,
                )
            elif meta_driver and meta_driver != instance.driver_id:
                # Different driver now — original conflict is moot
                close_task(
                    task,
                    resolution_notes="Auto-closed: driver reassigned, original conflict resolved",
                    auto=True,
                )

        # 3. Conflict tasks on OTHER legs that reference THIS leg
        # A conflict task on leg_b stores conflicting_leg_id=leg_a in
        # metadata. If leg_a's driver changed, the pair may no longer
        # share the same driver → conflict resolved.
        for task in OperationalTask.objects.filter(
            task_type=OperationalTask.TaskType.DRIVER_CONFLICT,
            status__in=list(OperationalTask.OPEN_STATUSES),
            metadata__conflicting_leg_id=instance.pk,
        ).select_related("leg"):
            if not task.leg or not task.leg.driver_id:
                continue  # leg's own driver is gone; handled by scanner
            if task.leg.driver_id != instance.driver_id:
                # The two legs no longer share a driver
                close_task(
                    task,
                    resolution_notes="Auto-closed: conflicting leg reassigned to different driver",
                    auto=True,
                )

    # ── B. Status change handling ───────────────────────────────────────
    if status_changed and instance.status in ("completed", "cancelled"):

        # 1. Close conflict tasks on THIS leg (leg is done)
        for task in OperationalTask.objects.filter(
            task_type=OperationalTask.TaskType.DRIVER_CONFLICT,
            leg=instance,
            status__in=list(OperationalTask.OPEN_STATUSES),
        ):
            close_task(
                task,
                resolution_notes=f"Auto-closed: leg {instance.status}",
                auto=True,
            )

        # 2. Close conflict tasks on OTHER legs that reference THIS leg
        for task in OperationalTask.objects.filter(
            task_type=OperationalTask.TaskType.DRIVER_CONFLICT,
            status__in=list(OperationalTask.OPEN_STATUSES),
            metadata__conflicting_leg_id=instance.pk,
        ):
            close_task(
                task,
                resolution_notes=f"Auto-closed: conflicting leg {instance.status}",
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
