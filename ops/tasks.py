"""
Scheduler-based task scanners for automatic ops task generation, escalation, and cleanup.

Called from ghl_integration/scheduler.py every 30 minutes.
Each _scan_* function queries for conditions that warrant a task, deduplicates
against existing open tasks, and bulk-creates new ones.
"""

import logging
from datetime import datetime, timedelta
from django.utils import timezone
from django.db.models import Q, Exists, OuterRef

from .models import OperationalTask
from .services import create_task, close_task

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

# Minutes after flight gate arrival before passenger is realistically ready.
# Prevents false driver conflicts for back-to-back airport pickups.
AIRPORT_ARRIVAL_GRACE_MINUTES = 15

# Default estimated trip duration when no RouteTimingMetric data exists.
FALLBACK_TRIP_DURATION_MINUTES = 75

# Mismatch severity thresholds (minutes)
MINOR_THRESHOLD = 30
MODERATE_THRESHOLD = 60
MAJOR_THRESHOLD = 120

# Priority matrix: (severity_tier, days_until_bucket) → Priority
# severity_tier: "minor" (30-60min), "moderate" (60-120min), "major" (120+min)
# days_bucket: "imminent" (1-2d), "soon" (3-5d), "distant" (6-7d)
# Note: same-day (0d) is handled separately as driver conflict → always CRITICAL.
# CRITICAL is reserved for same-day only. Future tasks max out at HIGH.
_PRIORITY_MATRIX = {
    ("minor", "imminent"): OperationalTask.Priority.MEDIUM,
    ("minor", "soon"): OperationalTask.Priority.LOW,
    ("minor", "distant"): OperationalTask.Priority.LOW,
    ("moderate", "imminent"): OperationalTask.Priority.HIGH,
    ("moderate", "soon"): OperationalTask.Priority.MEDIUM,
    ("moderate", "distant"): OperationalTask.Priority.LOW,
    ("major", "imminent"): OperationalTask.Priority.HIGH,
    ("major", "soon"): OperationalTask.Priority.MEDIUM,
    ("major", "distant"): OperationalTask.Priority.LOW,
}

# Due-date offsets per priority (how long staff has to address)
_DUE_DELAYS = {
    OperationalTask.Priority.CRITICAL: timedelta(hours=0),
    OperationalTask.Priority.HIGH: timedelta(hours=4),
    OperationalTask.Priority.MEDIUM: timedelta(hours=8),
    OperationalTask.Priority.LOW: timedelta(hours=24),
}

