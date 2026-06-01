"""
Context processor that injects pending ops task count into every template.
Used to show a badge in the dispatcher navbar.
"""

from django.core.cache import cache

from .models import OperationalTask, TimeClockShift

# Cache key and TTL for the pending task count badge
_PENDING_COUNT_CACHE_KEY = "ops_pending_task_count"
_PENDING_COUNT_TTL = 60  # seconds


def pending_task_count(request):
    """Add ops_pending_count to template context for staff users."""
    if not hasattr(request, "user") or not request.user.is_authenticated or not request.user.is_staff:
        return {}

    count = cache.get(_PENDING_COUNT_CACHE_KEY)
    if count is None:
        count = OperationalTask.objects.filter(
            status__in=["pending", "in_progress", "escalated"],
        ).count()
        cache.set(_PENDING_COUNT_CACHE_KEY, count, _PENDING_COUNT_TTL)

    return {"ops_pending_count": count}


def timeclock_status(request):
    """
    Inject the logged-in staffer's live clock state for the navbar pill:
    ``timeclock_state`` = clocked_out | clocked_in | on_break.

    Deliberately UNCACHED — it is per-user and must flip the instant they
    clock in/out/break. It's a single indexed query (idx_tcshift_open),
    well within the SlowRequestMiddleware budget.
    """
    if not hasattr(request, "user") or not request.user.is_authenticated or not request.user.is_staff:
        return {}

    shift = (
        TimeClockShift.objects.filter(user=request.user, clock_out_at__isnull=True)
        .prefetch_related("breaks")
        .first()
    )
    if not shift:
        return {"timeclock_state": "clocked_out"}
    return {"timeclock_state": "on_break" if shift.open_break else "clocked_in"}
