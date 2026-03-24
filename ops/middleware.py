"""
StaffActivity tracking middleware.
Records page views on dispatching URLs for staff users,
deduplicated to 5-minute windows per path using Django's cache.
"""

import hashlib
import logging

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

DEDUP_SECONDS = 1800  # 30 minutes — reduced DB writes (was 5 min)


class StaffActivityMiddleware:
    """
    Passively tracks staff page views on dispatching URLs.
    Only records GET requests from authenticated staff users.
    Deduplicates within 5-minute windows using Django cache
    (works correctly across multiple gunicorn workers).
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

        # Dedup check via cache (works across workers)
        path_hash = hashlib.md5(path.encode()).hexdigest()[:10]
        cache_key = f"staff_act_{request.user.id}_{path_hash}"
        if cache.get(cache_key):
            return response

        cache.set(cache_key, True, timeout=DEDUP_SECONDS)

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
