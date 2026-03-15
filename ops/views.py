"""
Views for the operational task queue and related API endpoints.
"""

import json
import logging
from datetime import timedelta
from collections import defaultdict

from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth import get_user_model
from django.db.models import Count, Avg, Q, F
from django.db.models.functions import TruncDate
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST, require_http_methods
from django.contrib import messages

from .models import OperationalTask, CommunicationAttempt, StaffActivity
from .services import close_task, cancel_task, log_communication, create_task

User = get_user_model()
logger = logging.getLogger(__name__)


def _is_superuser(user):
    return user.is_superuser


@login_required(login_url="login")
@user_passes_test(_is_superuser, login_url="login")
def task_queue_view(request):
    """
    Main staff task queue — shows all open operational tasks sorted by priority.
    Supports filtering by task_type, assigned_to, and status.
    """
    # Parse filters
    type_filter = request.GET.get("type", "")
    assignee_filter = request.GET.get("assignee", "")
    status_filter = request.GET.get("status", "")
    overdue_only = request.GET.get("overdue") == "1"

    today = timezone.localdate()

    tasks = OperationalTask.objects.filter(
        status__in=["pending", "in_progress", "escalated", "snoozed"],
    ).exclude(
        # Hide driver assignment tasks for past pickup dates
        task_type=OperationalTask.TaskType.DRIVER_ASSIGNMENT,
        leg__pickup_date__lt=today,
    ).select_related(
        "reservation",
        "reservation__customer",
        "leg",
        "leg__reservation",
        "leg__reservation__customer",
        "leg__flight_information",
        "lead",
        "contact_form",
        "assigned_to",
        "blocked_by",
    ).order_by("priority", "due_at")

    if type_filter:
        tasks = tasks.filter(task_type=type_filter)
    if assignee_filter == "me":
        tasks = tasks.filter(assigned_to=request.user)
    elif assignee_filter == "unassigned":
        tasks = tasks.filter(assigned_to__isnull=True)
    if status_filter:
        tasks = tasks.filter(status=status_filter)
    if overdue_only:
        tasks = tasks.filter(due_at__lt=timezone.now())

    now = timezone.now()

    # Summary counts (unfiltered, for the summary bar)
    from django.db.models import Count

    summary_qs = OperationalTask.objects.filter(
        status__in=["pending", "in_progress", "escalated"],
    )
    type_counts = dict(
        summary_qs.values_list("task_type").annotate(c=Count("id")).values_list("task_type", "c")
    )
    total_open = sum(type_counts.values())
    overdue_count = summary_qs.filter(due_at__lt=now).count()

    # Build priority-grouped task list for the template
    priority_config = [
        (1, "critical", "Critical", "Needs immediate action"),
        (2, "high", "High", "Address within a few hours"),
        (3, "medium", "Medium", "Handle today"),
        (4, "low", "Low", "When time permits"),
    ]
    priority_groups = []
    task_list = list(tasks)
    for pval, key, label, hint in priority_config:
        group_tasks = [t for t in task_list if t.priority == pval]
        priority_groups.append({
            "priority": pval,
            "key": key,
            "label": label,
            "hint": hint,
            "tasks": group_tasks,
        })

    context = {
        "tasks": task_list,
        "priority_groups": priority_groups,
        "now": now,
        "type_filter": type_filter,
        "assignee_filter": assignee_filter,
        "status_filter": status_filter,
        "overdue_only": overdue_only,
        "type_counts": type_counts,
        "total_open": total_open,
        "overdue_count": overdue_count,
        "task_types": OperationalTask.TaskType.choices,
    }
    return render(request, "dispatching/task_queue.html", context)


