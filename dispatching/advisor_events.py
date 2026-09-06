"""
The Recovery Advisor's ledger — write a card down when it is raised, grade it
when the day is over.

WHY (06_DAY_MANAGER §3.3, §3.4, Phase 1.2). The advisor's precision exists as a
single offline number: 28 days replayed through it, scored against what really
happened (``analysis/23_advisor_replay.py``). D5 will not show a warning class
below 70%, so that number has to keep being true after launch — and nothing in
production writes down what the rail said. This module is the writing-down half
and the grading half, and nothing else.

POSTURE. Strictly a ledger. Detection, generation, ranking and the apply path
never read it. Every writer here is wrapped so that a failure logs and returns
instead of breaking the poll it rode in on: a broken ledger must never break the
board. The recorders are also cheap by construction — one SELECT and at most two
writes per call, whatever the card count — because the busiest of them sits on a
60-second poll.

THE TWO HALVES

  RECORDING     ``record_cards`` upserts one row per card EPISODE (see
                ``AdvisorEvent``'s docstring for why an episode, not an id), plus
                four stamps for what a dispatcher then did: applied, rejected,
                snoozed, task filed.

  GRADING       ``fill_outcomes`` runs on the existing GHL loop (no new daemon —
                04 §6) once a service date has closed, and asks
                ``leg_lateness`` whether the card's impact leg actually ran late.

WHAT "SEEN" MEANS. A sighting is "the server sent this card somewhere", never
"a dispatcher read it". The rail polls while collapsed (`.ra-rail.is-collapsed`
hides the body, not the timer) and fires a catch-up poll the instant a hidden tab
is refocused. These counts are an upper bound on attention.

THE OUTCOME IS 23'S, LINE FOR LINE — this is the part that must not drift.
``leg_lateness`` is an ORM twin of ``23_advisor_replay.build_truth``:

  * the LAST on-location tap, ordered ascending. ``LegStatus.Meta.ordering`` is
    ``["-timestamp"]``, so the ordering here is explicit; production's own
    ``analytics.first_status_times`` returns the EARLIEST tap and is the wrong
    helper. (Measured: first-vs-last flips the >15 verdict on 10 of 3,108 legs.)
  * against ``pickup_policy.pickup_deadline(leg, aware=False)`` — gate arrival
    + 10 at an in-terminal meet, booked time everywhere else — in NAIVE local,
    both sides.
  * 19's batch-tap rule (a picked-up and a completed tap within 120 s of each
    other, and no on-location tap at all) discards the leg as ``batch``.
  * rounded to ONE DECIMAL and compared strictly: 15.0 is not late, 15.1 is.
  * NO status filter. build_truth scores cancelled legs too; excluding them here
    would make the live number quietly incomparable with the replay's.

``analysis/27_advisor_event_gate.py --verify-fill`` is the standing check on
that claim: it runs both implementations over the same legs and fails on any
disagreement.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# ── Config (module constants, house advisor style; 0/False disables) ────────

#: A sighting this long after the previous one starts a NEW episode rather than
#: extending the old one.
#:
#: Bounded from below by the observation cadence — the sweep's 180 s tick, and
#: the rail's 5-minute board-fingerprint clock bucket — and from ABOVE by how
#: far apart two genuine episodes of one card id actually sit, which was
#: measured rather than assumed: 98 boundaries over the 28 replayed days
#: (out/27_card_episodes.csv), min 6 min, P25 12, **P50 28.5**, P75 72, max 561.
#: A first draft of this file put it at 45 on the guess that real boundaries were
#: much wider than that. They are not — 45 would have merged 67% of them, and
#: merged them unevenly (60 of the 98 boundaries are `flight_change`), which is
#: precisely how a per-class denominator goes quietly wrong. At 10 minutes 23%
#: merge, against a floor of 17% at 6 — the price of tolerating a card that
#: flickers off for a tick or two, which the loop's drifting cadence guarantees.
#:
#: This is why ADVISOR_EVENT_SWEEP matters for more than coverage: with the
#: sweep off, the only feed is a rail nobody may be watching, gaps of hours are
#: routine, and every one of them would read as a new episode.
EPISODE_GAP_MIN = 10

#: Only a card whose impact leg had no usable tap yet is worth asking about
#: again — 'batch', 'no_deadline' and 'unknown' never become 'ok'. Taps do land
#: late (the snapshot holds on-location re-taps up to a month after the trip),
#: so an unresolved row is retried for this many days and then left alone.
OUTCOME_RETRY_DAYS = 7
OUTCOME_MAX_ATTEMPTS = 8
#: The attempt cap is a runaway guard, NOT the schedule. Without this spacing the
#: fill rides the GHL loop's 30-minute cadence, so all eight attempts are spent
#: four hours after the service date closes and OUTCOME_RETRY_DAYS never binds at
#: all — the row is frozen 'none' before the driver who forgot to tap has even
#: started the next day. At 20 hours, eight attempts span about a week, which is
#: what the constant above says.
OUTCOME_RETRY_EVERY_HOURS = 20
#: Rows graded per nightly pass. ~44 unique cards/day were measured on the
#: replay, so this is roughly a fortnight of backlog in one tick.
OUTCOME_FILL_LIMIT = 600

#: The unattended feed. False disables it and the log then holds only what a
#: superuser happened to have on screen (advisor_views.advisor_visible_to gates
#: the rail to superusers, and there are two active ones).
#:
#: MEASURED, before this existed — analysis/27_advisor_event_gate.py over the
#: same 28 days on a 3-minute grid, out/27_cadence_coverage.csv. Share of card
#: episodes a sampler sees AT ALL, averaged over every phase offset:
#:
#:     every  3 min (this loop)   100.0%   precision bias  +0.0 pts
#:     every  6 min                95.3%                   +0.1
#:     every 15 min                86.5%                   -0.1
#:     every 30 min (GHL loop)     76.1%                   -0.9
#:     every 60 min                60.8%                   -3.7
#:
#: That is why this rides the 180 s Samsara tick rather than the 30-minute GHL
#: loop the plan named for the nightly fill. It is not the precision bias — that
#: stays under a point until an hour. It is WHICH cards a coarse sampler loses,
#: and D5 is a per-class bar, so the pooled figure above is the wrong one to
#: judge it by (out/27_cadence_by_kind.csv):
#:
#:                        3 min    15 min   30 min   60 min
#:     overrun            100.0%    69.4%    47.1%    24.7%
#:     unassigned         100.0%    66.0%    50.4%    32.0%
#:     late_cascade       100.0%    80.1%    63.6%    41.4%
#:     flight_change      100.0%    92.7%    89.1%    83.5%
#:     overlap            100.0%    96.0%    89.9%    75.8%
#:
#: 44.7% of episodes live under 30 minutes and they are not spread evenly
#: (overrun 93.1% of episodes under 30 min, unassigned 75.7%, late_cascade
#: 68.5%, against overlap's 25.3%). On the GHL loop, `overrun` would be scored
#: on fewer than half its cards while `overlap` kept nine in ten — a 40-point
#: spread inside a bar applied class by class.
ADVISOR_EVENT_SWEEP = True
#: Local hours the sweep runs, matching 23's replay window so the live and
#: replayed populations are the same population. Outside it the board is empty
#: and the compute would be pure cost.
SWEEP_HOURS = (6, 23)

#: Statuses 19 reads to decide tap quality, and its batch threshold.
_TAP_STATUSES = ("on-location", "picked-up", "completed")
BATCH_TAP_MAX_SEC = 120


# ══════════════════════════════════════════════════════════════════════════
# GRADING — the ORM twin of 23's build_truth
# ══════════════════════════════════════════════════════════════════════════

class LegOutcome:
    """What actually happened at one pickup. ``late_min`` is signed minutes past
    the deadline to one decimal, or None whenever ``quality`` is not 'ok'."""

    __slots__ = ("late_min", "quality", "deadline", "basis", "tap_at",
                 "pickup_date")

    def __init__(self, late_min, quality, deadline=None, basis="", tap_at=None,
                 pickup_date=None):
        self.late_min = late_min
        self.quality = quality
        self.deadline = deadline
        self.basis = basis
        self.tap_at = tap_at
        #: The leg's CURRENT service date. The caller compares it with the
        #: ledger row's own — a leg that moved dates after the card was raised
        #: must not be graded under the date it left.
        self.pickup_date = pickup_date

    def __repr__(self):                                   # pragma: no cover
        return f"LegOutcome({self.late_min!r}, {self.quality!r})"


def _naive_local(dt):
    """Aware UTC row -> naive local, the one bridge 23 uses (``C.to_local``)."""
    if dt is None:
        return None
    if timezone.is_aware(dt):
        return timezone.localtime(dt).replace(tzinfo=None)
    return dt


def _aware(naive):
    """Naive local -> aware, for storage. Never raises: a DST-ambiguous minute
    costs the stored timestamp, never the verdict."""
    if naive is None:
        return None
    try:
        return timezone.make_aware(naive, timezone.get_current_timezone())
    except Exception:
        return None


def leg_lateness(leg_ids):
    """{leg_id: LegOutcome} for the given legs, by 23's definition exactly.

    Four queries whatever the leg count: the legs (with the prefetch
    ``pickup_policy.controlling_flight`` reads by name), and their taps. Legs
    that do not exist are simply absent from the result — the caller records
    that as 'unknown', which is what 23's scorer does for an impact leg that is
    not on the date."""
    from reservations.models import Leg, LegStatus
    from dispatching.pickup_policy import pickup_deadline

    ids = [i for i in dict.fromkeys(leg_ids) if i]
    if not ids:
        return {}

    legs = (Leg.objects.filter(id__in=ids)
            .select_related("reservation", "flight_information")
            .prefetch_related("legflight_set__flight"))

    taps = {}
    for leg_id, status, ts in (LegStatus.objects
                               .filter(leg_id__in=ids, status__in=_TAP_STATUSES)
                               # Meta.ordering is ["-timestamp"] — 23 reads
                               # rows[-1] off an ASCENDING scan, so say so.
                               .order_by("leg_id", "timestamp")
                               .values_list("leg_id", "status", "timestamp")):
        taps.setdefault(leg_id, []).append((status, ts))

    out = {}
    for leg in legs:
        rows = taps.get(leg.id, [])
        ol = [ts for s, ts in rows if s == "on-location"]
        booked = getattr(leg, "pickup_time", None)
        if not rows or booked is None:
            out[leg.id] = LegOutcome(None, "none", pickup_date=leg.pickup_date)
            continue
        if not ol:
            pu = [ts for s, ts in rows if s == "picked-up"]
            cm = [ts for s, ts in rows if s == "completed"]
            quality = "none"
            if pu and cm:
                gap = (cm[-1] - pu[-1]).total_seconds()
                if abs(gap) <= BATCH_TAP_MAX_SEC:
                    quality = "batch"
            out[leg.id] = LegOutcome(None, quality,
                                     pickup_date=leg.pickup_date)
            continue
        try:
            deadline, basis = pickup_deadline(leg, aware=False)
        except Exception:
            # pickup_deadline only guards is_flight_tracked_arrival(); a bad
            # flight row can still raise out of controlling_arrival_dt. 23
            # wraps the whole call and records no_deadline — so does this, or
            # one unpriceable leg would abort the whole nightly batch.
            deadline, basis = None, ""
        if deadline is None:
            out[leg.id] = LegOutcome(None, "no_deadline",
                                     pickup_date=leg.pickup_date)
            continue
        at = _naive_local(ol[-1])
        out[leg.id] = LegOutcome(
            round((at - deadline).total_seconds() / 60.0, 1), "ok",
            deadline=_aware(deadline), basis=basis or "", tap_at=ol[-1],
            pickup_date=leg.pickup_date)
    return out


# ══════════════════════════════════════════════════════════════════════════
# RECORDING
# ══════════════════════════════════════════════════════════════════════════

def _impact_at(card, day):
    """The card's ``impact_at`` (naive local ISO minutes) as an aware datetime."""
    raw = card.get("impact_at")
    if not raw:
        return None
    from datetime import datetime
    try:
        return _aware(datetime.fromisoformat(raw))
    except (ValueError, TypeError):
        return None


