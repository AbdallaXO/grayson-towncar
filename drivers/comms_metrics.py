"""
Per-chauffeur client-communication rates: how often the standard on-the-way,
on-location and review-request texts actually went out.

WHAT THESE NUMBERS MEAN. Every rate here is

    distinct completed trips where the chauffeur opened the message
    ------------------------------------------------------------------
    distinct completed trips he could have opened it on

The numerator counts a TAP in the driver app, not a delivery receipt. The
messages leave from the chauffeur's own handset via an `sms:` link (so the guest
can reply to the man driving them), and a phone never reports back to us. Read
every figure as "sent from the app". See reservations.LegClientMessage.

DENOMINATOR RULES, and why each one is there:

* status="completed", cancelled reservations excluded — the canonical definition
  of work performed (dispatching/load_metrics.py:41, :182-193).
* pickup_date >= tracking_start() — nothing was recorded before this feature
  shipped, so counting older trips would bury every driver at 0% forever. Same
  device as driver_performance gating on status_history existing
  (dispatching/views.py:14098).
* in-house drivers only — affiliates and operators never see a job card with
  these buttons on it (their portal card has no customer phone at all), so they
  would score a permanent 0% that reads as a performance failure.
* pickup_date filtered directly on Leg, never reservation__pickup_date.

This is a QUALITY metric, so it is barred from the chauffeur load/KPI pages by
SOP-003 ("Volume is not quality" — SOPS/chauffeur-load-metrics.md:186-189). Its
sanctioned homes are the driver profile and the admin comms page.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.conf import settings
from django.db.models import Count

from .client_messages import KINDS, KIND_CHOICES

#: Communication tracking went live on this date. Trips before it carry no
#: records, so they are excluded from the denominator rather than counted as
#: misses. Read through a function, not a module constant, so settings (and
#: tests) can move it after import.
_DEFAULT_TRACKING_START = date(2026, 8, 19)


def tracking_start():
    """First date a chauffeur could possibly have used these buttons."""
    return getattr(settings, "CLIENT_MESSAGE_TRACKING_START", _DEFAULT_TRACKING_START)


#: Selectable look-back windows, matching the chauffeur pages' vocabulary.
WINDOWS = {"7": 7, "30": 30, "90": 90, "365": 365}
WINDOW_DEFAULT = "30"
WINDOW_LABELS = {"7": "7 days", "30": "30 days", "90": "90 days", "365": "12 months"}


def resolve_window(raw):
    """('30', 30) from a querystring value, falling back to the default."""
    key = str(raw or "").strip() or WINDOW_DEFAULT
    if key not in WINDOWS:
        key = WINDOW_DEFAULT
    return key, WINDOWS[key]


def window_bounds(days, *, today=None):
    """(start, end) for a look-back window.

    Ends YESTERDAY, never today — a trip still running today has not had its
    chance to be texted yet, and counting it drags every rate down before noon.
    Matches the chauffeur pages (dispatching/views.py:20468-20472).

    The start is clamped to tracking_start() so a 12-month window never reaches
    back into untracked history.
    """
    from django.utils import timezone

    today = today or timezone.localdate()
    end = today - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    floor = tracking_start()
    if start < floor:
        start = floor
    return start, end


def window_is_empty(start, end):
    """True when the window contains no days at all.

    Happens on and just after launch day: the window ends YESTERDAY but is
    clamped forward to tracking_start(), so start lands after end. Every rate is
    then legitimately unknowable, and the page must SAY so — a grid of dashes
    reads like "nobody is doing it" when it means "no data could exist yet".
    """
    return start > end


def recent_activity(*, days=7, driver=None, today=None):
    """Raw count of texts logged recently, ignoring every rate-window rule.

    Exists so the feature is verifiable on day one. A chauffeur's tap shows up
    here the moment it lands, long before the trip is completed and long before
    any rate window can include it. Without this there is no way to tell a
    working system from a broken one during the first days.
    """
    from django.utils import timezone

    from reservations.models import LegClientMessage

    today = today or timezone.localdate()
    qs = LegClientMessage.objects.all()
    if driver is not None:
        qs = qs.filter(driver_id=getattr(driver, "id", driver))
    since = today - timedelta(days=days - 1)
    window_qs = qs.filter(created_at__date__gte=since)
    latest = qs.order_by("-created_at").values_list("created_at", flat=True).first()
    return {
        "days": days,
        "window": window_qs.count(),
        "today": qs.filter(created_at__date=today).count(),
        "total": qs.count(),
        "latest": latest,
    }


def _eligible_legs(start, end, *, driver_ids=None):
    """The denominator queryset: completed, uncancelled, in-window trips."""
    from reservations.models import Leg

    qs = (
        Leg.objects.filter(
            status="completed",
            pickup_date__gte=max(start, tracking_start()),
            pickup_date__lte=end,
            driver__isnull=False,
            driver__driver_type="inhouse",
        )
        .exclude(reservation__status="cancelled")
    )
    if driver_ids is not None:
        qs = qs.filter(driver_id__in=driver_ids)
    return qs


def _blank_row():
    return {
        kind: {"sent": 0, "eligible": 0, "pct": None} for kind in KINDS
    }


def _pct(sent, eligible):
    if not eligible:
        return None
    return round(sent / eligible * 100)


def comms_stats_bulk(driver_ids, start, end):
    """{driver_id: {kind: {sent, eligible, pct}}} for many drivers in 2 queries.

    Counts DISTINCT LEGS, not message rows — a chauffeur who re-texts a guest
    who did not answer must not score 150%.

    Deliberately two separate queries: putting the trip count and the message
    count in one annotate() would multiply rows across the join and silently
    inflate both (the trap documented at drivers/views.py:851-853).
    """
    from reservations.models import LegClientMessage

    ids = list(driver_ids or [])
    out = {did: _blank_row() for did in ids}
    if not ids:
        return out

    eligible = _eligible_legs(start, end, driver_ids=ids)

    trips = eligible.values("driver_id").annotate(n=Count("id", distinct=True))
    trip_counts = {r["driver_id"]: r["n"] for r in trips}

    sent = (
        LegClientMessage.objects
        .filter(leg__in=eligible)
        .values("leg__driver_id", "kind")
        .annotate(n=Count("leg_id", distinct=True))
    )
    sent_counts = {(r["leg__driver_id"], r["kind"]): r["n"] for r in sent}

    for did in ids:
        total = trip_counts.get(did, 0)
        for kind in KINDS:
            n = sent_counts.get((did, kind), 0)
            out[did][kind] = {
                "sent": n,
                "eligible": total,
                "pct": _pct(n, total),
            }
    return out


def comms_stats(driver, start, end):
    """One driver's rates. `driver` may be a Driver or an id."""
    did = getattr(driver, "id", driver)
    return comms_stats_bulk([did], start, end)[did]


def as_tiles(stats):
    """Ordered list for template rendering: label + n/N + pct, ready to paint.

    `accent` is threshold-based so the eye lands on the weak one, using the
    same green/amber/grey ladder as the Tracking column on Driver Performance.
    """
    labels = dict(KIND_CHOICES)
    tiles = []
    for kind in KINDS:
        row = stats.get(kind) or {"sent": 0, "eligible": 0, "pct": None}
        pct = row["pct"]
        if pct is None:
            accent = "muted"
        elif pct >= 80:
            accent = "green"
        elif pct >= 50:
            accent = "amber"
        else:
            accent = "red"
        tiles.append(
            {
                "kind": kind,
                "label": labels.get(kind, kind),
                "sent": row["sent"],
                "eligible": row["eligible"],
                "pct": pct,
                "accent": accent,
            }
        )
    return tiles


def overall_pct(stats):
    """A single headline rate across all three message kinds, or None."""
    sent = sum((stats.get(k) or {}).get("sent", 0) for k in KINDS)
    eligible = sum((stats.get(k) or {}).get("eligible", 0) for k in KINDS)
    return _pct(sent, eligible)