@login_required(login_url="login")
@user_passes_test(_is_superuser, login_url="login")
@require_POST
def task_claim(request):
    """Claim a task — assign it to the current user and set status to in_progress."""
    try:
        data = json.loads(request.body)
        task_id = data.get("task_id")
    except (json.JSONDecodeError, AttributeError):
        task_id = request.POST.get("task_id")

    task = get_object_or_404(OperationalTask, id=task_id)

    if not task.is_open:
        return JsonResponse({"success": False, "error": "Task is not open"})

    task.assigned_to = request.user
    task.status = OperationalTask.Status.IN_PROGRESS
    task.save(update_fields=["assigned_to", "status", "updated_at"])

    StaffActivity.objects.create(
        user=request.user,
        action_type=StaffActivity.ActionType.TASK_CLAIMED,
        task=task,
    )

    return JsonResponse({"success": True, "task_id": task.id})


@login_required(login_url="login")
@user_passes_test(_is_superuser, login_url="login")
@require_POST
def task_complete(request):
    """Mark a task as completed."""
    try:
        data = json.loads(request.body)
        task_id = data.get("task_id")
        notes = data.get("notes", "")
    except (json.JSONDecodeError, AttributeError):
        task_id = request.POST.get("task_id")
        notes = request.POST.get("notes", "")

    task = get_object_or_404(OperationalTask, id=task_id)

    if not task.is_open:
        return JsonResponse({"success": False, "error": "Task is not open"})

    close_task(task, resolved_by=request.user, resolution_notes=notes)

    StaffActivity.objects.create(
        user=request.user,
        action_type=StaffActivity.ActionType.TASK_COMPLETED,
        task=task,
    )

    return JsonResponse({"success": True, "task_id": task.id})


@login_required(login_url="login")
@user_passes_test(_is_superuser, login_url="login")
@require_POST
def task_snooze(request):
    """Snooze a task for a specified duration."""
    try:
        data = json.loads(request.body)
        task_id = data.get("task_id")
        duration = data.get("duration", "1h")
    except (json.JSONDecodeError, AttributeError):
        task_id = request.POST.get("task_id")
        duration = request.POST.get("duration", "1h")

    task = get_object_or_404(OperationalTask, id=task_id)

    if not task.is_open:
        return JsonResponse({"success": False, "error": "Task is not open"})

    now = timezone.now()
    durations = {
        "1h": timedelta(hours=1),
        "4h": timedelta(hours=4),
        "tomorrow": timedelta(days=1),
    }
    delta = durations.get(duration, timedelta(hours=1))

    # For "tomorrow", set to 9 AM Eastern next day
    if duration == "tomorrow":
        import pytz

        eastern = pytz.timezone("US/Eastern")
        tomorrow_9am = (
            now.astimezone(eastern).replace(hour=9, minute=0, second=0, microsecond=0)
            + timedelta(days=1)
        )
        task.snoozed_until = tomorrow_9am
    else:
        task.snoozed_until = now + delta

    task.status = OperationalTask.Status.SNOOZED
    task.save(update_fields=["status", "snoozed_until", "updated_at"])

    StaffActivity.objects.create(
        user=request.user,
        action_type=StaffActivity.ActionType.TASK_SNOOZED,
        task=task,
        metadata={"duration": duration, "snoozed_until": task.snoozed_until.isoformat()},
    )

    return JsonResponse({
        "success": True,
        "task_id": task.id,
        "snoozed_until": task.snoozed_until.isoformat(),
    })


@login_required(login_url="login")
@user_passes_test(_is_superuser, login_url="login")
@require_POST
def task_cancel(request):
    """Cancel a task."""
    try:
        data = json.loads(request.body)
        task_id = data.get("task_id")
        reason = data.get("reason", "Manually cancelled")
    except (json.JSONDecodeError, AttributeError):
        task_id = request.POST.get("task_id")
        reason = request.POST.get("reason", "Manually cancelled")

    task = get_object_or_404(OperationalTask, id=task_id)

    if not task.is_open:
        return JsonResponse({"success": False, "error": "Task is not open"})

    cancel_task(task, reason=reason)
    return JsonResponse({"success": True, "task_id": task.id})


