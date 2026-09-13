"""
Context processor that injects pending ops task count into every template.
Used to show a badge in the dispatcher navbar.
"""

import logging

from django.core.cache import cache

from .models import OperationalTask, TimeClockShift

logger = logging.getLogger(__name__)

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


def critical_disruption_count(request):
    """Red pill on the dispatch Dashboard link: today's visible critical
    Recovery Advisor cards.

    Cache-READ-only — ``ra_crit_count`` is mirrored by the advisor state
    endpoint whenever it computes today's board (dispatching/advisor_views.py);
    this NEVER computes anything itself and degrades to 0 when the cache is
    cold or expired (TTL 300 s)."""
    from dispatching.advisor_views import advisor_visible_to
    if not hasattr(request, "user") or not advisor_visible_to(request.user):
        return {}
    return {"critical_disruption_count": cache.get("ra_crit_count") or 0}


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


# The day's opener/closer, cached for everyone rather than per user. Resolving
# it costs a prefetched roster query plus a coverage pass, and the navbar asks
# on EVERY page — but the answer is the same for all viewers and changes only
# when the roster does, so one entry serves the whole floor.
_SHIFT_DUTY_TTL = 300  # seconds


def shift_menu(request):
    """Which shift checklists this person is offered in the navbar.

    Founder direction 2026-09-13: the opener gets the open, the closer gets the
    close. Two links that both look equally like yours is how a dispatcher ends
    up filling in the wrong one — which is exactly what happened, and why the
    close checklist was being worked as though it were an open.

    Who still sees BOTH, and why each case is deliberate:
      * anyone who can review checklists (the lead, a superuser) — they oversee
        both ends of the day, so neither is "theirs" and both must be reachable;
      * anyone the roster does not name as opener OR closer — a mid-day
        dispatcher has no duty of their own, so there is no basis to hide either
        one from them, and they are the person most likely to be covering;
      * everyone, if the roster cannot answer at all. A schedule that has not
        been filled in must never cost the floor its checklists.

    This hides MENU ENTRIES, not the pages. ``shift_open`` and ``shift_close``
    stay reachable by URL on purpose: someone covering an unscheduled close at
    9 PM needs a way in that does not involve editing the roster first, and a
    tidy menu is not worth a shift that cannot be closed.
    """
    if (not hasattr(request, "user") or not request.user.is_authenticated
            or not request.user.is_staff):
        return {}

    both = {"show_open_shift": True, "show_close_shift": True}

    # The lead sees the whole day, both ends.
    if request.user.is_superuser or request.user.has_perm("ops.review_checklists"):
        return both

    from django.utils import timezone

    today = timezone.localdate()
    key = f"ops_shift_duties:{today:%Y-%m-%d}"
    duties = cache.get(key)
    if duties is None:
        try:
            from . import shift_services

            opener, closer = shift_services.scheduled_duties(today)
            duties = (getattr(opener, "pk", None), getattr(closer, "pk", None))
            cache.set(key, duties, _SHIFT_DUTY_TTL)
        except Exception:
            # Never cost anyone the navbar over a roster lookup.
            logger.exception("Could not resolve today's shift duties")
            return both

    opener_pk, closer_pk = duties
    if opener_pk is None and closer_pk is None:
        return both

    is_opener = opener_pk == request.user.pk
    is_closer = closer_pk == request.user.pk
    if not is_opener and not is_closer:
        return both

    return {"show_open_shift": is_opener, "show_close_shift": is_closer}
