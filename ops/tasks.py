"""
Scheduler-based task scanners for automatic ops task generation, escalation, and cleanup.

Called from ghl_integration/scheduler.py every 30 minutes.
Each _scan_* function queries for conditions that warrant a task, deduplicates
against existing open tasks, and bulk-creates new ones.
"""

import logging
from datetime import timedelta
from django.utils import timezone
from django.db.models import Q, Exists, OuterRef

from .models import OperationalTask
from .services import create_task, close_task

logger = logging.getLogger(__name__)


def generate_ops_tasks():
    """
    Main entry point called by the scheduler every 30 minutes.
    Returns a summary dict of actions taken.
    """
    created = 0
    closed = 0
    reopened = 0

    created += _scan_flight_mismatches()
    created += _scan_upcoming_confirmations()
    created += _scan_unassigned_legs()
    created += _scan_unpaid_reservations()
    closed += _auto_close_resolved_tasks()
    reopened += _reopen_snoozed_tasks()

    # Run escalation engine
    from .escalation import run_escalations
    escalated = 0
    try:
        escalated = run_escalations()
    except Exception as e:
        logger.error(f"Escalation engine error: {e}", exc_info=True)

    return {"created": created, "closed": closed, "reopened": reopened, "escalated": escalated}


def _scan_flight_mismatches():
    """
    Create flight_verify tasks for arrival legs (tomorrow and day-after)
    where the flight time diverges from the booked pickup time by 30+ min.
    """
    from reservations.models import Leg

    now = timezone.now()
    today = timezone.localdate()
    tomorrow = today + timedelta(days=1)
    day_after = today + timedelta(days=2)

    legs = (
        Leg.objects.filter(
            pickup_date__in=[tomorrow, day_after],
            flight_information__isnull=False,
        )
        .exclude(status__in=["completed", "cancelled"])
        .exclude(reservation__status="cancelled")
        .select_related("flight_information", "reservation", "reservation__customer")
    )

    created = 0
    for leg in legs:
        if leg.get_trip_type() != "arrival":
            continue
        if not leg.has_flight_time_mismatch(threshold_minutes=30):
            continue

        mismatch = leg.get_flight_time_mismatch_display()
        if not mismatch:
            continue

        customer_name = leg.reservation.customer.get_full_name()
        flight = leg.flight_information
        flight_label = f"{flight.airline_display_name or flight.airline or ''} {flight.flight_number or ''}".strip()

        task = create_task(
            task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
            title=f"Flight mismatch: {customer_name} — {flight_label} {mismatch['label']}",
            due_at=now,
            priority=OperationalTask.Priority.HIGH,
            description=(
                f"Pickup at {leg.pickup_time:%I:%M %p} but flight is {mismatch['label']}. "
                f"Call guest to verify correct pickup time."
            ),
            leg=leg,
            reservation=leg.reservation,
            escalate_at=now + timedelta(hours=4),
            metadata={
                "mismatch_direction": mismatch["direction"],
                "mismatch_minutes": mismatch["minutes"],
                "mismatch_label": mismatch["label"],
                "flight_ident": flight_label,
                "pickup_date": str(leg.pickup_date),
                "pickup_time": str(leg.pickup_time),
            },
        )
        if task:
            created += 1

    if created:
        logger.info(f"Flight scan: created {created} flight_verify tasks")
    return created


