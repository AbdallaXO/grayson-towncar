"""
Escalation engine for operational tasks.

Called from the scheduler every 30 minutes via generate_ops_tasks().
Escalates tasks that have passed their escalate_at time and sends
NTFY push notifications to the owner.
"""

import logging
from django.utils import timezone

from .models import OperationalTask

logger = logging.getLogger(__name__)


def run_escalations():
    """
    Main escalation entry point. Called by the scheduler.
    Returns count of tasks escalated.
    """
    now = timezone.now()
    escalated = 0

    # Find tasks past their escalation time that haven't been escalated yet
    tasks_to_escalate = OperationalTask.objects.filter(
        escalate_at__isnull=False,
        escalate_at__lte=now,
        status__in=["pending", "in_progress", "snoozed"],
    ).exclude(
        status="escalated",
    ).select_related("reservation", "reservation__customer", "leg")

    for task in tasks_to_escalate:
        _escalate_task(task)
        escalated += 1

    if escalated:
        logger.info(f"Escalation engine: escalated {escalated} tasks")

    return escalated


def _escalate_task(task):
    """
    Escalate a single task: update status, bump priority, send NTFY.
    """
    old_priority = task.priority
    task.status = OperationalTask.Status.ESCALATED

    # Bump priority to CRITICAL if not already
    if task.priority > OperationalTask.Priority.CRITICAL:
        task.priority = OperationalTask.Priority.CRITICAL

    task.save(update_fields=["status", "priority", "updated_at"])

    logger.info(
        f"Task #{task.id} escalated: {task.title} "
        f"(priority {old_priority} -> {task.priority})"
    )

    # Send NTFY notification
    _send_escalation_ntfy(task)


def _send_escalation_ntfy(task):
    """
    Send an NTFY push notification for an escalated task.
    Reuses the existing dispatch alert infrastructure.
    """
    try:
        from reservations.utils import send_dispatch_alert_notification

        type_label = task.get_task_type_display()
        title = f"ESCALATED: {type_label}"

        # Build message body
        lines = [task.title]
        if task.description:
            lines.append(task.description[:200])

        # Add context based on task type
        if task.customer:
            customer = task.customer
            lines.append(f"Guest: {customer.get_full_name()}")
            if hasattr(customer, "phone_number") and customer.phone_number:
                lines.append(f"Phone: {customer.phone_number}")

        if task.attempts > 0:
            lines.append(f"Attempts: {task.attempts}/{task.max_attempts}")

        message = "\n".join(lines)

        # Map task types to NTFY tags
        tags_map = {
            "payment_chase": ["money_with_wings", "warning"],
            "flight_verify": ["airplane", "warning"],
            "driver_assign": ["car", "warning"],
            "contact_form": ["envelope", "warning"],
        }
        tags = tags_map.get(task.task_type, ["warning"])

        send_dispatch_alert_notification(
            title=title,
            message=message,
            priority="urgent",
            tags=tags,
        )
    except Exception as e:
        logger.warning(f"Failed to send escalation NTFY for task #{task.id}: {e}")
