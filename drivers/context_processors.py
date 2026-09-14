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


_FLEET_KEY = "fleet_now_count"
_FLEET_TTL = 60


def fleet_nav(request):
    """What the top bar shows the fleet role.

    ``fleet_nav`` is True for a fleet manager who is NOT a superuser: they get
    the fleet-only bar and land on the Fleet desk. Founders keep the full
    dispatch bar with a Fleet entry added. ``fleet_now_count`` is the red
    pill on that entry — the cheap facts only (a car overdue back, an open
    "do not drive" / "fix soon" report, a lit fault code, expired paperwork),
    cached a minute, never the full desk computation.
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated or not user.is_staff:
        return {}
    profile = getattr(user, "profile", None)
    is_fleet = bool(profile and getattr(profile, "is_fleet_manager", False))
    out = {"fleet_nav": is_fleet and not user.is_superuser, "is_fleet_manager": is_fleet}
    if not (is_fleet or user.is_superuser):
        return out
    count = cache.get(_FLEET_KEY)
    if count is None:
        try:
            from dispatching.fleet_desk import quick_now_count
            count = quick_now_count()
        except Exception:
            count = 0
        cache.set(_FLEET_KEY, count, _FLEET_TTL)
    out["fleet_now_count"] = count
    return out


def invalidate_fleet_now_count():
    cache.delete(_FLEET_KEY)


def webpush_public_key(request):
    """Expose the VAPID public key to driver-portal templates so the subscribe
    JS can call pushManager.subscribe(). Empty string = push not configured →
    the bell UI hides itself."""
    from django.conf import settings
    return {"WEBPUSH_VAPID_PUBLIC_KEY": settings.WEBPUSH_VAPID_PUBLIC_KEY}
