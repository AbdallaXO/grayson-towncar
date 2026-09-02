"""
Per-chauffeur client-communication rates: how often the standard on-the-way,
on-location and review-request texts actually went out.

WHAT THESE NUMBERS MEAN. Every rate here is

    distinct trips where the chauffeur opened that message
    -------------------------------------------------------
    distinct trips where his chance to open it had already come

The numerator counts a TAP in the driver app, not a delivery receipt. The
messages leave from the chauffeur's own handset via an `sms:` link (so the guest
can reply to the man driving them), and a phone never reports back to us. Read
every figure as "sent from the app". See reservations.LegClientMessage.

LIVE, NOT END-OF-TRIP — this is the load-bearing rule, and the reason each kind
carries its OWN denominator. The three texts belong to three different moments,
and a chance only exists once its moment has arrived:

    on_the_way   opens when the chauffeur sets off      (status on-the-way)
    on_location  opens when he is standing at the meet  (status on-location)
    review       opens once the guest is aboard         (status picked-up)

...and a completed trip has passed all three, so it counts everywhere, exactly
as it always did. No historical rate moves because of this: what changes is that
a trip in flight now shows up the same minute the chauffeur works it, instead of
being invisible until somebody marks the job complete. That is what makes a tap
read as 1/1 straight away.

The consequence to keep hold of: a chance that has NOT opened yet is not a miss
and must never be counted as one. A trip booked for tonight contributes nothing
to anybody's rate this morning; a trip whose driver is on the way contributes to
the on-the-way rate only. A trip that ended without ever being marked past
'confirmed' contributes nothing either — the same silence it produced before.

One extra rule closes the last gap: a leg with a LOGGED TAP of a given kind is
always in that kind's denominator, whatever stage the leg shows. Chauffeurs
routinely text the guest before they touch the status control, and a numerator
that can outrun its denominator would print rates over 100%.

OTHER DENOMINATOR RULES, and why each one is there:

* cancelled reservations and cancelled legs excluded — the canonical definition
  of work performed (dispatching/load_metrics.py:41, :182-193).
* pickup_date >= tracking_start() — nothing was recorded before this feature
  shipped, so counting older trips would bury every driver at 0% forever. Same
  device as driver_performance gating on status_history existing
  (dispatching/views.py:14098).
* in-house drivers only — an affiliate chauffeur DOES see the same texting
  buttons on his job card (only true operators, who never drive and never
  reach a card with a guest phone number, are excluded from the feature
  itself). Affiliates are contractors, not held to Grayson's in-house guest-
  communication standard, so scoring them here would read as a performance
  failure against a bar they were never asked to meet.
* pickup_date filtered directly on Leg, never reservation__pickup_date.

This is a QUALITY metric, so it is barred from the chauffeur load/KPI pages by
SOP-003 ("Volume is not quality" — SOPS/chauffeur-load-metrics.md:186-189). Its
sanctioned homes are the driver profile and the admin comms page.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.conf import settings
from django.db.models import Count

from .client_messages import KINDS, KIND_CHOICES, ON_LOCATION, ON_THE_WAY, REVIEW

#: Communication tracking went live on this date. Trips before it carry no
#: records, so they are excluded from the denominator rather than counted as
#: misses. Read through a function, not a module constant, so settings (and
#: tests) can move it after import.
_DEFAULT_TRACKING_START = date(2026, 8, 19)


def tracking_start():
    """First date a chauffeur could possibly have used these buttons."""
    return getattr(settings, "CLIENT_MESSAGE_TRACKING_START", _DEFAULT_TRACKING_START)


# ── Trip stages ────────────────────────────────────────────────────────────
#: The driver-app statuses a job moves through, in order. Mirrors
#: reservations.constants.DRIVER_STATUS and LegStatus.STATUS_CHOICES;
#: 'cancelled' is deliberately absent because a cancelled leg is never a chance
#: at anything. 'assigned' appears in LegStatus history but never on Leg.status
#: itself, and would sit below every opening stage anyway.
STAGE_ORDER = [
    "in-progress",   # the model default: assigned, nothing done yet
    "confirmed",
    "on-the-way",
    "on-location",
    "picked-up",
    "completed",
]

#: The stage at which each message's chance OPENS. Before it there is nothing
#: to score — the moment for that text has not arrived.
KIND_OPENS_AT = {
    ON_THE_WAY: "on-the-way",
    ON_LOCATION: "on-location",
    REVIEW: "picked-up",
}


def _stages_from(stage):
    return frozenset(STAGE_ORDER[STAGE_ORDER.index(stage):])


#: {kind: frozenset of leg statuses at or past that kind's opening stage}.
OPEN_STAGES = {kind: _stages_from(stage) for kind, stage in KIND_OPENS_AT.items()}


def chance_is_open(kind, leg_status):
    """Has this trip reached the moment `kind` is supposed to be sent?"""
    return (leg_status or "") in OPEN_STAGES.get(kind, frozenset())


#: Selectable look-back windows, matching the chauffeur pages' vocabulary.
#: "Today" exists because the rates are live: the owner's first question after
#: a shift starts is "who is texting right now", not "how was the month".
WINDOWS = {"1": 1, "7": 7, "30": 30, "90": 90, "365": 365}
WINDOW_DEFAULT = "30"
WINDOW_LABELS = {"1": "Today", "7": "7 days", "30": "30 days", "90": "90 days", "365": "12 months"}


def resolve_window(raw):
    """('30', 30) from a querystring value, falling back to the default."""
    key = str(raw or "").strip() or WINDOW_DEFAULT
    if key not in WINDOWS:
        key = WINDOW_DEFAULT
    return key, WINDOWS[key]


def window_bounds(days, *, today=None):
    """(start, end) for a look-back window.

    Ends TODAY — a tap counts toward a chauffeur's rate the moment it lands,
    same as recent_activity(). Today's trips are in scope as soon as they are
    under way, so a rate can move several times over the course of a shift.

    The start is clamped to tracking_start() so a 12-month window never reaches
    back into untracked history.
    """
    from django.utils import timezone

    today = today or timezone.localdate()
    end = today
    start = end - timedelta(days=days - 1)
    floor = tracking_start()
    if start < floor:
        start = floor
    return start, end


def window_is_empty(start, end):
    """True when the window contains no days at all.

    Happens on and just before launch day: the window is clamped forward to
    tracking_start(), which can land after end. Every rate is then legitimately
    unknowable, and the page must SAY so — a grid of dashes reads like "nobody
    is doing it" when it means "no data could exist yet".
    """
    return start > end


def days_live(*, today=None):
    """How many days this feature has been recording. 1 on launch day."""
    from django.utils import timezone

    today = today or timezone.localdate()
    return max((today - tracking_start()).days + 1, 0)


def recent_activity(*, days=7, driver=None, today=None):
    """Raw count of texts logged recently, ignoring every rate-window rule.

    The rates themselves are live now, so this is no longer the only proof the
    system works — but it stays as the one figure with NO eligibility rule
    behind it at all, which is what you want when the question is "is anything
    reaching the database".

    One query — called on both the driver profile and the KPI dashboard, so
    the four separate .count()/.first() calls this used to make (four round
    trips for what's really one pass over the same rows) are collapsed into a
    single .aggregate().
    """
    from django.db.models import Count, Max, Q
    from django.utils import timezone

    from reservations.models import LegClientMessage

    today = today or timezone.localdate()
    qs = LegClientMessage.objects.all()
    if driver is not None:
        qs = qs.filter(driver_id=getattr(driver, "id", driver))
    since = today - timedelta(days=days - 1)
    agg = qs.aggregate(
        window=Count("id", filter=Q(created_at__date__gte=since)),
        today=Count("id", filter=Q(created_at__date=today)),
        total=Count("id"),
        latest=Max("created_at"),
    )
    return {"days": days, **agg}


def _base_legs(start, end, *, driver_ids=None):
    """Every trip in scope, at whatever stage it has reached.

    NOT the denominator on its own — each kind takes the slice of this that has
    reached its own opening stage (OPEN_STAGES). Cancellations are dropped here,
    at both levels: a cancelled reservation, and a leg cancelled on its own.
    """
    from reservations.models import Leg

    qs = (
        Leg.objects.filter(
            pickup_date__gte=max(start, tracking_start()),
            pickup_date__lte=end,
            driver__isnull=False,
            driver__driver_type="inhouse",
        )
        .exclude(status="cancelled")
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


def accent_for(pct):
    """green/amber/red/muted for a rate — the single threshold ladder (>=80
    green, >=50 amber) every page that colors a rate must use, so the fleet
    headline and the per-row "overall" figure can't silently drift from the
    per-kind cells `as_tiles()` colors."""
    if pct is None:
        return "muted"
    if pct >= 80:
        return "green"
    if pct >= 50:
        return "amber"
    return "red"


# ── Row states ─────────────────────────────────────────────────────────────
# "Has taken none of his chances" and "takes half of them" are two different
# conversations, and painting the first one red is how you end up with a page
# nobody trusts. A chauffeur who has never used the buttons is NOT-STARTED:
# shown neutrally, and said in words. A red percentage is kept for someone who
# has clearly started and is letting it slide.
STATE_NO_CHANCES = "no_chances"    # nothing has opened yet — no rate exists
STATE_NOT_STARTED = "not_started"  # chances opened, none taken
STATE_RATED = "rated"

STATE_LABELS = {
    STATE_NO_CHANCES: "No trips yet",
    STATE_NOT_STARTED: "Not started",
    STATE_RATED: "",
}


def totals(stats):
    """(sent, eligible) summed across all three kinds."""
    sent = sum((stats.get(k) or {}).get("sent", 0) for k in KINDS)
    eligible = sum((stats.get(k) or {}).get("eligible", 0) for k in KINDS)
    return sent, eligible


def row_state(stats):
    """Which of the three stories this chauffeur's row is telling."""
    sent, eligible = totals(stats)
    if not eligible:
        return STATE_NO_CHANCES
    if not sent:
        return STATE_NOT_STARTED
    return STATE_RATED


def comms_stats_bulk(driver_ids, start, end):
    """{driver_id: {kind: {sent, eligible, pct}}} for many drivers in 2 queries.

    Counts DISTINCT LEGS, not message rows — a chauffeur who re-texts a guest
    who did not answer must not score 150%.

    Deliberately two separate queries: putting the trip count and the message
    count in one annotate() would multiply rows across the join and silently
    inflate both (the trap documented at drivers/views.py:851-853).

    Query 1 buckets a chauffeur's trips by the stage they have reached, which
    is all three denominators at once. Query 2 fetches the taps carrying enough
    of the leg (its status, and who it belongs to NOW) to tell whether query 1
    already counted that leg for this chauffeur — so a tap made before the
    status control was touched, or one on a leg since reassigned, still lands
    in a denominator instead of pushing a rate past 100%.
    """
    from reservations.models import LegClientMessage

    ids = list(driver_ids or [])
    out = {did: _blank_row() for did in ids}
    if not ids:
        return out

    base = _base_legs(start, end, driver_ids=ids)

    # Query 1 — how far each chauffeur's trips have got.
    by_stage = {did: {} for did in ids}
    for row in base.values("driver_id", "status").annotate(n=Count("id", distinct=True)):
        by_stage.setdefault(row["driver_id"], {})[row["status"] or ""] = row["n"]

    for did in ids:
        stages = by_stage.get(did) or {}
        for kind in KINDS:
            open_stages = OPEN_STAGES[kind]
            out[did][kind]["eligible"] = sum(
                n for status, n in stages.items() if status in open_stages
            )

    # Query 2 — the taps. driver_id here is the message's OWN chauffeur, not
    # the leg's current one: LegClientMessage.driver is denormalised precisely
    # so a later reassignment cannot move the credit.
    tapped = (
        LegClientMessage.objects
        .filter(leg__in=base)
        .values("driver_id", "kind", "leg__status", "leg__driver_id")
        .annotate(n=Count("leg_id", distinct=True))
    )
    for row in tapped:
        did, kind = row["driver_id"], row["kind"]
        if did not in out or kind not in KINDS:
            continue
        cell = out[did][kind]
        cell["sent"] += row["n"]
        already_counted = (
            row["leg__driver_id"] == did
            and chance_is_open(kind, row["leg__status"])
        )
        if not already_counted:
            cell["eligible"] += row["n"]

    for did in ids:
        for kind in KINDS:
            cell = out[did][kind]
            cell["pct"] = _pct(cell["sent"], cell["eligible"])
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
        accent = accent_for(pct)
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
    sent, eligible = totals(stats)
    return _pct(sent, eligible)