def record_cards(day, cards, *, source="rail", seen_at=None, whole_board=True):
    """Upsert one AdvisorEvent episode per card in ``cards``.

    ``cards`` is the serialized card list exactly as it leaves the state
    endpoint — post-snooze-filter, farm-pending reminders included — because
    that is what was actually sent to a screen.

    ``whole_board=False`` for a leg-filtered compute (the ops task-detail card,
    and the rail's ``?leg=`` requests). Those narrow the card set BEFORE
    ``ADVISOR_MAX_DISRUPTIONS`` and the 4 s budget are applied
    (``conflict_advisor._advisor_state``), so one surviving card always gets
    full plan generation — it would look like it carried a plan even on a board
    where the whole-board compute left it detected_only. The sighting is
    recorded either way; the two columns that would lie are not.

    Cost is two or three queries regardless of card count. Concurrency is
    handled by the unique constraint rather than a lock: two gunicorn workers
    computing the same board can race to open the same episode, and the loser's
    insert is dropped (``ignore_conflicts``) — the next sighting updates the row
    that won. Never raises."""
    from dispatching.models import AdvisorEvent

    if not cards:
        return 0
    now = seen_at or timezone.now()
    try:
        by_id = {}
        for c in cards:
            cid = str(c.get("id") or "")[:120]
            if cid:
                by_id[cid] = c          # last one wins; ids are unique per state
        if not by_id:
            return 0

        # One query for the newest episode of each card id on this date.
        latest = {}
        for row in (AdvisorEvent.objects
                    .filter(service_date=day, card_id__in=list(by_id))
                    .order_by("card_id", "-episode")):
            latest.setdefault(row.card_id, row)

        fresh, touched = [], []
        for cid, c in by_id.items():
            legs = [i for i in (c.get("leg_ids") or []) if i]
            impact = legs[-1] if legs else None
            row = latest.get(cid)
            if row is not None and (now - row.last_seen_at) <= timedelta(
                    minutes=EPISODE_GAP_MIN):
                row.last_seen_at = now
                row.sightings = (row.sightings or 0) + 1
                row.severity_last = str(c.get("severity") or "")[:10]
                row.basis_last = str(c.get("basis") or "")[:24]
                row.impact_leg_id = impact
                row.leg_count = len(legs)
                row.impact_at = _impact_at(c, day) or row.impact_at
                if whole_board:
                    row.had_plans = row.had_plans or bool(c.get("plans"))
                    row.detected_only = (row.detected_only
                                         or bool(c.get("detected_only")))
                touched.append(row)
                continue
            fresh.append(AdvisorEvent(
                service_date=day, card_id=cid,
                episode=(row.episode + 1) if row is not None else 1,
                kind=str(c.get("kind") or "")[:24],
                severity=str(c.get("severity") or "")[:10],
                basis=str(c.get("basis") or "")[:24],
                severity_last=str(c.get("severity") or "")[:10],
                basis_last=str(c.get("basis") or "")[:24],
                headline=str(c.get("headline") or "")[:200],
                impact_leg_id=impact, impact_leg_first_id=impact,
                leg_count=len(legs), impact_at=_impact_at(c, day),
                first_seen_at=now, last_seen_at=now, sightings=1,
                source=source,
                had_plans=bool(c.get("plans")) and whole_board,
                detected_only=bool(c.get("detected_only")) and whole_board,
            ))

        if touched:
            AdvisorEvent.objects.bulk_update(
                touched, ["last_seen_at", "sightings", "severity_last",
                          "basis_last", "impact_leg_id", "leg_count",
                          "impact_at", "had_plans", "detected_only"])
        if fresh:
            AdvisorEvent.objects.bulk_create(fresh, ignore_conflicts=True)
        return len(fresh) + len(touched)
    except Exception:
        logger.exception("advisor event recording failed (day=%s, source=%s)",
                         day, source)
        return 0


