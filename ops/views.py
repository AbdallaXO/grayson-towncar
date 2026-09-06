"""
Views for the operational task queue and related API endpoints.
"""

import csv
import io
import json
import logging
from datetime import timedelta, datetime, time
from collections import defaultdict

from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth import get_user_model
from django.db.models import Count, Avg, Q, F, Min, Max
from django.db.models.functions import TruncDate
from django.http import Http404, JsonResponse, HttpResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST, require_http_methods
from django.contrib import messages

from .models import (
    OperationalTask,
    CommunicationAttempt,
    StaffActivity,
    EmailLog,
    TimeClockShift,
    TimeClockBreak,
    StaffWeeklySchedule,
    StaffScheduleOverride,
    StaffOnCall,
    StaffExtraShift,
    TimeClockRequest,
    STAFF_ROLE_CHOICES,
    WORK_LOCATION_CHOICES,
    WORK_LOCATION_SHORT,
)
from .services import close_task, cancel_task, log_communication, create_task
from .services import (
    clock_out as tc_clock_out,
    start_break as tc_start_break,
    end_break as tc_end_break,
    get_open_shift,
    auto_close_stale_shifts,
    TimeClockError,
    admin_punch_in,
    admin_punch_out,
    admin_create_shift,
    admin_update_shift,
    admin_delete_shift,
    admin_add_break,
    admin_update_break,
    admin_delete_break,
    clock_in_or_request,
    cancel_clock_in_request,
    clock_in_request_state,
    approve_clock_in_request,
    deny_clock_in_request,
    expire_stale_requests,
    GRANT_VALID_MIN,
)
from . import scheduling
from . import coverage
from . import timeoff

User = get_user_model()
logger = logging.getLogger(__name__)


def _is_superuser(user):
    return user.is_superuser


def _is_staff(user):
    return user.is_staff or user.is_superuser


@login_required(login_url="login")
@user_passes_test(_is_staff, login_url="login")
def task_queue_view(request):
    """
    Operations command center — shared work queue with claim-based workflow.

    Renders five lanes (Unclaimed, Mine, Others, Waiting, Future Blockers)
    plus a Completed-Today section and a "Next Up" anchor card. All lanes
    share one query and one in-memory partitioning pass to keep queries flat.

    Query params:
      lane=unclaimed|mine|others|waiting|future|completed  (default: unclaimed)
      type=<task_type>
      q=<search>     # title / description / customer name / R<id> / L<id>
      overdue=1
    """
    from django.db.models import Count, Q

    type_filter = (request.GET.get("type") or "").strip()
    lane = (request.GET.get("lane") or "unclaimed").strip()
    search_q = (request.GET.get("q") or "").strip()
    overdue_only = request.GET.get("overdue") == "1"

    now = timezone.now()
    today = timezone.localdate()

    # ── Single base queryset for everything that's open ─────────────────────
    open_statuses = list(OperationalTask.OPEN_STATUSES)

    base_qs = (
        OperationalTask.objects.filter(status__in=open_statuses)
        .exclude(
            # Past-date driver_assign tasks are noise (the auto-closer kills
            # them on the next cycle, but hide them from the UI immediately).
            task_type=OperationalTask.TaskType.DRIVER_ASSIGNMENT,
            leg__pickup_date__lt=today,
        )
        .select_related(
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
        )
        .order_by("priority", "due_at")
    )

    if type_filter:
        base_qs = base_qs.filter(task_type=type_filter)
    if overdue_only:
        base_qs = base_qs.filter(due_at__lt=now)
    if search_q:
        # Match title, description, customer first/last, and Rxx / Lxx tokens
        q = Q(title__icontains=search_q) | Q(description__icontains=search_q)
        q |= Q(reservation__customer__first_name__icontains=search_q)
        q |= Q(reservation__customer__last_name__icontains=search_q)
        q |= Q(leg__reservation__customer__first_name__icontains=search_q)
        q |= Q(leg__reservation__customer__last_name__icontains=search_q)
        digits = "".join(ch for ch in search_q if ch.isdigit())
        if digits:
            try:
                num = int(digits)
                q |= Q(reservation_id=num) | Q(leg_id=num) | Q(leg__reservation_id=num)
            except (ValueError, OverflowError):
                pass
        base_qs = base_qs.filter(q).distinct()

    all_open = list(base_qs)

    # ── Partition into lanes ────────────────────────────────────────────────
    user_id = request.user.id
    unclaimed, mine, others, waiting, future_blockers = [], [], [], [], []

    for t in all_open:
        # Waiting takes priority over other classifications — a snoozed or
        # blocked task is intentionally parked, regardless of who owns it.
        is_waiting = (
            t.status == OperationalTask.Status.SNOOZED
            or (t.blocked_by_id and t.blocked_by and t.blocked_by.is_open)
        )
        if is_waiting:
            waiting.append(t)
            continue

        # Future schedule blockers — an open task on a leg booked for a
        # future date. These are the "schedule-risk" items the owner asked
        # for. Same-day items belong in the main lanes.
        leg_date = t.leg.pickup_date if t.leg_id and t.leg else None
        if not leg_date and t.reservation_id and t.reservation:
            # Reservation-only tasks (e.g. payment_chase) — use earliest
            # upcoming leg from prefetched metadata if present.
            meta_pickup = (t.metadata or {}).get("earliest_pickup")
            if meta_pickup:
                try:
                    from datetime import date as _date
                    leg_date = _date.fromisoformat(meta_pickup)
                except (ValueError, TypeError):
                    leg_date = None
        is_future = leg_date and leg_date > today
        if is_future:
            future_blockers.append(t)

        # Ownership lanes (a task can be in both future_blockers AND one of
        # the ownership lanes — they're orthogonal views).
        if t.assigned_to_id is None:
            unclaimed.append(t)
        elif t.assigned_to_id == user_id:
            mine.append(t)
        else:
            others.append(t)

    # ── Completed today ────────────────────────────────────────────────────
    # Local-day range so "today" matches the dispatcher's clock, not UTC.
    local_today = timezone.localtime(now).date()
    tz_local = timezone.get_current_timezone()
    day_start = timezone.make_aware(
        datetime.combine(local_today, datetime.min.time()), tz_local
    )
    completed_today_qs = (
        OperationalTask.objects.filter(
            status=OperationalTask.Status.COMPLETED,
            resolved_at__gte=day_start,
        )
        .select_related(
            "reservation",
            "reservation__customer",
            "leg",
            "leg__reservation",
            "leg__reservation__customer",
            "resolved_by",
        )
        .order_by("-resolved_at")
    )
    completed_today = list(completed_today_qs[:30])
    completed_today_count = completed_today_qs.count()

    # ── Lane selection for the active tab ──────────────────────────────────
    lane_tasks = {
        "unclaimed": unclaimed,
        "mine": mine,
        "others": others,
        "waiting": waiting,
        "future": future_blockers,
        "completed": completed_today,
    }
    if lane not in lane_tasks:
        lane = "unclaimed"
    active_tasks = lane_tasks[lane]

    # Group the active lane by priority — keeps the visual hierarchy that
    # already works on the queue but applied to a single lane at a time.
    priority_config = [
        (1, "critical", "Critical", "Needs immediate action"),
        (2, "high", "High", "Address within a few hours"),
        (3, "medium", "Medium", "Handle today"),
        (4, "low", "Low", "When time permits"),
    ]
    priority_groups = []
    if lane != "completed":
        for pval, key, label, hint in priority_config:
            group_tasks = [t for t in active_tasks if t.priority == pval]
            if not group_tasks:
                continue
            group_tasks.sort(key=lambda t: (t.due_at, t.task_type))
            priority_groups.append({
                "priority": pval,
                "key": key,
                "label": label,
                "hint": hint,
                "tasks": group_tasks,
            })

    # ── "Next Up" anchor: the single most-urgent unclaimed task ─────────────
    # If the user already has active tasks, anchor on their most-urgent one
    # instead so the page tells them to finish what they started before
    # claiming more.
    next_up = None
    if mine:
        next_up = mine[0]  # already priority-sorted
    elif unclaimed:
        next_up = unclaimed[0]

    # ── Summary counts (global, not filtered by type/search) ────────────────
    summary_qs = OperationalTask.objects.filter(status__in=open_statuses).exclude(
        task_type=OperationalTask.TaskType.DRIVER_ASSIGNMENT,
        leg__pickup_date__lt=today,
    )
    type_counts = dict(
        summary_qs.values_list("task_type")
        .annotate(c=Count("id"))
        .values_list("task_type", "c")
    )
    total_open = sum(type_counts.values())
    overdue_count = summary_qs.filter(due_at__lt=now).count()

    ops_staff = list(
        User.objects.filter(is_staff=True, is_active=True)
        .order_by("first_name", "username")
        .values("id", "first_name", "username")
    )

    lane_meta = [
        {"key": "unclaimed", "label": "Unclaimed", "count": len(unclaimed),
         "hint": "Open work — grab one"},
        {"key": "mine", "label": "Mine", "count": len(mine),
         "hint": "Tasks you've claimed"},
        {"key": "others", "label": "Others", "count": len(others),
         "hint": "Claimed by teammates"},
        {"key": "waiting", "label": "Waiting", "count": len(waiting),
         "hint": "Snoozed or blocked"},
        {"key": "future", "label": "Future Blockers", "count": len(future_blockers),
         "hint": "Issues on upcoming trips"},
        {"key": "completed", "label": "Completed Today", "count": completed_today_count,
         "hint": "Today's wins"},
    ]

    context = {
        "active_lane": lane,
        "lane_meta": lane_meta,
        "priority_groups": priority_groups,
        "active_tasks": active_tasks,
        "completed_today": completed_today,
        "completed_today_count": completed_today_count,
        "next_up": next_up,
        "now": now,
        "today": today,
        "type_filter": type_filter,
        "search_q": search_q,
        "overdue_only": overdue_only,
        "type_counts": type_counts,
        "total_open": total_open,
        "overdue_count": overdue_count,
        "unclaimed_count": len(unclaimed),
        "mine_count": len(mine),
        "others_count": len(others),
        "waiting_count": len(waiting),
        "future_blockers_count": len(future_blockers),
        "task_types": OperationalTask.TaskType.choices,
        "priorities": OperationalTask.Priority.choices,
        "ops_staff": ops_staff,
    }
    return render(request, "dispatching/task_queue.html", context)


@login_required(login_url="login")
@user_passes_test(_is_staff, login_url="login")
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
    task.assigned_at = timezone.now()
    task.status = OperationalTask.Status.IN_PROGRESS
    task.save(update_fields=["assigned_to", "assigned_at", "status", "updated_at"])

    StaffActivity.objects.create(
        user=request.user,
        action_type=StaffActivity.ActionType.TASK_CLAIMED,
        task=task,
    )

    return JsonResponse({"success": True, "task_id": task.id})


@login_required(login_url="login")
@user_passes_test(_is_staff, login_url="login")
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
@user_passes_test(_is_staff, login_url="login")
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
@user_passes_test(_is_staff, login_url="login")
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
        assignee = get_object_or_404(User, id=user_id, is_active=True)
        task.assigned_to = assignee
        task.assigned_at = timezone.now()
        label = assignee.first_name or assignee.username
    else:
        task.assigned_to = None
        task.assigned_at = None
        label = None

    if task.status == OperationalTask.Status.PENDING:
        task.status = OperationalTask.Status.IN_PROGRESS
        task.save(update_fields=["assigned_to", "assigned_at", "status", "updated_at"])
    else:
        task.save(update_fields=["assigned_to", "assigned_at", "updated_at"])

    StaffActivity.objects.create(
        user=request.user,
        action_type=StaffActivity.ActionType.TASK_ASSIGNED,
        task=task,
        metadata={"assigned_to": label or "unassigned"},
    )

    return JsonResponse({"success": True, "task_id": task.id, "assigned_to": label})


@login_required(login_url="login")
@user_passes_test(_is_staff, login_url="login")
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
@user_passes_test(_is_staff, login_url="login")
@require_POST
def task_release(request):
    """
    Release a claimed task back to the unclaimed queue.
    Clears assigned_to and returns status to PENDING so another staff member can claim it.
    """
    try:
        data = json.loads(request.body)
        task_id = data.get("task_id")
        reason = data.get("reason", "")
    except (json.JSONDecodeError, AttributeError):
        task_id = request.POST.get("task_id")
        reason = request.POST.get("reason", "")

    task = get_object_or_404(OperationalTask, id=task_id)

    if not task.is_open:
        return JsonResponse({"success": False, "error": "Task is not open"})

    previous_assignee = task.assigned_to
    task.assigned_to = None
    task.assigned_at = None
    if task.status == OperationalTask.Status.IN_PROGRESS:
        task.status = OperationalTask.Status.PENDING
    task.save(update_fields=["assigned_to", "assigned_at", "status", "updated_at"])

    StaffActivity.objects.create(
        user=request.user,
        action_type=StaffActivity.ActionType.TASK_ASSIGNED,
        task=task,
        metadata={
            "released_from": (
                previous_assignee.first_name or previous_assignee.username
                if previous_assignee else None
            ),
            "reason": reason,
            "released": True,
        },
    )

    return JsonResponse({"success": True, "task_id": task.id})