def _scan_upcoming_confirmations():
    """
    Create guest_confirm tasks for legs tomorrow that haven't been confirmed yet.
    Only for legs with a driver assigned and not cancelled.
    """
    from reservations.models import Leg

    now = timezone.now()
    tomorrow = timezone.localdate() + timedelta(days=1)

    legs = (
        Leg.objects.filter(
            pickup_date=tomorrow,
            driver__isnull=False,
            confirmation_sms_sent_at__isnull=True,
        )
        .exclude(status__in=["completed", "cancelled"])
        .exclude(reservation__status="cancelled")
        .select_related("reservation", "reservation__customer")
    )

    # Set due_at to today 3 PM Eastern (confirmations should go out afternoon before)
    try:
        import pytz
        eastern = pytz.timezone("US/Eastern")
        today_3pm = timezone.now().astimezone(eastern).replace(
            hour=15, minute=0, second=0, microsecond=0
        )
        if today_3pm < timezone.now().astimezone(eastern):
            # Already past 3 PM, due now
            due = now
        else:
            due = today_3pm
    except Exception:
        due = now

    # Check for open flight_verify tasks to set up soft dependency
    open_flight_tasks = dict(
        OperationalTask.objects.filter(
            task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
            status__in=list(OperationalTask.OPEN_STATUSES),
        ).values_list("leg_id", "id")
    )

    created = 0
    for leg in legs:
        customer_name = leg.reservation.customer.get_full_name()
        blocked_by_id = open_flight_tasks.get(leg.id)
        blocked_by_task = None
        if blocked_by_id:
            try:
                blocked_by_task = OperationalTask.objects.get(id=blocked_by_id)
            except OperationalTask.DoesNotExist:
                pass

        task = create_task(
            task_type=OperationalTask.TaskType.GUEST_CONFIRMATION,
            title=f"Confirm: {customer_name} — {leg.pickup_date:%b %d} {leg.pickup_time:%I:%M %p}",
            due_at=due,
            priority=OperationalTask.Priority.MEDIUM,
            description=f"{leg.pickup_location} → {leg.dropoff_location}",
            leg=leg,
            reservation=leg.reservation,
            blocked_by=blocked_by_task,
            escalate_at=now.replace(hour=18, minute=0, second=0),
            metadata={
                "pickup_date": str(leg.pickup_date),
                "pickup_time": str(leg.pickup_time),
                "pickup_location": leg.pickup_location or "",
                "dropoff_location": leg.dropoff_location or "",
            },
        )
        if task:
            created += 1

    if created:
        logger.info(f"Confirmation scan: created {created} guest_confirm tasks")
    return created


def _scan_unassigned_legs():
    """
    Create driver_assign / coverage_gap tasks for legs in the next 3 days
    without a driver assigned.
    """
    from reservations.models import Leg

    now = timezone.now()
    today = timezone.localdate()
    horizon = today + timedelta(days=3)

    legs = (
        Leg.objects.filter(
            pickup_date__range=[today, horizon],
            driver__isnull=True,
        )
        .exclude(status__in=["completed", "cancelled"])
        .exclude(reservation__status="cancelled")
        .select_related("reservation", "reservation__customer")
    )

    created = 0
    for leg in legs:
        days_until = (leg.pickup_date - today).days
        if days_until <= 1:
            priority = OperationalTask.Priority.CRITICAL
            task_type = OperationalTask.TaskType.DRIVER_ASSIGNMENT
        elif days_until <= 2:
            priority = OperationalTask.Priority.HIGH
            task_type = OperationalTask.TaskType.DRIVER_ASSIGNMENT
        else:
            priority = OperationalTask.Priority.MEDIUM
            task_type = OperationalTask.TaskType.COVERAGE_GAP

        customer_name = leg.reservation.customer.get_full_name()

        task = create_task(
            task_type=task_type,
            title=f"No driver: {customer_name} — {leg.pickup_date:%b %d} {leg.pickup_time:%I:%M %p}",
            due_at=now if days_until <= 1 else now + timedelta(hours=days_until * 8),
            priority=priority,
            description=f"{leg.pickup_location} → {leg.dropoff_location}",
            leg=leg,
            reservation=leg.reservation,
            escalate_at=timezone.make_aware(
                timezone.datetime.combine(
                    leg.pickup_date - timedelta(days=1),
                    timezone.datetime.min.time().replace(hour=9),
                )
            ) if days_until > 1 else now,
            metadata={
                "pickup_date": str(leg.pickup_date),
                "days_until": days_until,
            },
        )
        if task:
            created += 1

    if created:
        logger.info(f"Driver scan: created {created} assignment/coverage tasks")
    return created


