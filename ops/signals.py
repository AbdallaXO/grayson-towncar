"""
Signal receivers for automatic ops task creation and closure.
Imported by ops.apps.OpsConfig.ready().

These provide immediate task creation/closure for events that shouldn't
wait for the 30-minute scheduler cycle:
- New lead → lead_response task
- New reservation (unpaid) → payment_chase task (deferred)
- New leg (no driver) → driver_assign task
- Payment received → close payment_chase
- Lead converted/lost → close lead_response
- Driver assigned → close driver_assign/coverage_gap
- Reservation cancelled → cancel all linked tasks
"""

import logging
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone
from datetime import timedelta

logger = logging.getLogger(__name__)

# Store old values for change detection (same pattern as reservations/signals.py)
_leg_old_driver = {}
_lead_old_status = {}
_res_old_status = {}


# ── Lead signals ──

@receiver(pre_save, sender="reservations.Lead")
def _ops_store_lead_old_status(sender, instance, **kwargs):
    """Store old lead status for change detection."""
    if instance.pk:
        try:
            from reservations.models import Lead
            old = Lead.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
            _lead_old_status[instance.pk] = old
        except Exception:
            pass


@receiver(post_save, sender="reservations.Lead")
def _ops_lead_task_handler(sender, instance, created, **kwargs):
    """Create lead_response task on new lead; close on conversion/lost."""
    from .models import OperationalTask
    from .services import create_task, close_task

    if created:
        # New lead — create a lead_response task immediately
        days_until = None
        if instance.pickup_date:
            days_until = (instance.pickup_date - timezone.localdate()).days

        if days_until is not None and days_until <= 3:
            priority = OperationalTask.Priority.CRITICAL
        else:
            priority = OperationalTask.Priority.HIGH

        name = f"{instance.first_name or ''} {instance.last_name or ''}".strip() or "Unknown"
        location_info = ""
        if instance.pickup_location and instance.dropoff_location:
            location_info = f" — {instance.pickup_location} → {instance.dropoff_location}"

        create_task(
            task_type=OperationalTask.TaskType.LEAD_RESPONSE,
            title=f"New lead: {name}{location_info}",
            due_at=timezone.now(),
            priority=priority,
            description=f"Phone: {instance.phone or 'N/A'}, Email: {instance.email or 'N/A'}",
            lead=instance,
            escalate_at=timezone.now() + timedelta(minutes=15),
            metadata={
                "pickup_date": str(instance.pickup_date) if instance.pickup_date else None,
                "estimated_price": str(instance.estimated_price) if instance.estimated_price else None,
                "source": instance.utm_source or "",
            },
        )
    else:
        # Status change — check if lead was converted or lost
        old_status = _lead_old_status.pop(instance.pk, None)
        if old_status and old_status != instance.status:
            if instance.status in ("converted", "lost"):
                # Close any open lead_response tasks
                open_tasks = OperationalTask.objects.filter(
                    task_type=OperationalTask.TaskType.LEAD_RESPONSE,
                    lead=instance,
                    status__in=list(OperationalTask.OPEN_STATUSES),
                )
                for task in open_tasks:
                    close_task(
                        task,
                        resolution_notes=f"Auto-closed: lead marked as {instance.status}",
                        auto=True,
                    )


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
    """
    On new leg without driver → driver_assign task.
    On driver assignment → close driver_assign/coverage_gap tasks.
    """
    from .models import OperationalTask
    from .services import create_task, close_task

    if created:
        if not instance.driver_id:
            days_until = (instance.pickup_date - timezone.localdate()).days if instance.pickup_date else 999
            if days_until <= 3:
                customer_name = instance.reservation.customer.get_full_name()
                priority = (
                    OperationalTask.Priority.CRITICAL if days_until <= 1
                    else OperationalTask.Priority.HIGH if days_until <= 2
                    else OperationalTask.Priority.MEDIUM
                )
                create_task(
                    task_type=OperationalTask.TaskType.DRIVER_ASSIGNMENT,
                    title=f"No driver: {customer_name} — {instance.pickup_date:%b %d}",
                    due_at=timezone.now(),
                    priority=priority,
                    leg=instance,
                    reservation=instance.reservation,
                    metadata={"pickup_date": str(instance.pickup_date)},
                )
    else:
        # Check if driver was just assigned (old was None, new is not)
        old_driver = _leg_old_driver.pop(instance.pk, None)
        if old_driver is None and instance.driver_id:
            # Driver was assigned — close driver_assign/coverage_gap tasks
            tasks = OperationalTask.objects.filter(
                task_type__in=[
                    OperationalTask.TaskType.DRIVER_ASSIGNMENT,
                    OperationalTask.TaskType.COVERAGE_GAP,
                ],
                leg=instance,
                status__in=list(OperationalTask.OPEN_STATUSES),
            )
            for task in tasks:
                close_task(
                    task,
                    resolution_notes=f"Auto-closed: driver {instance.driver} assigned",
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