def _open_episode(day, card_id, *, create_at=None, defaults=None):
    """The newest episode of one card id on one date, opening a bare row when
    the sighting was never recorded (the ops task-detail surface does not log,
    and a card can be applied from there). Returns None if it cannot."""
    from dispatching.models import AdvisorEvent

    card_id = str(card_id or "")[:120]
    if not card_id:
        return None
    row = (AdvisorEvent.objects.filter(service_date=day, card_id=card_id)
           .order_by("-episode").first())
    if row is not None:
        return row
    if create_at is None:
        return None
    kwargs = {"service_date": day, "card_id": card_id,
              # The kind IS recoverable from the id prefix; severity is not
              # available on any write path, and is left blank rather than
              # guessed.
              "kind": card_id.split(":", 1)[0][:24],
              "first_seen_at": create_at, "last_seen_at": create_at,
              "sightings": 0, "source": "task"}
    kwargs.update(defaults or {})
    try:
        # Its own savepoint: a failed INSERT marks the surrounding atomic block
        # for rollback, and Django then refuses every later query on it — so
        # without this the recovery read below raises TransactionManagementError
        # and the stamp is lost in exactly the race it exists to survive.
        with transaction.atomic():
            return AdvisorEvent.objects.create(**kwargs)
    except IntegrityError:
        return (AdvisorEvent.objects.filter(service_date=day, card_id=card_id)
                .order_by("-episode").first())


