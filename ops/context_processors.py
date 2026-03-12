"""
Context processor that injects pending ops task count into every template.
Used to show a badge in the dispatcher navbar.
"""

from .models import OperationalTask


def pending_task_count(request):
    """Add ops_pending_count to template context for staff users."""
    if not hasattr(request, "user") or not request.user.is_authenticated or not request.user.is_staff:
        return {}

    count = OperationalTask.objects.filter(
        status__in=["pending", "in_progress", "escalated"],
    ).count()

    return {"ops_pending_count": count}