def _scan_unpaid_reservations():
    """
    Create payment_chase tasks for confirmed reservations with upcoming legs
    that are not fully paid.
    """
    from reservations.models import Reservation
    from payment.models import Payment
    from django.db.models import Sum, F
    from decimal import Decimal

    now = timezone.now()
    today = timezone.localdate()
    horizon = today + timedelta(days=7)

    # Get confirmed reservations with legs in the next 7 days
    reservations = (
        Reservation.objects.filter(
            status="confirmed",
            legs__pickup_date__range=[today, horizon],
        )
        .exclude(status="cancelled")
        .distinct()
        .select_related("customer")
        .prefetch_related("payments", "legs")
    )

    created = 0
    for res in reservations:
        # Use the model's cached payment properties
        if res.payment_status == "paid":
            continue

        amount_owed = res.amount_owed
        if amount_owed <= Decimal("0.01"):
            continue

        # Find earliest upcoming leg
        earliest_leg = (
            res.legs.filter(pickup_date__gte=today)
            .exclude(status__in=["completed", "cancelled"])
            .order_by("pickup_date", "pickup_time")
            .first()
        )
        if not earliest_leg:
            continue

        days_until = (earliest_leg.pickup_date - today).days
        if days_until <= 2:
            priority = OperationalTask.Priority.CRITICAL
        elif days_until <= 5:
            priority = OperationalTask.Priority.HIGH
        else:
            priority = OperationalTask.Priority.MEDIUM

        customer_name = res.customer.get_full_name()
        task = create_task(
            task_type=OperationalTask.TaskType.PAYMENT_CHASE,
            title=f"Unpaid ${amount_owed}: {customer_name} — trip {earliest_leg.pickup_date:%b %d}",
            due_at=now,
            priority=priority,
            description=f"Total: ${res.total_price}, Paid: ${res.total_paid}, Owed: ${amount_owed}",
            reservation=res,
            escalate_at=timezone.make_aware(
                timezone.datetime.combine(
                    earliest_leg.pickup_date - timedelta(days=1),
                    timezone.datetime.min.time().replace(hour=9),
                )
            ) if days_until > 1 else now,
            metadata={
                "amount_owed": str(amount_owed),
                "total_price": str(res.total_price),
                "earliest_pickup": str(earliest_leg.pickup_date),
                "days_until_pickup": days_until,
            },
        )
        if task:
            created += 1

    if created:
        logger.info(f"Payment scan: created {created} payment_chase tasks")
    return created


def _auto_close_resolved_tasks():
    """
    Close tasks whose triggering condition has resolved.
    Runs every 30 minutes to catch state changes not covered by signals.
    """
    from reservations.models import Leg

    closed = 0

    # 1. Flight verify tasks where mismatch no longer exists
    flight_tasks = OperationalTask.objects.filter(
        task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
        status__in=list(OperationalTask.OPEN_STATUSES),
        leg__isnull=False,
    ).select_related("leg", "leg__flight_information")

    for task in flight_tasks:
        if not task.leg.has_flight_time_mismatch(threshold_minutes=30):
            close_task(task, resolution_notes="Auto-closed: flight mismatch resolved")
            closed += 1

    # 2. Guest confirm tasks where confirmation was sent
    confirm_tasks = OperationalTask.objects.filter(
        task_type=OperationalTask.TaskType.GUEST_CONFIRMATION,
        status__in=list(OperationalTask.OPEN_STATUSES),
        leg__isnull=False,
    ).select_related("leg")

    for task in confirm_tasks:
        if task.leg.confirmation_sms_sent_at:
            close_task(task, resolution_notes="Auto-closed: confirmation SMS sent")
            closed += 1

    # 3. Driver assign / coverage gap tasks where driver was assigned
    driver_tasks = OperationalTask.objects.filter(
        task_type__in=[
            OperationalTask.TaskType.DRIVER_ASSIGNMENT,
            OperationalTask.TaskType.COVERAGE_GAP,
        ],
        status__in=list(OperationalTask.OPEN_STATUSES),
        leg__isnull=False,
    ).select_related("leg")

    for task in driver_tasks:
        if task.leg.driver_id:
            close_task(task, resolution_notes="Auto-closed: driver assigned")
            closed += 1

    # 4. Cancel tasks linked to cancelled reservations
    cancelled_res_tasks = OperationalTask.objects.filter(
        status__in=list(OperationalTask.OPEN_STATUSES),
        reservation__status="cancelled",
    )
    for task in cancelled_res_tasks:
        task.status = OperationalTask.Status.CANCELLED
        task.resolved_at = timezone.now()
        task.resolution_notes = "Auto-cancelled: reservation cancelled"
        task.save(update_fields=["status", "resolved_at", "resolution_notes", "updated_at"])
        closed += 1

    if closed:
        logger.info(f"Auto-close scan: closed {closed} tasks")
    return closed


def _reopen_snoozed_tasks():
    """
    Re-open snoozed tasks whose snooze period has expired.
    """
    now = timezone.now()
    snoozed = OperationalTask.objects.filter(
        status=OperationalTask.Status.SNOOZED,
        snoozed_until__lte=now,
    )

    reopened = 0
    for task in snoozed:
        task.status = OperationalTask.Status.PENDING
        task.snoozed_until = None
        task.save(update_fields=["status", "snoozed_until", "updated_at"])
        reopened += 1

    if reopened:
        logger.info(f"Snooze scan: reopened {reopened} tasks")
    return reopened