def _stamp(day, card_id, fields, *, create=True):
    """Write ``fields`` onto the card's open episode inside its own savepoint.
    ``fields`` may be a dict, or a callable taking the row (when a value depends
    on what the row already holds).

    The savepoint matters: on the apply path this runs inside the caller's
    transaction, and a swallowed database error would otherwise poison it —
    Django refuses every later query on a broken atomic block. Never raises."""
    try:
        with transaction.atomic():
            row = _open_episode(day, card_id,
                                create_at=timezone.now() if create else None)
            if row is None:
                return None
            values = fields(row) if callable(fields) else fields
            for k, v in values.items():
                setattr(row, k, v)
            row.save(update_fields=list(values))
            return row
    except Exception:
        logger.exception("advisor event stamp failed (day=%s, card=%s)",
                         day, card_id)
        return None


def record_applied(day, card_id, *, plan_id="", user=None, mode="",
                   snapshot_id=None, at=None):
    """A dispatcher applied one of this card's plans and the write landed."""
    return _stamp(day, card_id, {
        "applied_at": at or timezone.now(),
        "applied_by": user if getattr(user, "pk", None) else None,
        "applied_plan_id": str(plan_id or "")[:140],
        "applied_mode": str(mode or "")[:8],
        "applied_snapshot_id": snapshot_id,
    })


