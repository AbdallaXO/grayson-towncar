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
from django.db import transaction
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

    Tests BOTH names for the FK: Django accepts the field name ("driver") and the
    attname ("driver_id") in update_fields and normalises neither, and it builds
    update_fields from the ATTNAME on its own when saving a deferred instance
    (one loaded via .only()/.defer()). Matching only "driver" silently disabled
    every conflict-task close and flag takedown on those saves — the guard this
    whole path leans on, failing quietly. Same test the sandbox tripwire uses
    (dispatching/assignment.py:179).
    """
    if not instance.pk:
        return
    update_fields = kwargs.get("update_fields")
    track_driver = update_fields is None or bool({"driver", "driver_id"} & set(update_fields))
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

    # Conflict + tight-turn tasks both share the driver_id/conflicting_leg_id metadata
    # shape, so a reassignment resolves either tier.
    _CONFLICT_TYPES = [
        OperationalTask.TaskType.DRIVER_CONFLICT,
        OperationalTask.TaskType.TIGHT_TURN,
    ]
    # A dispatcher-initiated reassignment sets this on the leg (see
    # dispatching.views.update_leg_assignment) so the auto-close can be attributed
    # in the activity feed; system-driven saves leave it None (closed silently).
    _actor = getattr(instance, "_reassigned_by", None)

    # Every leg whose conflict FLAG may be stale once the closes below are done.
    # Collected from the tasks themselves as they close, because a task's flag
    # does not always sit on task.leg — a same-day flight shift flags the OTHER
    # leg it delays (metadata['affected_leg_id']). The saved leg is always a
    # candidate: its flag may already have been orphaned by an earlier sweep.
    touched_leg_ids = {instance.pk}

    def _mark_touched(task):
        meta = task.metadata or {}
        affected = meta.get("affected_leg_id")
        if affected:
            touched_leg_ids.add(affected)
            return
        # No affected_leg_id: the task predates the key (every task written before
        # 2026-08-27), and the flag may sit on EITHER leg of the pair. Offer both —
        # reconcile re-tests each against the invariant, so a wrong guess costs one
        # indexed seek while a missing one leaves red on the board for half an hour.
        touched_leg_ids.add(task.leg_id)
        if meta.get("conflicting_leg_id"):
            touched_leg_ids.add(meta["conflicting_leg_id"])

    def _close_and_log(task, notes):
        _mark_touched(task)
        close_task(task, resolved_by=_actor, resolution_notes=notes, auto=True)
        if _actor:
            try:
                from .models import StaffActivity
                StaffActivity.objects.create(
                    user=_actor,
                    action_type=StaffActivity.ActionType.TASK_COMPLETED,
                    task=task,
                    metadata={"resolution": "driver_reassigned",
                              "new_driver_id": instance.driver_id},
                )
            except Exception:
                logger.warning("Failed to log reassign activity for task %s", task.id)

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
            task_type__in=_CONFLICT_TYPES,
            leg=instance,
            status__in=list(OperationalTask.OPEN_STATUSES),
        ):
            meta_driver = (task.metadata or {}).get("driver_id")
            if not instance.driver_id:
                # Driver removed — no conflict possible
                _close_and_log(task, "Auto-closed: driver unassigned")
            elif meta_driver and meta_driver != instance.driver_id:
                # Different driver now — original conflict is moot
                _close_and_log(
                    task,
                    "Auto-closed: driver reassigned, original conflict resolved",
                )

        # 3. Conflict tasks on OTHER legs that reference THIS leg
        # A conflict task on leg_b stores conflicting_leg_id=leg_a in
        # metadata. If leg_a's driver changed, the pair may no longer
        # share the same driver → conflict resolved.
        for task in OperationalTask.objects.filter(
            task_type__in=_CONFLICT_TYPES,
            status__in=list(OperationalTask.OPEN_STATUSES),
            metadata__conflicting_leg_id=instance.pk,
        ).select_related("leg"):
            if not task.leg or not task.leg.driver_id:
                continue  # leg's own driver is gone; handled by scanner
            if task.leg.driver_id != instance.driver_id:
                # The two legs no longer share a driver
                _close_and_log(
                    task,
                    "Auto-closed: conflicting leg reassigned to different driver",
                )

    # ── B. Status change handling ───────────────────────────────────────
    if status_changed and instance.status in ("completed", "cancelled"):

        # 1. Close conflict tasks on THIS leg (leg is done)
        for task in OperationalTask.objects.filter(
            task_type__in=_CONFLICT_TYPES,
            leg=instance,
            status__in=list(OperationalTask.OPEN_STATUSES),
        ):
            _mark_touched(task)
            close_task(
                task,
                resolution_notes=f"Auto-closed: leg {instance.status}",
                auto=True,
            )

        # 2. Close conflict tasks on OTHER legs that reference THIS leg
        for task in OperationalTask.objects.filter(
            task_type__in=_CONFLICT_TYPES,
            status__in=list(OperationalTask.OPEN_STATUSES),
            metadata__conflicting_leg_id=instance.pk,
        ):
            _mark_touched(task)
            close_task(
                task,
                resolution_notes=f"Auto-closed: conflicting leg {instance.status}",
                auto=True,
            )

    # ── C. Take the board's conflict flags down with the tasks ──────────────
    # The closes above are invisible to dispatchers — the red KEOI badge on the
    # board is what they read. Without this the flag outlived its conflict until
    # the next 30-minute sweep, so a dispatcher who had just fixed the problem
    # still saw red (leg 30493 carried a flag naming a driver the leg no longer
    # had). Same rule as the sweep, scoped to the legs this save touched.
    #
    # Never allowed to break a leg save: a flag that fails to come down is a
    # cosmetic problem, a failed save is a real one.
    if driver_changed or status_changed:
        try:
            from .tasks import reconcile_conflict_keois
            # A savepoint, not just try/except. publish_draft wraps its whole
            # per-leg loop in transaction.atomic() (dispatching/views.py), so a
            # DatabaseError swallowed here would leave the connection needing
            # rollback and blow up the NEXT query in that loop — costing a whole
            # day's publish to save a badge. The savepoint absorbs it instead.
            with transaction.atomic():
                reconcile_conflict_keois(touched_leg_ids)
        except Exception:
            logger.exception(
                "Could not reconcile conflict KEOIs after leg %s changed", instance.pk
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
