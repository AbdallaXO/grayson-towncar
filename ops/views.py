"""
Views for the operational task queue and related API endpoints.
"""

import json
import logging
from datetime import timedelta
from collections import defaultdict

from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth import get_user_model
from django.db.models import Count, Avg, Q, F, Min, Max
from django.db.models.functions import TruncDate
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST, require_http_methods
from django.contrib import messages

from .models import OperationalTask, CommunicationAttempt, StaffActivity, EmailLog
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

    # Split out the current user's active (claimed/in-progress) tasks
    task_list = list(tasks)
    my_active = [
        t for t in task_list
        if t.assigned_to_id == request.user.id
        and t.status in ("in_progress", "pending")
    ]
    my_active_ids = {t.id for t in my_active}
    remaining_tasks = [t for t in task_list if t.id not in my_active_ids]

    # Build priority-grouped task list for the template
    priority_config = [
        (1, "critical", "Critical", "Needs immediate action"),
        (2, "high", "High", "Address within a few hours"),
        (3, "medium", "Medium", "Handle today"),
        (4, "low", "Low", "When time permits"),
    ]
    priority_groups = []
    for pval, key, label, hint in priority_config:
        group_tasks = [t for t in remaining_tasks if t.priority == pval]
        priority_groups.append({
            "priority": pval,
            "key": key,
            "label": label,
            "hint": hint,
            "tasks": group_tasks,
        })

    ops_staff = list(
        User.objects.filter(is_superuser=True, is_active=True)
        .order_by("first_name", "username")
        .values("id", "first_name", "username")
    )

    context = {
        "tasks": task_list,
        "my_active": my_active,
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
        "ops_staff": ops_staff,
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
def task_assign(request):
    """Assign a task to a staff member."""
    try:
        data = json.loads(request.body)
        task_id = data.get("task_id")
        user_id = data.get("user_id")
    except (json.JSONDecodeError, AttributeError):
        task_id = request.POST.get("task_id")
        user_id = request.POST.get("user_id")

    task = get_object_or_404(OperationalTask, id=task_id)

    if user_id:
        assignee = get_object_or_404(User, id=user_id, is_superuser=True)
        task.assigned_to = assignee
        label = assignee.first_name or assignee.username
    else:
        task.assigned_to = None
        label = None

    if task.status == OperationalTask.Status.PENDING:
        task.status = OperationalTask.Status.IN_PROGRESS
        task.save(update_fields=["assigned_to", "status", "updated_at"])
    else:
        task.save(update_fields=["assigned_to", "updated_at"])

    StaffActivity.objects.create(
        user=request.user,
        action_type=StaffActivity.ActionType.TASK_ASSIGNED,
        task=task,
        metadata={"assigned_to": label or "unassigned"},
    )

    return JsonResponse({"success": True, "task_id": task.id, "assigned_to": label})


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
def contact_form_update_status(request):
    """Update a contact form's status (contacted/closed) and optionally close its task."""
    from users.models import ContactUsForm

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    form_id = data.get("form_id")
    new_status = data.get("status")

    if new_status not in ("contacted", "closed"):
        return JsonResponse({"success": False, "error": "Invalid status"}, status=400)

    form = get_object_or_404(ContactUsForm, id=form_id)
    form.status = new_status
    if new_status == "contacted" and not form.contacted_at:
        form.contacted_at = timezone.now()
    form.save(update_fields=["status", "contacted_at"])

    # If closed, also close any open ops tasks linked to this form
    if new_status == "closed":
        open_tasks = OperationalTask.objects.filter(
            contact_form=form, status__in=["open", "snoozed"]
        )
        for task in open_tasks:
            close_task(task, resolved_by=request.user, resolution_notes="Contact form closed")

    return JsonResponse({"success": True, "status": new_status})


@login_required(login_url="login")
@user_passes_test(_is_superuser, login_url="login")
@require_POST
def contact_form_delete(request):
    """Delete a spam contact form and cancel its associated task."""
    from users.models import ContactUsForm

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    form_id = data.get("form_id")
    form = get_object_or_404(ContactUsForm, id=form_id)

    # Cancel any open tasks linked to this form
    open_tasks = OperationalTask.objects.filter(
        contact_form=form, status__in=["open", "snoozed"]
    )
    for task in open_tasks:
        cancel_task(task, reason="Contact form deleted (spam)")

    form.delete()
    return JsonResponse({"success": True, "redirect": True})


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

    # Parse due_at or default to end of today
    due_at_str = data.get("due_at")
    if due_at_str:
        from django.utils.dateparse import parse_datetime
        due_at = parse_datetime(due_at_str) or timezone.now()
    else:
        # Default to 5 PM today (end of business) so it's not immediately overdue
        import pytz
        eastern = pytz.timezone("US/Eastern")
        today_eod = timezone.now().astimezone(eastern).replace(hour=17, minute=0, second=0, microsecond=0)
        if today_eod <= timezone.now():
            today_eod += timedelta(days=1)
        due_at = today_eod

    task = create_task(
        task_type=OperationalTask.TaskType.MANUAL,
        title=title,
        due_at=due_at,
        priority=priority,
        description=description,
        created_by=request.user,
    )

    if task:
        # Assign to a staff member if specified
        assign_to_id = data.get("assigned_to")
        if assign_to_id:
            try:
                assignee = User.objects.get(id=assign_to_id, is_superuser=True, is_active=True)
                task.assigned_to = assignee
                task.status = OperationalTask.Status.IN_PROGRESS
                task.save(update_fields=["assigned_to", "status", "updated_at"])
            except User.DoesNotExist:
                pass

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
    from reservations.constants import DRIVER_STATUS
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
        .prefetch_related("status_history")
        .order_by("pickup_time")
    )

    # Always include the trigger and conflicting legs even if reassigned/completed
    # so the conflict breakdown and schedule show the full picture
    existing_ids = {leg.pk for leg in day_legs}
    must_include_ids = set()
    if task.leg_id and task.leg_id not in existing_ids:
        must_include_ids.add(task.leg_id)
    if conflicting_leg_id and conflicting_leg_id not in existing_ids:
        must_include_ids.add(conflicting_leg_id)
    if must_include_ids:
        extra_legs = list(
            Leg.objects.filter(pk__in=must_include_ids)
            .select_related(
                "flight_information",
                "reservation",
                "reservation__customer",
            )
            .prefetch_related("status_history")
        )
        day_legs.extend(extra_legs)
        day_legs.sort(key=lambda l: l.pickup_time)

    # Build schedule entries with timing info
    from dispatching.scheduler import (
        get_drive_time as sched_get_drive_time,
        get_airport_dwell_time,
        _get_best_flight_arrival,
    )
    from dispatching.analytics import (
        categorize_location,
        categorize_time_of_day,
        categorize_day_type,
    )

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
        flight_landing_str = ""
        flight_gate_arrival_str = ""
        flight_status = ""
        if leg.flight_information:
            fi = leg.flight_information
            airline = fi.airline_display_name or fi.airline or ""
            flight_label = f"{airline} {fi.flight_number}".strip()
            flight_status = fi.status or ""
            # Runway arrival (landing)
            runway_dt = (
                fi.actual_arrival_local
                or fi.estimated_arrival_local
                or fi.scheduled_arrival_local
            )
            if runway_dt:
                from django.utils import timezone as tz
                if tz.is_aware(runway_dt):
                    runway_dt = tz.make_naive(runway_dt, tz.get_current_timezone())
                flight_landing_str = runway_dt.time().strftime("%I:%M %p").lstrip("0")
            # Gate arrival (arriving at gate)
            gate_dt = (
                fi.actual_gate_arrival_local
                or fi.estimated_gate_arrival_local
                or fi.scheduled_gate_arrival_local
            )
            if gate_dt:
                from django.utils import timezone as tz
                if tz.is_aware(gate_dt):
                    gate_dt = tz.make_naive(gate_dt, tz.get_current_timezone())
                flight_gate_arrival_str = gate_dt.time().strftime("%I:%M %p").lstrip("0")

        # Drive time — prefer live Google Maps, fall back to historical P75
        from drivers.utils import get_drive_time as google_drive_time
        pickup_cat = categorize_location(leg.pickup_location or "")
        dropoff_cat = categorize_location(leg.dropoff_location or "")
        time_cat = categorize_time_of_day(leg.pickup_time)
        day_cat = categorize_day_type(pickup_date)

        live_drive = google_drive_time(leg.pickup_location, leg.dropoff_location)
        if live_drive:
            drive_minutes = round(live_drive["duration_seconds"] / 60)
            drive_label = live_drive["duration_text"]
            drive_is_live = True
        else:
            drive_minutes = sched_get_drive_time(pickup_cat, dropoff_cat, time_cat, day_cat)
            drive_label = f"{drive_minutes} min"
            drive_is_live = False

        dwell_minutes = 0
        if trip_type == "arrival":
            dwell_minutes = get_airport_dwell_time(pickup_cat, dropoff_cat, time_cat, day_cat)

        # Leg status and active-job tracking
        leg_status = leg.status or ""
        leg_status_display = dict(DRIVER_STATUS).get(leg_status, leg_status)
        is_active_job = leg_status in ("on-the-way", "on-location", "picked-up")

        # For active jobs, get the timestamp of the latest status update
        active_since_str = ""
        if is_active_job:
            matching = [
                sh.timestamp for sh in leg.status_history.all()
                if sh.status == leg_status
            ]
            latest_update = max(matching) if matching else None
            if latest_update:
                from django.utils import timezone as tz
                if tz.is_aware(latest_update):
                    latest_update = tz.make_naive(latest_update, tz.get_current_timezone())
                active_since_str = latest_update.strftime("%I:%M %p").lstrip("0")

        schedule.append({
            "leg": leg,
            "pickup_str": pickup_str,
            "ready_str": ready_str,
            "end_str": end_str,
            "leg_status": leg_status,
            "leg_status_display": leg_status_display,
            "is_active_job": is_active_job,
            "active_since_str": active_since_str,
            "ready_time": ready_time,
            "end_time": end_time,
            "customer_name": customer_name,
            "trip_type": trip_type,
            "flight_label": flight_label,
            "flight_landing_str": flight_landing_str,
            "flight_gate_arrival_str": flight_gate_arrival_str,
            "flight_status": flight_status,
            "drive_minutes": drive_minutes,
            "drive_label": drive_label,
            "drive_is_live": drive_is_live,
            "dwell_minutes": dwell_minutes,
            "is_trigger": is_trigger,
            "is_conflicting": is_conflicting,
            "pickup_location": leg.pickup_location,
            "dropoff_location": leg.dropoff_location,
        })

    # ── Travel time between consecutive legs (D/O → next P/U) ──
    for i in range(len(schedule) - 1):
        prev_dropoff = schedule[i]["dropoff_location"]
        next_pickup = schedule[i + 1]["pickup_location"]
        if prev_dropoff and next_pickup:
            transit = google_drive_time(prev_dropoff, next_pickup)
            if transit:
                schedule[i + 1]["transit_label"] = transit["duration_text"]
                schedule[i + 1]["transit_from"] = prev_dropoff
                schedule[i + 1]["transit_is_live"] = True
            else:
                # Fall back to historical
                p_cat = categorize_location(prev_dropoff)
                n_cat = categorize_location(next_pickup)
                mins = sched_get_drive_time(p_cat, n_cat, None, None)
                schedule[i + 1]["transit_label"] = f"{mins} min"
                schedule[i + 1]["transit_from"] = prev_dropoff
                schedule[i + 1]["transit_is_live"] = False

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
            # Prefer live Google data, fall back to historical
            travel_is_live = False
            live_travel = google_drive_time(
                first_leg.dropoff_location, second_leg.pickup_location
            )
            if live_travel:
                travel_minutes = round(live_travel["duration_seconds"] / 60)
                travel_label = live_travel["duration_text"]
                travel_is_live = True
            else:
                from_cat = categorize_location(first_leg.dropoff_location or "")
                to_cat = categorize_location(second_leg.pickup_location or "")
                time_cat = categorize_time_of_day(first_leg.pickup_time)
                day_cat = categorize_day_type(pickup_date)
                travel_minutes = get_drive_time(from_cat, to_cat, time_cat, day_cat)
                travel_label = f"~{travel_minutes} min"

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
            do_cat = categorize_location(first_leg.dropoff_location or "")
            pu_cat = categorize_location(second_leg.pickup_location or "")
            is_reposition = do_cat == pu_cat and "Terminal" in do_cat

            conflict_detail = {
                "first_customer": first["customer_name"],
                "second_customer": second["customer_name"],
                "clears_at_str": clears_at.strftime("%I:%M %p").lstrip("0"),
                "clears_location": first_leg.dropoff_location or "",
                "travel_to": second_leg.pickup_location or "",
                "travel_minutes": travel_minutes,
                "travel_label": travel_label,
                "travel_is_live": travel_is_live,
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
    already_matched = False

    if can_match_flight:
        try:
            from dispatching.scheduler import _get_best_flight_arrival
            flight_dt = _get_best_flight_arrival(task.leg)
            if flight_dt:
                flight_arrival_str = flight_dt.time().strftime("%I:%M %p").lstrip("0")
                booked_pickup_str = task.leg.pickup_time.strftime("%I:%M %p").lstrip("0")

                # If pickup already matches flight arrival, no need to show the button
                from django.utils import timezone as tz
                if tz.is_aware(flight_dt):
                    flight_naive = tz.make_naive(flight_dt, tz.get_current_timezone())
                else:
                    flight_naive = flight_dt
                if task.leg.pickup_time == flight_naive.time():
                    already_matched = True
                    can_match_flight = False

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

    # Build trigger/conflicting leg summaries for clear display
    trigger_entry = next((e for e in schedule if e["is_trigger"]), None)
    conflicting_entry = next((e for e in schedule if e["is_conflicting"]), None)

    trigger_summary = None
    if trigger_entry:
        trigger_summary = {
            "time": trigger_entry["pickup_str"],
            "customer": trigger_entry["customer_name"],
            "pickup": trigger_entry["pickup_location"],
            "dropoff": trigger_entry["dropoff_location"],
            "flight": trigger_entry["flight_label"],
            "trip_type": trigger_entry["trip_type"],
            "clear_time": trigger_entry["end_str"],
            "ready_time": trigger_entry["ready_time"],
            "reassigned": trigger_entry["leg"].driver_id != int(driver_id) if trigger_entry["leg"].driver_id else False,
        }

    conflicting_summary = None
    if conflicting_entry:
        conflicting_summary = {
            "time": conflicting_entry["pickup_str"],
            "customer": conflicting_entry["customer_name"],
            "pickup": conflicting_entry["pickup_location"],
            "dropoff": conflicting_entry["dropoff_location"],
            "flight": conflicting_entry["flight_label"],
            "trip_type": conflicting_entry["trip_type"],
            "clear_time": conflicting_entry["end_str"],
            "ready_time": conflicting_entry["ready_time"],
            "reassigned": conflicting_entry["leg"].driver_id != int(driver_id) if conflicting_entry["leg"].driver_id else False,
        }

    # Ensure earlier leg is "first" (left side) and later is "second" (right side)
    if trigger_summary and conflicting_summary:
        if trigger_summary["ready_time"] <= conflicting_summary["ready_time"]:
            first_summary = trigger_summary
            second_summary = conflicting_summary
        else:
            first_summary = conflicting_summary
            second_summary = trigger_summary
    else:
        first_summary = trigger_summary
        second_summary = conflicting_summary

    # Use freshly calculated late_minutes from conflict_detail if available,
    # otherwise fall back to stale metadata value
    recalc_minutes = conflict_detail["late_minutes"] if conflict_detail else meta.get("conflict_minutes", 0)

    return {
        "driver_schedule": schedule,
        "driver_name": driver_name,
        "driver_phone": driver_phone,
        "conflict_minutes": recalc_minutes,
        "mismatch_minutes": meta.get("mismatch_minutes", 0),
        "mismatch_label": meta.get("mismatch_label", ""),
        "flight_ident": meta.get("flight_ident", ""),
        "is_flight_triggered": is_flight_triggered,
        "has_flight": has_flight,
        "can_match_flight": can_match_flight,
        "already_matched": already_matched,
        "is_arrival_leg": is_arrival_leg,
        "flight_arrival_str": flight_arrival_str,
        "booked_pickup_str": booked_pickup_str,
        "post_match_ok": post_match_ok,
        "post_match_overlap_min": post_match_overlap_min,
        "late_night_flag": late_night_flag,
        "pickup_date_str": pickup_date_str,
        "conflict_detail": conflict_detail,
        "trigger_summary": trigger_summary,
        "conflicting_summary": conflicting_summary,
        "first_summary": first_summary,
        "second_summary": second_summary,
    }


def _build_flight_verify_context(task):
    """
    Build extra context for flight_verify task detail: flight mismatch info,
    assigned driver schedule, conflict check, and quick actions.
    """
    from reservations.models import Leg
    from datetime import datetime, date as date_type
    from ops.tasks import (
        _get_effective_ready_time,
        _estimate_leg_end_time,
        AIRPORT_ARRIVAL_GRACE_MINUTES,
    )

    leg = task.leg
    if not leg:
        return {}

    meta = task.metadata or {}
    flight = leg.flight_information
    pickup_date = leg.pickup_date

    # ── Flight mismatch info ──
    mismatch_info = {
        "direction": meta.get("mismatch_direction", ""),
        "minutes": meta.get("mismatch_minutes", 0),
        "label": meta.get("mismatch_label", ""),
        "severity": meta.get("severity_tier", ""),
        "flight_ident": meta.get("flight_ident", ""),
    }

    # Live flight times
    scheduled_str = ""
    estimated_str = ""
    flight_arrival_str = ""
    booked_pickup_str = leg.pickup_time.strftime("%I:%M %p").lstrip("0")

    if flight:
        sched = flight.scheduled_gate_arrival_local or flight.scheduled_arrival_local
        if sched:
            from django.utils import timezone as tz
            if tz.is_aware(sched):
                sched = tz.make_naive(sched, tz.get_current_timezone())
            scheduled_str = sched.time().strftime("%I:%M %p").lstrip("0")

        est = flight.estimated_gate_arrival_local
        if est:
            from django.utils import timezone as tz
            if tz.is_aware(est):
                est = tz.make_naive(est, tz.get_current_timezone())
            estimated_str = est.time().strftime("%I:%M %p").lstrip("0")

        # Best available arrival (for "Match Flight Time")
        try:
            from dispatching.scheduler import _get_best_flight_arrival
            flight_dt = _get_best_flight_arrival(leg)
            if flight_dt:
                flight_arrival_str = flight_dt.time().strftime("%I:%M %p").lstrip("0")
        except Exception:
            pass

    # ── Driver info + schedule ──
    driver = leg.driver
    driver_name = ""
    driver_phone = ""
    driver_schedule = []
    has_driver_conflict = False
    conflict_minutes = 0

    if driver:
        driver_name = str(driver)
        driver_phone = getattr(driver, "phone_number", "") or ""

        # Build driver's day schedule
        day_legs = list(
            Leg.objects.filter(
                driver=driver,
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

        for dl in day_legs:
            ready_time = _get_effective_ready_time(dl, pickup_date)
            end_time = _estimate_leg_end_time(dl, pickup_date)
            is_this_leg = dl.pk == leg.pk
            customer = dl.reservation.customer if dl.reservation else None
            customer_name = customer.get_full_name() if customer else "Unknown"
            trip_type = dl.get_trip_type() if hasattr(dl, "get_trip_type") else ""

            flight_label = ""
            if dl.flight_information:
                fi = dl.flight_information
                airline = fi.airline_display_name or fi.airline or ""
                flight_label = f"{airline} {fi.flight_number}".strip()

            driver_schedule.append({
                "leg": dl,
                "pickup_str": dl.pickup_time.strftime("%I:%M %p").lstrip("0"),
                "ready_str": ready_time.strftime("%I:%M %p").lstrip("0"),
                "end_str": end_time.strftime("%I:%M %p").lstrip("0"),
                "ready_time": ready_time,
                "end_time": end_time,
                "customer_name": customer_name,
                "trip_type": trip_type,
                "flight_label": flight_label,
                "is_this_leg": is_this_leg,
                "pickup_location": dl.pickup_location,
                "dropoff_location": dl.dropoff_location,
            })

        # Check for conflicts if flight time were matched
        if flight_arrival_str and len(day_legs) > 1:
            try:
                from ops.tasks import detect_driver_conflicts
                conflicts = detect_driver_conflicts(leg, pickup_date)
                if conflicts:
                    has_driver_conflict = True
                    conflict_minutes = max(c["conflict_minutes"] for c in conflicts)
            except Exception:
                logger.exception("Error checking driver conflicts for flight verify task %s", task.id)

    # Trip info
    trip_type = leg.get_trip_type() if hasattr(leg, "get_trip_type") else ""
    is_arrival = trip_type == "arrival"

    return {
        "fv_mismatch": mismatch_info,
        "fv_scheduled_str": scheduled_str,
        "fv_estimated_str": estimated_str,
        "fv_flight_arrival_str": flight_arrival_str,
        "fv_booked_pickup_str": booked_pickup_str,
        "fv_driver_name": driver_name,
        "fv_driver_phone": driver_phone,
        "fv_driver_schedule": driver_schedule,
        "fv_has_driver_conflict": has_driver_conflict,
        "fv_conflict_minutes": conflict_minutes,
        "fv_has_driver": bool(driver),
        "fv_is_arrival": is_arrival,
        "fv_pickup_date_str": str(pickup_date),
        "fv_trip_type": trip_type,
        "is_flight_verify": True,
    }


def _build_payment_chase_context(task):
    """
    Build extra context for payment_chase task detail: reservation payment
    summary, upcoming legs, guest contact, and quick action links.
    """
    reservation = task.reservation
    if not reservation:
        return {}

    from reservations.models import Leg
    from decimal import Decimal

    customer = reservation.customer
    meta = task.metadata or {}

    # Payment summary (live, not from metadata)
    total_price = reservation.total_price or Decimal("0")
    total_paid = reservation.total_paid or Decimal("0")
    amount_owed = reservation.amount_owed or Decimal("0")
    payment_status = reservation.payment_status
    detailed_status = reservation.detailed_payment_status

    # All upcoming legs for this reservation
    today = timezone.localdate()
    legs = list(
        reservation.legs.filter(pickup_date__gte=today)
        .exclude(status__in=["cancelled"])
        .select_related("driver", "flight_information")
        .order_by("pickup_date", "pickup_time")
    )

    leg_data = []
    for lg in legs:
        driver_name = str(lg.driver) if lg.driver else "Unassigned"
        leg_data.append({
            "leg": lg,
            "pickup_date": lg.pickup_date,
            "pickup_time_str": lg.pickup_time.strftime("%I:%M %p").lstrip("0"),
            "pickup_location": lg.pickup_location,
            "dropoff_location": lg.dropoff_location,
            "driver_name": driver_name,
            "status": lg.status,
            "has_driver": bool(lg.driver),
        })

    # Guest contact
    guest_name = customer.get_full_name() if customer else "Unknown"
    guest_phone = getattr(customer, "phone_number", "") or "" if customer else ""
    guest_email = getattr(customer, "email", "") or "" if customer else ""

    # Payment history
    payments = list(reservation.payments.all().order_by("-created_at"))
    payment_history = []
    for p in payments:
        payment_history.append({
            "amount": p.amount,
            "status": p.status,
            "payment_type": p.payment_type,
            "description": p.description or "",
            "created_at": p.created_at,
            "has_card": bool(p.stripe_payment_method_id),
        })

    return {
        "pc_total_price": total_price,
        "pc_total_paid": total_paid,
        "pc_amount_owed": amount_owed,
        "pc_payment_status": payment_status,
        "pc_detailed_status": detailed_status,
        "pc_legs": leg_data,
        "pc_guest_name": guest_name,
        "pc_guest_phone": guest_phone,
        "pc_guest_email": guest_email,
        "pc_payment_history": payment_history,
        "pc_reservation_uuid": str(reservation.uuid),
        "pc_reservation_id": reservation.id,
        "pc_days_until": meta.get("days_until_pickup", ""),
        "pc_has_saved_card": any(p.stripe_payment_method_id for p in payments),
        "is_payment_chase": True,
    }


def _build_driver_assign_context(task):
    """
    Build context for driver_assign task detail: leg info, available
    in-house drivers with their day load, and quick actions.
    """
    from reservations.models import Leg
    from drivers.models import Driver
    from django.db.models import Count, Q

    leg = task.leg
    if not leg:
        return {}

    pickup_date = leg.pickup_date
    meta = task.metadata or {}

    # Leg details
    customer = leg.reservation.customer if leg.reservation else None
    customer_name = customer.get_full_name() if customer else "Unknown"
    trip_type = leg.get_trip_type() if hasattr(leg, "get_trip_type") else ""

    flight_label = ""
    if leg.flight_information:
        fi = leg.flight_information
        airline = fi.airline_display_name or fi.airline or ""
        flight_label = f"{airline} {fi.flight_number}".strip()

    # Only drivers with a vehicle assignment for the day (i.e., working)
    from datetime import datetime as dt_cls
    from ops.tasks import _estimate_leg_end_time
    from drivers.models import DriverVehicleAssignment

    # Get vehicle assignments for this date
    vehicle_assignments = {
        va.driver_id: va
        for va in DriverVehicleAssignment.objects.filter(date=pickup_date)
        .select_related("vehicle", "vehicle__vehicle_type", "driver")
    }

    drivers = list(
        Driver.objects.filter(
            id__in=vehicle_assignments.keys(),
            driver_type="inhouse",
            profile__is_active=True,
        )
        .select_related("profile")
        .annotate(
            day_legs=Count(
                "legs",
                filter=Q(
                    legs__pickup_date=pickup_date,
                    legs__status__in=["in-progress", "confirmed", "pending"],
                ) & ~Q(legs__reservation__status="cancelled"),
            )
        )
        .order_by("day_legs", "profile__first_name")
    )

    # Fetch all active legs for all drivers on this day (one query)
    all_driver_legs = list(
        Leg.objects.filter(
            driver__in=[d.id for d in drivers],
            pickup_date=pickup_date,
        )
        .exclude(status__in=["completed", "cancelled"])
        .exclude(reservation__status="cancelled")
        .select_related("driver", "reservation", "reservation__customer")
        .order_by("pickup_time")
    )

    # Group legs by driver
    legs_by_driver = {}
    for dl in all_driver_legs:
        legs_by_driver.setdefault(dl.driver_id, []).append(dl)

    # This leg's pickup as datetime for overlap checking
    this_pickup_dt = dt_cls.combine(pickup_date, leg.pickup_time)

    driver_list = []
    for d in drivers:
        d_legs = legs_by_driver.get(d.id, [])

        # Build mini-schedule for this driver
        mini_schedule = []
        latest_end = None
        has_overlap = False
        for dl in d_legs:
            try:
                end_time = _estimate_leg_end_time(dl, pickup_date)
            except Exception:
                end_time = dt_cls.combine(pickup_date, dl.pickup_time) + timedelta(minutes=60)
            if latest_end is None or end_time > latest_end:
                latest_end = end_time

            cust = dl.reservation.customer if dl.reservation else None
            mini_schedule.append({
                "pickup_str": dl.pickup_time.strftime("%I:%M %p").lstrip("0"),
                "end_str": end_time.strftime("%I:%M %p").lstrip("0"),
                "route": f"{(dl.pickup_location or '')[:25]} → {(dl.dropoff_location or '')[:25]}",
                "customer": cust.get_full_name() if cust else "Unknown",
            })

        # Check for time conflict with the unassigned leg
        for dl in d_legs:
            try:
                end_time = _estimate_leg_end_time(dl, pickup_date)
            except Exception:
                end_time = dt_cls.combine(pickup_date, dl.pickup_time) + timedelta(minutes=60)
            dl_pickup_dt = dt_cls.combine(pickup_date, dl.pickup_time)
            # Overlap: driver's leg spans across this leg's pickup time
            if dl_pickup_dt <= this_pickup_dt < end_time:
                has_overlap = True
                break

        # Availability status
        if d.day_legs == 0:
            avail_status = "free"
            avail_label = "Available all day"
        elif has_overlap:
            avail_status = "conflict"
            avail_label = "Busy at pickup time"
        elif latest_end and latest_end <= this_pickup_dt:
            avail_status = "free"
            avail_label = f"Free from {latest_end.strftime('%I:%M %p').lstrip('0')}"
        elif latest_end:
            avail_status = "maybe"
            avail_label = f"Clears ~{latest_end.strftime('%I:%M %p').lstrip('0')}"
        else:
            avail_status = "free"
            avail_label = "Available"

        # Vehicle assignment for the day
        va = vehicle_assignments.get(d.id)
        vehicle_label = ""
        if va and va.vehicle:
            v = va.vehicle
            vtype = v.vehicle_type.vehicle_type if v.vehicle_type else ""
            vehicle_label = f"{vtype} #{v.vehicle_number}".strip() if vtype else f"#{v.vehicle_number}"

        driver_list.append({
            "id": d.id,
            "name": str(d),
            "phone": d.phone_number or "",
            "day_legs": d.day_legs,
            "vehicle_label": vehicle_label,
            "mini_schedule": mini_schedule,
            "avail_status": avail_status,
            "avail_label": avail_label,
            "has_overlap": has_overlap,
        })

    return {
        "da_customer_name": customer_name,
        "da_trip_type": trip_type,
        "da_flight_label": flight_label,
        "da_pickup_date_str": str(pickup_date),
        "da_pickup_time_str": leg.pickup_time.strftime("%I:%M %p").lstrip("0"),
        "da_pickup_location": leg.pickup_location,
        "da_dropoff_location": leg.dropoff_location,
        "da_drivers": driver_list,
        "is_driver_assign": True,
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
            "leg__driver",
            "leg__driver__profile",
            "lead",
            "contact_form",
            "assigned_to",
            "created_by",
            "resolved_by",
            "blocked_by",
        ).prefetch_related(
            "reservation__payments",
        ),
        id=task_id,
    )

    comm_attempts = task.comm_attempts.select_related("staff_user").order_by("-created_at")
    activities = task.staff_activities.select_related("user").order_by("-created_at")[:20]

    ops_staff = (
        User.objects.filter(is_superuser=True, is_active=True)
        .order_by("first_name", "username")
        .values("id", "first_name", "username")
    )

    context = {
        "task": task,
        "comm_attempts": comm_attempts,
        "activities": activities,
        "channels": CommunicationAttempt.Channel.choices,
        "outcomes": CommunicationAttempt.Outcome.choices,
        "ops_staff": list(ops_staff),
    }

    # ── Task-type-specific context ──
    if task.task_type == OperationalTask.TaskType.DRIVER_CONFLICT and task.leg:
        context.update(_build_driver_conflict_context(task))
    elif task.task_type == OperationalTask.TaskType.FLIGHT_VERIFICATION and task.leg:
        context.update(_build_flight_verify_context(task))
    elif task.task_type == OperationalTask.TaskType.PAYMENT_CHASE and task.reservation:
        context.update(_build_payment_chase_context(task))
    elif task.task_type == OperationalTask.TaskType.DRIVER_ASSIGNMENT and task.leg:
        context.update(_build_driver_assign_context(task))

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

    # ── Staff activity timeline (full range, not just today) ──
    today_start = now.astimezone(eastern).replace(hour=0, minute=0, second=0, microsecond=0)
    range_activities = list(
        StaffActivity.objects.filter(
            created_at__gte=range_start,
        )
        .exclude(action_type=StaffActivity.ActionType.PAGE_VIEW)
        .select_related("user", "task")
        .order_by("-created_at")[:200]
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

    # ── First / last active per staff (today) ──
    staff_active_times = {
        row["user__id"]: row
        for row in StaffActivity.objects.filter(
            created_at__gte=today_start,
        )
        .values("user__id", "user__first_name", "user__username")
        .annotate(first_active=Min("created_at"), last_active=Max("created_at"))
        .order_by("first_active")
    }
    for row in staff_active_times.values():
        row["name"] = row["user__first_name"] or row["user__username"]

    # ── AuditLog actions per staff (in range) ──
    from reservations.models import AuditLog
    audit_actions = list(
        AuditLog.objects.filter(
            timestamp__gte=range_start,
            user__isnull=False,
        )
        .values("user__id", "user__first_name", "user__username")
        .annotate(
            driver_assignments=Count("id", filter=Q(action="driver_assigned")),
            payment_actions=Count("id", filter=Q(action="payment_processed")),
            status_changes=Count("id", filter=Q(action="status_changed")),
            total_actions=Count("id"),
        )
        .order_by("-total_actions")
    )
    for row in audit_actions:
        row["name"] = row["user__first_name"] or row["user__username"]

    # ── Emails sent per staff (in range) ──
    from .models import EmailLog
    staff_emails = list(
        EmailLog.objects.filter(
            sent_at__gte=range_start,
            sent_by__isnull=False,
        )
        .values("sent_by__id", "sent_by__first_name", "sent_by__username")
        .annotate(
            total=Count("id"),
            confirmations=Count("id", filter=Q(email_type="confirmation")),
            payment_reminders=Count("id", filter=Q(email_type="payment_reminder")),
            statements=Count("id", filter=Q(
                email_type__in=["driver_statement", "agent_commission", "agency_commission"]
            )),
        )
        .order_by("-total")
    )
    for row in staff_emails:
        row["name"] = row["sent_by__first_name"] or row["sent_by__username"]

    # ── Reservations / Legs modified per staff (in range) ──
    from reservations.models import Reservation, Leg
    from django.db.models import Sum, DecimalField
    from django.db.models.functions import Coalesce
    from decimal import Decimal

    staff_modifications = {}
    # Count distinct legs modified per staff
    leg_mods = (
        Leg.history.filter(
            history_date__gte=range_start,
            history_type="~",
            history_user__isnull=False,
        )
        .values("history_user__id", "history_user__first_name", "history_user__username")
        .annotate(legs_modified=Count("id", distinct=True))
    )
    for row in leg_mods:
        uid = row["history_user__id"]
        staff_modifications[uid] = {
            "user_id": uid,
            "name": row["history_user__first_name"] or row["history_user__username"],
            "legs_modified": row["legs_modified"],
            "reservations_modified": 0,
        }
    # Count distinct reservations modified per staff
    res_mods = (
        Reservation.history.filter(
            history_date__gte=range_start,
            history_type="~",
            history_user__isnull=False,
        )
        .values("history_user__id", "history_user__first_name", "history_user__username")
        .annotate(res_modified=Count("id", distinct=True))
    )
    for row in res_mods:
        uid = row["history_user__id"]
        if uid in staff_modifications:
            staff_modifications[uid]["reservations_modified"] = row["res_modified"]
        else:
            staff_modifications[uid] = {
                "user_id": uid,
                "name": row["history_user__first_name"] or row["history_user__username"],
                "legs_modified": 0,
                "reservations_modified": row["res_modified"],
            }
    staff_modifications_list = sorted(
        staff_modifications.values(),
        key=lambda x: x["legs_modified"] + x["reservations_modified"],
        reverse=True,
    )

    # ── Correction / override detection ──
    # Find cases where the same field on the same Leg was changed by different
    # users within 24 hours (indicating a correction or management override).
    corrections = []
    CORRECTION_FIELDS = {
        "pickup_time", "pickup_date", "pickup_location", "dropoff_location",
        "driver", "status", "total_price", "base_price", "gratuity_amount",
        "passenger_count", "private_notes",
    }
    leg_history_in_range = (
        Leg.history.filter(
            history_date__gte=range_start,
            history_type="~",
            history_user__isnull=False,
        )
        .select_related("history_user")
        .order_by("id", "history_date")[:500]
    )
    # Group history records by leg id
    from itertools import groupby
    leg_records_by_id = defaultdict(list)
    for rec in leg_history_in_range:
        leg_records_by_id[rec.id].append(rec)

    for leg_id, records in leg_records_by_id.items():
        for i in range(1, len(records)):
            rec = records[i]
            prev = records[i - 1]
            # Different users and within 24 hours
            if (
                rec.history_user_id != prev.history_user_id
                and (rec.history_date - prev.history_date).total_seconds() < 86400
            ):
                try:
                    delta = rec.diff_against(prev)
                except Exception:
                    continue
                for change in delta.changes:
                    if change.field in CORRECTION_FIELDS:
                        corrections.append({
                            "timestamp": rec.history_date,
                            "model": "Leg",
                            "object_id": leg_id,
                            "field": change.field,
                            "original_by": prev.history_user.first_name or prev.history_user.username,
                            "corrected_by": rec.history_user.first_name or rec.history_user.username,
                            "old": str(change.old) if change.old is not None else "",
                            "new": str(change.new) if change.new is not None else "",
                            "reservation_id": rec.reservation_id,
                        })
    corrections.sort(key=lambda x: x["timestamp"], reverse=True)

    staff_reservations = list(
        Reservation.objects.filter(
            created_at__gte=range_start,
            created_by__isnull=False,
        )
        .values("created_by__id", "created_by__first_name", "created_by__username")
        .annotate(
            count=Count("id"),
            revenue=Coalesce(Sum("total_price"), Decimal("0"), output_field=DecimalField()),
        )
        .order_by("-revenue")
    )
    for s in staff_reservations:
        s["name"] = s["created_by__first_name"] or s["created_by__username"]

    total_staff_reservations = sum(s["count"] for s in staff_reservations)
    total_staff_revenue = sum(s["revenue"] for s in staff_reservations)

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
        # Activity timeline (full range)
        "range_activities": range_activities,
        "page_views_today": page_views_today,
        # Type performance
        "type_performance": type_performance,
        "task_type_labels": dict(OperationalTask.TaskType.choices),
        "priority_labels": dict(OperationalTask.Priority.choices),
        # Staff reservations
        "staff_reservations": staff_reservations,
        "total_staff_reservations": total_staff_reservations,
        "total_staff_revenue": total_staff_revenue,
        # First/last active today
        "staff_active_times": staff_active_times,
        # AuditLog actions per staff
        "audit_actions": audit_actions,
        # Modifications per staff
        "staff_modifications": staff_modifications_list,
        # Corrections / overrides
        "corrections": corrections,
        # Emails sent per staff
        "staff_emails": staff_emails,
    }
    return render(request, "dispatching/staff_metrics.html", context)


@login_required(login_url="login")
@user_passes_test(_is_superuser, login_url="dashboard")
def staff_detail_view(request, user_id):
    """
    Per-staff detail page: reservations created, revenue, tasks resolved,
    communication history, and activity timeline.
    """
    import pytz
    from reservations.models import Reservation
    from django.db.models import Sum, DecimalField
    from django.db.models.functions import Coalesce, TruncDate
    from decimal import Decimal

    staff_user = get_object_or_404(User, id=user_id)
    eastern = pytz.timezone("US/Eastern")
    now = timezone.now()
    today = timezone.localdate()

    # Support three modes:
    #   ?date=2026-03-15           (single day)
    #   ?from=2026-04-01&to=2026-04-05  (custom range)
    #   ?range=30                  (rolling N days, default)
    from datetime import date as date_type
    date_param = request.GET.get("date", "")
    from_param = request.GET.get("from", "")
    to_param = request.GET.get("to", "")
    view_date = None
    custom_from = None
    custom_to = None

    if date_param:
        try:
            view_date = date_type.fromisoformat(date_param)
        except ValueError:
            pass

    if from_param and to_param:
        try:
            custom_from = date_type.fromisoformat(from_param)
            custom_to = date_type.fromisoformat(to_param)
            if custom_from > custom_to:
                custom_from, custom_to = custom_to, custom_from
        except ValueError:
            custom_from = custom_to = None

    if view_date:
        # Single-day mode
        range_start = timezone.make_aware(
            timezone.datetime.combine(view_date, timezone.datetime.min.time()),
            timezone.get_current_timezone(),
        )
        range_end = range_start + timedelta(days=1)
        days_back = 0  # signals single-day mode in template
    elif custom_from and custom_to:
        # Custom date range mode
        range_start = timezone.make_aware(
            timezone.datetime.combine(custom_from, timezone.datetime.min.time()),
            timezone.get_current_timezone(),
        )
        range_end = timezone.make_aware(
            timezone.datetime.combine(custom_to + timedelta(days=1), timezone.datetime.min.time()),
            timezone.get_current_timezone(),
        )
        days_back = (custom_to - custom_from).days + 1
    else:
        range_param = request.GET.get("range", "14")
        try:
            days_back = int(range_param)
        except ValueError:
            days_back = 14
        days_back = min(days_back, 365)
        range_start = now - timedelta(days=days_back)
        range_end = now

    # ── Reservations created by this staff ──
    staff_res = Reservation.objects.filter(
        created_by=staff_user,
        created_at__gte=range_start,
        created_at__lt=range_end,
    ).select_related("customer").order_by("-created_at")

    res_count = staff_res.count()
    res_revenue = staff_res.aggregate(
        total=Coalesce(Sum("total_price"), Decimal("0"), output_field=DecimalField())
    )["total"]

    # Recent reservations (last 25)
    recent_reservations = list(staff_res[:25])

    # Daily reservation trend — single query, build both dicts
    daily_stats = (
        staff_res.annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(
            count=Count("id"),
            rev=Coalesce(Sum("total_price"), Decimal("0"), output_field=DecimalField()),
        )
        .order_by("day")
    )
    daily_res = {}
    daily_rev = {}
    for row in daily_stats:
        daily_res[row["day"]] = row["count"]
        daily_rev[row["day"]] = row["rev"]

    if custom_from and custom_to:
        trend_start = custom_from
    elif view_date:
        trend_start = view_date
    else:
        trend_start = today - timedelta(days=days_back - 1)

    res_trend = []
    for i in range(days_back):
        d = trend_start + timedelta(days=i)
        res_trend.append({
            "day": d.isoformat(),
            "count": daily_res.get(d, 0),
            "revenue": float(daily_rev.get(d, 0)),
        })

    # ── Tasks resolved by this staff ──
    resolved_tasks = OperationalTask.objects.filter(
        resolved_by=staff_user,
        resolved_at__gte=range_start,
        resolved_at__lt=range_end,
    ).order_by("-resolved_at")

    tasks_resolved_count = resolved_tasks.count()
    resolved_by_type = dict(
        resolved_tasks.values_list("task_type")
        .annotate(c=Count("id"))
        .values_list("task_type", "c")
    )

    recent_resolved = list(
        resolved_tasks.select_related("reservation", "reservation__customer", "leg")[:25]
    )

    # ── Tasks currently assigned to this staff ──
    assigned_tasks = list(
        OperationalTask.objects.filter(
            assigned_to=staff_user,
            status__in=list(OperationalTask.OPEN_STATUSES),
        )
        .select_related("reservation", "reservation__customer", "leg")
        .order_by("priority", "due_at")[:20]
    )
    # Add age_days for staleness badges in template
    for task in assigned_tasks:
        task.age_days = (now - task.created_at).days

    # ── Communication attempts by this staff ──
    staff_comms = CommunicationAttempt.objects.filter(
        staff_user=staff_user,
        created_at__gte=range_start,
        created_at__lt=range_end,
    )
    comms_total = staff_comms.count()
    comms_by_channel = dict(
        staff_comms.values_list("channel")
        .annotate(c=Count("id"))
        .values_list("channel", "c")
    )
    comms_by_outcome = dict(
        staff_comms.values_list("outcome")
        .annotate(c=Count("id"))
        .values_list("outcome", "c")
    )
    recent_comms = list(
        staff_comms.select_related("task").order_by("-created_at")[:25]
    )

    # ── Emails sent by this staff ──
    staff_email_qs = EmailLog.objects.filter(
        sent_by=staff_user,
        sent_at__gte=range_start,
        sent_at__lt=range_end,
    )
    emails_total = staff_email_qs.count()
    emails_by_type = dict(
        staff_email_qs.values_list("email_type")
        .annotate(c=Count("id"))
        .values_list("email_type", "c")
    )
    recent_emails = list(
        staff_email_qs.select_related("reservation").order_by("-sent_at")[:25]
    )

    # ── Activity timeline (last 50) ──
    recent_activities = list(
        StaffActivity.objects.filter(
            user=staff_user,
            created_at__gte=range_start,
            created_at__lt=range_end,
        )
        .exclude(action_type=StaffActivity.ActionType.PAGE_VIEW)
        .select_related("task")
        .order_by("-created_at")[:50]
    )

    # ── Change history from django-simple-history ──
    from reservations.models import Leg, Reservation as Res

    # Fields worth showing (skip noisy internal fields)
    INTERESTING_FIELDS = {
        "pickup_time", "pickup_date", "pickup_location", "dropoff_location",
        "driver", "status", "total_price", "base_price", "gratuity_amount",
        "passenger_count", "luggage_count", "private_notes",
        "driver_base_pay", "driver_gratuity", "driver_additional",
        "flight_information",
    }

    # Fields auto-set by signals when a trigger field changes — suppress from
    # change history so only the real staff action (e.g. driver reassignment)
    # is shown.
    SIGNAL_CASCADED_PAIRS = {
        "driver": {"driver_base_pay", "driver_gratuity", "driver_additional"},
    }

    change_history = []

    # Pre-load driver names for resolving FK IDs in change history
    from drivers.models import Driver
    driver_name_map = dict(
        Driver.objects.values_list("id", "profile__first_name")
    )
    # Fallback: also get last names for drivers without first names
    driver_last_map = dict(
        Driver.objects.values_list("id", "profile__last_name")
    )

    def _resolve_driver(val):
        """Convert a driver FK ID to a display name."""
        if not val:
            return ""
        try:
            did = int(val)
        except (ValueError, TypeError):
            return str(val)
        name = driver_name_map.get(did, "")
        if name:
            last = driver_last_map.get(did, "")
            return f"{name} {last}".strip() if last else name
        return f"Driver #{did}"

    def _format_time(val):
        """Convert military time string (HH:MM:SS) to 12-hour format."""
        if not val:
            return ""
        s = str(val)
        try:
            from datetime import time as time_type
            if isinstance(val, time_type):
                return val.strftime("%I:%M %p").lstrip("0")
            # Parse string like "22:45:00"
            parts = s.split(":")
            h, m = int(parts[0]), int(parts[1])
            t = time_type(h, m)
            return t.strftime("%I:%M %p").lstrip("0")
        except Exception:
            return s

    def _format_value(field, val):
        """Format a change value based on field type."""
        if not val and val != 0:
            return ""
        if field == "driver":
            return _resolve_driver(val)
        if field == "pickup_time":
            return _format_time(val)
        return str(val)

    # Pre-load reservation UUIDs for linking
    # Collect all reservation IDs we'll need, then batch-fetch UUIDs
    _res_ids_needed = set()

    # Leg changes by this user
    leg_changes = (
        Leg.history.filter(
            history_user=staff_user,
            history_date__gte=range_start,
            history_date__lt=range_end,
            history_type="~",  # only updates, not creates
        )
        .select_related("history_user")
        .order_by("-history_date")[:100]
    )
    for rec in leg_changes:
        prev = rec.prev_record
        if not prev:
            continue
        try:
            delta = rec.diff_against(prev)
        except Exception:
            continue
        # Collect all changed field names in this record to detect signal cascades
        changed_fields = {c.field for c in delta.changes}
        suppressed = set()
        for trigger, cascaded in SIGNAL_CASCADED_PAIRS.items():
            if trigger in changed_fields:
                suppressed |= cascaded
        for change in delta.changes:
            if change.field not in INTERESTING_FIELDS:
                continue
            if change.field in suppressed:
                continue
            if rec.reservation_id:
                _res_ids_needed.add(rec.reservation_id)
            change_history.append({
                "timestamp": rec.history_date,
                "model": "Leg",
                "object_id": rec.id,
                "field": change.field,
                "old": _format_value(change.field, change.old),
                "new": _format_value(change.field, change.new),
                "reservation_id": rec.reservation_id,
            })

    # Reservation changes by this user
    res_changes = (
        Res.history.filter(
            history_user=staff_user,
            history_date__gte=range_start,
            history_date__lt=range_end,
            history_type="~",
        )
        .select_related("history_user")
        .order_by("-history_date")[:100]
    )
    for rec in res_changes:
        prev = rec.prev_record
        if not prev:
            continue
        try:
            delta = rec.diff_against(prev)
        except Exception:
            continue
        _res_ids_needed.add(rec.id)
        for change in delta.changes:
            if change.field not in INTERESTING_FIELDS:
                continue
            change_history.append({
                "timestamp": rec.history_date,
                "model": "Reservation",
                "object_id": rec.id,
                "field": change.field,
                "old": _format_value(change.field, change.old),
                "new": _format_value(change.field, change.new),
                "reservation_id": rec.id,
            })

    # Batch-fetch reservation UUIDs for clickable links
    res_uuid_map = dict(
        Res.objects.filter(id__in=_res_ids_needed).values_list("id", "uuid")
    )
    for entry in change_history:
        entry["reservation_uuid"] = str(res_uuid_map.get(entry["reservation_id"], ""))

    # Sort all changes by timestamp descending
    change_history.sort(key=lambda x: x["timestamp"], reverse=True)

    # ── All staff users (for sidebar navigation) ──
    all_staff = list(
        User.objects.filter(is_staff=True, is_active=True)
        .order_by("first_name", "username")
    )

    # Date navigation helpers
    yesterday = today - timedelta(days=1)

    context = {
        "staff_user": staff_user,
        "range_days": days_back,
        "range_start": range_start,
        "view_date": view_date,
        "custom_from": custom_from,
        "custom_to": custom_to,
        "today": today,
        "yesterday": yesterday,
        # Reservations
        "res_count": res_count,
        "res_revenue": res_revenue,
        "recent_reservations": recent_reservations,
        "res_trend_json": json.dumps(res_trend),
        # Tasks
        "tasks_resolved_count": tasks_resolved_count,
        "resolved_by_type": resolved_by_type,
        "recent_resolved": recent_resolved,
        "assigned_tasks": assigned_tasks,
        "task_type_labels": dict(OperationalTask.TaskType.choices),
        # Communication
        "comms_total": comms_total,
        "comms_by_channel": comms_by_channel,
        "comms_by_outcome": comms_by_outcome,
        "recent_comms": recent_comms,
        # Emails
        "emails_total": emails_total,
        "emails_by_type": emails_by_type,
        "recent_emails": recent_emails,
        "email_type_labels": dict(EmailLog.EmailType.choices),
        # Activity & Changes
        "recent_activities": recent_activities,
        "change_history": change_history,
        # Navigation
        "all_staff": all_staff,
    }
    return render(request, "dispatching/staff_detail.html", context)