def record_rejected(day, card_id, *, status=None, error="", at=None):
    """A dispatcher tried to apply and the engine refused — a 409 because the
    board moved under them, or a hard rule. Not the same as a plan nobody
    wanted, which is why it is recorded separately.

    ``create=False``: a refusal is only meaningful against a card that was
    logged. Minting a row here would add an event with no card behind it — no
    class, no severity, nothing the precision readout could group by."""
    return _stamp(day, card_id, {
        "rejected_at": at or timezone.now(),
        "rejected_status": status,
        "rejected_error": str(error or "")[:200],
    }, create=False)


def record_snoozed(day, card_id, *, minutes=None, user=None, at=None):
    """A card was dismissed for a while. The snooze itself lives only in the
    cache and expires within four hours, so this row is the only durable record
    that it ever happened — and the only record of who did it.

    Re-snoozing is legal (the cache entry is overwritten in place), so the
    counter is bumped from whatever the row already holds."""
    def _fields(row):
        return {
            "snoozed_at": at or timezone.now(),
            "snoozed_by": user if getattr(user, "pk", None) else None,
            "snoozed_minutes": minutes,
            "snooze_count": (row.snooze_count or 0) + 1,
        }
    return _stamp(day, card_id, _fields)


def record_task_filed(day, card_id, *, task_id=None, created=False, user=None,
                      at=None):
    """A card's offer became an ops task. ``created`` False means dedup or the
    two-hour cooldown swallowed it because the 30-minute scanner had already
    filed the same task — the honest 'superseded' signal."""
    return _stamp(day, card_id, {
        "task_filed_at": at or timezone.now(),
        "task_filed_by": user if getattr(user, "pk", None) else None,
        "task_id": task_id,
        "task_created": bool(created),
    })


# ══════════════════════════════════════════════════════════════════════════
# THE NIGHTLY FILL
# ══════════════════════════════════════════════════════════════════════════

