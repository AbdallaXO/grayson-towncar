"""
Service helpers for the ops task system.
Provides create_task() and log_communication() used by signals, views, and scheduler.
"""

import logging
from django.utils import timezone
from .models import OperationalTask, CommunicationAttempt, StaffActivity

logger = logging.getLogger(__name__)


def create_task(
    task_type,
    title,
    due_at=None,
    priority=OperationalTask.Priority.MEDIUM,
    description="",
    reservation=None,
    leg=None,
    lead=None,
    created_by=None,
    assigned_to=None,
    escalate_at=None,
    blocked_by=None,
    metadata=None,
    max_attempts=5,
):
    """
    Create an OperationalTask with dedup check.
    Returns the task if created, or None if a duplicate open task already exists.
    """
    if due_at is None:
        due_at = timezone.now()

    # Dedup: check for existing open task of same type on same related object
    dedup_filter = {
        "task_type": task_type,
        "status__in": list(OperationalTask.OPEN_STATUSES),
    }
    if leg:
        dedup_filter["leg"] = leg
    elif reservation:
        dedup_filter["reservation"] = reservation
    elif lead:
        dedup_filter["lead"] = lead
    else:
        # Manual tasks or tasks without related objects don't dedup
        dedup_filter = None

    if dedup_filter and OperationalTask.objects.filter(**dedup_filter).exists():
        return None

    task = OperationalTask.objects.create(
        task_type=task_type,
        title=title,
        due_at=due_at,
        priority=priority,
        description=description,
        reservation=reservation,
        leg=leg,
        lead=lead,
        created_by=created_by,
        assigned_to=assigned_to,
        escalate_at=escalate_at,
        blocked_by=blocked_by,
        metadata=metadata or {},
        max_attempts=max_attempts,
    )
    logger.info(f"Ops task created: [{task.get_priority_display()}] {title} (#{task.id})")
    return task


def close_task(task, resolved_by=None, resolution_notes="", auto=False):
    """
    Mark an OperationalTask as completed.
    """
    if not task.is_open:
        return

    task.status = OperationalTask.Status.COMPLETED
    task.resolved_at = timezone.now()
    task.resolved_by = resolved_by
    task.resolution_notes = resolution_notes or ("Auto-closed" if auto else "")
    task.save(update_fields=["status", "resolved_at", "resolved_by", "resolution_notes", "updated_at"])
    logger.info(f"Ops task closed: {task.title} (#{task.id}) — {task.resolution_notes}")


def cancel_task(task, reason=""):
    """
    Cancel an OperationalTask (e.g. reservation was cancelled).
    """
    if not task.is_open:
        return

    task.status = OperationalTask.Status.CANCELLED
    task.resolved_at = timezone.now()
    task.resolution_notes = reason or "Cancelled"
    task.save(update_fields=["status", "resolved_at", "resolution_notes", "updated_at"])
    logger.info(f"Ops task cancelled: {task.title} (#{task.id}) — {reason}")


def close_tasks_for_reservation(reservation, resolved_by=None, task_types=None, reason=""):
    """
    Close all open tasks linked to a reservation (and its legs).
    Optionally filter by task_types list.
    """
    qs = OperationalTask.objects.filter(
        status__in=list(OperationalTask.OPEN_STATUSES),
    ).filter(
        # Tasks linked to this reservation directly or via its legs
        models_Q_reservation_or_legs(reservation)
    )
    if task_types:
        qs = qs.filter(task_type__in=task_types)

    for task in qs:
        close_task(task, resolved_by=resolved_by, resolution_notes=reason, auto=True)


def models_Q_reservation_or_legs(reservation):
    """Build a Q filter for tasks linked to a reservation or any of its legs."""
    from django.db.models import Q

    leg_ids = list(reservation.legs.values_list("id", flat=True))
    q = Q(reservation=reservation)
    if leg_ids:
        q |= Q(leg_id__in=leg_ids)
    return q


def log_communication(task, channel, outcome, user, notes="", contact_value="", duration=None, metadata=None):
    """
    Log a communication attempt on a task and update the task's attempt counters.
    Returns the CommunicationAttempt instance.
    """
    attempt = CommunicationAttempt.objects.create(
        task=task,
        channel=channel,
        outcome=outcome,
        staff_user=user,
        notes=notes,
        contact_value=contact_value,
        duration_seconds=duration,
        metadata=metadata or {},
    )

    task.attempts += 1
    task.last_attempt_at = timezone.now()
    task.save(update_fields=["attempts", "last_attempt_at", "updated_at"])

    # Log staff activity
    StaffActivity.objects.create(
        user=user,
        action_type=StaffActivity.ActionType.COMM_LOGGED,
        task=task,
        metadata={"channel": channel, "outcome": outcome},
    )

    logger.info(
        f"Comm logged on task #{task.id}: {channel} → {outcome} by {user}"
    )
    return attempt