@login_required(login_url="login")
@user_passes_test(_is_staff, login_url="login")
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
@user_passes_test(_is_staff, login_url="login")
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
@user_passes_test(_is_staff, login_url="login")
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
@user_passes_test(_is_staff, login_url="login")
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

    # Manual tasks default to MANUAL but staff can pick any operational type
    # so the task lands in the right typed lane (e.g. payment_chase for a
    # one-off "remind this guest to pay" task).
    requested_type = (data.get("task_type") or OperationalTask.TaskType.MANUAL).strip()
    valid_types = {choice[0] for choice in OperationalTask.TaskType.choices}
    task_type = requested_type if requested_type in valid_types else OperationalTask.TaskType.MANUAL

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
        task_type=task_type,
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
                assignee = User.objects.get(id=assign_to_id, is_active=True)
                task.assigned_to = assignee
                task.assigned_at = timezone.now()
                task.status = OperationalTask.Status.IN_PROGRESS
                task.save(update_fields=["assigned_to", "assigned_at", "status", "updated_at"])
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
        categorize_day_type,
        leg_time_of_day_category,
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
        time_cat = leg_time_of_day_category(leg)
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
    redesign = {}
    trigger_entry = next((e for e in schedule if e["is_trigger"]), None)
    conflicting_entry = next((e for e in schedule if e["is_conflicting"]), None)
    if trigger_entry and conflicting_entry:
        try:
            from dispatching.scheduler import get_drive_time
            from dispatching.analytics import (
                categorize_location,
                categorize_day_type,
                leg_time_of_day_category,
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
                time_cat = leg_time_of_day_category(first_leg)
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

            # ── Redesign v3: headline metric (driver-at-curb vs flight gate) + a
            #    fully dynamic two-lane timeline. All positions are precomputed as
            #    percentages of the window so the template stays arithmetic-free.
            _gate_dt = (
                datetime.combine(pickup_date, flight_dt.time())
                if (second_is_arrival and flight_dt) else None
            )
            _prior_pickup = datetime.combine(pickup_date, first_leg.pickup_time)
            _arr_drive = second.get("drive_minutes") or 0
            _arr_end = earliest_arrival + timedelta(minutes=_arr_drive)
            _behind_gate = (
                int((earliest_arrival - _gate_dt).total_seconds() / 60) if _gate_dt else None
            )

            def _mins(dt):
                return dt.hour * 60 + dt.minute + dt.second / 60.0

            _events = [_prior_pickup, clears_at, earliest_arrival, second_pickup, _arr_end]
            if _gate_dt:
                _events.append(_gate_dt)
            _lo = min(_mins(e) for e in _events) - 5
            _lo -= _lo % 15
            _hi = max(_mins(e) for e in _events) + 5
            _hi += (15 - (_hi % 15)) % 15
            _win = max(_hi - _lo, 15)

            def _pct(dt):
                return round((_mins(dt) - _lo) / _win * 100, 2)

            def _bar(a, b):
                _l = _pct(a)
                return {"left": _l, "width": round(_pct(b) - _l, 2)}

            _ticks = []
            _t = _lo
            while _t <= _hi + 0.01:
                _h = int(_t // 60) % 24
                _ticks.append({
                    "pct": round((_t - _lo) / _win * 100, 2),
                    "label": f"{_h % 12 or 12}:{int(_t % 60):02d}",
                })
                _t += 15

            # No gate anchor (two departures / pure overlap) → "ETA mode": the
            # story is booked pickup vs driver ETA. A small shortfall is a
            # monitor-first situation, not a reassign.
            _monitor_first = _gate_dt is None and late_minutes <= 10

            redesign = {
                "behind_gate": _behind_gate,
                "monitor_first": _monitor_first,
                "driver_curb_str": earliest_arrival.strftime("%I:%M %p").lstrip("0"),
                "gate_str": flight_gate_str,
                "booked_str": second_pickup.strftime("%I:%M %p").lstrip("0"),
                "reposition_min": travel_minutes,
                "prior_label": first["customer_name"],
                "prior_pickup_str": _prior_pickup.strftime("%I:%M %p").lstrip("0"),
                "prior_pickup_loc": first_leg.pickup_location or "",
                "prior_dropoff_loc": first_leg.dropoff_location or "",
                "prior_route": f"{first_leg.pickup_location} → {first_leg.dropoff_location}",
                "prior_clear_str": clears_at.strftime("%I:%M %p").lstrip("0"),
                "arr_label": second["customer_name"],
                "arr_pickup_loc": second_leg.pickup_location or "",
                "arr_route": f"{second_leg.pickup_location} → {second_leg.dropoff_location}",
                "flight_label": second["flight_label"],
                "timeline": {
                    "ticks": _ticks,
                    "driver_prior": _bar(_prior_pickup, clears_at),
                    "driver_reposition": _bar(clears_at, earliest_arrival),
                    "driver_arrival": _bar(earliest_arrival, _arr_end),
                    "guest_terminal": _bar(_gate_dt, earliest_arrival) if _gate_dt else None,
                    "guest_enroute": _bar(earliest_arrival, _arr_end),
                    # Shortfall band: gate → driver-free in gate mode; booked
                    # pickup → driver-free in ETA mode (guest waiting).
                    "band": (
                        _bar(_gate_dt, earliest_arrival) if _gate_dt
                        else (
                            _bar(second_pickup, earliest_arrival)
                            if earliest_arrival > second_pickup else None
                        )
                    ),
                    "band_label": (
                        f"+{_behind_gate} MIN AFTER ARRIVAL" if _gate_dt
                        else f"≈{late_minutes} MIN BEHIND"
                    ),
                    "marker_gate": _pct(_gate_dt) if _gate_dt else None,
                    "marker_booked": _pct(second_pickup),
                    "marker_driver_free": _pct(earliest_arrival),
                },
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

    # ── Resolution Ladder: which in-house drivers can cover the conflicted leg
    #    (arrival or departure alike), plus an affiliate fallback. Offline-safe:
    #    with USE_LIVE_DISTANCE off, feasibility uses the category drive-time
    #    table — no synchronous Google call in the request path (see CLAUDE.md
    #    hotfix 6da1626a / NEXT #7).
    ladder = {"inhouse": [], "affiliates": [], "step3_unlocked": True, "checked": False}
    if task.leg:
        try:
            from dispatching.scheduler import (
                build_driver_schedules,
                check_feasibility,
                load_all_driver_vtypes,
                get_compatible_vehicle_types,
            )
            cover_leg = task.leg
            cover_vtype = cover_leg.effective_vehicle_type
            inhouse_drivers = list(
                Driver.objects.filter(driver_type="inhouse", is_active=True)
                .exclude(profile__username__icontains="placeholder")
                .exclude(profile__first_name__icontains="placeholder")
                .select_related("profile")
            )
            day_legs_all = list(
                Leg.objects.filter(pickup_date=pickup_date, driver__in=inhouse_drivers)
                .exclude(status__in=["completed", "cancelled"])
                .exclude(reservation__status="cancelled")
                .select_related(
                    "reservation", "reservation__customer", "flight_information"
                )
            )
            sched_map = build_driver_schedules(day_legs_all, inhouse_drivers, pickup_date)
            vtypes = load_all_driver_vtypes(pickup_date)
            for d in inhouse_drivers:
                if d.id == int(driver_id):
                    continue
                dv = vtypes.get(d.id)
                # Only offer drivers actually on the roster today — i.e. assigned a
                # vehicle for this date (load_all_driver_vtypes is keyed off the day's
                # DriverVehicleAssignment rows). This drops anyone who isn't working
                # today and anyone without a vehicle to drive. A rostered driver with
                # zero jobs still qualifies (they're the freest cover).
                if not dv:
                    continue
                # The assigned vehicle must be able to cover the leg's tier.
                if cover_vtype and cover_vtype not in get_compatible_vehicle_types(dv):
                    continue
                ds = sched_map.get(d.id)
                if ds is None:
                    continue
                feas = check_feasibility(ds, cover_leg, pickup_date)
                if not feas.feasible:
                    continue
                ladder["inhouse"].append({
                    "id": d.id,
                    "name": str(d),
                    "vehicle": dv or "",
                    "jobs": len(ds.slots),
                    "buffer": feas.buffer_minutes,
                })
            # Fewest existing jobs first (easiest to slot), then most slack.
            ladder["inhouse"].sort(key=lambda c: (c["jobs"], -c["buffer"]))
            ladder["inhouse"] = ladder["inhouse"][:6]
            ladder["step3_unlocked"] = len(ladder["inhouse"]) == 0
            ladder["affiliates"] = [
                {"id": a.id, "name": str(a)}
                for a in Driver.objects.filter(
                    driver_type="affiliate", is_active=True
                ).select_related("profile")[:8]
            ]
            ladder["checked"] = True
        except Exception:
            logger.exception("Error building resolution ladder for task %s", task.id)

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
        "redesign": redesign,
        "ladder": ladder,
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

    verify_sent_at = getattr(leg, "flight_verification_email_sent_at", None)
    verify_hours_since = leg.hours_since_verify_email if verify_sent_at else None

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
        "fv_verify_email_sent_at": verify_sent_at,
        "fv_verify_email_hours_since": verify_hours_since,
        "is_flight_verify": True,
    }


def _build_payment_chase_context(task, request, comm_attempts):
    """
    Build extra context for payment_chase task detail: reservation payment
    summary, upcoming legs, guest contact, the playbook-driven resolution
    ladder (state derived from ``comm_attempts``), and quick action links.
    """
    reservation = task.reservation
    if not reservation:
        return {}

    import re
    from decimal import Decimal
    from django.urls import reverse
    from ops.playbooks import get_playbook, build_ladder_steps, resolve_actions

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

    # ── Playbook-driven resolution ladder (state from real attempt history) ──
    playbook = get_playbook(task.task_type)
    ladder_steps = build_ladder_steps(playbook, comm_attempts)
    for st in ladder_steps:
        branches = st.get("branches") or {}
        st["answered_actions"] = resolve_actions(branches.get("answered"))
        st["advance_actions"] = resolve_actions(branches.get("no_answer"))
        st["resolve_actions"] = resolve_actions(branches.get("resolve"))

    # Reminders logged so far, by channel (drives the hero sentence + vitals).
    reminders = {"call": 0, "sms": 0, "email": 0, "total": 0}
    for a in comm_attempts:
        if a.channel in reminders:
            reminders[a.channel] += 1
        reminders["total"] += 1

    # Trip-when fragment for the hero sentence (no %-d — cross-platform).
    # trip_spoken/trip_countdown are the same trip said out loud on the call:
    # "August 27 at 10:40 AM", "coming up in 2 days".
    trip_spoken = ""
    trip_countdown = ""
    if leg_data:
        first = leg_data[0]
        pickup_date = first["pickup_date"]
        trip_when = "a trip on %s %d at %s" % (
            pickup_date.strftime("%b"), pickup_date.day, first["pickup_time_str"],
        )
        trip_spoken = "%s %d at %s" % (
            pickup_date.strftime("%B"), pickup_date.day, first["pickup_time_str"],
        )
        days_out = (pickup_date - today).days
        if days_out == 0:
            trip_countdown = "Your trip is later today"
        elif days_out == 1:
            trip_countdown = "Your trip is coming up tomorrow"
        elif days_out > 1:
            trip_countdown = "Your trip is coming up in %d days" % days_out
    else:
        trip_when = "this reservation"

    # Action wiring — reuse existing flows (payment portal / reservation page /
    # checkout link). No new payment or SMS endpoints (logged-manual text).
    portal_url = reverse("dispatcher_payment_portal", kwargs={"reservation_id": reservation.uuid})
    reservation_url = reverse("reservation_details", args=[reservation.uuid])
    checkout_url = request.build_absolute_uri(
        reverse("create_checkout_session", args=[str(reservation.uuid)])
    )
    phone_href = re.sub(r"[^\d+]", "", guest_phone) if guest_phone else ""
    first_name = (customer.first_name if customer and customer.first_name else "there")
    sms_draft = (
        f"Hi {first_name}, this is Grayson Towncar.\n\n"
        f"We're just checking in regarding your upcoming reservation, which is still pending.\n\n"
        f"If you'd still like to keep it, please use this link to finalize it, since we're not "
        f"able to assign anyone while the reservation is still pending: {checkout_url}\n\n"
        f"If not, please let us know so we can cancel it. Otherwise, unpaid reservations may be "
        f"automatically canceled the day before service. Thank you!"
    )
    # Opening of the call, said out loud. Deliberately a confirmation call, not a
    # collections one: the guest hears someone checking their trip is still on, and
    # the card is how it gets confirmed. It stops after the ask — the dispatcher
    # pauses there and takes the call wherever the guest goes next.
    confirm_line = (
        f"I'm just reaching out to confirm your transportation for {trip_spoken}."
        if trip_spoken else
        "I'm just reaching out to confirm your upcoming transportation."
    )
    pending_line = (
        f"{trip_countdown}, and it's still showing as pending on our end, so I wanted to "
        f"make sure everything is still good."
        if trip_countdown else
        "It's still showing as pending on our end, so I wanted to make sure everything "
        "is still good."
    )
    # Who is making the call — whoever is reading this page. A login handle is only
    # usable when it's a plain name (usernames here are lowercase first names);
    # anything else falls back to the placeholder rather than have someone
    # introduce themselves to a guest as "dispatcher1".
    caller = (getattr(request.user, "first_name", "") or "").strip()
    if not caller:
        handle = (getattr(request.user, "username", "") or "").strip()
        caller = handle.title() if handle.isalpha() else "[your name]"
    call_script = (
        f"Hi {first_name}, this is {caller} calling from Grayson Towncar.\n\n"
        f"{confirm_line} {pending_line}\n\n"
        f"If everything is still good, I can get it confirmed for you now. I would just "
        f"need a card to finalize the reservation, and once that's done, you'll receive "
        f"an email confirmation."
    )

    ladder_ctx = {
        "phone": guest_phone,
        "phone_href": phone_href,
        "sms_draft": sms_draft,
        "call_script": call_script,
        "portal_url": portal_url,
        "reservation_url": reservation_url,
    }

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
        "pc_reminders": reminders,
        "pc_trip_when": trip_when,
        "ladder_steps": ladder_steps,
        "ladder_ctx": ladder_ctx,
        "payment_redesign": True,
        "is_payment_chase": True,
    }


def _build_driver_assign_context(task):
    """
    Build context for driver_assign task detail: leg info, available
    in-house drivers with their day load, and quick actions.
    """
    from reservations.models import Leg
    from drivers.models import Driver

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
        .order_by("profile__first_name")
    )

    # Fetch ALL legs (including completed) for these drivers on this day so we
    # can show the true day load + when each driver started/cleared.
    all_driver_legs = list(
        Leg.objects.filter(
            driver__in=[d.id for d in drivers],
            pickup_date=pickup_date,
        )
        .exclude(status="cancelled")
        .exclude(reservation__status="cancelled")
        .select_related("driver", "reservation", "reservation__customer")
        .order_by("pickup_time")
    )

    # Group legs by driver
    legs_by_driver = {}
    for dl in all_driver_legs:
        legs_by_driver.setdefault(dl.driver_id, []).append(dl)

    # This leg's pickup as datetime for overlap / fitness checking
    this_pickup_dt = dt_cls.combine(pickup_date, leg.pickup_time)
    completed_states = {"completed"}

    def _fmt_time(dtobj):
        return dtobj.strftime("%I:%M %p").lstrip("0")

    driver_list = []
    for d in drivers:
        d_legs = legs_by_driver.get(d.id, [])
        day_legs_total = len(d_legs)
        active_legs = [dl for dl in d_legs if dl.status not in completed_states]

        # Compute end times once
        end_times = {}
        for dl in d_legs:
            try:
                end_times[dl.id] = _estimate_leg_end_time(dl, pickup_date)
            except Exception:
                end_times[dl.id] = dt_cls.combine(pickup_date, dl.pickup_time) + timedelta(minutes=60)

        # Day stats: when did the driver start, when do they clear?
        first_pickup_dt = None
        latest_end = None              # latest end across ALL legs (incl completed)
        latest_active_end = None       # latest end across remaining (active) legs
        has_overlap = False
        for dl in d_legs:
            pickup_dt = dt_cls.combine(pickup_date, dl.pickup_time)
            end_time = end_times[dl.id]
            if first_pickup_dt is None or pickup_dt < first_pickup_dt:
                first_pickup_dt = pickup_dt
            if latest_end is None or end_time > latest_end:
                latest_end = end_time
            if dl.status not in completed_states:
                if latest_active_end is None or end_time > latest_active_end:
                    latest_active_end = end_time
                # Overlap check only matters for not-yet-done legs
                if pickup_dt <= this_pickup_dt < end_time:
                    has_overlap = True

        # Mini-schedule: only show remaining (active) legs, capped at 4 rows
        mini_schedule = []
        for dl in active_legs[:4]:
            cust = dl.reservation.customer if dl.reservation else None
            mini_schedule.append({
                "pickup_str": _fmt_time(dt_cls.combine(pickup_date, dl.pickup_time)),
                "end_str": _fmt_time(end_times[dl.id]),
                "route": f"{(dl.pickup_location or '')[:25]} → {(dl.dropoff_location or '')[:25]}",
                "customer": cust.get_full_name() if cust else "Unknown",
            })
        more_legs = max(0, len(active_legs) - len(mini_schedule))

        # Hours on duty so far (start of first leg → max(now, this pickup))
        hours_on_duty = 0.0
        if first_pickup_dt:
            ref_end = latest_end if latest_end and latest_end > this_pickup_dt else this_pickup_dt
            hours_on_duty = max(0.0, (ref_end - first_pickup_dt).total_seconds() / 3600.0)

        # Fitness / availability label
        # Priority: hard conflict > heavy day > maybe > free
        HEAVY_HOURS = 11.0   # ~11h on duty by the time of this pickup = overworked
        if has_overlap:
            avail_status = "conflict"
            avail_label = "Busy at pickup time"
        elif day_legs_total == 0:
            avail_status = "free"
            avail_label = "Available all day"
        elif latest_active_end and latest_active_end > this_pickup_dt:
            # Has a remaining leg that ends after this pickup but doesn't overlap
            # — they're booked elsewhere when we'd need them.
            avail_status = "maybe"
            avail_label = f"Booked until {_fmt_time(latest_active_end)}"
        elif hours_on_duty >= HEAVY_HOURS:
            avail_status = "maybe"
            avail_label = f"Heavy day ({hours_on_duty:.1f}h on duty)"
        elif latest_end and latest_end <= this_pickup_dt:
            avail_status = "free"
            avail_label = f"Free since {_fmt_time(latest_end)}"
        else:
            avail_status = "free"
            avail_label = "Available"

        started_str = _fmt_time(first_pickup_dt) if first_pickup_dt else ""
        cleared_str = _fmt_time(latest_end) if latest_end else ""

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
            "day_legs": day_legs_total,
            "active_legs": len(active_legs),
            "started_str": started_str,
            "cleared_str": cleared_str,
            "hours_on_duty": round(hours_on_duty, 1),
            "vehicle_label": vehicle_label,
            "mini_schedule": mini_schedule,
            "more_legs": more_legs,
            "avail_status": avail_status,
            "avail_label": avail_label,
            "has_overlap": has_overlap,
        })

    # Order: free → maybe → conflict, then by day load
    _order = {"free": 0, "maybe": 1, "conflict": 2}
    driver_list.sort(key=lambda x: (_order.get(x["avail_status"], 9), x["day_legs"], x["name"]))

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


def _build_confirmation_texts_context(task):
    """
    Build context for the daily confirmation_texts batch task. Shows totals,
    a few sample unsent legs, and a link to the existing Confirmations page
    pre-filtered to the target date.
    """
    from datetime import datetime as dt_cls
    from reservations.models import Leg

    meta = task.metadata or {}
    target_str = meta.get("target_date")
    target_date = None
    if target_str:
        try:
            target_date = dt_cls.strptime(target_str, "%Y-%m-%d").date()
        except ValueError:
            target_date = None
    if target_date is None:
        target_date = timezone.localdate() + timedelta(days=1)

    base_qs = (
        Leg.objects.filter(pickup_date=target_date)
        .exclude(status="cancelled")
        .exclude(reservation__status="cancelled")
    )
    total = base_qs.count()
    unsent_qs = base_qs.filter(confirmation_sms_sent_at__isnull=True).select_related(
        "reservation", "reservation__customer"
    ).order_by("pickup_time")
    unsent_count = unsent_qs.count()
    sent_count = total - unsent_count

    # ── Step 1: flight verification state for the target date ──
    # Two signals matter:
    #   1) Open FLIGHT_VERIFICATION ops tasks for legs on that day
    #   2) Arrival legs on that day with a flight-time mismatch (raw)
    open_flight_tasks = list(
        OperationalTask.objects.filter(
            task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
            status__in=list(OperationalTask.OPEN_STATUSES),
            leg__pickup_date=target_date,
        )
        .select_related("leg", "leg__reservation", "leg__reservation__customer")
        .order_by("leg__pickup_time")
    )
    open_flight_task_count = len(open_flight_tasks)

    # Catch arrival legs that mismatch but don't yet have a verify task
    raw_mismatch_count = 0
    for lg in base_qs.filter(flight_information__isnull=False).select_related("flight_information"):
        if hasattr(lg, "has_flight_time_mismatch"):
            try:
                if lg.has_flight_time_mismatch(threshold_minutes=30):
                    raw_mismatch_count += 1
            except Exception:
                pass

    flights_clean = (open_flight_task_count == 0 and raw_mismatch_count == 0)

    flight_task_samples = []
    for ft in open_flight_tasks[:5]:
        lg = ft.leg
        cust = lg.reservation.customer if (lg and lg.reservation) else None
        flight_task_samples.append({
            "task_id": ft.id,
            "guest": cust.get_full_name() if cust else "Unknown",
            "pickup_time_str": lg.pickup_time.strftime("%I:%M %p").lstrip("0") if lg and lg.pickup_time else "",
            "title": ft.title,
        })
    more_flight_tasks = max(0, open_flight_task_count - len(flight_task_samples))

    # ── Step 2: unsent confirmation samples ──
    sample_legs = []
    for lg in unsent_qs[:8]:
        cust = lg.reservation.customer if lg.reservation else None
        phone = getattr(cust, "phone_number", "") if cust else ""
        sample_legs.append({
            "id": lg.id,
            "guest": cust.get_full_name() if cust else "Unknown",
            "pickup_time_str": lg.pickup_time.strftime("%I:%M %p").lstrip("0") if lg.pickup_time else "",
            "trip_type": lg.get_trip_type() if hasattr(lg, "get_trip_type") else "",
            "from": (lg.pickup_location or "")[:40],
            "to": (lg.dropoff_location or "")[:40],
            "phone": phone or "",
        })
    more_unsent = max(0, unsent_count - len(sample_legs))

    return {
        "ct_target_date": target_date,
        "ct_target_date_str": str(target_date),
        "ct_total": total,
        "ct_sent": sent_count,
        "ct_unsent": unsent_count,
        "ct_sample_legs": sample_legs,
        "ct_more_unsent": more_unsent,
        "ct_open_flight_tasks": open_flight_task_count,
        "ct_raw_mismatches": raw_mismatch_count,
        "ct_flights_clean": flights_clean,
        "ct_flight_task_samples": flight_task_samples,
        "ct_more_flight_tasks": more_flight_tasks,
        "is_confirmation_texts": True,
    }


def _build_afterhours_fee_context(task):
    """Context for the after-hours fee task detail: leg/customer summary, fee
    amount, whether a card is on file, and the one-click charge action."""
    from decimal import Decimal
    from reservations.utils import AFTERHOURS_FEE_AMOUNT

    leg = task.leg
    if not leg:
        return {}
    reservation = leg.reservation
    customer = reservation.customer if reservation else None
    already_applied = (leg.afterhours_fee or Decimal("0.00")) >= AFTERHOURS_FEE_AMOUNT

    return {
        "ah_leg": leg,
        "ah_reservation": reservation,
        "ah_customer": customer,
        "ah_amount": AFTERHOURS_FEE_AMOUNT,
        "ah_has_card_on_file": bool(getattr(customer, "stripe_customer_id", None)),
        "ah_already_applied": already_applied,
        "ah_pickup_str": (
            leg.pickup_time.strftime("%I:%M %p").lstrip("0") if leg.pickup_time else ""
        ),
        "is_afterhours_fee": True,
    }


def _build_tight_turn_context(task):
    """Tight-turn task detail. Reuses the rich driver-conflict schedule view (driver's
    full day, the two legs, drive/transit times, and the Match-Flight-Time action) but
    frames it as a softer 'keep an eye' tight turn rather than a hard conflict. The
    cushion (`tt_late_minutes` = minutes the driver reaches the airport after the RAW
    flight arrival) and the early new-arrival time come from the task metadata."""
    ctx = _build_driver_conflict_context(task)
    meta = task.metadata or {}
    if not ctx:
        ctx = {}
    ctx["is_tight_turn"] = True
    ctx["tt_late_minutes"] = meta.get("late_minutes", ctx.get("conflict_minutes", 0))
    ctx["tt_new_arrival"] = meta.get("new_arrival_time", ctx.get("flight_arrival_str", ""))
    return ctx


def _advisor_card_for_task(task):
    """Recovery Advisor card for this task's leg, or None.

    Read-only: ``conflict_advisor.compute_advisor_state`` narrowed to the leg
    (``for_leg_id``), picking the disruption that contains it. Serves the same
    whole-board-validated plans as the dispatch-board rail — unlike the page's
    legacy .cf-assign candidates, and with ZERO external calls (the advisor
    path never touches live Google / AeroAPI / Samsara HTTP).

    Cached per (date, board fingerprint, leg) so a page reload on an unchanged
    board is a fingerprint check + cache read, never a fresh compute (the
    full compute can run a swap search up to the advisor budget — too slow to
    sit inline in every render). ``False`` is the cached no-card marker."""
    leg = task.leg
    if not leg or not leg.pickup_date:
        return None
    from django.core.cache import cache
    from dispatching.advisor_views import RA_CARDS_TTL_S, RA_CARD_SHAPE_V
    from dispatching.conflict_advisor import (compute_advisor_state,
                                              compute_board_fingerprint)

    # Shape version in the key: the fingerprint tracks the board, not the card
    # contract, so a deploy that changes the card shape must not be served the
    # old one out of cache.
    fp = compute_board_fingerprint(leg.pickup_date)
    cache_key = (f"ra_taskcard_v{RA_CARD_SHAPE_V}_"
                 f"{leg.pickup_date.isoformat()}_{fp}_{leg.id}")
    cached = cache.get(cache_key)
    if cached is not None:
        return cached or None
    state = compute_advisor_state(leg.pickup_date, for_leg_id=leg.id)
    card = next((c for c in state["disruptions"]
                 if leg.id in c.get("leg_ids", [])), None)
    cache.set(cache_key, card if card is not None else False, RA_CARDS_TTL_S)
    return card