# Escalation delays per priority level
_ESCALATION_DELAYS = {
    OperationalTask.Priority.CRITICAL: timedelta(hours=0),
    OperationalTask.Priority.HIGH: timedelta(hours=4),
    OperationalTask.Priority.MEDIUM: timedelta(hours=8),
    OperationalTask.Priority.LOW: timedelta(hours=24),
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_severity_tier(mismatch_minutes):
    """Classify mismatch into minor/moderate/major."""
    if mismatch_minutes >= MAJOR_THRESHOLD:
        return "major"
    elif mismatch_minutes >= MODERATE_THRESHOLD:
        return "moderate"
    return "minor"


def _get_days_bucket(days_until):
    """Classify days-until-pickup into imminent/soon/distant."""
    if days_until <= 2:
        return "imminent"
    elif days_until <= 5:
        return "soon"
    return "distant"


def _get_flight_priority(mismatch_minutes, days_until):
    """Look up priority from the severity × proximity matrix."""
    severity = _get_severity_tier(mismatch_minutes)
    bucket = _get_days_bucket(days_until)
    return _PRIORITY_MATRIX.get((severity, bucket), OperationalTask.Priority.MEDIUM)


def _estimate_leg_end_time(leg, target_date):
    """
    Estimate when a driver finishes a leg. Reuses the existing scheduling engine.
    Falls back to pickup_time + FALLBACK_TRIP_DURATION_MINUTES if the scheduler
    function is unavailable.
    """
    try:
        from dispatching.scheduler import estimate_job_end_time
        return estimate_job_end_time(leg, target_date)
    except Exception:
        pickup_dt = datetime.combine(target_date, leg.pickup_time)
        return pickup_dt + timedelta(minutes=FALLBACK_TRIP_DURATION_MINUTES)


def _get_effective_ready_time(leg, target_date):
    """
    For airport arrival legs: flight_arrival + AIRPORT_ARRIVAL_GRACE_MINUTES
    (passenger needs time to deplane, walk to pickup).
    For all other legs: just the pickup_time.
    """
    trip_type = leg.get_trip_type()

    if trip_type == "arrival" and leg.flight_information:
        try:
            from dispatching.scheduler import _get_best_flight_arrival
            flight_dt = _get_best_flight_arrival(leg)
            if flight_dt:
                # Normalize to target_date
                flight_dt = datetime.combine(target_date, flight_dt.time())
                return flight_dt + timedelta(minutes=AIRPORT_ARRIVAL_GRACE_MINUTES)
        except Exception:
            pass

    return datetime.combine(target_date, leg.pickup_time)


def detect_driver_conflicts(leg, target_date):
    """
    For a given leg with a shifted flight time, check if the assigned in-house
    driver has any conflicting legs on the same day.

    Returns a list of conflict dicts, each containing:
      - conflicting_leg: the Leg object that conflicts
      - driver_clears_at: datetime when driver finishes the prior leg
      - effective_ready: datetime when passenger is ready for the checked leg
      - conflict_minutes: how many minutes late the driver would be

    Only evaluates in-house drivers. Returns empty list for affiliates or
    unassigned legs.
    """
    from reservations.models import Leg

    if not leg.driver_id:
        return []

    driver = leg.driver
    # Skip affiliates
    if getattr(driver, "driver_type", "affiliate") != "inhouse":
        return []

    # Get all other same-day legs for this driver
    other_legs = (
        Leg.objects.filter(
            driver=driver,
            pickup_date=target_date,
        )
        .exclude(pk=leg.pk)
        .exclude(status__in=["completed", "cancelled"])
        .exclude(reservation__status="cancelled")
        .select_related("flight_information", "reservation", "reservation__customer")
        .order_by("pickup_time")
    )

    if not other_legs.exists():
        return []

    # The effective ready time for THIS leg (the one with the shifted flight)
    this_ready_time = _get_effective_ready_time(leg, target_date)
    this_end_time = _estimate_leg_end_time(leg, target_date)

    conflicts = []

    def _travel_minutes(from_location, to_location):
        """Get travel time between two locations (live traffic → historical → 0)."""
        if not from_location or not to_location:
            return 0
        try:
            from drivers.utils import get_drive_time as google_drive_time
            live = google_drive_time(from_location, to_location)
            if live:
                return round(live["duration_seconds"] / 60)
        except Exception:
            pass
        try:
            from dispatching.analytics import categorize_location
            from dispatching.scheduler import get_drive_time as sched_drive
            fc = categorize_location(from_location)
            tc = categorize_location(to_location)
            return sched_drive(fc, tc, None, None)
        except Exception:
            return 0

    for other in other_legs:
        other_ready_time = _get_effective_ready_time(other, target_date)
        other_end_time = _estimate_leg_end_time(other, target_date)

        # Check two directions, including travel time between legs:
        # 1. Does THIS leg's shifted time cause the driver to be late for OTHER leg?
        #    Driver finishes this leg, then travels from this dropoff → other pickup
        if this_ready_time < other_ready_time:
            travel = _travel_minutes(leg.dropoff_location, other.pickup_location)
            driver_arrives = this_end_time + timedelta(minutes=travel)
            conflict_mins = int((driver_arrives - other_ready_time).total_seconds() / 60)
            if conflict_mins > 0:
                conflicts.append({
                    "conflicting_leg": other,
                    "driver_clears_at": this_end_time,
                    "effective_ready": other_ready_time,
                    "conflict_minutes": conflict_mins,
                    "direction": "this_delays_other",
                })

        # 2. Does OTHER leg cause driver to be late for THIS shifted leg?
        #    Driver finishes other leg, then travels from other dropoff → this pickup
        if other_ready_time < this_ready_time:
            travel = _travel_minutes(other.dropoff_location, leg.pickup_location)
            driver_arrives = other_end_time + timedelta(minutes=travel)
            conflict_mins = int((driver_arrives - this_ready_time).total_seconds() / 60)
            if conflict_mins > 0:
                conflicts.append({
                    "conflicting_leg": other,
                    "driver_clears_at": other_end_time,
                    "effective_ready": this_ready_time,
                    "conflict_minutes": conflict_mins,
                    "direction": "other_delays_this",
                })

    return conflicts


# ── Main entry point ─────────────────────────────────────────────────────────

def generate_ops_tasks():
    """
    Main entry point called by the scheduler every 30 minutes.
    Returns a summary dict of actions taken.
    """
    created = 0
    closed = 0
    reopened = 0

    created += _scan_flight_mismatches()
    created += _scan_driver_overlaps()
    created += _scan_unassigned_legs()
    created += _scan_unpaid_reservations()
    created += _scan_uncontacted_forms()
    created += _scan_confirmation_texts()
    closed += _auto_close_resolved_tasks()
    reopened += _reopen_snoozed_tasks()

    # Send automated unpaid-reservation reminder emails and flag T-2h cases
    # for manual cancellation review. Runs after _scan_unpaid_reservations so
    # the PAYMENT_CHASE task exists when the engine attaches CommunicationAttempt
    # rows to it.
    _process_unpaid_reminders()

    # Auto-escalation disabled — staff are responsible for their own tasks.
    # Escalation engine (ops/escalation.py) still exists if re-enabled later.

    return {"created": created, "closed": closed, "reopened": reopened, "escalated": 0}


def _process_unpaid_reminders():
    """Run the automated unpaid-reservation reminder pipeline."""
    try:
        from ops.unpaid_reminders import UnpaidReminderEngine
        UnpaidReminderEngine().process()
    except Exception as exc:
        logger.error(f"Unpaid reminder engine error: {exc}", exc_info=True)


# ── Flight mismatch scanner ─────────────────────────────────────────────────

def _scan_flight_mismatches():
    """
    Scan arrival legs for the next 7 days for flight time mismatches.

    Same-day legs: check for real driver conflicts (in-house only).
      - True conflict → CRITICAL driver_conflict task
      - No conflict → skip (minor shifts are normal, just "Match Flight Time")
      - Unassigned leg → skip conflict check (no driver to conflict with)

    Future legs (1-7 days): create flight_verify tasks with tiered priority
    based on mismatch severity × days until pickup.
    """
    from reservations.models import Leg

    now = timezone.now()
    today = timezone.localdate()
    horizon = today + timedelta(days=7)

    legs = (
        Leg.objects.filter(
            pickup_date__range=[today, horizon],
            flight_information__isnull=False,
        )
        .exclude(status__in=["completed", "cancelled"])
        .exclude(reservation__status="cancelled")
        .select_related(
            "flight_information", "reservation", "reservation__customer",
            "driver",
        )
    )

    local_now_time = timezone.localtime(now).time()

    created = 0
    for leg in legs:
        if leg.get_trip_type() != "arrival":
            continue

        # Skip same-day legs whose pickup time has already passed
        if leg.pickup_date == today and leg.pickup_time < local_now_time:
            continue

        if not leg.has_flight_time_mismatch(threshold_minutes=MINOR_THRESHOLD):
            continue

        mismatch = leg.get_flight_time_mismatch_display()
        if not mismatch:
            continue

        days_until = (leg.pickup_date - today).days
        is_same_day = (days_until == 0)

        customer_name = leg.reservation.customer.get_full_name()
        flight = leg.flight_information
        flight_label = (
            f"{flight.airline_display_name or flight.airline or ''} "
            f"{flight.flight_number or ''}"
        ).strip()

        if is_same_day:
            # Same-day: create driver conflict if real overlap exists
            conflict_created = _handle_same_day_mismatch(
                leg, mismatch, customer_name, flight_label, now
            )
            if conflict_created:
                created += conflict_created
            else:
                # No driver conflict, but flight still shifted — create
                # a flight_verify task so dispatch knows about the change.
                created += _handle_future_mismatch(
                    leg, mismatch, customer_name, flight_label, days_until=0, now=now
                )
        else:
            # Future: tiered priority guest-verification task
            created += _handle_future_mismatch(
                leg, mismatch, customer_name, flight_label, days_until, now
            )

    if created:
        logger.info(f"Flight scan: created {created} flight tasks")
    return created


def _handle_same_day_mismatch(leg, mismatch, customer_name, flight_label, now):
    """
    Same-day flight shift: check for real driver conflicts.
    Only flags in-house drivers. Returns 1 if task created, 0 otherwise.
    """
    today = timezone.localdate()

    # No driver assigned — nothing to conflict with
    if not leg.driver_id:
        return 0

    # Skip affiliates
    driver = leg.driver
    if getattr(driver, "driver_type", "affiliate") != "inhouse":
        return 0

    conflicts = detect_driver_conflicts(leg, today)
    if not conflicts:
        return 0

    # Use the worst conflict for the task description
    worst = max(conflicts, key=lambda c: c["conflict_minutes"])
    conflicting = worst["conflicting_leg"]
    driver_name = str(driver)

    title = f"Driver Conflict — {driver_name}"
    clears_str = worst["driver_clears_at"].strftime("%I:%M %p").lstrip("0")

    description = (
        f"Flight {mismatch['label']}. "
        f"Driver will be {worst['conflict_minutes']} min late — reassign or adjust times."
    )

    task = create_task(
        task_type=OperationalTask.TaskType.DRIVER_CONFLICT,
        title=title,
        due_at=now,
        priority=OperationalTask.Priority.CRITICAL,
        description=description,
        leg=leg,
        reservation=leg.reservation,
        escalate_at=now,  # Immediate escalation for same-day conflicts
        metadata={
            "driver_id": driver.id,
            "driver_name": driver_name,
            "flight_ident": flight_label,
            "mismatch_direction": mismatch["direction"],
            "mismatch_minutes": mismatch["minutes"],
            "mismatch_label": mismatch["label"],
            "conflict_minutes": worst["conflict_minutes"],
            "conflicting_leg_id": conflicting.id,
            "conflicting_pickup_time": str(conflicting.pickup_time),
            "driver_clears_at": clears_str,
            "pickup_date": str(leg.pickup_date),
            "pickup_time": str(leg.pickup_time),
        },
    )
    return 1 if task else 0


def _handle_future_mismatch(leg, mismatch, customer_name, flight_label, days_until, now):
    """
    Future flight mismatch (1-7 days out): create a flight_verify task
    with priority based on severity × proximity matrix.
    Returns 1 if task created, 0 otherwise.
    """
    priority = _get_flight_priority(mismatch["minutes"], days_until)
    due_delay = _DUE_DELAYS.get(priority, timedelta(hours=8))
    escalate_delay = _ESCALATION_DELAYS.get(priority, timedelta(hours=8))
    severity = _get_severity_tier(mismatch["minutes"])

    task = create_task(
        task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
        title=f"Flight mismatch: {customer_name} — {flight_label} {mismatch['label']}",
        due_at=now + due_delay,
        priority=priority,
        description=(
            f"Booked pickup {leg.pickup_time:%I:%M %p} — flight {mismatch['label']}."
        ),
        leg=leg,
        reservation=leg.reservation,
        escalate_at=now + escalate_delay,
        metadata={
            "mismatch_direction": mismatch["direction"],
            "mismatch_minutes": mismatch["minutes"],
            "mismatch_label": mismatch["label"],
            "severity_tier": severity,
            "days_until_pickup": days_until,
            "flight_ident": flight_label,
            "pickup_date": str(leg.pickup_date),
            "pickup_time": str(leg.pickup_time),
        },
    )
    return 1 if task else 0


# ── Other scanners (unchanged) ──────────────────────────────────────────────

def _scan_uncontacted_forms():
    """
    Create contact_form tasks for Contact Us submissions still in 'pending' status.
    """
    from users.models import ContactUsForm

    now = timezone.now()

    pending_forms = ContactUsForm.objects.filter(status="pending")

    created = 0
    for form in pending_forms:
        name = f"{form.first_name} {form.last_name}".strip()
        task = create_task(
            task_type=OperationalTask.TaskType.CONTACT_FORM,
            title=f"Contact form: {name}",
            due_at=now + _DUE_DELAYS[OperationalTask.Priority.HIGH],
            priority=OperationalTask.Priority.HIGH,
            description=form.about[:200] if form.about else "",
            contact_form=form,
            escalate_at=now + timedelta(hours=4),
            metadata={
                "email": form.email or "",
                "phone": form.phone_number or "",
                "contact_method": form.contact_method or "",
            },
        )
        if task:
            created += 1

    if created:
        logger.info(f"Contact form scan: created {created} contact_form tasks")
    return created


def _scan_driver_overlaps():
    """
    Scan today's legs for in-house driver scheduling overlaps, independent
    of flight changes. Catches cases like two legs assigned to the same
    driver where the first leg's estimated end time overlaps the second
    leg's effective ready time.

    Only checks same-day, in-house drivers. Skips legs that already have
    an open driver_conflict task (deduplication handled by create_task).
    Skips legs whose pickup time has already passed (no actionable conflict).
    """
    from reservations.models import Leg
    from itertools import groupby

    now = timezone.now()
    today = timezone.localdate()
    local_now_time = timezone.localtime(now).time()

    # All today's active legs with an in-house driver, ordered by driver then time
    # Skip legs whose pickup time has already passed — no point alerting on past events
    legs = list(
        Leg.objects.filter(
            pickup_date=today,
            driver__isnull=False,
            driver__driver_type="inhouse",
            pickup_time__gte=local_now_time,
        )
        .exclude(status__in=["completed", "cancelled"])
        .exclude(reservation__status="cancelled")
        .select_related(
            "driver", "driver__profile",
            "flight_information",
            "reservation", "reservation__customer",
        )
        .order_by("driver_id", "pickup_time")
    )

    # Pre-fetch legs that already have open driver_conflict tasks to avoid duplicates
    # with the flight mismatch scanner (which may have already created a task
    # for the same conflict from the flight's perspective).
    legs_with_open_conflict = set(
        OperationalTask.objects.filter(
            task_type=OperationalTask.TaskType.DRIVER_CONFLICT,
            status__in=list(OperationalTask.OPEN_STATUSES),
            leg__isnull=False,
        ).values_list("leg_id", flat=True)
    )

    created = 0
    for driver_id, driver_legs in groupby(legs, key=lambda l: l.driver_id):
        driver_legs = list(driver_legs)
        if len(driver_legs) < 2:
            continue

        # Compare each consecutive pair
        for i in range(len(driver_legs) - 1):
            leg_a = driver_legs[i]
            leg_b = driver_legs[i + 1]

            # Skip if either leg already has an open driver_conflict task
            if leg_a.pk in legs_with_open_conflict or leg_b.pk in legs_with_open_conflict:
                continue

            end_a = _estimate_leg_end_time(leg_a, today)
            ready_b = _get_effective_ready_time(leg_b, today)

            # Include travel time from leg_a dropoff to leg_b pickup
            travel_mins = 0
            if leg_a.dropoff_location and leg_b.pickup_location:
                try:
                    from drivers.utils import get_drive_time as google_drive_time
                    live = google_drive_time(leg_a.dropoff_location, leg_b.pickup_location)
                    if live:
                        travel_mins = round(live["duration_seconds"] / 60)
                except Exception:
                    pass
                if not travel_mins:
                    try:
                        from dispatching.analytics import categorize_location
                        from dispatching.scheduler import get_drive_time as sched_drive
                        fc = categorize_location(leg_a.dropoff_location)
                        tc = categorize_location(leg_b.pickup_location)
                        travel_mins = sched_drive(fc, tc, None, None)
                    except Exception:
                        pass

            driver_arrives_b = end_a + timedelta(minutes=travel_mins)

            if driver_arrives_b <= ready_b:
                continue  # No overlap

            conflict_minutes = int((driver_arrives_b - ready_b).total_seconds() / 60)
            if conflict_minutes <= 0:
                continue

            driver = leg_a.driver
            driver_name = str(driver)

            # Use leg_b as the "affected" leg (the one the driver will be late to)
            pickup_str_a = leg_a.pickup_time.strftime("%I:%M %p").lstrip("0")
            pickup_str_b = leg_b.pickup_time.strftime("%I:%M %p").lstrip("0")
            clears_str = end_a.strftime("%I:%M %p").lstrip("0")

            customer_a = leg_a.reservation.customer.get_full_name() if leg_a.reservation else "Unknown"
            customer_b = leg_b.reservation.customer.get_full_name() if leg_b.reservation else "Unknown"

            title = f"Driver Conflict — {driver_name}"
            description = (
                f"{pickup_str_a} and {pickup_str_b} legs conflict — "
                f"driver will be {conflict_minutes} min late. Reassign or adjust times."
            )

            # Flight label if either leg has one
            flight_label = ""
            for check_leg in (leg_a, leg_b):
                if check_leg.flight_information:
                    fi = check_leg.flight_information
                    flight_label = f"{fi.airline_display_name or fi.airline or ''} {fi.flight_number or ''}".strip()
                    break

            task = create_task(
                task_type=OperationalTask.TaskType.DRIVER_CONFLICT,
                title=title,
                due_at=now,
                priority=OperationalTask.Priority.CRITICAL,
                description=description,
                leg=leg_b,  # The leg the driver will be late to
                reservation=leg_b.reservation,
                escalate_at=now,
                metadata={
                    "driver_id": driver.id,
                    "driver_name": driver_name,
                    "flight_ident": flight_label,
                    "mismatch_direction": "overlap",
                    "mismatch_minutes": conflict_minutes,
                    "mismatch_label": f"{conflict_minutes} min late",
                    "conflict_minutes": conflict_minutes,
                    "conflicting_leg_id": leg_a.id,
                    "conflicting_pickup_time": str(leg_a.pickup_time),
                    "driver_clears_at": clears_str,
                    "pickup_date": str(today),
                    "pickup_time": str(leg_b.pickup_time),
                },
            )
            if task:
                created += 1

    if created:
        logger.info(f"Driver overlap scan: created {created} driver_conflict tasks")
    return created


def _scan_unassigned_legs():
    """
    Create driver_assign tasks for TODAY's legs without a driver.
    Only today — upcoming days are normal dispatch scheduling, not ops tasks.
    Skips legs whose pickup time has already passed.
    """
    from reservations.models import Leg

    now = timezone.now()
    today = timezone.localdate()
    local_now_time = timezone.localtime(now).time()

    legs = (
        Leg.objects.filter(
            pickup_date=today,
            driver__isnull=True,
            pickup_time__gte=local_now_time,
        )
        .exclude(status__in=["completed", "cancelled"])
        .exclude(reservation__status="cancelled")
        .select_related("reservation", "reservation__customer")
    )

    created = 0
    for leg in legs:
        customer_name = leg.reservation.customer.get_full_name()
        task = create_task(
            task_type=OperationalTask.TaskType.DRIVER_ASSIGNMENT,
            title=f"No driver: {customer_name} — {leg.pickup_date:%b %d} {leg.pickup_time:%I:%M %p}",
            due_at=now,
            priority=OperationalTask.Priority.CRITICAL,
            description=f"{leg.pickup_location} → {leg.dropoff_location}",
            leg=leg,
            reservation=leg.reservation,
            escalate_at=now,
            metadata={
                "pickup_date": str(leg.pickup_date),
                "pickup_time": str(leg.pickup_time),
            },
        )
        if task:
            created += 1

    if created:
        logger.info(f"Driver scan: created {created} driver_assign tasks (today only)")
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
        # Skip paid and card_saved — saved cards can be charged anytime, not unpaid
        if res.payment_status in ("paid", "card_saved"):
            continue

        amount_owed = res.amount_owed
        if amount_owed <= Decimal("0.01"):
            continue

        # Find earliest upcoming leg (skip same-day legs whose pickup time passed)
        local_now_time = timezone.localtime(now).time()
        upcoming_legs = (
            res.legs.filter(pickup_date__gte=today)
            .exclude(status__in=["completed", "cancelled"])
            .order_by("pickup_date", "pickup_time")
        )
        earliest_leg = None
        for candidate in upcoming_legs:
            if candidate.pickup_date == today and candidate.pickup_time < local_now_time:
                continue  # pickup already passed today
            earliest_leg = candidate
            break
        if not earliest_leg:
            continue

        days_until = (earliest_leg.pickup_date - today).days
        if days_until == 0:
            priority = OperationalTask.Priority.CRITICAL
        elif days_until <= 2:
            priority = OperationalTask.Priority.HIGH
        elif days_until <= 5:
            priority = OperationalTask.Priority.MEDIUM
        else:
            priority = OperationalTask.Priority.LOW

        customer_name = res.customer.get_full_name()
        due_delay = _DUE_DELAYS.get(priority, timedelta(hours=8))
        task = create_task(
            task_type=OperationalTask.TaskType.PAYMENT_CHASE,
            title=f"Unpaid ${amount_owed}: {customer_name} — trip {earliest_leg.pickup_date:%b %d}",
            due_at=now + due_delay,
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


# ── Confirmation texts scanner ──────────────────────────────────────────────

# Earliest local hour at which the daily confirmation-texts task should appear.
# Confirmations go out the day before — surface the task in the morning so
# staff can verify all of tomorrow's flights first, then send the batch.
CONFIRMATION_TEXTS_OPEN_HOUR = 9   # 9 AM local

# Local hour by which the batch should be done (used for due_at + escalate_at).
CONFIRMATION_TEXTS_DUE_HOUR = 17    # 5 PM local
CONFIRMATION_TEXTS_ESCALATE_HOUR = 19  # 7 PM local


def _scan_confirmation_texts():
    """
    Create one batch task per day reminding staff to send tomorrow's
    confirmation SMS messages. Only one task per target_date is allowed.

    Skipped before CONFIRMATION_TEXTS_OPEN_HOUR (so it doesn't show up at 6 AM
    when the rest of the day's flight verification is still in progress).
    Auto-closes when every leg for that date has confirmation_sms_sent_at set
    (or zero legs need a confirmation).
    """
    from reservations.models import Leg

    now = timezone.now()
    local_now = timezone.localtime(now)
    if local_now.hour < CONFIRMATION_TEXTS_OPEN_HOUR:
        return 0

    target_date = local_now.date() + timedelta(days=1)

    # Count legs that still need a confirmation text for tomorrow
    unsent_qs = (
        Leg.objects.filter(
            pickup_date=target_date,
            confirmation_sms_sent_at__isnull=True,
        )
        .exclude(status="cancelled")
        .exclude(reservation__status="cancelled")
    )
    unsent = unsent_qs.count()
    if unsent == 0:
        return 0

    # Manual dedup: this task type doesn't link to a leg/reservation, so
    # create_task() can't dedup it for us. Match on metadata.target_date.
    existing = OperationalTask.objects.filter(
        task_type=OperationalTask.TaskType.CONFIRMATION_TEXTS,
        status__in=list(OperationalTask.OPEN_STATUSES),
        metadata__target_date=str(target_date),
    ).first()
    if existing:
        # Refresh the unsent count on the existing task so the title stays accurate
        meta = existing.metadata or {}
        if meta.get("unsent_count") != unsent:
            meta["unsent_count"] = unsent
            existing.metadata = meta
            existing.title = f"Send {unsent} confirmation text{'s' if unsent != 1 else ''} for {target_date:%b %d}"
            existing.save(update_fields=["metadata", "title", "updated_at"])
        return 0

    # Cooldown: don't recreate within 2h of a recent close/cancel for the same date
    recent_closed = OperationalTask.objects.filter(
        task_type=OperationalTask.TaskType.CONFIRMATION_TEXTS,
        status__in=[OperationalTask.Status.COMPLETED, OperationalTask.Status.CANCELLED],
        metadata__target_date=str(target_date),
        resolved_at__gte=now - timedelta(hours=2),
    ).exists()
    if recent_closed:
        return 0

    # due/escalate are in local time, then made aware
    tz_local = timezone.get_current_timezone()
    due_local = datetime.combine(
        local_now.date(),
        datetime.min.time().replace(hour=CONFIRMATION_TEXTS_DUE_HOUR),
    )
    escalate_local = datetime.combine(
        local_now.date(),
        datetime.min.time().replace(hour=CONFIRMATION_TEXTS_ESCALATE_HOUR),
    )
    due_at = timezone.make_aware(due_local, tz_local)
    escalate_at = timezone.make_aware(escalate_local, tz_local)

    title = f"Send {unsent} confirmation text{'s' if unsent != 1 else ''} for {target_date:%b %d}"
    description = (
        f"Step 1: Verify every flight for {target_date:%b %d} is matched and "
        "the schedule is clean. Resolve any open Flight Verification tasks "
        f"for that day first.\nStep 2: Send the confirmation SMS batch from "
        "the Confirmations page."
    )

    task = create_task(
        task_type=OperationalTask.TaskType.CONFIRMATION_TEXTS,
        title=title,
        due_at=due_at,
        priority=OperationalTask.Priority.HIGH,
        description=description,
        escalate_at=escalate_at,
        metadata={
            "target_date": str(target_date),
            "unsent_count": unsent,
        },
    )
    if task:
        logger.info(f"Confirmation scan: created task for {target_date} ({unsent} unsent)")
        return 1
    return 0


def _auto_close_resolved_tasks():
    """
    Close tasks whose triggering condition has resolved.
    Runs every 30 minutes to catch state changes not covered by signals.

    Each section is wrapped in try/except so a failure in one section
    doesn't prevent other sections from running.
    """
    from reservations.models import Leg

    closed = 0
    now = timezone.now()
    today = timezone.localdate()
    local_now_time = timezone.localtime(now).time()

    # ── 0. Bulk-close ALL stale tasks whose leg's pickup date has passed ─────
    # This is the catch-all: any open task linked to a leg from a past date
    # is no longer actionable. Handles every task type in one query.
    try:
        stale_leg_tasks = OperationalTask.objects.filter(
            status__in=list(OperationalTask.OPEN_STATUSES),
            leg__isnull=False,
            leg__pickup_date__lt=today,
        )
        for task in stale_leg_tasks:
            try:
                close_task(task, resolution_notes="Auto-closed: pickup date has passed")
                closed += 1
            except Exception as e:
                logger.error(f"Error closing stale task #{task.id}: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Auto-close stale leg tasks error: {e}", exc_info=True)

    # ── 0b. Close tasks whose leg is completed or cancelled ──────────────────
    try:
        done_leg_tasks = OperationalTask.objects.filter(
            status__in=list(OperationalTask.OPEN_STATUSES),
            leg__isnull=False,
            leg__status__in=["completed", "cancelled"],
        )
        for task in done_leg_tasks:
            try:
                close_task(task, resolution_notes=f"Auto-closed: leg {task.leg.status}")
                closed += 1
            except Exception as e:
                logger.error(f"Error closing done-leg task #{task.id}: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Auto-close done-leg tasks error: {e}", exc_info=True)

    # ── 1. Flight verify tasks where mismatch no longer exists ───────────────
    try:
        flight_tasks = OperationalTask.objects.filter(
            task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
            status__in=list(OperationalTask.OPEN_STATUSES),
            leg__isnull=False,
        ).select_related("leg", "leg__flight_information")

        for task in flight_tasks:
            try:
                if not task.leg.has_flight_time_mismatch(threshold_minutes=MINOR_THRESHOLD):
                    close_task(task, resolution_notes="Auto-closed: flight mismatch resolved")
                    closed += 1
            except Exception as e:
                logger.error(f"Error checking flight task #{task.id}: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Auto-close flight tasks error: {e}", exc_info=True)

    # ── 2. Driver conflict tasks where conflict no longer exists ─────────────
    try:
        conflict_tasks = list(
            OperationalTask.objects.filter(
                task_type=OperationalTask.TaskType.DRIVER_CONFLICT,
                status__in=list(OperationalTask.OPEN_STATUSES),
                leg__isnull=False,
            ).select_related("leg", "leg__driver", "leg__flight_information")
        )

        # Batch-fetch all conflicting legs referenced in task metadata
        conflicting_leg_ids = set()
        for task in conflict_tasks:
            cid = (task.metadata or {}).get("conflicting_leg_id")
            if cid:
                conflicting_leg_ids.add(cid)
        conflicting_legs_map = {}
        if conflicting_leg_ids:
            conflicting_legs_map = {
                lg.pk: lg for lg in Leg.objects.filter(pk__in=conflicting_leg_ids)
            }

        for task in conflict_tasks:
            try:
                leg = task.leg
                meta = task.metadata or {}
                is_pure_overlap = meta.get("mismatch_direction") == "overlap"
                conflicting_leg_id = meta.get("conflicting_leg_id")

                if not leg.driver_id:
                    close_task(task, resolution_notes="Auto-closed: driver unassigned")
                    closed += 1
                    continue

                # Auto-close if the pickup time is 3+ hours in the past (stale conflict)
                pickup_dt = datetime.combine(leg.pickup_date, leg.pickup_time)
                pickup_aware = timezone.make_aware(pickup_dt, timezone.get_current_timezone())
                if pickup_aware < now - timedelta(hours=3):
                    close_task(task, resolution_notes="Auto-closed: pickup time has passed")
                    closed += 1
                    continue

                # Check if the conflicting leg was reassigned to a different driver or cancelled
                if conflicting_leg_id:
                    other_leg = conflicting_legs_map.get(conflicting_leg_id)
                    if other_leg is None:
                        close_task(task, resolution_notes="Auto-closed: conflicting leg deleted")
                        closed += 1
                        continue
                    if other_leg.driver_id != leg.driver_id:
                        close_task(task, resolution_notes="Auto-closed: conflicting leg reassigned to different driver")
                        closed += 1
                        continue
                    if other_leg.status in ("completed", "cancelled"):
                        close_task(task, resolution_notes=f"Auto-closed: conflicting leg {other_leg.status}")
                        closed += 1
                        continue

                # Re-check if the schedule conflict still exists (regardless of flight mismatch status)
                if leg.driver_id:
                    conflicts = detect_driver_conflicts(leg, leg.pickup_date)
                    if not conflicts:
                        if not is_pure_overlap and not leg.has_flight_time_mismatch(threshold_minutes=MINOR_THRESHOLD):
                            close_task(task, resolution_notes="Auto-closed: flight matched and conflict resolved")
                        else:
                            close_task(task, resolution_notes="Auto-closed: driver conflict resolved")
                        closed += 1
            except Exception as e:
                logger.error(f"Error checking conflict task #{task.id}: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Auto-close conflict tasks error: {e}", exc_info=True)

    # ── 3. Contact form tasks where form was contacted/closed ────────────────
    try:
        contact_tasks = OperationalTask.objects.filter(
            task_type=OperationalTask.TaskType.CONTACT_FORM,
            status__in=list(OperationalTask.OPEN_STATUSES),
            contact_form__isnull=False,
        ).select_related("contact_form")

        for task in contact_tasks:
            try:
                if task.contact_form.status in ("contacted", "closed"):
                    close_task(task, resolution_notes=f"Auto-closed: form marked {task.contact_form.status}")
                    closed += 1
            except Exception as e:
                logger.error(f"Error checking contact task #{task.id}: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Auto-close contact tasks error: {e}", exc_info=True)

    # ── 4. Driver assign tasks where driver was assigned or pickup time has passed
    try:
        driver_tasks = OperationalTask.objects.filter(
            task_type=OperationalTask.TaskType.DRIVER_ASSIGNMENT,
            status__in=list(OperationalTask.OPEN_STATUSES),
            leg__isnull=False,
        ).select_related("leg")

        for task in driver_tasks:
            try:
                if task.leg.driver_id:
                    close_task(task, resolution_notes="Auto-closed: driver assigned")
                    closed += 1
                elif task.leg.pickup_date < today:
                    close_task(task, resolution_notes="Auto-closed: pickup date has passed")
                    closed += 1
                elif task.leg.pickup_date == today and task.leg.pickup_time < local_now_time:
                    close_task(task, resolution_notes="Auto-closed: pickup time has passed")
                    closed += 1
            except Exception as e:
                logger.error(f"Error checking driver task #{task.id}: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Auto-close driver tasks error: {e}", exc_info=True)

    # ── 5. Payment chase tasks where payment received or pickup passed ───────
    try:
        payment_tasks = OperationalTask.objects.filter(
            task_type=OperationalTask.TaskType.PAYMENT_CHASE,
            status__in=list(OperationalTask.OPEN_STATUSES),
            reservation__isnull=False,
        ).select_related("reservation")

        for task in payment_tasks:
            try:
                # Close if card has been saved — not truly unpaid
                if task.reservation.payment_status in ("paid", "card_saved"):
                    reason = "Auto-closed: card saved on file" if task.reservation.payment_status == "card_saved" else "Auto-closed: payment received"
                    close_task(task, resolution_notes=reason)
                    closed += 1
                    continue

                meta = task.metadata or {}
                earliest_pickup_str = meta.get("earliest_pickup")
                if earliest_pickup_str:
                    earliest_date = datetime.strptime(earliest_pickup_str, "%Y-%m-%d").date()
                    if earliest_date < today:
                        close_task(task, resolution_notes="Auto-closed: pickup date has passed")
                        closed += 1
            except Exception as e:
                logger.error(f"Error checking payment task #{task.id}: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Auto-close payment tasks error: {e}", exc_info=True)

    # ── 5b. Confirmation-texts batch tasks: close once everything is sent ───
    try:
        from reservations.models import Leg

        ct_tasks = OperationalTask.objects.filter(
            task_type=OperationalTask.TaskType.CONFIRMATION_TEXTS,
            status__in=list(OperationalTask.OPEN_STATUSES),
        )
        for task in ct_tasks:
            try:
                meta = task.metadata or {}
                target_str = meta.get("target_date")
                if not target_str:
                    continue
                target = datetime.strptime(target_str, "%Y-%m-%d").date()

                # Stale: target date already in the past
                if target < today:
                    close_task(task, resolution_notes="Auto-closed: confirmation date has passed")
                    closed += 1
                    continue

                unsent = (
                    Leg.objects.filter(
                        pickup_date=target,
                        confirmation_sms_sent_at__isnull=True,
                    )
                    .exclude(status="cancelled")
                    .exclude(reservation__status="cancelled")
                    .count()
                )
                if unsent == 0:
                    close_task(task, resolution_notes="Auto-closed: all confirmation texts sent")
                    closed += 1
                    continue

                # Keep title/metadata accurate while the task is still open
                if meta.get("unsent_count") != unsent:
                    meta["unsent_count"] = unsent
                    task.metadata = meta
                    task.title = f"Send {unsent} confirmation text{'s' if unsent != 1 else ''} for {target:%b %d}"
                    task.save(update_fields=["metadata", "title", "updated_at"])
            except Exception as e:
                logger.error(f"Error checking confirmation-texts task #{task.id}: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Auto-close confirmation-texts tasks error: {e}", exc_info=True)

    # ── 6. Cancel tasks linked to cancelled reservations ─────────────────────
    try:
        cancelled_res_tasks = OperationalTask.objects.filter(
            status__in=list(OperationalTask.OPEN_STATUSES),
            reservation__status="cancelled",
        )
        for task in cancelled_res_tasks:
            try:
                task.status = OperationalTask.Status.CANCELLED
                task.resolved_at = timezone.now()
                task.resolution_notes = "Auto-cancelled: reservation cancelled"
                task.save(update_fields=["status", "resolved_at", "resolution_notes", "updated_at"])
                closed += 1
            except Exception as e:
                logger.error(f"Error cancelling task #{task.id}: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Auto-close cancelled-res tasks error: {e}", exc_info=True)

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


# ── Auto-refresh flight data ────────────────────────────────────────────────

def auto_refresh_flights():
    """
    Auto-refresh arrival flight data from AeroAPI on a tiered schedule:
      - Today: every call (every 30 min via scheduler)
      - Next 2 days: every 4 hours (every 8th cycle)
      - Days 3-7: once per day (every 48th cycle)

    Only refreshes arrival legs with flight info. Skips legs with no flight ident.
    Returns summary dict with counts.
    """
    from reservations.models import Leg
    from dispatching.aeroapi_service import AeroAPIService

    today = timezone.localdate()
    refreshed = 0
    errors = 0

    # Determine which date ranges to refresh this cycle
    # The scheduler passes cycle_count so we can tier the refresh frequency
    date_ranges = _get_refresh_date_ranges(today)
    if not date_ranges:
        return {"refreshed": 0, "errors": 0}

    aeroapi = AeroAPIService()
    if not aeroapi.api_key:
        logger.warning("AeroAPI key not configured, skipping auto-refresh")
        return {"refreshed": 0, "errors": 0}

    legs = (
        Leg.objects.filter(
            pickup_date__in=date_ranges,
            flight_information__isnull=False,
        )
        .exclude(status__in=["completed", "cancelled"])
        .exclude(reservation__status="cancelled")
        .select_related("flight_information")
    )

    for leg in legs:
        if leg.get_trip_type() != "arrival":
            continue

        flight = leg.flight_information
        flight_ident = flight.get_flight_ident()
        if not flight_ident:
            continue

        try:
            flight_date = leg.pickup_date.strftime("%Y-%m-%d")
            trip_type = leg.get_trip_type()
            flight_data = aeroapi.get_flight_data(
                flight_ident, flight_date=flight_date, trip_type=trip_type
            )

            if flight_data.get("status") == "success":
                _apply_flight_update(flight, flight_data)
                refreshed += 1
            else:
                errors += 1
                logger.debug(
                    f"Auto-refresh skip: {flight_ident} leg {leg.id} — "
                    f"{flight_data.get('error', 'unknown')}"
                )
        except Exception as e:
            errors += 1
            logger.error(f"Auto-refresh error for leg {leg.id}: {e}", exc_info=True)

    if refreshed:
        logger.info(f"Auto-refresh: updated {refreshed} flights, {errors} errors")
    return {"refreshed": refreshed, "errors": errors}


def _get_refresh_date_ranges(today):
    """
    Return the list of dates to refresh based on the current scheduler cycle.
    Called from the scheduler — uses _cycle_count from the scheduler module.
    """
    try:
        from ghl_integration.scheduler import _cycle_count
    except ImportError:
        _cycle_count = 1

    dates = []

    # Today: always refresh (every 30 min)
    dates.append(today)

    # Next 2 days: every 8 cycles (every 4 hours)
    if _cycle_count % 8 == 0 or _cycle_count <= 1:
        dates.append(today + timedelta(days=1))
        dates.append(today + timedelta(days=2))

    # Days 3-7: every 48 cycles (once per day)
    if _cycle_count % 48 == 0 or _cycle_count <= 1:
        for d in range(3, 8):
            dates.append(today + timedelta(days=d))

    return dates


def _apply_flight_update(flight, flight_data):
    """
    Apply AeroAPI response data to a Flight model instance.
    Mirrors the logic in dispatching/views.py:refresh_flight_data but
    operates headlessly without a request.
    """
    from django.utils import timezone as tz

    update_fields = []

    # Basic flight info
    if flight_data.get("flight_iata"):
        flight.flight_iata = flight_data["flight_iata"]
        update_fields.append("flight_iata")

    if flight_data.get("origin"):
        flight.origin = flight_data["origin"]
        update_fields.append("origin")

    if flight_data.get("destination"):
        flight.destination = flight_data["destination"]
        update_fields.append("destination")

    # Flight status (e.g. Scheduled, En Route, Landed)
    # Use flight_status only — never fall back to "status" which is "success"
    flight_status = flight_data.get("flight_status", "")
    if flight_status:
        flight.status = flight_status
        update_fields.append("status")

    # Scheduled times — always update (even to None to clear stale data)
    scheduled_arrival = flight_data.get("scheduled_arrival_local")
    flight.scheduled_arrival_local = scheduled_arrival
    update_fields.append("scheduled_arrival_local")

    scheduled_gate_arrival = flight_data.get("scheduled_gate_arrival_local")
    flight.scheduled_gate_arrival_local = scheduled_gate_arrival
    update_fields.append("scheduled_gate_arrival_local")

    # Determine if flight is in the future
    now = tz.now()
    is_future = False
    if scheduled_arrival and scheduled_arrival > now:
        is_future = True
    elif scheduled_gate_arrival and scheduled_gate_arrival > now:
        is_future = True

    if is_future:
        # Future flight: clear actuals (stale from previous day's recurring flight),
        # but keep/update estimated times (AeroAPI predictions for en-route flights)
        flight.actual_arrival_local = None
        flight.actual_gate_arrival_local = None
        update_fields.extend(["actual_arrival_local", "actual_gate_arrival_local"])

        est_arrival = flight_data.get("estimated_arrival_local")
        if est_arrival is not None:
            flight.estimated_arrival_local = est_arrival
            update_fields.append("estimated_arrival_local")
        est_gate = flight_data.get("estimated_gate_arrival_local")
        if est_gate is not None:
            flight.estimated_gate_arrival_local = est_gate
            update_fields.append("estimated_gate_arrival_local")
    else:
        # Past/current flight: update actuals and estimates from AeroAPI
        actual_runway = flight_data.get("actual_runway_arrival_local")
        if actual_runway is not None:
            flight.actual_arrival_local = actual_runway
            update_fields.append("actual_arrival_local")
        actual_gate = flight_data.get("actual_gate_arrival_local")
        if actual_gate is not None:
            flight.actual_gate_arrival_local = actual_gate
            update_fields.append("actual_gate_arrival_local")

        # Keep estimated times too (useful while taxiing — runway actual exists
        # but gate estimate may still be needed until actual gate arrival)
        est_arrival = flight_data.get("estimated_arrival_local")
        if est_arrival is not None:
            flight.estimated_arrival_local = est_arrival
            update_fields.append("estimated_arrival_local")
        est_gate = flight_data.get("estimated_gate_arrival_local")
        if est_gate is not None:
            flight.estimated_gate_arrival_local = est_gate
            update_fields.append("estimated_gate_arrival_local")

    # Terminal, gate, baggage — always update to clear stale values
    flight.terminal = flight_data.get("terminal") or ""
    update_fields.append("terminal")
    flight.gate = flight_data.get("gate") or ""
    update_fields.append("gate")
    flight.baggage_claim = flight_data.get("baggage_claim") or ""
    update_fields.append("baggage_claim")

    # Always update last_updated
    flight.last_updated = tz.now()
    update_fields.append("last_updated")

    # Deduplicate and save
    flight.save(update_fields=list(set(update_fields)))
