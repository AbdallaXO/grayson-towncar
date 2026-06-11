"""Inject pending time-off request count for the dispatcher navbar badge."""

from django.core.cache import cache

_KEY = "timeoff_pending_count"
_TTL = 60  # seconds — short enough that new submissions show up promptly


def pending_timeoff_count(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated or not request.user.is_staff:
        return {}
    count = cache.get(_KEY)
    if count is None:
        # Lazy import — avoids AppRegistryNotReady at startup.
        from drivers.models import DriverDateOverride
        count = DriverDateOverride.objects.filter(status="pending").count()
        cache.set(_KEY, count, _TTL)
    return {"pending_timeoff_count": count}


def invalidate_pending_timeoff_count():
    """Call after creating, approving, or denying a request so the badge updates fast."""
    cache.delete(_KEY)


def webpush_public_key(request):
    """Expose the VAPID public key to driver-portal templates so the subscribe
    JS can call pushManager.subscribe(). Empty string = push not configured →
    the bell UI hides itself."""
    from django.conf import settings
    return {"WEBPUSH_VAPID_PUBLIC_KEY": settings.WEBPUSH_VAPID_PUBLIC_KEY}