@login_required(login_url="login")
@user_passes_test(_is_superuser, login_url="login")
@require_POST
def task_log_comm(request):
    """Log a communication attempt on a task."""
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    task_id = data.get("task_id")
    channel = data.get("channel")
    outcome = data.get("outcome")
    notes = data.get("notes", "")
    contact_value = data.get("contact_value", "")
    duration = data.get("duration_seconds")

    if not all([task_id, channel, outcome]):
        return JsonResponse({"success": False, "error": "Missing required fields"}, status=400)

    task = get_object_or_404(OperationalTask, id=task_id)
    attempt = log_communication(
        task=task,
        channel=channel,
        outcome=outcome,
        user=request.user,
        notes=notes,
        contact_value=contact_value,
        duration=int(duration) if duration else None,
    )

    return JsonResponse({
        "success": True,
        "attempt_id": attempt.id,
        "task_attempts": task.attempts,
    })


@login_required(login_url="login")
@user_passes_test(_is_superuser, login_url="login")
@require_http_methods(["GET", "POST"])
def task_create_manual(request):
    """Create a manual task via form POST or JSON."""
    if request.method == "GET":
        # Return a simple form — or handle in the queue template
        return JsonResponse({"error": "Use POST"}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    title = data.get("title", "").strip()
    if not title:
        return JsonResponse({"success": False, "error": "Title is required"}, status=400)

    priority = int(data.get("priority", OperationalTask.Priority.MEDIUM))
    description = data.get("description", "")

    # Parse due_at or default to now
    due_at_str = data.get("due_at")
    if due_at_str:
        from django.utils.dateparse import parse_datetime
        due_at = parse_datetime(due_at_str) or timezone.now()
    else:
        due_at = timezone.now()

    task = create_task(
        task_type=OperationalTask.TaskType.MANUAL,
        title=title,
        due_at=due_at,
        priority=priority,
        description=description,
        created_by=request.user,
    )

    if task:
        StaffActivity.objects.create(
            user=request.user,
            action_type=StaffActivity.ActionType.TASK_CREATED,
            task=task,
        )
        return JsonResponse({"success": True, "task_id": task.id})
    else:
        return JsonResponse({"success": False, "error": "Duplicate task exists"})


def _build_driver_conflict_context(task):
    """
    Build extra context for driver_conflict task detail: the driver's full
    day schedule with conflict highlighting and timing data.

    Handles two flavours:
      1. Flight-triggered — a flight shifted, which causes (or would cause)
         a scheduling conflict. Shows flight shift info + "what happens
         after matching flight time" analysis.
      2. Pure scheduling overlap — two legs assigned to the same driver
         overlap regardless of any flight change.
    """
    from reservations.models import Leg
    from datetime import datetime
    from ops.tasks import (
        _get_effective_ready_time,
        _estimate_leg_end_time,
        AIRPORT_ARRIVAL_GRACE_MINUTES,
    )

    meta = task.metadata or {}
    driver_id = meta.get("driver_id")
    pickup_date_str = meta.get("pickup_date")
    conflicting_leg_id = meta.get("conflicting_leg_id")

    if not driver_id or not pickup_date_str:
        return {}

    from datetime import date as date_type
    pickup_date = date_type.fromisoformat(pickup_date_str)

    # Is this flight-triggered or a pure scheduling overlap?
    is_flight_triggered = meta.get("mismatch_direction") != "overlap"
    has_flight = bool(task.leg and task.leg.flight_information) if task.leg else False
    # "Match Flight Time" only makes sense for arrival legs (airport → hotel)
    is_arrival_leg = (
        has_flight
        and hasattr(task.leg, "get_trip_type")
        and task.leg.get_trip_type() == "arrival"
    )
    can_match_flight = is_arrival_leg

    # All legs for this driver on this day, ordered by pickup_time
    day_legs = list(
        Leg.objects.filter(
            driver_id=driver_id,
            pickup_date=pickup_date,
        )
        .exclude(status__in=["completed", "cancelled"])
        .exclude(reservation__status="cancelled")
        .select_related(
            "flight_information",
            "reservation",
            "reservation__customer",
        )
        .order_by("pickup_time")
    )

    # Build schedule entries with timing info
    schedule = []
    for leg in day_legs:
        ready_time = _get_effective_ready_time(leg, pickup_date)
        end_time = _estimate_leg_end_time(leg, pickup_date)
        is_trigger = leg.pk == task.leg_id
        is_conflicting = leg.pk == conflicting_leg_id

        # Format times
        ready_str = ready_time.strftime("%I:%M %p").lstrip("0")
        end_str = end_time.strftime("%I:%M %p").lstrip("0")
        pickup_str = leg.pickup_time.strftime("%I:%M %p").lstrip("0")

        # Customer name
        customer = leg.reservation.customer if leg.reservation else None
        customer_name = customer.get_full_name() if customer else "Unknown"

        # Trip type display
        trip_type = leg.get_trip_type() if hasattr(leg, "get_trip_type") else ""

        # Flight info
        flight_label = ""
        if leg.flight_information:
            fi = leg.flight_information
            airline = fi.airline_display_name or fi.airline or ""
            flight_label = f"{airline} {fi.flight_number}".strip()

        schedule.append({
            "leg": leg,
            "pickup_str": pickup_str,
            "ready_str": ready_str,
            "end_str": end_str,
            "ready_time": ready_time,
            "end_time": end_time,
            "customer_name": customer_name,
            "trip_type": trip_type,
            "flight_label": flight_label,
            "is_trigger": is_trigger,
            "is_conflicting": is_conflicting,
            "pickup_location": leg.pickup_location,
            "dropoff_location": leg.dropoff_location,
        })

    # ── Conflict breakdown: travel time between conflicting legs ──
    conflict_detail = None
    trigger_entry = next((e for e in schedule if e["is_trigger"]), None)
    conflicting_entry = next((e for e in schedule if e["is_conflicting"]), None)
    if trigger_entry and conflicting_entry:
        try:
            from dispatching.scheduler import get_drive_time
            from dispatching.analytics import (
                categorize_location,
                categorize_time_of_day,
                categorize_day_type,
            )

            # Determine which leg is first chronologically
            if trigger_entry["ready_time"] <= conflicting_entry["ready_time"]:
                first, second = trigger_entry, conflicting_entry
            else:
                first, second = conflicting_entry, trigger_entry

            first_leg = first["leg"]
            second_leg = second["leg"]

            # Travel from first leg's dropoff to second leg's pickup
            from_cat = categorize_location(first_leg.dropoff_location or "")
            to_cat = categorize_location(second_leg.pickup_location or "")
            time_cat = categorize_time_of_day(first_leg.pickup_time)
            day_cat = categorize_day_type(pickup_date)
            travel_minutes = get_drive_time(from_cat, to_cat, time_cat, day_cat)

            clears_at = first["end_time"]
            earliest_arrival = clears_at + timedelta(minutes=travel_minutes)
            second_pickup = datetime.combine(pickup_date, second_leg.pickup_time)
            late_minutes = max(
                0,
                int((earliest_arrival - second_pickup).total_seconds() / 60),
            )

            # Check if second leg is an arrival with flight data
            second_is_arrival = (
                hasattr(second_leg, "get_trip_type")
                and second_leg.get_trip_type() == "arrival"
                and second_leg.flight_information
            )

            # Flight-specific context for arrival legs
            flight_gate_str = ""
            original_flight_str = ""
            guest_ready_str = ""
            guest_ready_minutes = 0

            if second_is_arrival:
                from dispatching.scheduler import _get_best_flight_arrival
                flight_dt = _get_best_flight_arrival(second_leg)
                if flight_dt:
                    flight_gate_str = flight_dt.time().strftime("%I:%M %p").lstrip("0")
                    grace = AIRPORT_ARRIVAL_GRACE_MINUTES
                    guest_ready_dt = datetime.combine(pickup_date, flight_dt.time()) + timedelta(minutes=grace)
                    guest_ready_str = guest_ready_dt.strftime("%I:%M %p").lstrip("0")
                    guest_ready_minutes = grace

                    # Late vs guest ready (not vs booked pickup)
                    late_minutes = max(
                        0,
                        int((earliest_arrival - guest_ready_dt).total_seconds() / 60),
                    )

                    # Original scheduled time (to show the shift)
                    fi = second_leg.flight_information
                    sched = fi.scheduled_arrival_local or fi.scheduled_gate_arrival_local
                    if sched:
                        from django.utils import timezone as tz
                        if tz.is_aware(sched):
                            sched = tz.make_naive(sched, tz.get_current_timezone())
                        original_flight_str = sched.time().strftime("%I:%M %p").lstrip("0")

            # Same-airport reposition vs regular travel
            is_reposition = from_cat == to_cat and "Terminal" in from_cat

            conflict_detail = {
                "first_customer": first["customer_name"],
                "second_customer": second["customer_name"],
                "clears_at_str": clears_at.strftime("%I:%M %p").lstrip("0"),
                "clears_location": first_leg.dropoff_location or "",
                "travel_to": second_leg.pickup_location or "",
                "travel_minutes": travel_minutes,
                "is_reposition": is_reposition,
                "earliest_arrival_str": earliest_arrival.strftime("%I:%M %p").lstrip("0"),
                "second_pickup_str": second["pickup_str"],
                "late_minutes": late_minutes,
                # Flight arrival context
                "second_is_arrival": second_is_arrival,
                "flight_gate_str": flight_gate_str,
                "original_flight_str": original_flight_str,
                "guest_ready_str": guest_ready_str,
                "guest_ready_minutes": guest_ready_minutes,
                "second_flight_label": second["flight_label"],
            }
        except Exception:
            logger.exception("Error computing conflict breakdown for task %s", task.id)

    # ── Flight-triggered: compute post-match analysis ──
    flight_arrival_str = ""
    booked_pickup_str = ""
    post_match_ok = None  # None = not applicable, True = schedule works, False = still conflicts
    post_match_overlap_min = 0
    late_night_flag = False

    if can_match_flight:
        try:
            from dispatching.scheduler import _get_best_flight_arrival
            flight_dt = _get_best_flight_arrival(task.leg)
            if flight_dt:
                flight_arrival_str = flight_dt.time().strftime("%I:%M %p").lstrip("0")
                booked_pickup_str = task.leg.pickup_time.strftime("%I:%M %p").lstrip("0")

                if flight_dt.hour >= 22:
                    late_night_flag = True

                # Simulate: if we match flight time, does the schedule still conflict?
                # After matching, the trigger leg's ready_time becomes flight_arrival + grace
                matched_ready = datetime.combine(
                    pickup_date, flight_dt.time()
                ) + timedelta(minutes=AIRPORT_ARRIVAL_GRACE_MINUTES)

                # Get the trigger leg's current duration to estimate new end time
                trigger_entry = next(
                    (e for e in schedule if e["is_trigger"]), None
                )
                conflicting_entry = next(
                    (e for e in schedule if e["is_conflicting"]), None
                )
                if trigger_entry and conflicting_entry:
                    # Use the same trip duration (end - ready) from the current estimate
                    trip_duration = trigger_entry["end_time"] - trigger_entry["ready_time"]
                    matched_end = matched_ready + trip_duration

                    other_end = conflicting_entry["end_time"]
                    other_ready = conflicting_entry["ready_time"]

                    if other_end > matched_ready and other_ready < matched_ready:
                        # Other leg finishes after we need to start
                        post_match_ok = False
                        post_match_overlap_min = int(
                            (other_end - matched_ready).total_seconds() / 60
                        )
                    elif matched_end > other_ready and matched_ready < other_ready:
                        # Our matched leg would finish after the other needs to start
                        post_match_ok = False
                        post_match_overlap_min = int(
                            (matched_end - other_ready).total_seconds() / 60
                        )
                    else:
                        post_match_ok = True
        except Exception:
            logger.exception("Error computing post-match analysis for task %s", task.id)

    # Driver info
    from drivers.models import Driver
    try:
        driver = Driver.objects.select_related("profile").get(pk=driver_id)
        driver_name = str(driver)
        driver_phone = driver.phone_number or ""
    except Driver.DoesNotExist:
        driver_name = meta.get("driver_name", "Unknown")
        driver_phone = ""

    return {
        "driver_schedule": schedule,
        "driver_name": driver_name,
        "driver_phone": driver_phone,
        "conflict_minutes": meta.get("conflict_minutes", 0),
        "mismatch_minutes": meta.get("mismatch_minutes", 0),
        "mismatch_label": meta.get("mismatch_label", ""),
        "flight_ident": meta.get("flight_ident", ""),
        "is_flight_triggered": is_flight_triggered,
        "has_flight": has_flight,
        "can_match_flight": can_match_flight,
        "is_arrival_leg": is_arrival_leg,
        "flight_arrival_str": flight_arrival_str,
        "booked_pickup_str": booked_pickup_str,
        "post_match_ok": post_match_ok,
        "post_match_overlap_min": post_match_overlap_min,
        "late_night_flag": late_night_flag,
        "pickup_date_str": pickup_date_str,
        "conflict_detail": conflict_detail,
    }


@login_required(login_url="login")
@user_passes_test(_is_superuser, login_url="login")
def task_detail_view(request, task_id):
    """
    Detailed view of a single task with full communication history,
    related object info, and inline communication logging form.
    """
    task = get_object_or_404(
        OperationalTask.objects.select_related(
            "reservation",
            "reservation__customer",
            "leg",
            "leg__reservation",
            "leg__reservation__customer",
            "leg__flight_information",
            "lead",
            "contact_form",
            "assigned_to",
            "created_by",
            "resolved_by",
            "blocked_by",
        ),
        id=task_id,
    )

    comm_attempts = task.comm_attempts.select_related("staff_user").order_by("-created_at")
    activities = task.staff_activities.select_related("user").order_by("-created_at")[:20]

    context = {
        "task": task,
        "comm_attempts": comm_attempts,
        "activities": activities,
        "channels": CommunicationAttempt.Channel.choices,
        "outcomes": CommunicationAttempt.Outcome.choices,
    }

    # ── Driver Conflict: build the driver's full day schedule ──
    if task.task_type == OperationalTask.TaskType.DRIVER_CONFLICT and task.leg:
        context.update(_build_driver_conflict_context(task))

    return render(request, "dispatching/task_detail.html", context)


# ── Staff Metrics Dashboard (superuser-only) ──


@login_required(login_url="login")
@user_passes_test(_is_superuser, login_url="dashboard")
def staff_metrics_view(request):
    """
    Owner-only metrics dashboard showing staff productivity, response times,
    queue health, and communication volume.
    """
    import pytz

    eastern = pytz.timezone("US/Eastern")
    now = timezone.now()
    today = timezone.localdate()

    # Time range selection
    range_param = request.GET.get("range", "7")
    try:
        days_back = int(range_param)
    except ValueError:
        days_back = 7
    days_back = min(days_back, 90)

    range_start = now - timedelta(days=days_back)

    # ── Queue Health (current snapshot) ──
    open_tasks = OperationalTask.objects.filter(
        status__in=list(OperationalTask.OPEN_STATUSES),
    )
    queue_by_type = dict(
        open_tasks.values_list("task_type").annotate(c=Count("id")).values_list("task_type", "c")
    )
    queue_by_priority = dict(
        open_tasks.values_list("priority").annotate(c=Count("id")).values_list("priority", "c")
    )
    overdue_count = open_tasks.filter(due_at__lt=now).count()
    total_open = sum(queue_by_type.values())
    escalated_count = open_tasks.filter(status="escalated").count()

    # ── Tasks completed per staff (in range) ──
    completed_in_range = OperationalTask.objects.filter(
        status="completed",
        resolved_at__gte=range_start,
    )
    staff_completions = list(
        completed_in_range.filter(resolved_by__isnull=False)
        .values("resolved_by__id", "resolved_by__first_name", "resolved_by__username")
        .annotate(count=Count("id"))
        .order_by("-count")
    )
    for s in staff_completions:
        s["name"] = s["resolved_by__first_name"] or s["resolved_by__username"]

    auto_closed_count = completed_in_range.filter(resolved_by__isnull=True).count()

    # ── Communication volume per staff (in range) ──
    comms_in_range = CommunicationAttempt.objects.filter(created_at__gte=range_start)
    staff_comms = list(
        comms_in_range.values("staff_user__id", "staff_user__first_name", "staff_user__username")
        .annotate(
            total=Count("id"),
            calls=Count("id", filter=Q(channel="call")),
            sms=Count("id", filter=Q(channel="sms")),
            emails=Count("id", filter=Q(channel="email")),
        )
        .order_by("-total")
    )
    for s in staff_comms:
        s["name"] = s["staff_user__first_name"] or s["staff_user__username"]

    # ── Daily task creation/completion trend (for chart) ──
    daily_created = dict(
        OperationalTask.objects.filter(created_at__gte=range_start)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(count=Count("id"))
        .values_list("day", "count")
    )
    daily_completed = dict(
        completed_in_range.annotate(day=TruncDate("resolved_at"))
        .values("day")
        .annotate(count=Count("id"))
        .values_list("day", "count")
    )

    trend_data = []
    for i in range(days_back):
        d = today - timedelta(days=days_back - 1 - i)
        trend_data.append({
            "day": d.isoformat(),
            "created": daily_created.get(d, 0),
            "completed": daily_completed.get(d, 0),
        })

    # ── Lead response times (in range) ──
    # Average time from task creation to first communication attempt
    lead_tasks = OperationalTask.objects.filter(
        task_type="lead_response",
        created_at__gte=range_start,
    ).prefetch_related("comm_attempts")

    response_times = []
    for task in lead_tasks:
        first_comm = task.comm_attempts.order_by("created_at").first()
        if first_comm:
            delta = (first_comm.created_at - task.created_at).total_seconds() / 60
            response_times.append(delta)

    avg_response_min = round(sum(response_times) / len(response_times), 1) if response_times else None
    median_response_min = round(sorted(response_times)[len(response_times) // 2], 1) if response_times else None

    # ── Today's staff activity timeline ──
    today_start = now.astimezone(eastern).replace(hour=0, minute=0, second=0, microsecond=0)
    today_activities = list(
        StaffActivity.objects.filter(
            created_at__gte=today_start,
        )
        .exclude(action_type=StaffActivity.ActionType.PAGE_VIEW)
        .select_related("user", "task")
        .order_by("-created_at")[:50]
    )

    # ── Task type performance ──
    type_performance = list(
        completed_in_range.values("task_type")
        .annotate(
            count=Count("id"),
            avg_attempts=Avg("attempts"),
        )
        .order_by("-count")
    )

    # ── Page view counts per staff (today) ──
    page_views_today = list(
        StaffActivity.objects.filter(
            action_type=StaffActivity.ActionType.PAGE_VIEW,
            created_at__gte=today_start,
        )
        .values("user__id", "user__first_name", "user__username")
        .annotate(views=Count("id"))
        .order_by("-views")
    )
    for pv in page_views_today:
        pv["name"] = pv["user__first_name"] or pv["user__username"]

    context = {
        "range_days": days_back,
        "range_start": range_start,
        # Queue health
        "total_open": total_open,
        "overdue_count": overdue_count,
        "escalated_count": escalated_count,
        "queue_by_type": queue_by_type,
        "queue_by_priority": queue_by_priority,
        # Staff productivity
        "staff_completions": staff_completions,
        "auto_closed_count": auto_closed_count,
        "staff_comms": staff_comms,
        # Trend chart
        "trend_json": json.dumps(trend_data),
        # Response times
        "avg_response_min": avg_response_min,
        "median_response_min": median_response_min,
        "response_count": len(response_times),
        # Today
        "today_activities": today_activities,
        "page_views_today": page_views_today,
        # Type performance
        "type_performance": type_performance,
        "task_type_labels": dict(OperationalTask.TaskType.choices),
        "priority_labels": dict(OperationalTask.Priority.choices),
    }
    return render(request, "dispatching/staff_metrics.html", context)