@login_required(login_url="login")
@user_passes_test(_is_staff, login_url="login")
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
        User.objects.filter(is_staff=True, is_active=True)
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
        context.update(_build_payment_chase_context(task, request, comm_attempts))
    elif task.task_type == OperationalTask.TaskType.DRIVER_ASSIGNMENT and task.leg:
        context.update(_build_driver_assign_context(task))
    elif task.task_type == OperationalTask.TaskType.CONFIRMATION_TEXTS:
        context.update(_build_confirmation_texts_context(task))
    elif task.task_type == OperationalTask.TaskType.AFTERHOURS_FEE and task.leg:
        context.update(_build_afterhours_fee_context(task))
    elif task.task_type == OperationalTask.TaskType.TIGHT_TURN and task.leg:
        context.update(_build_tight_turn_context(task))

    # Conflict / tight-turn tasks get the redesigned "resolution ladder" page;
    # everything else keeps the standard ops task detail.
    if (
        task.task_type in (
            OperationalTask.TaskType.DRIVER_CONFLICT,
            OperationalTask.TaskType.TIGHT_TURN,
        )
        and context.get("driver_schedule")
        and context.get("redesign")
    ):
        # Recovery Advisor plans (validated, cached per board fingerprint) —
        # advisory only: any failure leaves the page exactly as before.
        # SUPERUSER-ONLY while the owner trials it; the gate lives in one place
        # (advisor_views.advisor_visible_to) so every surface opens together.
        from dispatching.advisor_views import advisor_visible_to
        try:
            context["advisor_card"] = (_advisor_card_for_task(task)
                                       if advisor_visible_to(request.user)
                                       else None)
            # Ledger (Phase 1.2, invisible): this page can apply a plan, so a
            # card shown only here must still exist in the log. Cheap — one
            # card, and the recorder swallows its own failures.
            if context["advisor_card"]:
                from dispatching import advisor_events
                advisor_events.record_cards(
                    task.leg.pickup_date, [context["advisor_card"]],
                    source="task", whole_board=False)
        except Exception:
            logger.exception("Recovery Advisor card failed for task %s", task.id)
            context["advisor_card"] = None
        # Held-day secondary choice (owner decision): the apply JS may offer
        # staging into the draft only to sandbox-granted users.
        try:
            from dispatching.assignment import can_use_sandbox
            context["advisor_can_stage"] = bool(can_use_sandbox(request.user))
        except Exception:
            context["advisor_can_stage"] = False
        return render(request, "dispatching/conflict_task_detail.html", context)

    # Payment-chase tasks get the redesigned playbook-driven collection ladder.
    if (
        task.task_type == OperationalTask.TaskType.PAYMENT_CHASE
        and context.get("payment_redesign")
    ):
        return render(request, "dispatching/payment_task_detail.html", context)

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
    prior_start = range_start - timedelta(days=days_back)
    prior_end = range_start

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

    # ── Imports for this view ──
    from reservations.models import Reservation, Leg, AuditLog
    from django.db.models import Sum, DecimalField
    from django.db.models.functions import Coalesce, TruncDate as _TruncDate
    from decimal import Decimal
    from itertools import groupby
    from users.models import UserProfile
    from django.contrib.auth.models import User as AuthUser

    # ═══════════════════════════════════════════════════════════════════
    # DISPATCHER ALLOWLIST — compute upfront so every query below filters
    # to office staff only (excludes drivers, travel agents, non-staff).
    # ═══════════════════════════════════════════════════════════════════
    non_dispatcher_profile_uids = set(
        UserProfile.objects.filter(
            Q(is_driver=True) | Q(is_travel_agent=True)
        ).values_list("user_id", flat=True)
    )
    dispatcher_uids = set(
        AuthUser.objects.filter(is_staff=True)
        .exclude(id__in=non_dispatcher_profile_uids)
        .values_list("id", flat=True)
    )

    # ═══════════════════════════════════════════════════════════════════
    # DAILY STAFF SUMMARY — 7 queries grouped by (day, user).
    # These are the SINGLE SOURCE for per-staff metrics; overview totals
    # are derived by summing across days (no duplicate queries).
    # ═══════════════════════════════════════════════════════════════════

    # Helper: build (day_str, uid) → value map from a queryset
    def _day_user_map(qs, day_field, user_field, val_field):
        result = {}
        for row in qs:
            key = (row[day_field].isoformat(), row[user_field])
            result[key] = row[val_field]
        return result

    # 1. First/last active per staff per day (StaffActivity)
    activity_by_day = list(
        StaffActivity.objects.filter(created_at__gte=range_start, user_id__in=dispatcher_uids)
        .annotate(day=_TruncDate("created_at"))
        .values("day", "user__id", "user__first_name", "user__username")
        .annotate(first_active=Min("created_at"), last_active=Max("created_at"))
    )
    staff_names = {}
    activity_first = {}
    activity_last = {}
    for row in activity_by_day:
        uid = row["user__id"]
        staff_names[uid] = row["user__first_name"] or row["user__username"]
        key = (row["day"].isoformat(), uid)
        activity_first[key] = row["first_active"]
        activity_last[key] = row["last_active"]

    # 2. Reservations created per day per staff
    res_by_day = list(
        Reservation.objects.filter(created_at__gte=range_start, created_by_id__in=dispatcher_uids)
        .annotate(day=_TruncDate("created_at"))
        .values("day", "created_by__id", "created_by__first_name", "created_by__username")
        .annotate(count=Count("id"), revenue=Coalesce(Sum("total_price"), Decimal("0"), output_field=DecimalField()))
    )
    res_count_map = {}
    res_rev_map = {}
    for row in res_by_day:
        key = (row["day"].isoformat(), row["created_by__id"])
        res_count_map[key] = row["count"]
        res_rev_map[key] = float(row["revenue"])
        staff_names.setdefault(row["created_by__id"], row["created_by__first_name"] or row["created_by__username"])

    # 3. Tasks resolved per day per staff
    tasks_by_day = _day_user_map(
        OperationalTask.objects.filter(status="completed", resolved_at__gte=range_start, resolved_by_id__in=dispatcher_uids)
        .annotate(day=_TruncDate("resolved_at"))
        .values("day", "resolved_by__id")
        .annotate(count=Count("id")),
        "day", "resolved_by__id", "count"
    )

    # 4. Driver assignments per day per staff (AuditLog)
    assigns_by_day = _day_user_map(
        AuditLog.objects.filter(timestamp__gte=range_start, action="driver_assigned", user_id__in=dispatcher_uids)
        .annotate(day=_TruncDate("timestamp"))
        .values("day", "user__id")
        .annotate(count=Count("id")),
        "day", "user__id", "count"
    )

    # 5. Legs modified per day per staff
    legs_mod_by_day = _day_user_map(
        Leg.history.filter(history_date__gte=range_start, history_type="~", history_user_id__in=dispatcher_uids)
        .annotate(day=_TruncDate("history_date"))
        .values("day", "history_user__id")
        .annotate(count=Count("id", distinct=True)),
        "day", "history_user__id", "count"
    )

    # 6. Comms per day per staff (with channel breakdown for overview table)
    comms_by_day_detail = list(
        CommunicationAttempt.objects.filter(created_at__gte=range_start, staff_user_id__in=dispatcher_uids)
        .annotate(day=_TruncDate("created_at"))
        .values("day", "staff_user__id", "staff_user__first_name", "staff_user__username")
        .annotate(
            count=Count("id"),
            calls=Count("id", filter=Q(channel="call")),
            sms=Count("id", filter=Q(channel="sms")),
            emails=Count("id", filter=Q(channel="email")),
        )
    )
    comms_by_day = {}
    for row in comms_by_day_detail:
        key = (row["day"].isoformat(), row["staff_user__id"])
        comms_by_day[key] = row["count"]
        staff_names.setdefault(row["staff_user__id"], row["staff_user__first_name"] or row["staff_user__username"])

    # 7. Emails per day per staff (with type breakdown for overview table)
    emails_by_day_detail = list(
        EmailLog.objects.filter(sent_at__gte=range_start, sent_by_id__in=dispatcher_uids)
        .annotate(day=_TruncDate("sent_at"))
        .values("day", "sent_by__id", "sent_by__first_name", "sent_by__username")
        .annotate(
            count=Count("id"),
            confirmations=Count("id", filter=Q(email_type="confirmation")),
            payment_reminders=Count("id", filter=Q(email_type="payment_reminder")),
            statements=Count("id", filter=Q(
                email_type__in=["driver_statement", "agent_commission", "agency_commission"]
            )),
        )
    )
    emails_by_day = {}
    for row in emails_by_day_detail:
        key = (row["day"].isoformat(), row["sent_by__id"])
        emails_by_day[key] = row["count"]
        staff_names.setdefault(row["sent_by__id"], row["sent_by__first_name"] or row["sent_by__username"])

    # ═══════════════════════════════════════════════════════════════════
    # DERIVE OVERVIEW TOTALS from daily summary data (no extra queries)
    # ═══════════════════════════════════════════════════════════════════

    # Tasks completed per staff — sum tasks_by_day across days
    _completions_by_uid = defaultdict(int)
    for (day_str, uid), cnt in tasks_by_day.items():
        _completions_by_uid[uid] += cnt
        staff_names.setdefault(uid, f"User #{uid}")
    staff_completions = sorted(
        [{"resolved_by__id": uid, "name": staff_names.get(uid, f"User #{uid}"), "count": cnt}
         for uid, cnt in _completions_by_uid.items()],
        key=lambda x: x["count"], reverse=True,
    )
    completed_in_range = OperationalTask.objects.filter(status="completed", resolved_at__gte=range_start)
    auto_closed_count = completed_in_range.filter(resolved_by__isnull=True).count()

    # Communication volume per staff — sum comms_by_day_detail across days
    _comms_by_uid = defaultdict(lambda: {"total": 0, "calls": 0, "sms": 0, "emails": 0})
    for row in comms_by_day_detail:
        uid = row["staff_user__id"]
        _comms_by_uid[uid]["total"] += row["count"]
        _comms_by_uid[uid]["calls"] += row["calls"]
        _comms_by_uid[uid]["sms"] += row["sms"]
        _comms_by_uid[uid]["emails"] += row["emails"]
    staff_comms = sorted(
        [{"staff_user__id": uid, "name": staff_names.get(uid, f"User #{uid}"), **vals}
         for uid, vals in _comms_by_uid.items()],
        key=lambda x: x["total"], reverse=True,
    )

    # Emails sent per staff — sum emails_by_day_detail across days
    _emails_by_uid = defaultdict(lambda: {"total": 0, "confirmations": 0, "payment_reminders": 0, "statements": 0})
    for row in emails_by_day_detail:
        uid = row["sent_by__id"]
        _emails_by_uid[uid]["total"] += row["count"]
        _emails_by_uid[uid]["confirmations"] += row["confirmations"]
        _emails_by_uid[uid]["payment_reminders"] += row["payment_reminders"]
        _emails_by_uid[uid]["statements"] += row["statements"]
    staff_emails = sorted(
        [{"sent_by__id": uid, "name": staff_names.get(uid, f"User #{uid}"), **vals}
         for uid, vals in _emails_by_uid.items()],
        key=lambda x: x["total"], reverse=True,
    )

    # Reservations per staff — sum res_by_day across days
    _res_by_uid = defaultdict(lambda: {"count": 0, "revenue": Decimal("0")})
    for row in res_by_day:
        uid = row["created_by__id"]
        _res_by_uid[uid]["count"] += row["count"]
        _res_by_uid[uid]["revenue"] += row["revenue"]
    staff_reservations = sorted(
        [{"created_by__id": uid, "name": staff_names.get(uid, f"User #{uid}"),
          "count": vals["count"], "revenue": vals["revenue"]}
         for uid, vals in _res_by_uid.items()],
        key=lambda x: x["revenue"], reverse=True,
    )
    total_staff_reservations = sum(s["count"] for s in staff_reservations)
    total_staff_revenue = sum(s["revenue"] for s in staff_reservations)

    # Legs/reservations modified per staff — sum legs_mod_by_day + res history
    _legs_by_uid = defaultdict(int)
    for (day_str, uid), cnt in legs_mod_by_day.items():
        _legs_by_uid[uid] += cnt
    # Reservation modifications — still need one query (not in daily data above)
    res_mods = (
        Reservation.history.filter(
            history_date__gte=range_start, history_type="~", history_user_id__in=dispatcher_uids,
        )
        .values("history_user__id", "history_user__first_name", "history_user__username")
        .annotate(res_modified=Count("id", distinct=True))
    )
    _res_mods_by_uid = {}
    for row in res_mods:
        uid = row["history_user__id"]
        _res_mods_by_uid[uid] = row["res_modified"]
        staff_names.setdefault(uid, row["history_user__first_name"] or row["history_user__username"])
    all_mod_uids = set(_legs_by_uid) | set(_res_mods_by_uid)
    staff_modifications_list = sorted(
        [{"user_id": uid, "name": staff_names.get(uid, f"User #{uid}"),
          "legs_modified": _legs_by_uid.get(uid, 0),
          "reservations_modified": _res_mods_by_uid.get(uid, 0)}
         for uid in all_mod_uids],
        key=lambda x: x["legs_modified"] + x["reservations_modified"], reverse=True,
    )

    # First/last active today — derive from activity_by_day
    today_str = today.isoformat()
    staff_active_times = {}
    for row in activity_by_day:
        if row["day"].isoformat() == today_str:
            uid = row["user__id"]
            staff_active_times[uid] = {
                "user__id": uid,
                "name": staff_names.get(uid, f"User #{uid}"),
                "first_active": row["first_active"],
                "last_active": row["last_active"],
            }
    # Sort by first_active
    staff_active_times = dict(sorted(staff_active_times.items(), key=lambda x: x[1]["first_active"]))

    # AuditLog actions per staff (overview needs full breakdown — still one query)
    audit_actions = list(
        AuditLog.objects.filter(timestamp__gte=range_start, user_id__in=dispatcher_uids)
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

    # ═══════════════════════════════════════════════════════════════════
    # REMAINING QUERIES (unique data, can't derive from daily summary)
    # ═══════════════════════════════════════════════════════════════════

    # ── Daily task creation/completion trend (chart needs day-level, not per-user) ──
    daily_created = dict(
        OperationalTask.objects.filter(created_at__gte=range_start)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(count=Count("id"))
        .values_list("day", "count")
    )
    # daily_completed — derive from tasks_by_day (sum across users per day)
    _daily_completed = defaultdict(int)
    for (day_str, uid), cnt in tasks_by_day.items():
        _daily_completed[day_str] += cnt

    trend_data = []
    for i in range(days_back):
        d = today - timedelta(days=days_back - 1 - i)
        trend_data.append({
            "day": d.isoformat(),
            "created": daily_created.get(d, 0),
            "completed": _daily_completed.get(d.isoformat(), 0),
        })

    # ── Lead response times ──
    lead_tasks = OperationalTask.objects.filter(
        task_type="lead_response", created_at__gte=range_start,
    ).prefetch_related("comm_attempts")
    response_times = []
    for task in lead_tasks:
        first_comm = task.comm_attempts.order_by("created_at").first()
        if first_comm:
            delta = (first_comm.created_at - task.created_at).total_seconds() / 60
            response_times.append(delta)
    avg_response_min = round(sum(response_times) / len(response_times), 1) if response_times else None
    median_response_min = round(sorted(response_times)[len(response_times) // 2], 1) if response_times else None

    # ── Staff activity timeline (full range, for the timeline section) ──
    today_start = now.astimezone(eastern).replace(hour=0, minute=0, second=0, microsecond=0)
    range_activities = list(
        StaffActivity.objects.filter(created_at__gte=range_start, user_id__in=dispatcher_uids)
        .exclude(action_type=StaffActivity.ActionType.PAGE_VIEW)
        .select_related("user", "task").order_by("-created_at")[:200]
    )

    # ── Task type performance ──
    type_performance = list(
        completed_in_range.values("task_type")
        .annotate(count=Count("id"), avg_attempts=Avg("attempts"))
        .order_by("-count")
    )

    # ── Page view counts per staff (today) ──
    page_views_today = list(
        StaffActivity.objects.filter(
            action_type=StaffActivity.ActionType.PAGE_VIEW,
            created_at__gte=today_start,
            user_id__in=dispatcher_uids,
        )
        .values("user__id", "user__first_name", "user__username")
        .annotate(views=Count("id"))
        .order_by("-views")
    )
    for pv in page_views_today:
        pv["name"] = pv["user__first_name"] or pv["user__username"]

    # ── Correction / override detection ──
    corrections = []
    CORRECTION_FIELDS = {
        "pickup_time", "pickup_date", "pickup_location", "dropoff_location",
        "driver", "status", "total_price", "base_price", "gratuity_amount",
        "passenger_count", "private_notes",
    }
    leg_history_in_range = (
        Leg.history.filter(
            history_date__gte=range_start, history_type="~", history_user_id__in=dispatcher_uids,
        )
        .select_related("history_user").order_by("id", "history_date")[:500]
    )
    leg_records_by_id = defaultdict(list)
    for rec in leg_history_in_range:
        leg_records_by_id[rec.id].append(rec)
    for leg_id, records in leg_records_by_id.items():
        for i in range(1, len(records)):
            rec = records[i]
            prev = records[i - 1]
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

    # ═══════════════════════════════════════════════════════════════════
    # TREND INDICATORS — 4 prior-period queries + assembly
    # ═══════════════════════════════════════════════════════════════════

    def _build_trend(current_val, prior_val):
        if prior_val and prior_val > 0:
            pct = round(((current_val - prior_val) / prior_val) * 100)
            direction = "up" if current_val > prior_val else ("down" if current_val < prior_val else "flat")
        elif current_val > 0:
            pct = 100
            direction = "up"
        else:
            pct = 0
            direction = "flat"
        return {"current": current_val, "prior": prior_val, "pct": pct, "direction": direction}

    prior_completions_map = dict(
        OperationalTask.objects.filter(
            status="completed", resolved_at__gte=prior_start, resolved_at__lt=prior_end,
            resolved_by_id__in=dispatcher_uids,
        )
        .values_list("resolved_by__id")
        .annotate(c=Count("id"))
        .values_list("resolved_by__id", "c")
    )
    prior_comms_map = dict(
        CommunicationAttempt.objects.filter(
            created_at__gte=prior_start, created_at__lt=prior_end,
            staff_user_id__in=dispatcher_uids,
        )
        .values_list("staff_user__id")
        .annotate(c=Count("id"))
        .values_list("staff_user__id", "c")
    )
    prior_res_map = {}
    prior_rev_map = {}
    for row in Reservation.objects.filter(
        created_at__gte=prior_start, created_at__lt=prior_end, created_by_id__in=dispatcher_uids,
    ).values("created_by__id").annotate(
        count=Count("id"),
        revenue=Coalesce(Sum("total_price"), Decimal("0"), output_field=DecimalField()),
    ):
        prior_res_map[row["created_by__id"]] = row["count"]
        prior_rev_map[row["created_by__id"]] = row["revenue"]
    prior_emails_map = dict(
        EmailLog.objects.filter(
            sent_at__gte=prior_start, sent_at__lt=prior_end, sent_by_id__in=dispatcher_uids,
        )
        .values_list("sent_by__id")
        .annotate(c=Count("id"))
        .values_list("sent_by__id", "c")
    )

    trends = {}
    for s in staff_completions:
        uid = s["resolved_by__id"]
        trends[(uid, "completions")] = _build_trend(s["count"], prior_completions_map.get(uid, 0))
    for s in staff_comms:
        uid = s["staff_user__id"]
        trends[(uid, "comms")] = _build_trend(s["total"], prior_comms_map.get(uid, 0))
    for s in staff_reservations:
        uid = s["created_by__id"]
        trends[(uid, "reservations")] = _build_trend(s["count"], prior_res_map.get(uid, 0))
        trends[(uid, "revenue")] = _build_trend(float(s["revenue"]), float(prior_rev_map.get(uid, Decimal("0"))))
    for s in staff_emails:
        uid = s["sent_by__id"]
        trends[(uid, "emails")] = _build_trend(s["total"], prior_emails_map.get(uid, 0))
    trends_dict = {f"{uid}_{metric}": t for (uid, metric), t in trends.items()}

    # Merge into daily_summary: list of {date, staff: [{name, ...metrics}]}
    # Collect all (day, uid) pairs
    all_day_uids = set()
    for m in [activity_first, res_count_map, tasks_by_day, assigns_by_day, legs_mod_by_day, comms_by_day, emails_by_day]:
        all_day_uids.update(m.keys())

    daily_data = defaultdict(dict)  # day_str → uid → metrics
    for (day_str, uid) in all_day_uids:
        key = (day_str, uid)
        daily_data[day_str][uid] = {
            "name": staff_names.get(uid, f"User #{uid}"),
            "user_id": uid,
            "first_in": activity_first.get(key, "").isoformat() if activity_first.get(key) else "",
            "last_out": activity_last.get(key, "").isoformat() if activity_last.get(key) else "",
            "reservations": res_count_map.get(key, 0),
            "revenue": res_rev_map.get(key, 0),
            "tasks": tasks_by_day.get(key, 0),
            "assigns": assigns_by_day.get(key, 0),
            "legs_modified": legs_mod_by_day.get(key, 0),
            "comms": comms_by_day.get(key, 0),
            "emails": emails_by_day.get(key, 0),
        }

    daily_summary = []
    for day_str in sorted(daily_data.keys(), reverse=True):
        staff_list = sorted(daily_data[day_str].values(), key=lambda x: x["name"])
        total = sum(
            s["reservations"] + s["tasks"] + s["assigns"] + s["legs_modified"] + s["comms"] + s["emails"]
            for s in staff_list
        )
        daily_summary.append({
            "date": day_str,
            "total_actions": total,
            "staff": staff_list,
        })

    # ═══════════════════════════════════════════════════════════════════
    # CONSOLIDATED ROSTER — one row per staffer, headline stats for the
    # period. Powers the "pick a person" launcher; each row links to the
    # per-person deep dive (staff_detail).
    # ═══════════════════════════════════════════════════════════════════
    _last_active_by_uid = {}
    for row in activity_by_day:
        uid = row["user__id"]
        la = row["last_active"]
        if uid not in _last_active_by_uid or (la and la > _last_active_by_uid[uid]):
            _last_active_by_uid[uid] = la

    roster_uids = (
        set(_res_by_uid) | set(_completions_by_uid)
        | set(_comms_by_uid) | set(_emails_by_uid) | set(_last_active_by_uid)
    )
    staff_roster = []
    for uid in roster_uids:
        res = _res_by_uid.get(uid, {"count": 0, "revenue": Decimal("0")})
        staff_roster.append({
            "user_id": uid,
            "name": staff_names.get(uid, f"User #{uid}"),
            "reservations": res["count"],
            "revenue": res["revenue"],
            "tasks": _completions_by_uid.get(uid, 0),
            "comms": _comms_by_uid.get(uid, {}).get("total", 0),
            "emails": _emails_by_uid.get(uid, {}).get("total", 0),
            "last_active": _last_active_by_uid.get(uid),
        })
    staff_roster.sort(key=lambda r: (r["revenue"], r["tasks"], r["comms"]), reverse=True)

    context = {
        "range_days": days_back,
        "range_start": range_start,
        "staff_roster": staff_roster,
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
        # Trend indicators
        "trends": trends_dict,
        # Daily staff summary
        "daily_summary_json": json.dumps(daily_summary),
    }
    return render(request, "dispatching/staff_metrics.html", context)


@login_required(login_url="login")
@user_passes_test(_is_superuser, login_url="dashboard")
def revenue_kpis_view(request):
    """
    Revenue & source-attribution dashboard.

    Sister page to staff_kpis_view, but answers a different question:
    "Where is the actual money coming from?" Every revenue figure on this
    page is filtered by Reservation.is_paid=True (the persisted column
    maintained by payment.signals), so unpaid/pending reservations never
    inflate the totals.

    Window selection (querystring):
      ?from=YYYY-MM-DD&to=YYYY-MM-DD   custom inclusive date range
      ?preset=last_month|this_month|last_7|last_30|last_90|ytd|all
      ?d=N                             rolling N-day window (legacy/default)
    """
    import json as _json
    from datetime import date as _date, timedelta as _td
    from decimal import Decimal as _Decimal
    from ops import kpis as kpi

    today = timezone.localdate()

    # ── Parse window params ──────────────────────────────────
    from_param = (request.GET.get("from") or "").strip()
    to_param = (request.GET.get("to") or "").strip()
    preset = (request.GET.get("preset") or "").strip()

    custom_from = None
    custom_to = None
    range_label = ""
    range_mode = "rolling"

    def _parse(s):
        try:
            return _date.fromisoformat(s)
        except ValueError:
            return None

    if from_param or to_param:
        custom_from = _parse(from_param) or today - _td(days=29)
        custom_to = _parse(to_param) or today
        if custom_from > custom_to:
            custom_from, custom_to = custom_to, custom_from
        range_mode = "custom"
        range_label = f"{custom_from.isoformat()} → {custom_to.isoformat()}"
    elif preset == "this_month":
        custom_from = today.replace(day=1)
        custom_to = today
        range_mode = "preset"
        range_label = f"This month ({custom_from.strftime('%B %Y')})"
    elif preset == "last_month":
        first_this = today.replace(day=1)
        last_prev = first_this - _td(days=1)
        custom_from = last_prev.replace(day=1)
        custom_to = last_prev
        range_mode = "preset"
        range_label = f"Last month ({custom_from.strftime('%B %Y')})"
    elif preset == "ytd":
        custom_from = today.replace(month=1, day=1)
        custom_to = today
        range_mode = "preset"
        range_label = f"Year to date ({today.year})"
    elif preset == "all":
        # All-time: leave the start open; resolve_range floors it to 2000-01-01.
        custom_from = None
        custom_to = today
        range_mode = "preset"
        range_label = "All time"
    elif preset in ("last_7", "last_30", "last_90"):
        days_n = int(preset.split("_")[1])
        custom_to = today
        custom_from = today - _td(days=days_n - 1)
        range_mode = "preset"
        range_label = f"Last {days_n} days"
    else:
        # Legacy ?d=N rolling window
        try:
            days_n = int(request.GET.get("d", "30"))
        except ValueError:
            days_n = 30
        days_n = max(1, min(days_n, 365))
        custom_to = today
        custom_from = today - _td(days=days_n - 1)
        preset = f"last_{days_n}" if days_n in (7, 30, 90) else ""
        range_label = f"Last {days_n} days"

    # Convert the inclusive date pair into the half-open datetime range
    # the kpi helpers expect.
    start_dt, end_dt = kpi.resolve_range(start=custom_from, end=custom_to)

    overview = kpi.overview(start_dt, end_dt)
    sources = kpi.by_source(start_dt, end_dt)
    agents = kpi.by_travel_agent(start_dt, end_dt)
    agent_totals = kpi.travel_agent_totals(start_dt, end_dt)
    routes = kpi.by_route(start_dt, end_dt)
    vehicles = kpi.by_vehicle(start_dt, end_dt)
    trend = kpi.revenue_trend(start_dt, end_dt)

    def _ser(value):
        if isinstance(value, _Decimal):
            return float(value)
        return value

    trend_payload = [
        {
            "day": row["day"].isoformat() if row["day"] else None,
            "revenue": _ser(row["paid_revenue"] or 0),
            "bookings": row["bookings"] or 0,
        }
        for row in trend
    ]

    context = {
        "range_from": custom_from.isoformat() if custom_from else "",
        "range_to": custom_to.isoformat(),
        "range_label": range_label,
        "range_mode": range_mode,
        "active_preset": preset,
        "today_iso": today.isoformat(),
        "overview": overview,
        "sources": sources,
        "agents": agents,
        "agent_totals": agent_totals,
        "routes": routes,
        "vehicles": vehicles,
        "trend_json": _json.dumps(trend_payload),
    }
    return render(request, "dispatching/revenue_kpis.html", context)


@login_required(login_url="login")
@user_passes_test(_is_superuser, login_url="dashboard")
def staff_kpis_view(request):
    """
    Dedicated KPI dashboard for workload balance, per-task-type specialization,
    and active-time / idle-detection across dispatchers. Companion to
    staff_metrics_view; this page is comparison-focused, not activity-feed.
    """
    from reservations.models import Reservation, Leg, AuditLog
    from users.models import UserProfile
    from django.contrib.auth.models import User as AuthUser
    from datetime import date as _date, datetime as _dt
    import pytz

    eastern = pytz.timezone("US/Eastern")
    now = timezone.now()
    today = timezone.localdate()

    # ── Time range — supports three modes ──
    #   ?date=YYYY-MM-DD             single day
    #   ?from=YYYY-MM-DD&to=YYYY-MM-DD   custom range (inclusive)
    #   ?range=N                     rolling N calendar days ending today (default)
    date_param = request.GET.get("date", "").strip()
    from_param = request.GET.get("from", "").strip()
    to_param = request.GET.get("to", "").strip()
    range_param = request.GET.get("range", "7").strip()

    range_mode = None
    view_date = None
    custom_from = None
    custom_to = None

    if date_param:
        try:
            view_date = _date.fromisoformat(date_param)
            range_mode = "single"
        except ValueError:
            pass

    if range_mode is None and from_param and to_param:
        try:
            custom_from = _date.fromisoformat(from_param)
            custom_to = _date.fromisoformat(to_param)
            if custom_from > custom_to:
                custom_from, custom_to = custom_to, custom_from
            range_mode = "custom"
        except ValueError:
            pass

    if range_mode is None:
        try:
            days_back_param = int(range_param)
        except ValueError:
            days_back_param = 7
        days_back_param = min(max(days_back_param, 1), 90)
        range_mode = "rolling"

    if range_mode == "single":
        start_date = view_date
        end_date_excl = view_date + timedelta(days=1)
        days_back = 1
        # Windows-safe day formatting (no %-d)
        range_label = view_date.strftime("%A, %b ") + str(view_date.day) + view_date.strftime(", %Y")
    elif range_mode == "custom":
        start_date = custom_from
        end_date_excl = custom_to + timedelta(days=1)
        days_back = (custom_to - custom_from).days + 1
        range_label = (
            custom_from.strftime("%b ") + str(custom_from.day) +
            " – " +
            custom_to.strftime("%b ") + str(custom_to.day) + custom_to.strftime(", %Y")
        )
    else:  # rolling
        days_back = days_back_param
        start_date = today - timedelta(days=days_back - 1)
        end_date_excl = today + timedelta(days=1)
        range_label = "Today" if days_back == 1 else f"Last {days_back} days"

    range_start = eastern.localize(_dt.combine(start_date, _dt.min.time()))
    range_end = eastern.localize(_dt.combine(end_date_excl, _dt.min.time()))

    # ── Dispatcher allowlist (mirrors staff_metrics_view pattern) ──
    non_dispatcher_profile_uids = set(
        UserProfile.objects.filter(
            Q(is_driver=True) | Q(is_travel_agent=True)
        ).values_list("user_id", flat=True)
    )
    dispatcher_users = list(
        AuthUser.objects.filter(is_staff=True)
        .exclude(id__in=non_dispatcher_profile_uids)
        .values("id", "first_name", "username")
    )
    dispatcher_uids = {u["id"] for u in dispatcher_users}
    staff_names = {u["id"]: (u["first_name"] or u["username"]) for u in dispatcher_users}

    # ═══════════════════════════════════════════════════════════════════
    # SECTION 0 — BOOKINGS & REVENUE per dispatcher per day  (headline KPI)
    # ═══════════════════════════════════════════════════════════════════
    from django.db.models import Sum, DecimalField
    from django.db.models.functions import Coalesce, TruncDate as _TruncDate
    from decimal import Decimal

    bookings_qs = (
        Reservation.objects
        .filter(
            created_at__gte=range_start,
            created_at__lt=range_end,
            created_by_id__in=dispatcher_uids,
        )
        .annotate(day=_TruncDate("created_at"))
        .values("day", "created_by_id")
        .annotate(
            count=Count("id"),
            revenue=Coalesce(Sum("total_price"), Decimal("0"), output_field=DecimalField()),
        )
    )

    # Build complete day list for the range so the chart has zero-fills
    day_list = [start_date + timedelta(days=i) for i in range(days_back)]
    day_iso_list = [d.isoformat() for d in day_list]

    # bookings[uid][day_iso] = {"count": int, "revenue": float}
    bookings = defaultdict(lambda: {di: {"count": 0, "revenue": 0.0} for di in day_iso_list})
    for row in bookings_qs:
        uid = row["created_by_id"]
        day_iso = row["day"].isoformat()
        if day_iso in bookings[uid]:
            bookings[uid][day_iso] = {
                "count": row["count"],
                "revenue": float(row["revenue"]),
            }

    booking_leaderboard = []
    for uid in dispatcher_uids:
        if uid not in bookings:
            continue
        daily = bookings[uid]
        total_count = sum(d["count"] for d in daily.values())
        total_rev = sum(d["revenue"] for d in daily.values())
        if total_count == 0:
            continue
        active_days = sum(1 for d in daily.values() if d["count"] > 0)
        # best day = highest revenue
        best_day_iso = max(daily.keys(), key=lambda k: daily[k]["revenue"])
        best_day = daily[best_day_iso]
        booking_leaderboard.append({
            "user_id": uid,
            "name": staff_names.get(uid, f"User #{uid}"),
            "total_count": total_count,
            "total_revenue": round(total_rev, 2),
            "avg_deal_size": round(total_rev / total_count, 2) if total_count > 0 else 0,
            "active_days": active_days,
            "avg_per_day": round(total_count / active_days, 1) if active_days > 0 else 0,
            "avg_rev_per_day": round(total_rev / active_days, 2) if active_days > 0 else 0,
            "best_day_iso": best_day_iso,
            "best_day_count": best_day["count"],
            "best_day_revenue": round(best_day["revenue"], 2),
        })
    booking_leaderboard.sort(key=lambda r: r["total_revenue"], reverse=True)

    booking_chart_payload = {
        "labels": day_iso_list,
        "series": [
            {
                "name": entry["name"],
                "user_id": entry["user_id"],
                "counts": [bookings[entry["user_id"]][di]["count"] for di in day_iso_list],
                "revenues": [round(bookings[entry["user_id"]][di]["revenue"], 2) for di in day_iso_list],
            }
            for entry in booking_leaderboard
        ],
    }
    booking_totals = {
        "count": sum(e["total_count"] for e in booking_leaderboard),
        "revenue": round(sum(e["total_revenue"] for e in booking_leaderboard), 2),
    }
    booking_lookup = {e["user_id"]: e for e in booking_leaderboard}

    # ═══════════════════════════════════════════════════════════════════
    # SECTION 0b — PERIOD-OVER-PERIOD + PRODUCER SCORECARD + EXEC SUMMARY
    # ═══════════════════════════════════════════════════════════════════
    def _bookings_window(w_start, w_end):
        agg = (
            Reservation.objects.filter(
                created_at__gte=w_start, created_at__lt=w_end,
                created_by_id__in=dispatcher_uids,
            )
            .values("created_by_id")
            .annotate(
                count=Count("id"),
                revenue=Coalesce(Sum("total_price"), Decimal("0"), output_field=DecimalField()),
            )
        )
        return {r["created_by_id"]: {"count": r["count"], "revenue": float(r["revenue"])} for r in agg}

    # Previous equivalent window, immediately preceding the current one.
    _window = range_end - range_start
    prev_bookings = _bookings_window(range_start - _window, range_start)

    def _delta_pct(cur, prev):
        if prev and prev > 0:
            return round((cur - prev) / prev * 100, 1)
        return None

    producer_rows = []
    for uid, e in booking_lookup.items():
        prev = prev_bookings.get(uid, {"count": 0, "revenue": 0.0})
        producer_rows.append({
            **e,
            "prev_revenue": round(prev["revenue"], 2),
            "prev_count": prev["count"],
            "rev_delta": round(e["total_revenue"] - prev["revenue"], 2),
            "rev_delta_pct": _delta_pct(e["total_revenue"], prev["revenue"]),
            "count_delta": e["total_count"] - prev["count"],
        })
    producer_rows.sort(key=lambda r: r["total_revenue"], reverse=True)

    team_rev_prev = round(sum(p["revenue"] for p in prev_bookings.values()), 2)
    team_bk_prev = sum(p["count"] for p in prev_bookings.values())
    movers = [r for r in producer_rows if r["rev_delta"] > 0]
    biggest_mover = max(movers, key=lambda r: r["rev_delta"]) if movers else None
    exec_summary = {
        "revenue": booking_totals["revenue"],
        "revenue_prev": team_rev_prev,
        "revenue_delta_pct": _delta_pct(booking_totals["revenue"], team_rev_prev),
        "bookings": booking_totals["count"],
        "bookings_prev": team_bk_prev,
        "bookings_delta_pct": _delta_pct(booking_totals["count"], team_bk_prev),
        "avg_deal": round(booking_totals["revenue"] / booking_totals["count"], 2) if booking_totals["count"] else 0,
        "top_producer": producer_rows[0] if producer_rows else None,
        "biggest_mover": biggest_mover,
    }

    # ═══════════════════════════════════════════════════════════════════
    # SECTION 1 — WORKLOAD BALANCE (current snapshot)
    # ═══════════════════════════════════════════════════════════════════
    open_qs = OperationalTask.objects.filter(status__in=list(OperationalTask.OPEN_STATUSES))

    open_per_uid = dict(
        open_qs.filter(assigned_to_id__in=dispatcher_uids)
        .values("assigned_to_id")
        .annotate(c=Count("id"))
        .values_list("assigned_to_id", "c")
    )
    overdue_per_uid = dict(
        open_qs.filter(assigned_to_id__in=dispatcher_uids, due_at__lt=now)
        .values("assigned_to_id")
        .annotate(c=Count("id"))
        .values_list("assigned_to_id", "c")
    )
    snoozed_per_uid = dict(
        OperationalTask.objects.filter(
            assigned_to_id__in=dispatcher_uids, status=OperationalTask.Status.SNOOZED,
        )
        .values("assigned_to_id")
        .annotate(c=Count("id"))
        .values_list("assigned_to_id", "c")
    )
    oldest_per_uid = dict(
        open_qs.filter(assigned_to_id__in=dispatcher_uids)
        .values("assigned_to_id")
        .annotate(oldest=Min("created_at"))
        .values_list("assigned_to_id", "oldest")
    )
    unassigned_open = open_qs.filter(assigned_to__isnull=True).count()

    workload_rows = []
    for uid in dispatcher_uids:
        cnt = open_per_uid.get(uid, 0)
        if cnt == 0 and overdue_per_uid.get(uid, 0) == 0 and snoozed_per_uid.get(uid, 0) == 0:
            continue  # skip dispatchers with literally nothing on their plate
        workload_rows.append({
            "user_id": uid,
            "name": staff_names.get(uid, f"User #{uid}"),
            "open": cnt,
            "overdue": overdue_per_uid.get(uid, 0),
            "snoozed": snoozed_per_uid.get(uid, 0),
            "oldest": oldest_per_uid.get(uid),
        })
    workload_rows.sort(key=lambda r: r["open"], reverse=True)
    total_workload = sum(r["open"] for r in workload_rows)
    mean_workload = round(total_workload / len(workload_rows), 1) if workload_rows else 0

    workload_chart_json = json.dumps([
        {"name": r["name"], "open": r["open"], "overdue": r["overdue"]}
        for r in workload_rows
    ])

    # ═══════════════════════════════════════════════════════════════════
    # SECTION 2 — SPECIALIZATION MATRIX (range-based)
    # ═══════════════════════════════════════════════════════════════════
    matrix_rows_qs = (
        OperationalTask.objects.filter(
            status="completed",
            resolved_at__gte=range_start,
            resolved_at__lt=range_end,
            resolved_by_id__in=dispatcher_uids,
        )
        .values("resolved_by_id", "task_type")
        .annotate(
            count=Count("id"),
            avg_seconds=Avg(F("resolved_at") - F("created_at")),
        )
    )

    task_types = list(OperationalTask.TaskType.choices)  # [(code, label), ...]
    type_codes = [c for c, _ in task_types]

    # matrix[uid][type_code] = {"count": int, "avg_seconds": float}
    matrix = defaultdict(lambda: {tc: {"count": 0, "avg_seconds": None} for tc in type_codes})
    for row in matrix_rows_qs:
        uid = row["resolved_by_id"]
        tc = row["task_type"]
        avg_td = row["avg_seconds"]
        avg_secs = avg_td.total_seconds() if avg_td else None
        matrix[uid][tc] = {"count": row["count"], "avg_seconds": avg_secs}

    # Per-row totals + per-column highlight metadata
    col_max_count = {tc: 0 for tc in type_codes}
    col_min_avg = {tc: None for tc in type_codes}
    for uid, cells in matrix.items():
        for tc in type_codes:
            cell = cells[tc]
            if cell["count"] > col_max_count[tc]:
                col_max_count[tc] = cell["count"]
            if cell["avg_seconds"] is not None:
                if col_min_avg[tc] is None or cell["avg_seconds"] < col_min_avg[tc]:
                    col_min_avg[tc] = cell["avg_seconds"]

    def _fmt_dur(secs):
        if secs is None:
            return ""
        secs = int(secs)
        if secs < 60:
            return f"{secs}s"
        m = secs // 60
        if m < 60:
            return f"{m}m"
        h = m // 60
        rem_m = m % 60
        if h < 24:
            return f"{h}h {rem_m}m" if rem_m else f"{h}h"
        d = h // 24
        return f"{d}d {h % 24}h"

    matrix_table = []
    for uid, cells in matrix.items():
        row_total = sum(cells[tc]["count"] for tc in type_codes)
        if row_total == 0:
            continue
        row_cells = []
        for tc in type_codes:
            cell = cells[tc]
            row_cells.append({
                "type_code": tc,
                "count": cell["count"],
                "avg_label": _fmt_dur(cell["avg_seconds"]),
                "is_top_count": cell["count"] > 0 and cell["count"] == col_max_count[tc],
                "is_fastest": (
                    cell["avg_seconds"] is not None
                    and col_min_avg[tc] is not None
                    and cell["avg_seconds"] == col_min_avg[tc]
                ),
            })
        matrix_table.append({
            "user_id": uid,
            "name": staff_names.get(uid, f"User #{uid}"),
            "cells": row_cells,
            "total": row_total,
        })
    matrix_table.sort(key=lambda r: r["total"], reverse=True)

    col_totals = []
    for tc in type_codes:
        col_totals.append(sum(matrix[uid][tc]["count"] for uid in matrix))

    # ═══════════════════════════════════════════════════════════════════
    # SECTION 2b — RESPONSIVENESS / SLA (tasks resolved in range)
    # ═══════════════════════════════════════════════════════════════════
    resp_agg = (
        OperationalTask.objects.filter(
            status="completed",
            resolved_at__gte=range_start,
            resolved_at__lt=range_end,
            resolved_by_id__in=dispatcher_uids,
        )
        .values("resolved_by_id")
        .annotate(
            resolved=Count("id"),
            avg_secs=Avg(F("resolved_at") - F("created_at")),
            on_time=Count("id", filter=Q(resolved_at__lte=F("due_at"))),
        )
    )
    responsiveness_rows = []
    for row in resp_agg:
        uid = row["resolved_by_id"]
        resolved = row["resolved"]
        avg_secs = row["avg_secs"].total_seconds() if row["avg_secs"] else None
        on_time = row["on_time"] or 0
        responsiveness_rows.append({
            "user_id": uid,
            "name": staff_names.get(uid, f"User #{uid}"),
            "resolved": resolved,
            "avg_label": _fmt_dur(avg_secs),
            "avg_secs": avg_secs or 0,
            "on_time": on_time,
            "late": resolved - on_time,
            "on_time_pct": round(on_time / resolved * 100) if resolved else 0,
            "open_overdue": overdue_per_uid.get(uid, 0),
        })
    responsiveness_rows.sort(key=lambda r: (-r["resolved"], r["avg_secs"]))
    _tot_resolved = sum(r["resolved"] for r in responsiveness_rows)
    _tot_ontime = sum(r["on_time"] for r in responsiveness_rows)
    responsiveness_team = {
        "resolved": _tot_resolved,
        "on_time_pct": round(_tot_ontime / _tot_resolved * 100) if _tot_resolved else 0,
        "overdue_now": sum(overdue_per_uid.values()),
    }

    # ═══════════════════════════════════════════════════════════════════
    # SECTION 3 — ACTIVE HOURS / IDLE DETECTION (per-day, Eastern local)
    # ═══════════════════════════════════════════════════════════════════
    IDLE_THRESHOLD_SEC = 30 * 60  # matches middleware DEDUP_SECONDS
    # A day only counts as a "working day" if the dispatcher logged at least
    # this much active time on it. Anything less is treated as a drive-by
    # check-in and excluded from the active-hours rollup so it doesn't drag
    # down comparisons (still visible in Daily Activity Detail).
    MIN_WORKING_DAY_SEC = 60 * 60  # 1 hour

    events = list(
        StaffActivity.objects.filter(
            created_at__gte=range_start,
            created_at__lt=range_end,
            user_id__in=dispatcher_uids,
        )
        .values("user_id", "created_at")
        .order_by("user_id", "created_at")
    )

    # Group events by user → then by local (Eastern) date so a late-evening
    # session doesn't get split across the UTC boundary.
    events_by_uid = defaultdict(list)
    for e in events:
        events_by_uid[e["user_id"]].append(e["created_at"])

    def _fmt_clock(dt):
        return dt.strftime("%I:%M %p").lstrip("0")

    def _fmt_gap(secs):
        m = int(secs // 60)
        if m < 60:
            return f"{m}m"
        h = m // 60
        rem = m % 60
        return f"{h}h {rem}m" if rem else f"{h}h"

    # daily_breakdown_by_uid[uid] = list of per-day stat dicts
    daily_breakdown_by_uid = defaultdict(list)
    for uid, ts_list in events_by_uid.items():
        if not ts_list:
            continue
        by_day = defaultdict(list)
        for ts in ts_list:
            local_ts = ts.astimezone(eastern)
            by_day[local_ts.date()].append(local_ts)
        for day in sorted(by_day.keys()):
            day_ts = sorted(by_day[day])
            first = day_ts[0]
            last = day_ts[-1]
            active_sec = 0
            idle_sec = 0
            sessions = 1
            gaps = []
            for i in range(1, len(day_ts)):
                gap = (day_ts[i] - day_ts[i - 1]).total_seconds()
                if gap <= IDLE_THRESHOLD_SEC:
                    active_sec += gap
                else:
                    idle_sec += gap
                    sessions += 1
                    gaps.append({
                        "start": _fmt_clock(day_ts[i - 1]),
                        "end": _fmt_clock(day_ts[i]),
                        "duration": _fmt_gap(gap),
                        "is_long": gap >= 3600,  # ≥ 1 hour away
                    })
            daily_breakdown_by_uid[uid].append({
                "date": day.isoformat(),
                "weekday": day.strftime("%a"),
                "first": _fmt_clock(first),
                "last": _fmt_clock(last),
                "active_hrs": round(active_sec / 3600, 1),
                "idle_hrs": round(idle_sec / 3600, 1),
                "active_sec": active_sec,
                "idle_sec": idle_sec,
                "sessions": sessions,
                "gaps": gaps,
                "gap_count": len(gaps),
                "is_working_day": active_sec >= MIN_WORKING_DAY_SEC,
            })

    # Roll up per-uid totals using ONLY real working days (≥ 1 active hour).
    # Drive-by check-ins are intentionally excluded from both numerator and
    # denominator so they don't dilute comparisons across people who work
    # different numbers of days per week.
    active_hours_by_uid = {}
    for uid in dispatcher_uids:
        days = daily_breakdown_by_uid.get(uid, [])
        working_days = [d for d in days if d["is_working_day"]]
        active_hours_by_uid[uid] = {
            "active_sec": sum(d["active_sec"] for d in working_days),
            "idle_sec": sum(d["idle_sec"] for d in working_days),
            "sessions": sum(d["sessions"] for d in working_days),
            "active_days": len(working_days),
            "driveby_days": len(days) - len(working_days),
        }

    # Pivot per-day breakdowns into a day-first structure for "compare every day"
    day_to_rows = defaultdict(list)
    for uid, days in daily_breakdown_by_uid.items():
        for d in days:
            day_to_rows[d["date"]].append({
                "user_id": uid,
                "name": staff_names.get(uid, f"User #{uid}"),
                **d,
            })
    daily_activity = []
    from datetime import date as _date
    for day_iso in sorted(day_to_rows.keys(), reverse=True):
        rows = sorted(day_to_rows[day_iso], key=lambda x: x["first"])
        d_obj = _date.fromisoformat(day_iso)
        daily_activity.append({
            "date": day_iso,
            "weekday": d_obj.strftime("%A"),
            "display": d_obj.strftime("%b %d"),
            "rows": rows,
            "total_active_hrs": round(sum(r["active_hrs"] for r in rows), 1),
            "dispatcher_count": len(rows),
        })

    active_table = []
    for uid in dispatcher_uids:
        stats = active_hours_by_uid.get(uid, {"active_sec": 0, "idle_sec": 0, "sessions": 0, "active_days": 0})
        active_hrs = stats["active_sec"] / 3600
        idle_hrs = stats["idle_sec"] / 3600
        if active_hrs == 0:
            continue
        active_pct = round(active_hrs / (active_hrs + idle_hrs) * 100) if (active_hrs + idle_hrs) > 0 else 0
        avg_per_day = round(active_hrs / stats["active_days"], 1) if stats["active_days"] > 0 else 0
        active_table.append({
            "user_id": uid,
            "name": staff_names.get(uid, f"User #{uid}"),
            "active_hrs": round(active_hrs, 1),
            "idle_hrs": round(idle_hrs, 1),
            "active_pct": active_pct,
            "avg_per_day": avg_per_day,
            "working_days": stats["active_days"],
            "driveby_days": stats["driveby_days"],
            "sessions": stats["sessions"],
        })
    active_table.sort(key=lambda r: r["active_hrs"], reverse=True)

    active_chart_json = json.dumps([
        {"name": r["name"], "active": r["active_hrs"], "idle": r["idle_hrs"]}
        for r in sorted(active_table, key=lambda x: x["active_hrs"], reverse=True)
    ])

    # ═══════════════════════════════════════════════════════════════════
    # SECTION 4 — SIDE-BY-SIDE COMPARISON (radar chart input)
    # ═══════════════════════════════════════════════════════════════════
    # Money-first axes: revenue, bookings, deal size are the headline KPIs.
    # Tasks/day and active hrs/day are kept as supporting context but
    # vanity proxies (open load, actions/hr, breadth) were intentionally dropped.
    raw_metrics = {}
    for uid in dispatcher_uids:
        active = next((r for r in active_table if r["user_id"] == uid), None)
        matrix_row = next((r for r in matrix_table if r["user_id"] == uid), None)
        booking = booking_lookup.get(uid)
        raw_metrics[uid] = {
            "name": staff_names.get(uid, f"User #{uid}"),
            "revenue_per_day": booking["avg_rev_per_day"] if booking else 0,
            "bookings_per_day": booking["avg_per_day"] if booking else 0,
            "avg_deal_size": booking["avg_deal_size"] if booking else 0,
            "tasks_per_day": round((matrix_row["total"] if matrix_row else 0) / days_back, 2),
            "active_per_day": active["avg_per_day"] if active else 0,
        }

    axes = ["revenue_per_day", "bookings_per_day", "avg_deal_size", "tasks_per_day", "active_per_day"]
    axis_max = {a: max([m[a] for m in raw_metrics.values()] + [0]) for a in axes}
    comparison_payload = []
    for uid, m in raw_metrics.items():
        if all(m[a] == 0 for a in axes):
            continue
        comparison_payload.append({
            "user_id": uid,
            "name": m["name"],
            "raw": {a: m[a] for a in axes},
            "norm": {a: round((m[a] / axis_max[a]) * 100, 1) if axis_max[a] else 0 for a in axes},
        })
    comparison_payload.sort(key=lambda x: x["name"])

    context = {
        "range_days": days_back,
        "range_mode": range_mode,
        "range_label": range_label,
        "range_start_date": start_date.isoformat(),
        "range_end_date": (end_date_excl - timedelta(days=1)).isoformat(),
        "today_iso": today.isoformat(),
        # Exec summary + producer scorecard (period-over-period)
        "exec_summary": exec_summary,
        "producer_rows": producer_rows,
        # Responsiveness / SLA
        "responsiveness_rows": responsiveness_rows,
        "responsiveness_team": responsiveness_team,
        # Section 0 — bookings & revenue (headline)
        "booking_leaderboard": booking_leaderboard,
        "booking_chart_json": json.dumps(booking_chart_payload),
        "booking_totals": booking_totals,
        # Section 1
        "workload_rows": workload_rows,
        "total_workload": total_workload,
        "mean_workload": mean_workload,
        "unassigned_open": unassigned_open,
        "workload_chart_json": workload_chart_json,
        # Section 2
        "matrix_table": matrix_table,
        "task_type_choices": task_types,
        "matrix_col_totals": list(zip(type_codes, col_totals)),
        # Section 3
        "active_table": active_table,
        "active_chart_json": active_chart_json,
        "daily_activity": daily_activity,
        # Section 4
        "comparison_json": json.dumps(comparison_payload),
        "comparison_axes": axes,
    }
    return render(request, "dispatching/staff_kpis.html", context)


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

    staff_user = get_object_or_404(User, id=user_id, is_staff=True)
    # Block detail view for drivers/travel agents
    from users.models import UserProfile
    try:
        profile = staff_user.profile
        if profile.is_driver or profile.is_travel_agent:
            raise Http404
    except UserProfile.DoesNotExist:
        pass
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

    # ── Unified chronological action feed ──
    from reservations.models import AuditLog as _AL
    unified_feed = []

    # 1. Staff activities (excl page views — already filtered)
    for act in recent_activities:
        unified_feed.append({
            "ts": act.created_at.isoformat(),
            "type": "activity",
            "icon": "clipboard-check",
            "color": "text-muted",
            "text": act.get_action_type_display(),
            "detail": act.task.title[:60] if act.task else "",
            "link": f"/dispatching/tasks/{act.task.id}/" if act.task else "",
        })

    # 2. Communications
    for comm in recent_comms:
        unified_feed.append({
            "ts": comm.created_at.isoformat(),
            "type": "comm",
            "icon": "chat-dots",
            "color": "text-purple",
            "text": f"{comm.get_channel_display()} — {comm.get_outcome_display()}",
            "detail": comm.contact_value or "",
            "link": f"/dispatching/tasks/{comm.task.id}/" if comm.task else "",
        })

    # 3. Emails
    for email in recent_emails:
        unified_feed.append({
            "ts": email.sent_at.isoformat(),
            "type": "email",
            "icon": "envelope",
            "color": "text-primary",
            "text": email.get_email_type_display(),
            "detail": email.recipient_email,
            "link": f"/dispatching/reservation/{email.reservation.uuid}/" if email.reservation_id else "",
        })

    # 4. Change history entries
    for ch in change_history:
        link = f"/dispatching/reservation/{ch['reservation_uuid']}/" if ch.get("reservation_uuid") else ""
        unified_feed.append({
            "ts": ch["timestamp"].isoformat(),
            "type": "change",
            "icon": "pencil-square",
            "color": "text-warning",
            "text": f"{ch['field'].replace('_', ' ').title()} on {ch['model']} #{ch['object_id']}",
            "detail": f"{ch['old'] or '(empty)'} → {ch['new']}" if ch.get("new") else "",
            "link": link,
        })

    # 5. AuditLog entries for this user
    audit_entries = _AL.objects.filter(
        user=staff_user,
        timestamp__gte=range_start,
        timestamp__lt=range_end,
    ).order_by("-timestamp")[:50]
    for entry in audit_entries:
        unified_feed.append({
            "ts": entry.timestamp.isoformat(),
            "type": "audit",
            "icon": "shield-check",
            "color": "text-info",
            "text": entry.get_action_display(),
            "detail": f"{entry.model_name} #{entry.object_id}" + (f" — {entry.field_name}" if entry.field_name else ""),
            "link": "",
        })

    # Sort and cap
    unified_feed.sort(key=lambda x: x["ts"], reverse=True)
    unified_feed = unified_feed[:200]

    # Count by type for filter badges
    feed_type_counts = defaultdict(int)
    for item in unified_feed:
        feed_type_counts[item["type"]] += 1
    feed_type_counts = dict(feed_type_counts)

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
        # Unified feed
        "unified_feed_json": json.dumps(unified_feed),
        "feed_type_counts": feed_type_counts,
        # Navigation
        "all_staff": all_staff,
    }
    return render(request, "dispatching/staff_detail.html", context)


# ═══════════════════════════════════════════════════════════════════════════
#  Admin Tasks Hub
# ═══════════════════════════════════════════════════════════════════════════

@login_required(login_url="login")
@user_passes_test(_is_superuser, login_url="login")
def admin_tasks_view(request):
    """
    Management-oriented task oversight page with table view, rich filtering,
    summary stats, staff workload, and bulk action support.
    """
    from django.core.paginator import Paginator

    now = timezone.now()
    today = timezone.localdate()
    OPEN = list(OperationalTask.OPEN_STATUSES)

    # ── Summary stats (single conditional-aggregate query) ──
    stats = OperationalTask.objects.aggregate(
        total_open=Count("id", filter=Q(status__in=OPEN)),
        overdue=Count("id", filter=Q(status__in=OPEN, due_at__lt=now)),
        unassigned=Count("id", filter=Q(status__in=OPEN, assigned_to__isnull=True)),
        due_today=Count("id", filter=Q(status__in=OPEN, due_at__date=today)),
        high_critical=Count("id", filter=Q(status__in=OPEN, priority__lte=2)),
        completed_today=Count(
            "id", filter=Q(status="completed", resolved_at__date=today)
        ),
    )

    # ── Staff workload ──
    workload_qs = (
        OperationalTask.objects.filter(status__in=OPEN, assigned_to__isnull=False)
        .values("assigned_to__id", "assigned_to__first_name", "assigned_to__username")
        .annotate(
            open_count=Count("id"),
            in_progress_count=Count(
                "id", filter=Q(status=OperationalTask.Status.IN_PROGRESS)
            ),
        )
        .order_by("-open_count")
    )
    # Completed-today per staff
    completed_today_by_staff = dict(
        OperationalTask.objects.filter(status="completed", resolved_at__date=today)
        .values_list("resolved_by__id")
        .annotate(c=Count("id"))
        .values_list("resolved_by__id", "c")
    )
    staff_workload = []
    for row in workload_qs:
        uid = row["assigned_to__id"]
        staff_workload.append({
            "id": uid,
            "name": row["assigned_to__first_name"] or row["assigned_to__username"],
            "open": row["open_count"],
            "in_progress": row["in_progress_count"],
            "completed_today": completed_today_by_staff.get(uid, 0),
        })

    # ── Status & type breakdowns ──
    status_breakdown = dict(
        OperationalTask.objects.filter(status__in=OPEN)
        .values_list("status")
        .annotate(c=Count("id"))
        .values_list("status", "c")
    )
    type_breakdown = dict(
        OperationalTask.objects.filter(status__in=OPEN)
        .values_list("task_type")
        .annotate(c=Count("id"))
        .values_list("task_type", "c")
    )

    # ── Parse filters ──
    preset = request.GET.get("preset", "open")
    search = request.GET.get("search", "").strip()
    f_status = request.GET.get("status", "")
    f_assignee = request.GET.get("assignee", "")
    f_type = request.GET.get("task_type", "")
    f_priority = request.GET.get("priority", "")
    f_due_from = request.GET.get("due_from", "")
    f_due_to = request.GET.get("due_to", "")
    f_overdue = request.GET.get("overdue") == "1"
    sort_field = request.GET.get("sort", "priority")
    sort_dir = request.GET.get("dir", "asc")

    # ── Base queryset ──
    qs = OperationalTask.objects.select_related(
        "reservation",
        "reservation__customer",
        "leg",
        "leg__reservation",
        "leg__reservation__customer",
        "assigned_to",
        "created_by",
        "resolved_by",
        "blocked_by",
    )

    # ── Apply preset ──
    if preset == "open":
        qs = qs.filter(status__in=OPEN)
    elif preset == "overdue":
        qs = qs.filter(status__in=OPEN, due_at__lt=now)
    elif preset == "unassigned":
        qs = qs.filter(status__in=OPEN, assigned_to__isnull=True)
    elif preset == "due_today":
        qs = qs.filter(status__in=OPEN, due_at__date=today)
    elif preset == "completed":
        qs = qs.filter(
            status=OperationalTask.Status.COMPLETED,
            resolved_at__gte=now - timedelta(days=7),
        )
    # preset == "all" → no status filter

    # ── Apply additional filters ──
    if f_status:
        qs = qs.filter(status=f_status)
    if f_assignee == "unassigned":
        qs = qs.filter(assigned_to__isnull=True)
    elif f_assignee:
        try:
            qs = qs.filter(assigned_to_id=int(f_assignee))
        except (ValueError, TypeError):
            pass
    if f_type:
        qs = qs.filter(task_type=f_type)
    if f_priority:
        try:
            qs = qs.filter(priority=int(f_priority))
        except (ValueError, TypeError):
            pass
    if f_due_from:
        qs = qs.filter(due_at__date__gte=f_due_from)
    if f_due_to:
        qs = qs.filter(due_at__date__lte=f_due_to)
    if f_overdue:
        qs = qs.filter(due_at__lt=now, status__in=OPEN)

    # ── Text search ──
    if search:
        qs = qs.filter(
            Q(title__icontains=search)
            | Q(description__icontains=search)
            | Q(reservation__customer__first_name__icontains=search)
            | Q(reservation__customer__last_name__icontains=search)
            | Q(leg__reservation__customer__first_name__icontains=search)
            | Q(leg__reservation__customer__last_name__icontains=search)
            | Q(reservation__id__icontains=search)
        )

    # ── Sorting ──
    SORT_MAP = {
        "priority": "priority",
        "due_at": "due_at",
        "created_at": "created_at",
        "updated_at": "updated_at",
        "status": "status",
        "type": "task_type",
        "assigned": "assigned_to__first_name",
    }
    order_field = SORT_MAP.get(sort_field, "priority")
    if sort_dir == "desc":
        order_field = f"-{order_field}"
    # Secondary sort for stability
    if sort_field == "priority":
        qs = qs.order_by(order_field, "due_at")
    else:
        qs = qs.order_by(order_field, "priority", "due_at")

    # ── Pagination ──
    paginator = Paginator(qs, 50)
    page_obj = paginator.get_page(request.GET.get("page", 1))

    # ── Staff list for assignment dropdowns ──
    ops_staff = list(
        User.objects.filter(is_staff=True, is_active=True)
        .order_by("first_name", "username")
        .values("id", "first_name", "username")
    )

    # ── Build query string without 'page' for pagination links ──
    qs_params = request.GET.copy()
    qs_params.pop("page", None)
    filter_query_string = qs_params.urlencode()

    context = {
        "page_obj": page_obj,
        "stats": stats,
        "staff_workload": staff_workload,
        "status_breakdown": status_breakdown,
        "type_breakdown": type_breakdown,
        "now": now,
        # Current filter values (for form state)
        "preset": preset,
        "search": search,
        "f_status": f_status,
        "f_assignee": f_assignee,
        "f_type": f_type,
        "f_priority": f_priority,
        "f_due_from": f_due_from,
        "f_due_to": f_due_to,
        "f_overdue": f_overdue,
        "sort_field": sort_field,
        "sort_dir": sort_dir,
        "filter_query_string": filter_query_string,
        # Choices for filter dropdowns
        "task_types": OperationalTask.TaskType.choices,
        "statuses": OperationalTask.Status.choices,
        "priorities": OperationalTask.Priority.choices,
        "ops_staff": ops_staff,
        # Display helpers
        "status_labels": dict(OperationalTask.Status.choices),
        "type_labels": dict(OperationalTask.TaskType.choices),
        "type_icons": OperationalTask.TASK_TYPE_ICONS,
        "type_colors": {
            OperationalTask.TaskType.PAYMENT_CHASE: "#fbbf24",
            OperationalTask.TaskType.FLIGHT_VERIFICATION: "#818cf8",
            OperationalTask.TaskType.DRIVER_CONFLICT: "#fb7185",
            OperationalTask.TaskType.DRIVER_ASSIGNMENT: "#c084fc",
            OperationalTask.TaskType.CONFIRMATION_TEXTS: "#34d399",
            OperationalTask.TaskType.CONTACT_FORM: "#38bdf8",
            OperationalTask.TaskType.AFTERHOURS_FEE: "#64748b",
            OperationalTask.TaskType.MANUAL: "#6b7089",
        },
        "priority_colors": {
            OperationalTask.Priority.CRITICAL: "#fb7185",
            OperationalTask.Priority.HIGH: "#fbbf24",
            OperationalTask.Priority.MEDIUM: "#818cf8",
            OperationalTask.Priority.LOW: "#6b7089",
        },
    }
    return render(request, "dispatching/admin_tasks.html", context)


@login_required(login_url="login")
@user_passes_test(_is_superuser, login_url="login")
@require_POST
def admin_tasks_bulk_action(request):
    """
    Bulk action endpoint for the Admin Tasks page.
    Accepts JSON: {task_ids: [...], action: "assign|complete|cancel|priority", params: {...}}
    """
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    task_ids = data.get("task_ids", [])
    action = data.get("action", "")
    params = data.get("params", {})

    if not task_ids or not action:
        return JsonResponse(
            {"success": False, "error": "task_ids and action required"}, status=400
        )

    ALLOWED_ACTIONS = {"assign", "complete", "cancel", "priority"}
    if action not in ALLOWED_ACTIONS:
        return JsonResponse(
            {"success": False, "error": f"Invalid action: {action}"}, status=400
        )

    tasks = OperationalTask.objects.filter(id__in=task_ids)
    count = 0

    if action == "assign":
        user_id = params.get("user_id")
        if user_id:
            assignee = get_object_or_404(User, id=user_id, is_active=True)
            label = assignee.first_name or assignee.username
        else:
            assignee = None
            label = "unassigned"

        for task in tasks:
            task.assigned_to = assignee
            task.assigned_at = timezone.now() if assignee else None
            if assignee and task.status == OperationalTask.Status.PENDING:
                task.status = OperationalTask.Status.IN_PROGRESS
                task.save(update_fields=["assigned_to", "assigned_at", "status", "updated_at"])
            else:
                task.save(update_fields=["assigned_to", "assigned_at", "updated_at"])
            StaffActivity.objects.create(
                user=request.user,
                action_type=StaffActivity.ActionType.TASK_ASSIGNED,
                task=task,
                metadata={"assigned_to": label, "bulk": True},
            )
            count += 1

    elif action == "complete":
        notes = params.get("notes", "Bulk completed from Admin Tasks")
        for task in tasks:
            if task.is_open:
                close_task(task, resolved_by=request.user, resolution_notes=notes)
                count += 1

    elif action == "cancel":
        reason = params.get("reason", "Bulk cancelled from Admin Tasks")
        for task in tasks:
            if task.is_open:
                cancel_task(task, reason=reason)
                count += 1

    elif action == "priority":
        try:
            new_priority = int(params.get("priority", 3))
        except (ValueError, TypeError):
            return JsonResponse(
                {"success": False, "error": "Invalid priority value"}, status=400
            )
        for task in tasks:
            task.priority = new_priority
            task.save(update_fields=["priority", "updated_at"])
            count += 1

    return JsonResponse({"success": True, "count": count})


# ═══════════════════════════════════════════════════════════════════════════
#  Staff Time Clock — dispatchers clock in/out + unpaid breaks; founder oversight
# ═══════════════════════════════════════════════════════════════════════════


def _tc_fmt_hm(total_minutes):
    """Format a minute count as 'Hh Mm' (e.g. 450 -> '7h 30m')."""
    total_minutes = int(total_minutes)
    h, m = divmod(total_minutes, 60)
    return f"{h}h {m}m"


def _tc_parse_range(request):
    """
    Resolve the report window from query params, in Eastern time.

    ``?start=YYYY-MM-DD&end=YYYY-MM-DD`` takes precedence; otherwise
    ``?range=N`` days back from today (default 7, capped 90). Returns
    ``(start_date, end_date, range_days, start_dt, end_dt)`` where the *_dt
    bounds are tz-aware ET datetimes spanning ``[start 00:00, end+1 00:00)``.
    """
    tz = timezone.get_current_timezone()
    today = timezone.localdate()
    start_str = (request.GET.get("start") or "").strip()
    end_str = (request.GET.get("end") or "").strip()

    start_date = end_date = None
    if start_str and end_str:
        try:
            start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_str, "%Y-%m-%d").date()
        except ValueError:
            start_date = end_date = None

    if start_date and end_date:
        if start_date > end_date:
            start_date, end_date = end_date, start_date
        if (end_date - start_date).days > 90:
            start_date = end_date - timedelta(days=90)
        range_days = (end_date - start_date).days + 1
    else:
        try:
            range_days = int(request.GET.get("range", 7))
        except (ValueError, TypeError):
            range_days = 7
        range_days = max(1, min(range_days, 90))
        end_date = today
        start_date = today - timedelta(days=range_days - 1)

    start_dt = timezone.make_aware(datetime.combine(start_date, datetime.min.time()), tz)
    end_dt = timezone.make_aware(
        datetime.combine(end_date + timedelta(days=1), datetime.min.time()), tz
    )
    return start_date, end_date, range_days, start_dt, end_dt


def _tc_aggregate(shifts, now=None):
    """Roll a prefetched shift queryset into per-staff totals (seconds-accurate)."""
    now = now or timezone.now()
    by_user = {}
    for s in shifts:
        agg = by_user.get(s.user_id)
        if agg is None:
            agg = by_user[s.user_id] = {
                "user": s.user,
                "name": s.user.get_full_name() or s.user.username,
                "shifts": 0,
                "gross_seconds": 0.0,
                "break_seconds": 0.0,
                "net_seconds": 0.0,
                "has_open": False,
                "has_auto_closed": False,
            }
        agg["shifts"] += 1
        agg["gross_seconds"] += s.gross_seconds(now)
        agg["break_seconds"] += s.break_seconds(now)
        agg["net_seconds"] += s.worked_seconds(now)
        agg["has_open"] = agg["has_open"] or s.is_open
        agg["has_auto_closed"] = agg["has_auto_closed"] or s.auto_closed

    rows = []
    for agg in by_user.values():
        agg["gross_minutes"] = int(agg["gross_seconds"] // 60)
        agg["break_minutes"] = int(agg["break_seconds"] // 60)
        agg["net_minutes"] = int(agg["net_seconds"] // 60)
        agg["gross_hm"] = _tc_fmt_hm(agg["gross_minutes"])
        agg["break_hm"] = _tc_fmt_hm(agg["break_minutes"])
        agg["net_hm"] = _tc_fmt_hm(agg["net_minutes"])
        rows.append(agg)
    return sorted(rows, key=lambda r: r["name"].lower())


def _tc_today_worked_seconds(user, now=None):
    """
    Worked seconds credited to *today* (ET) for a staffer's own "Today" line.

    Counts every shift that STARTED today, PLUS the currently-open shift even if
    it began before midnight — so an overnight shift keeps showing the live
    session instead of dropping to 0h 0m the instant the date rolls over.
    """
    now = now or timezone.now()
    tz = timezone.get_current_timezone()
    local_today = timezone.localdate(now)
    day_start = timezone.make_aware(datetime.combine(local_today, datetime.min.time()), tz)
    day_end = day_start + timedelta(days=1)
    shifts = list(
        TimeClockShift.objects.filter(
            user=user, clock_in_at__gte=day_start, clock_in_at__lt=day_end
        ).prefetch_related("breaks")
    )
    open_shift = get_open_shift(user)
    if open_shift and not any(s.pk == open_shift.pk for s in shifts):
        shifts.append(open_shift)  # overnight: started before midnight, still on the clock
    return sum(s.worked_seconds(now) for s in shifts)


@login_required(login_url="login")
@user_passes_test(_is_staff, login_url="login")
def timeclock_view(request):
    """A staffer's own clock — current state, action buttons, recent shifts."""
    now = timezone.now()
    shift = get_open_shift(request.user)
    open_break = shift.open_break if shift else None

    recent_shifts = list(
        TimeClockShift.objects.filter(user=request.user)
        .prefetch_related("breaks")
        .order_by("-clock_in_at")[:10]
    )
    for s in recent_shifts:
        s.net_hm = _tc_fmt_hm(int(s.worked_seconds(now) // 60))
        s.break_hm = _tc_fmt_hm(int(s.break_seconds(now) // 60))

    # Net minutes worked so far today (ET). Includes the current open shift even
    # if it started before midnight, so an overnight shift never reads 0h 0m.
    today_seconds = _tc_today_worked_seconds(request.user, now)

    # Read-only "Your schedule" card — today's planned window + this week.
    today = timezone.localdate()
    monday = today - timedelta(days=today.weekday())
    sched_user = (
        User.objects.prefetch_related("schedule_overrides", "weekly_schedule_rows", "extra_shifts")
        .get(pk=request.user.pk)
    )
    today_schedule = scheduling.resolve_staff_schedule(sched_user, today)
    week_sched = scheduling.week_schedule(sched_user, monday)

    # Request flow: a clock-in outside the schedule doesn't start the clock —
    # it files a request. Work out what the page should say right now.
    request_state, request_obj = (None, None)
    clockin_warning = ""
    if not shift:
        request_state, request_obj = clock_in_request_state(sched_user, now=now)
        # The heads-up under the button — only when a punch would file a
        # request (an approved grant means the next punch just works).
        if request_state != "approved":
            in_sched, _reason = scheduling.clock_in_schedule_check(sched_user, at=now)
            if not in_sched:
                if today_schedule["is_working"] and today_schedule["display_label"]:
                    clockin_warning = f"You're scheduled {today_schedule['display_label']} today."
                elif today_schedule["is_working"] is False:
                    clockin_warning = "You're not scheduled to work today."
                else:
                    clockin_warning = "You're outside your scheduled hours."

    context = {
        "shift": shift,
        "open_break": open_break,
        "state": shift.state if shift else TimeClockShift.State.CLOCKED_OUT,
        "recent_shifts": recent_shifts,
        "today_worked_hm": _tc_fmt_hm(int(today_seconds // 60)),
        "today_schedule": today_schedule,
        "week_schedule": week_sched,
        "today": today,
        "clockin_warning": clockin_warning,
        "request_state": request_state or "",
        "request_obj": request_obj,
        # Epoch millis drive the live JS timers (no tz ambiguity in the browser).
        "clock_in_ms": int(shift.clock_in_at.timestamp() * 1000) if shift else None,
        "break_start_ms": int(open_break.break_start_at.timestamp() * 1000) if open_break else None,
    }
    return render(request, "dispatching/timeclock.html", context)


@login_required(login_url="login")
@user_passes_test(_is_staff, login_url="login")
@require_POST
def timeclock_action(request):
    """Single POST endpoint for clock_in / clock_out / break_start / break_end."""
    try:
        action = json.loads(request.body).get("action")
    except (json.JSONDecodeError, AttributeError):
        action = request.POST.get("action")

    result = ""
    try:
        if action == "clock_in":
            # May open a shift, or (outside the schedule) file a request
            # instead — in which case the clock has NOT started.
            result, _obj = clock_in_or_request(request.user)
        elif action == "clock_out":
            tc_clock_out(request.user)
        elif action == "break_start":
            tc_start_break(request.user)
        elif action == "break_end":
            tc_end_break(request.user)
        elif action == "cancel_request":
            cancel_clock_in_request(request.user)
            result = "cancelled"
        elif action == "request_status":
            # Polled by the waiting page so approval flips it without a refresh.
            state, _req = clock_in_request_state(request.user)
            return JsonResponse({"success": True, "request_state": state or ""})
        else:
            return JsonResponse({"success": False, "error": "Unknown action"}, status=400)
    except TimeClockError as e:
        # Soft failure (e.g. a double-click) — never a 500. The page re-syncs.
        return JsonResponse({"success": False, "error": str(e)})

    shift = get_open_shift(request.user)
    open_break = shift.open_break if shift else None
    return JsonResponse({
        "success": True,
        "result": result,
        "state": shift.state if shift else TimeClockShift.State.CLOCKED_OUT,
        "clock_in_ms": int(shift.clock_in_at.timestamp() * 1000) if shift else None,
        "break_start_ms": int(open_break.break_start_at.timestamp() * 1000) if open_break else None,
    })


@login_required(login_url="login")
@user_passes_test(_is_superuser, login_url="login")
def timeclock_overview(request):
    """Founder view: who's on the clock now + per-staff hour totals over a range."""
    auto_close_stale_shifts()  # lazy cleanup — this app has no scheduler
    now = timezone.now()

    # ── Live: who is on the clock right now ──
    open_shifts = (
        TimeClockShift.objects.filter(clock_out_at__isnull=True)
        .select_related("user")
        .prefetch_related("breaks")
        .order_by("clock_in_at")
    )
    live = []
    for s in open_shifts:
        ob = s.open_break
        anchor = ob.break_start_at if ob else s.clock_in_at
        live.append({
            "name": s.user.get_full_name() or s.user.username,
            "on_break": ob is not None,
            "since": anchor,
            "since_ms": int(anchor.timestamp() * 1000),
            "clock_in": s.clock_in_at,
            "net_hm": _tc_fmt_hm(int(s.worked_seconds(now) // 60)),
        })

    # ── Report: per-staff totals over the selected range ──
    start_date, end_date, range_days, start_dt, end_dt = _tc_parse_range(request)
    shifts = (
        TimeClockShift.objects.filter(clock_in_at__gte=start_dt, clock_in_at__lt=end_dt)
        .select_related("user")
        .prefetch_related("breaks")
    )
    rows = _tc_aggregate(shifts, now)
    totals_net = sum(r["net_minutes"] for r in rows)
    totals = {
        "shifts": sum(r["shifts"] for r in rows),
        "gross_hm": _tc_fmt_hm(sum(r["gross_minutes"] for r in rows)),
        "break_hm": _tc_fmt_hm(sum(r["break_minutes"] for r in rows)),
        "net_hm": _tc_fmt_hm(totals_net),
    }

    context = {
        "live": live,
        "rows": rows,
        "totals": totals,
        "start_date": start_date,
        "end_date": end_date,
        "range_days": range_days,
        "is_preset_range": not (request.GET.get("start") and request.GET.get("end")),
        "export_query": request.GET.urlencode(),
    }
    return render(request, "dispatching/timeclock_overview.html", context)


@login_required(login_url="login")
@user_passes_test(_is_superuser, login_url="login")
def timeclock_export_csv(request):
    """One row per shift over the range, for payroll. Times shown in Eastern."""
    start_date, end_date, range_days, start_dt, end_dt = _tc_parse_range(request)
    shifts = (
        TimeClockShift.objects.filter(clock_in_at__gte=start_dt, clock_in_at__lt=end_dt)
        .select_related("user")
        .prefetch_related("breaks")
        .order_by("user__first_name", "user__username", "clock_in_at")
    )
    now = timezone.now()

    fieldnames = [
        "staff", "date", "clock_in", "clock_out",
        "gross_hours", "break_hours", "net_hours", "open", "auto_closed", "approval",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for s in shifts:
        ci = timezone.localtime(s.clock_in_at)
        co = timezone.localtime(s.clock_out_at) if s.clock_out_at else None
        gross_h = round(s.gross_seconds(now) / 3600, 2)
        break_h = round(s.break_seconds(now) / 3600, 2)
        writer.writerow({
            "staff": s.user.get_full_name() or s.user.username,
            "date": ci.strftime("%Y-%m-%d"),
            "clock_in": ci.strftime("%Y-%m-%d %I:%M %p"),
            "clock_out": co.strftime("%Y-%m-%d %I:%M %p") if co else "",
            "gross_hours": gross_h,
            "break_hours": break_h,
            "net_hours": round(gross_h - break_h, 2),
            "open": "yes" if s.is_open else "",
            "auto_closed": "yes" if s.auto_closed else "",
            # Blank = a normal in-schedule punch; otherwise the decision state
            # of an unscheduled clock-in (pending / approved / denied).
            "approval": s.approval_status,
        })

    resp = HttpResponse(buf.getvalue().encode("utf-8"), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="timeclock_{start_date}_{end_date}.csv"'
    return resp


# ═══════════════════════════════════════════════════════════════════════════
#  Time Clock — superuser management (punch/edit entries + staff scheduling)
# ═══════════════════════════════════════════════════════════════════════════


def _office_staff_qs():
    """Office dispatchers: is_staff & active, excluding drivers/travel agents.

    Thin wrapper over the importable ``ops.staff.office_staff_qs`` so the
    staffing board and this module share one definition. (The two staff-metrics
    views below keep their own inline variant on purpose — see ops/staff.py.)
    """
    from .staff import office_staff_qs
    return office_staff_qs()


def _tc_parse_et_dt(s):
    """Parse a 'YYYY-MM-DDTHH:MM' datetime-local (ET wall-clock) -> aware datetime, or None."""
    if not s:
        return None
    s = s.strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            naive = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return timezone.make_aware(naive, timezone.get_current_timezone())
    return None


def _parse_hm(s):
    if not s:
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(s.strip(), fmt).time()
        except (ValueError, AttributeError):
            continue
    return None


def _parse_ymd(s):
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None


def _serialize_override(o):
    return {
        "id": o.id,
        "date": o.date.strftime("%Y-%m-%d"),
        "end_date": o.end_date.strftime("%Y-%m-%d") if o.end_date else "",
        "kind": o.kind,
        "start_time": o.start_time.strftime("%H:%M") if o.start_time else "",
        "end_time": o.end_time.strftime("%H:%M") if o.end_time else "",
        "note": o.note,
        "role": o.role,
        "location": o.location,
        "location_label": WORK_LOCATION_SHORT.get(o.location, ""),
        "reason": o.reason,
        "reason_label": o.reason_label,
        "status": o.status,
        "status_label": o.status_label,
        "requested": o.requested_by_staff,
        "range_display": o.date_range_display,
        "kind_label": dict(StaffScheduleOverride.KIND_CHOICES).get(o.kind, o.kind),
    }


@login_required(login_url="login")
@user_passes_test(_is_superuser, login_url="login")
def timeclock_manage(request):
    """Superuser hub: roster + live status + quick punch + today scheduled-vs-actual."""
    auto_close_stale_shifts()
    now = timezone.now()
    today = timezone.localdate()
    roster = list(_office_staff_qs().prefetch_related("schedule_overrides", "weekly_schedule_rows", "extra_shifts"))

    open_by_user = {}
    for s in (
        TimeClockShift.objects.filter(clock_out_at__isnull=True)
        .select_related("user").prefetch_related("breaks")
    ):
        open_by_user[s.user_id] = s

    day_start = timezone.make_aware(datetime.combine(today, datetime.min.time()), timezone.get_current_timezone())
    day_end = day_start + timedelta(days=1)
    today_shifts = defaultdict(list)
    for s in (
        TimeClockShift.objects.filter(clock_in_at__gte=day_start, clock_in_at__lt=day_end).prefetch_related("breaks")
    ):
        today_shifts[s.user_id].append(s)

    # Earliest clock-in per user, so a staffer who never used the clock isn't flagged "absent".
    first_in_by_user = {}
    for row in TimeClockShift.objects.values("user_id").annotate(first=Min("clock_in_at")):
        first_in_by_user[row["user_id"]] = timezone.localtime(row["first"]).date()

    rows = []
    for u in roster:
        open_shift = open_by_user.get(u.id)
        vs = scheduling.schedule_vs_actual(
            u, today, shifts=today_shifts.get(u.id, []), now=now, tracking_since=first_in_by_user.get(u.id)
        )
        sched_today = scheduling.resolve_staff_schedule(u, today)
        state = "clocked_out"
        if open_shift:
            state = "on_break" if open_shift.open_break else "clocked_in"
        rows.append({
            "user": u,
            "name": u.get_full_name() or u.username,
            "state": state,
            "is_open": open_shift is not None,
            "since_ms": int(open_shift.clock_in_at.timestamp() * 1000) if open_shift else None,
            "today_status": vs["status"],
            "today_status_label": vs["label"],
            "today_quiet": vs["status"] in scheduling.QUIET_STATUSES,
            "today_scheduled": vs["scheduled_label"],
            "today_actual": vs["actual_label"],
            "location": sched_today["location"],
            "location_label": sched_today["location_label"],
            "location_flipped": sched_today["location_flipped"],
        })

    # Clock-in requests waiting for a decision. Nothing is on the clock for
    # these — approving lets the staffer start it, it doesn't start it for them.
    expire_stale_requests(now=now)
    pending_requests = [
        {
            "req": r,
            "name": r.user.get_full_name() or r.user.username,
            "user_id": r.user_id,
        }
        for r in TimeClockRequest.objects.filter(status=TimeClockRequest.Status.PENDING)
        .select_related("user").order_by("requested_at")
    ]
    # Grants waiting to be used — so "approved but still not on the clock"
    # reads as the staffer's move, not a glitch.
    open_grants = list(
        TimeClockRequest.objects.filter(
            status=TimeClockRequest.Status.APPROVED,
            decided_at__gte=now - timedelta(minutes=GRANT_VALID_MIN),
        ).select_related("user").order_by("decided_at")
    )

    # On-call panel: staff dropdown + upcoming assignments (today forward).
    oncall_staff = [{"id": u.id, "name": u.get_full_name() or u.username} for u in roster]
    oncall_upcoming = list(
        StaffOnCall.objects.filter(date__gte=today)
        .select_related("user").order_by("date", "user__first_name")[:40]
    )

    return render(request, "dispatching/timeclock_manage.html", {
        "rows": rows,
        "now": now,
        "today": today,
        "on_clock_count": len(open_by_user),
        "oncall_staff": oncall_staff,
        "oncall_upcoming": oncall_upcoming,
        "pending_requests": pending_requests,
        "open_grants": open_grants,
    })


@login_required(login_url="login")
@user_passes_test(_is_superuser, login_url="dashboard")
@require_POST
def timeclock_oncall_action(request):
    """Mark / unmark a dispatcher on-call for a date (from the manage page).

    On-call is additive to the regular schedule and feeds the staffing board's
    overnight coverage. Default window 12 AM–6 AM. update_or_create keeps the
    (user, date) unique constraint safe if the same night is marked twice.
    """
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, AttributeError):
        data = request.POST
    action = data.get("action")

    if action == "add":
        u = _office_staff_qs().filter(id=data.get("user_id")).first()
        if not u:
            return JsonResponse({"success": False, "error": "Unknown staff member."}, status=400)
        start_t = _parse_hm(data.get("start_time")) or time(0, 0)
        end_t = _parse_hm(data.get("end_time")) or time(6, 0)

        # Two ways to add: a single date, or a set of weekdays across a range
        # (e.g. Mon/Wed/Fri from now through next month).
        try:
            weekdays = {int(w) for w in (data.get("weekdays") or [])}
        except (TypeError, ValueError):
            weekdays = set()

        dates = []
        if weekdays:
            d0 = _parse_ymd(data.get("from"))
            d1 = _parse_ymd(data.get("to")) or (d0 + timedelta(days=6) if d0 else None)
            if not d0 or not d1:
                return JsonResponse({"success": False, "error": "Pick a start and end date for the repeat."})
            if d1 < d0:
                return JsonResponse({"success": False, "error": "End date must be on or after the start date."})
            if (d1 - d0).days > 186:
                return JsonResponse({"success": False, "error": "Keep the repeat range within about 6 months."})
            d = d0
            while d <= d1:
                if d.weekday() in weekdays:
                    dates.append(d)
                d += timedelta(days=1)
        else:
            single = _parse_ymd(data.get("date") or data.get("from"))
            if not single:
                return JsonResponse({"success": False, "error": "A date is required."})
            dates = [single]

        if not dates:
            return JsonResponse({"success": False, "error": "No matching days in that range."})

        for d in dates:
            StaffOnCall.objects.update_or_create(
                user=u, date=d,
                defaults={"start_time": start_t, "end_time": end_t, "created_by": request.user},
            )
        return JsonResponse({"success": True, "created": len(dates)})

    if action == "delete":
        # Accept a single id or a list of ids (bulk "delete selected").
        raw = data.get("ids")
        if raw is None:
            raw = data.get("id")
        # A scalar (or a stray string like "12") is one id — never iterate it
        # character-by-character, which would delete ids 1 and 2.
        if raw is None:
            raw = []
        elif not isinstance(raw, (list, tuple)):
            raw = [raw]
        try:
            ids = [int(i) for i in raw]
        except (TypeError, ValueError):
            return JsonResponse({"success": False, "error": "Invalid selection."}, status=400)
        deleted = 0
        if ids:
            deleted, _ = StaffOnCall.objects.filter(id__in=ids).delete()
        return JsonResponse({"success": True, "deleted": deleted})

    return JsonResponse({"success": False, "error": "Unknown action"}, status=400)


@login_required(login_url="login")
@user_passes_test(_is_superuser, login_url="login")
def timeclock_staff_detail(request, user_id):
    """Per-staff management: shifts (add/edit/delete), schedule editor, vs-actual table."""
    staff_user = get_object_or_404(User, id=user_id)
    now = timezone.now()
    start_date, end_date, range_days, start_dt, end_dt = _tc_parse_range(request)

    shifts = list(
        TimeClockShift.objects.filter(user=staff_user, clock_in_at__gte=start_dt, clock_in_at__lt=end_dt)
        .prefetch_related("breaks").select_related("edited_by").order_by("-clock_in_at")
    )
    for s in shifts:
        s.net_hm = _tc_fmt_hm(int(s.worked_seconds(now) // 60))
        s.break_hm = _tc_fmt_hm(int(s.break_seconds(now) // 60))
        s.edited = s.edited_by_id is not None
        s.in_local = timezone.localtime(s.clock_in_at).strftime("%Y-%m-%dT%H:%M")
        s.out_local = timezone.localtime(s.clock_out_at).strftime("%Y-%m-%dT%H:%M") if s.clock_out_at else ""
        s.breaks_json = json.dumps([
            {
                "id": b.id,
                "start": timezone.localtime(b.break_start_at).strftime("%Y-%m-%dT%H:%M"),
                "end": timezone.localtime(b.break_end_at).strftime("%Y-%m-%dT%H:%M") if b.break_end_at else "",
            }
            for b in s.breaks.all()
        ])

    open_shift = get_open_shift(staff_user)
    state = "clocked_out"
    if open_shift:
        state = "on_break" if open_shift.open_break else "clocked_in"

    today = timezone.localdate()
    monday = today - timedelta(days=today.weekday())
    sched_user = User.objects.prefetch_related("schedule_overrides", "weekly_schedule_rows", "extra_shifts").get(pk=staff_user.pk)
    week_sched = scheduling.week_schedule(sched_user, monday)
    weekly_rows = {r.day_of_week: r for r in sched_user.weekly_schedule_rows.all()}
    weekly_editor = []
    for dow, dname in StaffWeeklySchedule.DAY_CHOICES:
        r = weekly_rows.get(dow)
        weekly_editor.append({
            "day_of_week": dow,
            "day_name": dname,
            "is_working": r.is_working if r else False,
            "start_time": r.start_time.strftime("%H:%M") if (r and r.start_time) else "09:00",
            "end_time": r.end_time.strftime("%H:%M") if (r and r.end_time) else "17:00",
            "role": r.role if r else "",
            "location": r.location if r else "",
            "note": r.note if r else "",
        })
    overrides = list(
        sched_user.schedule_overrides.filter(
            Q(end_date__isnull=True, date__gte=today) | Q(end_date__gte=today)
        ).order_by("date")
    )

    # When did this person first clock in? Days before that have no data, so we
    # report them "untracked" rather than flagging a no-show that predates the clock.
    first_in = (
        TimeClockShift.objects.filter(user=staff_user)
        .order_by("clock_in_at").values_list("clock_in_at", flat=True).first()
    )
    tracking_since = timezone.localtime(first_in).date() if first_in else None

    shifts_by_date = defaultdict(list)
    for s in shifts:
        shifts_by_date[timezone.localtime(s.clock_in_at).date()].append(s)
    vs_rows = []
    d = start_date
    while d <= end_date:
        vs = scheduling.schedule_vs_actual(
            sched_user, d, shifts=shifts_by_date.get(d, []), now=now, tracking_since=tracking_since
        )
        if vs["status"] not in scheduling.QUIET_STATUSES or shifts_by_date.get(d):
            vs_rows.append({"date": d, **vs})
        d += timedelta(days=1)
    vs_rows.reverse()

    return render(request, "dispatching/timeclock_staff_detail.html", {
        "staff_user": staff_user,
        "staff_name": staff_user.get_full_name() or staff_user.username,
        "shifts": shifts,
        "state": state,
        "range_days": range_days,
        "start_date": start_date,
        "end_date": end_date,
        "is_preset_range": not (request.GET.get("start") and request.GET.get("end")),
        "week_schedule": week_sched,
        "weekly_editor": weekly_editor,
        "overrides": overrides,
        "vs_rows": vs_rows,
        "role_choices": STAFF_ROLE_CHOICES,
        "location_choices": WORK_LOCATION_CHOICES,
        "extra_shifts": list(sched_user.extra_shifts.all()),
    })


@login_required(login_url="login")
@user_passes_test(_is_superuser, login_url="login")
@require_POST
def timeclock_entry_action(request):
    """Single superuser endpoint: punch/add/edit/delete shifts and breaks."""
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, AttributeError):
        data = request.POST
    action = data.get("action")
    by = request.user

    def office_user(uid):
        return _office_staff_qs().filter(id=uid).first()

    try:
        if action in ("punch_in", "punch_out", "add_shift"):
            u = office_user(data.get("user_id"))
            if not u:
                return JsonResponse({"success": False, "error": "Unknown staff member."}, status=400)
            if action == "punch_in":
                admin_punch_in(u, by, at=_tc_parse_et_dt(data.get("at")))
            elif action == "punch_out":
                admin_punch_out(u, by, at=_tc_parse_et_dt(data.get("at")))
            else:
                admin_create_shift(
                    u, _tc_parse_et_dt(data.get("clock_in_at")),
                    _tc_parse_et_dt(data.get("clock_out_at")), by, note=data.get("note", ""),
                )
        elif action == "edit_shift":
            shift = get_object_or_404(TimeClockShift, id=data.get("shift_id"))
            admin_update_shift(
                shift, _tc_parse_et_dt(data.get("clock_in_at")),
                _tc_parse_et_dt(data.get("clock_out_at")), by, note=data.get("note"),
            )
        elif action == "delete_shift":
            admin_delete_shift(get_object_or_404(TimeClockShift, id=data.get("shift_id")))
        elif action == "approve_request":
            approve_clock_in_request(get_object_or_404(TimeClockRequest, id=data.get("request_id")), by)
        elif action == "deny_request":
            deny_clock_in_request(
                get_object_or_404(TimeClockRequest, id=data.get("request_id")), by,
                note=(data.get("note") or "")[:200],
            )
        elif action == "add_break":
            shift = get_object_or_404(TimeClockShift, id=data.get("shift_id"))
            admin_add_break(shift, _tc_parse_et_dt(data.get("break_start_at")), _tc_parse_et_dt(data.get("break_end_at")), by)
        elif action == "edit_break":
            brk = get_object_or_404(TimeClockBreak, id=data.get("break_id"))
            admin_update_break(brk, _tc_parse_et_dt(data.get("break_start_at")), _tc_parse_et_dt(data.get("break_end_at")), by)
        elif action == "delete_break":
            admin_delete_break(get_object_or_404(TimeClockBreak, id=data.get("break_id")), by=by)
        else:
            return JsonResponse({"success": False, "error": "Unknown action"}, status=400)
    except TimeClockError as e:
        return JsonResponse({"success": False, "error": str(e)})
    return JsonResponse({"success": True})


@login_required(login_url="login")
@user_passes_test(_is_superuser, login_url="login")
def staff_schedule_get(request):
    """Return a staffer's weekly schedule + upcoming overrides as JSON."""
    u = _office_staff_qs().filter(id=request.GET.get("user_id")).first()
    if not u:
        return JsonResponse({"success": False, "error": "Unknown staff member."}, status=400)
    today = timezone.localdate()
    weekly = {}
    for r in u.weekly_schedule_rows.all():
        weekly[str(r.day_of_week)] = {
            "is_working": r.is_working,
            "start_time": r.start_time.strftime("%H:%M") if r.start_time else "",
            "end_time": r.end_time.strftime("%H:%M") if r.end_time else "",
            "role": r.role,
            "location": r.location,
            "note": r.note,
        }
    overrides = [
        _serialize_override(o) for o in u.schedule_overrides.filter(
            Q(end_date__isnull=True, date__gte=today) | Q(end_date__gte=today)
        ).order_by("date")
    ]
    return JsonResponse({"success": True, "weekly": weekly, "overrides": overrides})


@login_required(login_url="login")
@user_passes_test(_is_superuser, login_url="login")
@require_POST
def staff_schedule_action(request):
    """Save weekly schedule / add-edit-delete overrides for a staffer."""
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, AttributeError):
        data = request.POST
    action = data.get("action")
    u = _office_staff_qs().filter(id=data.get("user_id")).first()
    if not u:
        return JsonResponse({"success": False, "error": "Unknown staff member."}, status=400)

    if action == "save_weekly":
        weekly = data.get("weekly", {})
        for dow_str, row in weekly.items():
            try:
                dow = int(dow_str)
            except (ValueError, TypeError):
                continue
            is_working = bool(row.get("is_working"))
            start_t = _parse_hm(row.get("start_time")) if is_working else None
            end_t = _parse_hm(row.get("end_time")) if is_working else None
            if is_working and (start_t is None or end_t is None):
                day = dict(StaffWeeklySchedule.DAY_CHOICES).get(dow, dow)
                return JsonResponse({"success": False, "error": f"{day}: start and end times are required when working."})
            role = (row.get("role") or "").strip()
            if role not in dict(STAFF_ROLE_CHOICES):
                role = ""
            location = (row.get("location") or "").strip()
            if location not in dict(WORK_LOCATION_CHOICES):
                location = ""
            StaffWeeklySchedule.objects.update_or_create(
                user=u, day_of_week=dow,
                defaults={"is_working": is_working, "start_time": start_t, "end_time": end_t,
                          "role": role if is_working else "",
                          "location": location if is_working else "",
                          "note": (row.get("note") or "")[:200]},
            )
        return JsonResponse({"success": True})

    if action in ("add_override", "edit_override"):
        date_ = _parse_ymd(data.get("date"))
        if not date_:
            return JsonResponse({"success": False, "error": "A date is required."})
        end_date = _parse_ymd(data.get("end_date")) if data.get("end_date") else None
        if end_date and end_date < date_:
            return JsonResponse({"success": False, "error": "End date must be on or after the start date."})
        kind = data.get("kind", "off")
        start_t = end_t = None
        if kind == "custom_hours":
            start_t, end_t = _parse_hm(data.get("start_time")), _parse_hm(data.get("end_time"))
            if start_t is None or end_t is None:
                return JsonResponse({"success": False, "error": "Custom hours need a start and end time."})
        role = (data.get("role") or "").strip()
        if role not in dict(STAFF_ROLE_CHOICES):
            role = ""
        location = (data.get("location") or "").strip()
        if location not in dict(WORK_LOCATION_CHOICES):
            location = ""
        reason = (data.get("reason") or "").strip()
        if reason not in dict(StaffScheduleOverride.REASON_CHOICES):
            reason = ""
        fields = dict(date=date_, end_date=end_date, kind=kind, start_time=start_t, end_time=end_t,
                      role=role, location=location if kind != "off" else "",
                      reason=reason if kind == "off" else "",
                      note=(data.get("note") or "")[:200])
        if action == "add_override":
            o = StaffScheduleOverride.objects.create(user=u, created_by=request.user, **fields)
        else:
            o = get_object_or_404(StaffScheduleOverride, id=data.get("id"), user=u)
            for k, v in fields.items():
                setattr(o, k, v)
            o.save()
        return JsonResponse({"success": True, "override": _serialize_override(o)})

    if action == "delete_override":
        o = get_object_or_404(StaffScheduleOverride, id=data.get("id"), user=u)
        o.delete()
        return JsonResponse({"success": True})

    return JsonResponse({"success": False, "error": "Unknown action"}, status=400)


def _dates_between(start, end):
    """Inclusive list of dates from ``start`` to ``end``."""
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _staffing_scope(request, today):
    """Resolve the board's scope from the query string.

    Returns ``(scope, dates, label, sub_label)`` where ``dates`` is empty for the
    dateless "pattern" scope. Anything unparseable falls back to this week rather
    than erroring — a bad bookmark should still render a board.

    * ``pattern``          the recurring standard week (no dates) — the default
    * ``week&start=…``     a Mon–Sun week; ``start`` is snapped back to its Monday
    * ``day&date=…``       one date
    * ``range&start=&end=` any span, capped at ``coverage.MAX_RANGE_DAYS``
    """
    scope = (request.GET.get("scope") or "pattern").strip().lower()
    if scope not in ("pattern", "week", "day", "range"):
        scope = "pattern"

    if scope == "pattern":
        return "pattern", [], "Weekly Staffing Pattern", "the standard week"

    start = _parse_ymd(request.GET.get("start"))
    end = _parse_ymd(request.GET.get("end"))

    if scope == "day":
        d = _parse_ymd(request.GET.get("date")) or start or today
        rel = (d - today).days
        when = "Today" if rel == 0 else ("Tomorrow" if rel == 1 else ("Yesterday" if rel == -1 else ""))
        sub = d.strftime("%A, %B %d, %Y").replace(" 0", " ")
        return "day", [d], (f"{when} · {coverage.md(d)}" if when else d.strftime("%A")), sub

    if scope == "range" and start and end:
        if end < start:
            start, end = end, start
        span = min((end - start).days + 1, coverage.MAX_RANGE_DAYS)
        dates = [start + timedelta(days=i) for i in range(span)]
        return "range", dates, f"{coverage.md(start)} – {coverage.md(dates[-1])}", f"{span} days"

    # Week (also the fallback for a malformed range).
    anchor = start or today
    monday = anchor - timedelta(days=anchor.weekday())
    dates = [monday + timedelta(days=i) for i in range(7)]
    this_monday = today - timedelta(days=today.weekday())
    delta_weeks = (monday - this_monday).days // 7
    label = {0: "This week", 1: "Next week", -1: "Last week"}.get(
        delta_weeks, f"Week of {coverage.md(monday)}")
    return "week", dates, label, f"{coverage.md(monday)} – {coverage.md(dates[-1])}"


@login_required(login_url="login")
@user_passes_test(_is_superuser, login_url="dashboard")
def staffing_board(request):
    """Dispatcher staffing & coverage board (superuser).

    Four scopes, one rendering. The default is the recurring *weekly pattern* —
    dateless columns straight from StaffWeeklySchedule, which is what this page
    always showed. The dated scopes (week / day / range) run the same shape
    through the schedule resolver instead, so approved time off, one-off custom
    hours and actual on-call assignments all land on the board.

    Opener and closer are *assigned* per shift where someone has been given the
    duty, and derived from the hours (earliest in / latest out) where nobody has —
    so a roster with no roles set reads exactly as it did before.
    """
    today = timezone.localdate()
    scope, dates, scope_label, scope_sub = _staffing_scope(request, today)

    prefetch = ["weekly_schedule_rows", "extra_shifts"] + ([] if scope == "pattern" else ["schedule_overrides"])
    roster = list(_office_staff_qs().prefetch_related(*prefetch))
    colors = coverage.assign_colors(roster)

    if scope == "pattern":
        data = coverage.weekly_pattern(roster, today_dow=today.weekday(), colors=colors)
    else:
        data = coverage.dated_range(dates, roster, today=today, colors=colors)

    # Timeline "now" marker (% across a 24h day) + hour-axis ticks.
    now = timezone.localtime()
    now_frac = round((now.hour * 60 + now.minute) / 1440 * 100, 3)
    hour_ticks = [{"pos": round(h / 24 * 100, 3), "label": lbl}
                  for h, lbl in [(0, "12a"), (6, "6a"), (12, "12p"), (18, "6p"), (24, "12a")]]
    grid_ticks = [t["pos"] for t in hour_ticks if 0 < t["pos"] < 100]

    # Week/day stepping. The pattern scope has no dates to step through, so its
    # arrows start from this week rather than pointing nowhere.
    anchor = dates[0] if dates else today - timedelta(days=today.weekday())
    step = timedelta(days=1 if scope == "day" else 7)
    nav = {
        "prev": (anchor - step).strftime("%Y-%m-%d"),
        "next": (anchor + step).strftime("%Y-%m-%d"),
        "this_week": (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d"),
        "next_week": (today - timedelta(days=today.weekday()) + timedelta(days=7)).strftime("%Y-%m-%d"),
        "today": today.strftime("%Y-%m-%d"),
        "anchor": anchor.strftime("%Y-%m-%d"),
        "end": (dates[-1] if dates else today).strftime("%Y-%m-%d"),
    }

    days = data["weekdays"]
    summary = {
        "shifts": sum(d["on_count"] for d in days),
        "flagged": sum(1 for d in days if d["cue"]["level"] != "ok"),
        "thinnest": min((d["peak"] for d in days), default=0),
        "roles_set": sum(1 for d in days for who in (d["opener"], d["closer"]) if who and who["assigned"]),
        "off_count": sum(len(d["time_off"]) for d in days),
        "unscheduled": sum(1 for r in data["rows"] if r["is_empty"]),
        # Any location tracked anywhere this scope? Gates the In-office row so
        # a roster that never sets office/WFH reads exactly as before.
        "loc_tracked": any(d["loc_any"] for d in days),
    }

    return render(request, "dispatching/staffing_board.html", {
        "weekdays": days,
        "rows": data["rows"],
        "roster_count": len(roster),
        "today_dow": today.weekday(),
        "now_frac": now_frac,
        "hour_ticks": hour_ticks,
        "grid_ticks": grid_ticks,
        "scope": scope,
        "is_dated": scope != "pattern",
        "scope_label": scope_label,
        "scope_sub": scope_sub,
        "nav": nav,
        "summary": summary,
        "role_choices": STAFF_ROLE_CHOICES,
        "location_choices": WORK_LOCATION_CHOICES,
        "reason_choices": StaffScheduleOverride.REASON_CHOICES,
        "pending_timeoff": timeoff.pending_requests(roster, today=today),
        "upcoming_timeoff": timeoff.upcoming_approved(roster, today=today),
        "staff_options": [{"id": u.id, "name": u.get_full_name() or u.username} for u in roster],
    })


@login_required(login_url="login")
@user_passes_test(_is_superuser, login_url="dashboard")
@require_POST
def staffing_action(request):
    """Board-side edits: assign a shift role, and decide/add/cancel time off."""
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, AttributeError):
        data = request.POST
    action = data.get("action")

    def _staff(uid):
        return _office_staff_qs().filter(id=uid).first()

    if action == "set_role":
        u = _staff(data.get("user_id"))
        if not u:
            return JsonResponse({"success": False, "error": "Unknown staff member."}, status=400)
        role = (data.get("role") or "").strip()
        if role and role not in dict(STAFF_ROLE_CHOICES):
            return JsonResponse({"success": False, "error": "Unknown role."}, status=400)

        date_ = _parse_ymd(data.get("date"))
        if date_:
            # Refuse a date the person isn't actually on. Beyond being nonsense,
            # a "note" override written over an approved day off would outrank it
            # (single-date overrides resolve newest-first) and quietly put them
            # back on the board.
            sched_u = User.objects.prefetch_related("weekly_schedule_rows", "schedule_overrides", "extra_shifts").get(pk=u.pk)
            if not scheduling.resolve_staff_schedule(sched_u, date_)["is_working"]:
                return JsonResponse({"success": False, "error": "That dispatcher isn't working that day."})

            # A dated cell: keep it to that one day via an override, so the
            # recurring pattern the rest of the month runs on is left alone.
            ov = (StaffScheduleOverride.objects
                  .filter(user=u, date=date_, end_date__isnull=True, status="approved")
                  .exclude(kind="off").first())
            if ov is None:
                if not role:
                    return JsonResponse({"success": True, "role": ""})
                ov = StaffScheduleOverride(user=u, date=date_, kind="note", created_by=request.user)
            ov.role = role
            # A note-only row with no role, no note and no location flip has
            # nothing left to say.
            if not role and ov.kind == "note" and not ov.note and not ov.location:
                ov.delete()
            else:
                ov.save()
            return JsonResponse({"success": True, "role": role, "scope": "date"})

        try:
            dow = int(data.get("dow"))
        except (TypeError, ValueError):
            return JsonResponse({"success": False, "error": "A weekday or date is required."}, status=400)
        if not 0 <= dow <= 6:
            return JsonResponse({"success": False, "error": "A weekday or date is required."}, status=400)
        row = StaffWeeklySchedule.objects.filter(user=u, day_of_week=dow).first()
        if row is None or not row.is_working:
            return JsonResponse({"success": False, "error": "That dispatcher isn't scheduled that day."})
        row.role = role
        row.save(update_fields=["role", "updated_at"])
        return JsonResponse({"success": True, "role": role, "scope": "weekly"})

    if action == "set_location":
        # Same shape as set_role: a weekday sets the recurring pattern, a date
        # flips just that day via an override (the usual-WFH person coming in).
        u = _staff(data.get("user_id"))
        if not u:
            return JsonResponse({"success": False, "error": "Unknown staff member."}, status=400)
        location = (data.get("location") or "").strip()
        if location and location not in dict(WORK_LOCATION_CHOICES):
            return JsonResponse({"success": False, "error": "Unknown location."}, status=400)

        date_ = _parse_ymd(data.get("date"))
        if date_:
            sched_u = User.objects.prefetch_related("weekly_schedule_rows", "schedule_overrides", "extra_shifts").get(pk=u.pk)
            if not scheduling.resolve_staff_schedule(sched_u, date_)["is_working"]:
                return JsonResponse({"success": False, "error": "That dispatcher isn't working that day."})
            ov = (StaffScheduleOverride.objects
                  .filter(user=u, date=date_, end_date__isnull=True, status="approved")
                  .exclude(kind="off").first())
            if ov is None:
                if not location:
                    return JsonResponse({"success": True, "location": ""})
                ov = StaffScheduleOverride(user=u, date=date_, kind="note", created_by=request.user)
            ov.location = location
            if not location and ov.kind == "note" and not ov.note and not ov.role:
                ov.delete()
            else:
                ov.save()
            return JsonResponse({"success": True, "location": location, "scope": "date"})

        try:
            dow = int(data.get("dow"))
        except (TypeError, ValueError):
            return JsonResponse({"success": False, "error": "A weekday or date is required."}, status=400)
        if not 0 <= dow <= 6:
            return JsonResponse({"success": False, "error": "A weekday or date is required."}, status=400)
        row = StaffWeeklySchedule.objects.filter(user=u, day_of_week=dow).first()
        if row is None or not row.is_working:
            return JsonResponse({"success": False, "error": "That dispatcher isn't scheduled that day."})
        row.location = location
        row.save(update_fields=["location", "updated_at"])
        return JsonResponse({"success": True, "location": location, "scope": "weekly"})

    if action == "add_shift":
        # Somebody covering a day they don't normally work — Joseph taking a Friday
        # while Luis is away. This is deliberately a one-off dated exception and
        # never a change to the recurring pattern: flipping "every Friday" on and
        # hoping to remember to flip it back is how a roster silently goes wrong
        # for months.
        u = _staff(data.get("user_id"))
        if not u:
            return JsonResponse({"success": False, "error": "Unknown staff member."}, status=400)
        date_ = _parse_ymd(data.get("date"))
        if not date_:
            return JsonResponse({"success": False, "error": "A date is required."})
        through = _parse_ymd(data.get("through"))
        if through and through < date_:
            return JsonResponse({"success": False, "error": "The last day must be on or after the first."})
        if through and (through - date_).days > 30:
            return JsonResponse({"success": False, "error": "Keep a one-off cover to 31 days or fewer."})
        start_t, end_t = _parse_hm(data.get("start")), _parse_hm(data.get("end"))
        if start_t is None or end_t is None:
            return JsonResponse({"success": False, "error": "A start and end time are required."})
        if start_t == end_t:
            return JsonResponse({"success": False, "error": "The start and end times can't match."})

        role = (data.get("role") or "").strip()
        if role not in dict(STAFF_ROLE_CHOICES):
            role = ""

        # Scheduling somebody who is approved off is a contradiction, not an
        # override — say so instead of silently outranking their time off.
        sched_u = User.objects.prefetch_related("weekly_schedule_rows", "schedule_overrides", "extra_shifts").get(pk=u.pk)
        clash_days = [d for d in _dates_between(date_, through or date_)
                      if scheduling.resolve_staff_schedule(sched_u, d).get("time_off")]
        if clash_days:
            when = clash_days[0].strftime("%b %d").replace(" 0", " ")
            return JsonResponse({"success": False, "error": (
                f"{u.get_full_name() or u.username} is booked off on {when} — "
                "remove that time off first.")})

        # If they already have a window that day, a second one is a SPLIT shift,
        # not a replacement — writing another override would silently overwrite
        # the morning half. But re-submitting for a date that already has a
        # one-off override is an *edit* of that cover, not a split, or fixing a
        # typo would quietly leave them scheduled twice.
        existing_cover = StaffScheduleOverride.objects.filter(
            user=u, date=date_, end_date__isnull=True, kind="custom_hours", status="approved",
        ).first()
        already_on = bool(scheduling.resolve_staff_schedule(sched_u, date_)["is_working"])
        as_extra = bool(data.get("as_extra")) or (already_on and existing_cover is None)
        if as_extra:
            for d in _dates_between(date_, through or date_):
                StaffExtraShift.objects.create(
                    user=u, date=d, start_time=start_t, end_time=end_t, role=role,
                    note=(data.get("note") or "")[:200], created_by=request.user,
                )
            return JsonResponse({"success": True, "as_extra": True})

        StaffScheduleOverride.objects.update_or_create(
            user=u, date=date_, end_date=through if through and through != date_ else None,
            defaults={"kind": "custom_hours", "start_time": start_t, "end_time": end_t,
                      "role": role, "reason": "", "status": "approved",
                      "requested_by_staff": False, "created_by": request.user,
                      "note": (data.get("note") or "")[:200]},
        )
        return JsonResponse({"success": True})

    if action == "add_recurring_extra":
        # The same split, every week — e.g. Iris works 9–1 and 5–9 every Wednesday.
        u = _staff(data.get("user_id"))
        if not u:
            return JsonResponse({"success": False, "error": "Unknown staff member."}, status=400)
        try:
            dow = int(data.get("dow"))
        except (TypeError, ValueError):
            dow = -1
        if not 0 <= dow <= 6:
            return JsonResponse({"success": False, "error": "A weekday is required."}, status=400)
        start_t, end_t = _parse_hm(data.get("start")), _parse_hm(data.get("end"))
        if start_t is None or end_t is None or start_t == end_t:
            return JsonResponse({"success": False, "error": "A start and end time are required."})
        role = (data.get("role") or "").strip()
        if role not in dict(STAFF_ROLE_CHOICES):
            role = ""
        StaffExtraShift.objects.create(
            user=u, day_of_week=dow, start_time=start_t, end_time=end_t, role=role,
            note=(data.get("note") or "")[:200], created_by=request.user,
        )
        return JsonResponse({"success": True})

    if action == "remove_shift":
        # Either half can be removed: an extra row, or the one-off override.
        if data.get("extra_id"):
            get_object_or_404(StaffExtraShift, id=data.get("extra_id")).delete()
            return JsonResponse({"success": True})
        ov = get_object_or_404(StaffScheduleOverride, id=data.get("id"), kind="custom_hours")
        ov.delete()
        return JsonResponse({"success": True})

    if action in ("approve_timeoff", "deny_timeoff"):
        ov = get_object_or_404(StaffScheduleOverride, id=data.get("id"))
        try:
            timeoff.decide(ov, request.user, approve=(action == "approve_timeoff"),
                           denial_reason=data.get("denial_reason", ""))
        except timeoff.TimeOffError as e:
            return JsonResponse({"success": False, "error": str(e)})
        return JsonResponse({"success": True, "status": ov.status})

    if action == "add_timeoff":
        u = _staff(data.get("user_id"))
        if not u:
            return JsonResponse({"success": False, "error": "Unknown staff member."}, status=400)
        try:
            timeoff.submit_request(
                u, _parse_ymd(data.get("start")), _parse_ymd(data.get("end")),
                reason=data.get("reason", ""), note=data.get("note", ""),
                by=request.user, approved=True,
            )
        except timeoff.TimeOffError as e:
            return JsonResponse({"success": False, "error": str(e)})
        return JsonResponse({"success": True})

    if action == "cancel_timeoff":
        timeoff.cancel(get_object_or_404(StaffScheduleOverride, id=data.get("id"), kind="off"))
        return JsonResponse({"success": True})

    return JsonResponse({"success": False, "error": "Unknown action"}, status=400)


@login_required(login_url="login")
@user_passes_test(_is_staff, login_url="login")
@require_POST
def my_timeoff_action(request):
    """A dispatcher's own time-off requests, from their schedule page.

    Submitting creates a *pending* row that changes nothing until a manager
    approves it; cancelling only ever touches the requester's own rows.
    """
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, AttributeError):
        data = request.POST
    action = data.get("action")

    if action == "request":
        try:
            timeoff.submit_request(
                request.user, _parse_ymd(data.get("start")), _parse_ymd(data.get("end")),
                reason=data.get("reason", ""), note=data.get("note", ""), by=request.user,
            )
        except timeoff.TimeOffError as e:
            return JsonResponse({"success": False, "error": str(e)})
        return JsonResponse({"success": True})

    if action == "cancel":
        ov = get_object_or_404(StaffScheduleOverride, id=data.get("id"), user=request.user, kind="off")
        timeoff.cancel(ov)
        return JsonResponse({"success": True})

    return JsonResponse({"success": False, "error": "Unknown action"}, status=400)


@login_required(login_url="login")
@user_passes_test(_is_staff, login_url="login")
def my_coverage(request):
    """Dispatcher-facing "My Schedule" — the calm flip side of the admin board.

    Any dispatcher (not superuser-only) sees their OWN recurring week: the days
    they work, who they're on with each shift, the handoffs, and a single calm
    "today" timeline. It carries none of the admin board's coverage-risk
    language — no gaps, "thin", targets, or red. Read-only; schedule edits stay
    with admins. On-call for *tonight* is looked up per date for the viewer only.
    """
    roster = list(_office_staff_qs().prefetch_related("weekly_schedule_rows", "schedule_overrides", "extra_shifts"))
    today = timezone.localdate()
    today_dow = today.weekday()
    this_monday = today - timedelta(days=today_dow)

    # The whole page runs on *actual* dates (one-off sick/off, custom hours and
    # WFH flips applied): the week overview cards and the day panels are the
    # same seven ``day_view_actual`` dicts. "This week" or "Next week" only.
    week_is_next = request.GET.get("week") == "next"
    monday = this_monday + timedelta(days=7 if week_is_next else 0)
    week = coverage.my_week(request.user, roster, today_dow=today_dow)  # meta only (on_roster/has_schedule)
    day_views = coverage.my_week_actual(request.user, roster, monday, today)

    # Week-at-a-glance numbers for the viewer, from the dated views.
    week_days_on = sum(1 for dv in day_views if dv["is_working"])
    week_hours = coverage._fmt_hours(sum(dv["my_minutes"] for dv in day_views))
    selected_dow = today_dow if not week_is_next else 0
    week_range_label = f"{coverage.md(monday)} – {coverage.md(monday + timedelta(days=6))}"

    # Who's on-call tonight — date-based, now shown plainly by name (a teammate's
    # coverage is useful to see), with the viewer's own marked "You". Informational.
    oncall_today = []
    for oc in (StaffOnCall.objects.filter(date=today, user__in=[u.id for u in roster])
               .select_related("user").order_by("start_time")):
        is_me = oc.user_id == request.user.id
        disp = oc.user.get_full_name() or oc.user.username
        s = oc.start_time.hour * 60 + oc.start_time.minute
        e = oc.end_time.hour * 60 + oc.end_time.minute
        oncall_today.append({
            "name": "You" if is_me else disp,
            "short": "You" if is_me else (disp.split()[0] if disp.split() else disp),
            "window": f"{coverage._fmt_min_long(s)} – {coverage._fmt_min_long(e)}",
            "is_me": is_me,
        })

    # Timeline "now" marker + hour-axis ticks. Every 3 hours with a full AM/PM
    # label; edge ticks align inward (translateX) so they never overflow the
    # scroll box (which would otherwise surface a stray horizontal scrollbar).
    now = timezone.localtime()
    now_frac = round((now.hour * 60 + now.minute) / 1440 * 100, 3)

    def _axis_label(h):
        hh = h % 24
        return f"{hh % 12 or 12} {'AM' if hh < 12 else 'PM'}"

    hour_ticks = []
    for h in range(0, 25, 3):
        tx = "0" if h == 0 else ("-100%" if h == 24 else "-50%")
        hour_ticks.append({"pos": round(h / 24 * 100, 3), "label": _axis_label(h), "tx": tx})
    grid_ticks = [round(h / 24 * 100, 3) for h in range(3, 24, 3)]

    return render(request, "dispatching/my_coverage.html", {
        "day_views": day_views,
        "week_is_next": week_is_next,
        "week_days_on": week_days_on,
        "week_hours": week_hours,
        "week_range_label": week_range_label,
        "selected_dow": selected_dow,
        "has_schedule": week["has_schedule"],
        "on_roster": week["on_roster"],
        "me_name": week["me_name"],
        "oncall_today": oncall_today,
        "today_dow": today_dow,
        "now_frac": now_frac,
        "hour_ticks": hour_ticks,
        "grid_ticks": grid_ticks,
        "my_timeoff": timeoff.my_requests(request.user, today=today),
        "reason_choices": StaffScheduleOverride.REASON_CHOICES,
        "today_str": today.strftime("%Y-%m-%d"),
    })
