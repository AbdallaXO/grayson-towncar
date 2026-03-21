"""
Context processor that injects pending ops task count into every template.
Used to show a badge in the dispatcher navbar.
"""

from django.core.cache import cache

from .models import OperationalTask

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
