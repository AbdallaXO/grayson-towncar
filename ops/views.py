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


@login_required(login_url="login")
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

    tasks = OperationalTask.objects.filter(
        status__in=["pending", "in_progress", "escalated", "snoozed"],
    ).select_related(
        "reservation",
        "reservation__customer",
        "leg",
        "leg__reservation",
        "leg__reservation__customer",
        "leg__flight_information",
        "lead",
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

    context = {
        "tasks": tasks,
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


@login_required(login_url="login")
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
    return render(request, "dispatching/task_detail.html", context)


# ── Staff Metrics Dashboard (superuser-only) ──


def _is_superuser(user):
    return user.is_superuser


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
