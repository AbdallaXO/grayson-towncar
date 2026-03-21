"""
StaffActivity tracking middleware.
Records page views on dispatching URLs for staff users,
deduplicated to 5-minute windows per path.
"""

import logging
from django.utils import timezone
from datetime import timedelta

logger = logging.getLogger(__name__)

# In-memory dedup cache: (user_id, path) → last_recorded_at
_recent_views = {}
DEDUP_SECONDS = 1800  # 30 minutes — reduced DB writes (was 5 min)


def _cleanup_stale_entries():
    """Remove entries older than 10 minutes to prevent memory growth."""
    cutoff = timezone.now() - timedelta(seconds=DEDUP_SECONDS * 2)
    stale = [k for k, v in _recent_views.items() if v < cutoff]
    for k in stale:
        del _recent_views[k]


class StaffActivityMiddleware:
    """
    Passively tracks staff page views on dispatching URLs.
    Only records GET requests from authenticated staff users.
    Deduplicates within 5-minute windows to avoid noise.
    """

    # Only track views under these URL prefixes
    TRACKED_PREFIXES = ("/dispatching/",)

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # Only track successful GET requests from authenticated staff
        if (
            request.method != "GET"
            or response.status_code != 200
            or not hasattr(request, "user")
            or not request.user.is_authenticated
            or not request.user.is_staff
        ):
            return response

        path = request.path
        if not any(path.startswith(p) for p in self.TRACKED_PREFIXES):
            return response

        # Skip AJAX/API requests
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return response
        content_type = response.get("Content-Type", "")
        if "application/json" in content_type:
            return response

        # Dedup check
        now = timezone.now()
        key = (request.user.id, path)
        last = _recent_views.get(key)
        if last and (now - last).total_seconds() < DEDUP_SECONDS:
            return response

        _recent_views[key] = now

        # Periodic cleanup
        if len(_recent_views) > 500:
            _cleanup_stale_entries()

        # Record the page view
        try:
            from ops.models import StaffActivity
            StaffActivity.objects.create(
                user=request.user,
                action_type=StaffActivity.ActionType.PAGE_VIEW,
                path=path,
                ip_address=self._get_ip(request),
            )
        except Exception as e:
            logger.warning(f"Failed to log staff activity: {e}")

        return response

    @staticmethod
    def _get_ip(request):
        xff = request.META.get("HTTP_X_FORWARDED_FOR")
        if xff:
            return xff.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR", "")