def fill_outcomes(now=None, limit=OUTCOME_FILL_LIMIT):
    """Grade every card whose service date has closed. Idempotent by data, not
    by a clock: a row is picked up because its outcome is missing, so a missed
    tick costs nothing and a double tick is a no-op. Rides the existing GHL loop
    (04 §6 bans a new daemon) and must never raise into it."""
    from django.db.models import Q
    from dispatching.models import AdvisorEvent

    now = now or timezone.now()
    today = timezone.localdate(now)
    try:
        qs = (AdvisorEvent.objects
              .filter(service_date__lt=today)
              .filter(Q(outcome_filled_at__isnull=True)
                      | Q(outcome_quality="none",
                          outcome_attempts__lt=OUTCOME_MAX_ATTEMPTS,
                          outcome_filled_at__lt=now - timedelta(
                              hours=OUTCOME_RETRY_EVERY_HOURS),
                          service_date__gte=today - timedelta(
                              days=OUTCOME_RETRY_DAYS)))
              .order_by("service_date", "id")[:limit])
        rows = list(qs)
        if not rows:
            return {"graded": 0, "scored": 0}

        outcomes = leg_lateness([r.impact_leg_id for r in rows])
        scored = 0
        for r in rows:
            o = outcomes.get(r.impact_leg_id)
            if o is None or o.pickup_date != r.service_date:
                # The leg is gone, or it is no longer on the date this card was
                # raised about — a guest confirming which night an overnight
                # arrival takes off moves the leg a day (overnight_arrival), and
                # the advisor raises cards on exactly that population. 23 builds
                # truth only over the date's own legs and returns 'unknown' for
                # both cases; grading the leg's NEW date under the OLD service
                # date would file a real lateness for a trip that never ran then.
                r.outcome_quality = "unknown"
                r.outcome_late_min = None
            else:
                r.outcome_quality = o.quality
                r.outcome_late_min = o.late_min
                r.outcome_deadline = o.deadline
                r.outcome_deadline_basis = (o.basis or "")[:60]
                r.outcome_tap_at = o.tap_at
                if o.quality == "ok":
                    scored += 1
            r.outcome_filled_at = now
            r.outcome_attempts = (r.outcome_attempts or 0) + 1
        AdvisorEvent.objects.bulk_update(
            rows, ["outcome_quality", "outcome_late_min", "outcome_deadline",
                   "outcome_deadline_basis", "outcome_tap_at",
                   "outcome_filled_at", "outcome_attempts"])
        return {"graded": len(rows), "scored": scored}
    except Exception:
        logger.exception("advisor outcome fill failed")
        return {"graded": 0, "scored": 0}


# ══════════════════════════════════════════════════════════════════════════
# THE UNATTENDED SWEEP
# ══════════════════════════════════════════════════════════════════════════

def sweep_today(now=None):
    """Compute today's advisor state and write down what it says — whether or
    not anybody is looking.

    WHY THIS EXISTS, since the plan did not ask for it. Phase 1.2 as written
    feeds the ledger from the rail, and the rail is gated to superusers
    (``advisor_views.advisor_visible_to``) — two active accounts, on a panel
    that can be collapsed and a tab that stops polling when hidden. A log fed
    only from there records what one person happened to have open, which cannot
    support either thing the log is FOR: 06 §3.3(b) is explicitly "ship nothing
    visible, build AdvisorEvent, log for a month", and Phase 2's gate is two
    live weeks of per-class precision BEFORE the rail opens. Both need the
    engine's verdict, not a browser's attendance record.

    Cost, measured: 76 ms P50 and 925 ms max per compute over 9,548 replayed
    ticks, zero over the 4 s budget — about 26 seconds of CPU across a whole
    day at this cadence. It rides an existing loop, so 04 §6 ("no new periodic
    daemon — reuse an existing loop") holds; ``fleet_sync`` is the precedent for
    riding this one.

    Two things it sees that the replay never can, and this is the other half of
    why the cadence matters: GPS (``dispatch_*``) is written by ``bulk_update``
    with no history, so ``gps_fresh`` classes are absent from every replayed
    number in §3.3 and can ONLY be scored from a live log — and a card whose GPS
    basis flips between sweeps is a card the replay cannot even see flip.

    Read-only apart from the ledger, and deliberately does NOT warm the rail's
    card cache: the sweep must stay something that can be switched off without
    changing a single thing a dispatcher sees."""
    from django.utils import timezone as djtz

    if not ADVISOR_EVENT_SWEEP:
        return {"cards": 0}
    now = now or djtz.now()
    local = djtz.localtime(now)
    if not (SWEEP_HOURS[0] <= local.hour < SWEEP_HOURS[1]):
        return {"cards": 0}
    try:
        from dispatching.conflict_advisor import compute_advisor_state
        state = compute_advisor_state(local.date(), now=now)
        cards = state.get("disruptions") or []
        record_cards(local.date(), cards, source="sweep", seen_at=now)
        return {"cards": len(cards)}
    except Exception:
        logger.exception("advisor sweep failed")
        return {"cards": 0}
