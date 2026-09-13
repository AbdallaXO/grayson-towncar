"""What the flight moves just broke.

The gap this closes, in the opener's own words: *"I refresh all flights, I click
match flights, then I open the board and scan line by line with my eyes to see
which two jobs are now clashing."*

Nothing recomputes turnarounds when a match is applied.
``dispatching.pickup_moves.apply_pickup_time_move`` writes the new time, stamps
the board's purple badge and files the audit rows — and stops. The conflict
detector only runs on the 30-minute scanner cycle
(``ops.tasks.generate_ops_tasks``), so between clicking Match and the next
sweep, the only thing that knows a 20-minute move broke a turn is the
dispatcher's eyes. That is the scan, and it is avoidable: every fact it needs is
already stored.

  * ``Leg.pickup_time_was`` / ``pickup_date_was`` — the pickup before the
    earliest still-unacknowledged change, preserved across successive moves and
    cleared on a net-zero revert (reservations/models.py:1330).
  * ``ops.tasks.classify_turn`` — the SAME detector that files DRIVER_CONFLICT
    and TIGHT_TURN tasks, so this can never disagree with the board's red badge.

So we ask the question nobody has asked: run the driver's chain as it stands,
run it again as it stood before the move, and report the turns that got worse.

STRICTLY READ-ONLY, like ``ops.shift_checks``. It files no tasks, writes no
flags and never saves a Leg. The "before" chain is built from shallow copies
whose ``pickup_time`` is rewound in memory only — see ``_rewound`` for why that
is safe and what would make it unsafe.

One honest limit, stated rather than hidden: for an airport ARRIVAL the driver's
deadline follows the flight, not the booked pickup, and we store the old pickup
but not the old flight time. Where the rewind therefore cannot show a real
"before", ``late_before`` comes back None and the caller says "now clashing"
instead of claiming the move caused it.
"""

import copy
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import groupby

from django.utils import timezone

logger = logging.getLogger(__name__)

#: Never report more than this many clashes on one date. A board that produces
#: more than this has a bigger problem than one opener can read in a list.
CLASH_LIMIT = 12

#: A pickup has to move by at least this much to be worth pointing at.
#:
#: AeroAPI re-times a flight on almost every refresh, so a board can carry
#: dozens of two- and four-minute shuffles that change nothing anybody would
#: act on — 46 "moved" pickups on one day, which is not a signal, it is a
#: weather report. Ten minutes is the founder's rule of thumb and it lines up
#: with the meet grace already in pickup_policy.ARRIVAL_MEET_GRACE_MIN.
#:
#: A move UNDER the threshold that actually breaks a turn is still drawn — see
#: _build_change_set. Size is a filter on noise, never on consequence.
MIN_MOVE_MINUTES = 10

#: How far back a pickup move still counts as "just moved".
#:
#: The acknowledgement flag alone is not enough to bound this. Acknowledging a
#: pickup change is something you do on the legs dashboard, and the people who
#: run the schedule live on the drag-and-drop board instead — so an unacked
#: change can sit there for days, and a list gated only on the flag would still
#: be reporting Tuesday's moves on Friday. A window makes it self-clearing
#: whether or not anyone ever clicks the badge. Twelve hours covers an overnight
#: close into the next morning's open, which is the longest real gap.
MOVE_WINDOW_HOURS = 12


def _clock(t):
    """12-hour time the way the board writes it."""
    if t is None:
        return ""
    return t.strftime("%-I:%M %p")


@dataclass
class Move:
    """One pickup that shifted, and by how much."""

    leg_id: int
    was_time: object
    now_time: object
    was_date: object = None
    now_date: object = None

    @property
    def minutes(self):
        """Signed minutes: negative = moved EARLIER (the dangerous direction)."""
        if self.was_time is None or self.now_time is None:
            return 0
        anchor = self.now_date or timezone.localdate()
        before = datetime.combine(self.was_date or anchor, self.was_time)
        after = datetime.combine(anchor, self.now_time)
        return int((after - before).total_seconds() // 60)

    @property
    def was_label(self):
        return _clock(self.was_time)

    @property
    def now_label(self):
        return _clock(self.now_time)

    @property
    def phrase(self):
        delta = self.minutes
        if delta == 0:
            return "moved"
        size = abs(delta)
        if size >= 60:
            hrs, mins = divmod(size, 60)
            amount = f"{hrs}h {mins}m" if mins else f"{hrs}h"
        else:
            amount = f"{size} min"
        return f"{amount} {'later' if delta > 0 else 'earlier'}"


@dataclass
class Clash:
    """Two consecutive jobs on one chauffeur that no longer work."""

    driver_id: int
    driver_name: str
    prev_leg: object
    curr_leg: object
    tier: str                 # 'red' (won't make it) | 'amber' (no margin)
    late_now: int             # minutes late into the second job
    late_before: object = None  # int, or None when it cannot be reconstructed
    moves: list = field(default_factory=list)

    @property
    def is_new(self):
        """The turn worked before the move and does not now."""
        return self.late_before is not None and self.late_before <= 0

    @property
    def got_worse_by(self):
        if self.late_before is None:
            return None
        return self.late_now - self.late_before

    @property
    def pair_label(self):
        """The two jobs, said as jobs.

        NOT "10:56 AM -> 12:00 PM": an arrow between two clock times reads as a
        time change, and then flatly contradicts the "moved 1h 44m earlier"
        underneath it. These are two different trips, not one trip's before and
        after.
        """
        first = _clock(self.prev_leg.pickup_time)
        second = _clock(self.curr_leg.pickup_time)
        return f"{first} job into the {second}"

    @property
    def move_notes(self):
        """One plain line per pickup that moved, each with its own before."""
        notes = []
        for move in self.moves:
            notes.append(
                f"the {move.now_label} moved {move.phrase} "
                f"— originally {move.was_label}"
            )
        return notes

    @property
    def verdict(self):
        """What the move cost, in one short clause."""
        if self.is_new:
            return "This turn worked before that."
        if self.got_worse_by:
            return f"That took {self.got_worse_by} more minutes off it."
        return f"{self.driver_name} is {self.late_now} min short."

    @property
    def why(self):
        """The whole story on one line, for anywhere a list will not fit."""
        moved = "; ".join(self.move_notes) or "the new times moved"
        # Not .capitalize(): it lowercases the rest of the string, which turns
        # "8:20 AM" into "8:20 am".
        moved = moved[:1].upper() + moved[1:]
        return f"{moved}. {self.verdict}"


def _chain_legs(target_date):
    """Active in-house legs on the date, driver-ordered — the scanner's own set.

    Mirrors ``ops.tasks._scan_driver_overlaps`` so a clash reported here is one
    the 30-minute sweep would file a task for, minus the past-pickup cutoff,
    which is applied by the caller against the checklist's own rule.
    """
    from reservations.models import Leg

    return list(
        Leg.objects.filter(
            pickup_date=target_date,
            driver__isnull=False,
            driver__driver_type="inhouse",
        )
        .exclude(status__in=["completed", "cancelled"])
        .exclude(reservation__status="cancelled")
        .select_related(
            "driver", "driver__profile",
            "flight_information",
            "reservation", "reservation__customer",
        )
        .order_by("driver_id", "pickup_time")
    )


def _rewound(leg, move):
    """A shallow copy of the leg with its pickup put back where it was.

    Shallow on purpose: ``classify_turn`` reads locations, flight rows and the
    reservation off the leg, and those are unchanged by a pickup move, so
    sharing them is both correct and cheap. The copy exists so the rewind cannot
    touch the real instance — which matters because ``Leg.save()`` re-stamps the
    pickup-change badge from ``_original_pickup_time``, so a rewound instance
    that ever reached a save would wipe the very badge that told us it moved.
    Nothing here saves. If that ever changes, this must become a detached
    reconstruction rather than a copy.
    """
    ghost = copy.copy(leg)
    if move.was_time is not None:
        ghost.pickup_time = move.was_time
    # Re-anchor the model's own change detection on the copy, so nothing
    # downstream reads the rewind as a fresh edit.
    ghost._original_pickup_time = move.was_time
    ghost._original_pickup_date = move.was_date or leg.pickup_date
    return ghost


def _turns(legs, target_date):
    """{(prev_id, curr_id): risk} for every consecutive pair in one chain."""
    from ops.tasks import classify_turn

    out = {}
    ordered = sorted(legs, key=lambda l: (l.pickup_time or timezone.localtime().time()))
    for i in range(len(ordered) - 1):
        prev_leg, curr_leg = ordered[i], ordered[i + 1]
        try:
            risk = classify_turn(prev_leg, curr_leg, target_date)
        except Exception:
            # One unclassifiable pair must never cost the opener the whole list.
            logger.warning(
                "classify_turn failed for legs %s -> %s", prev_leg.pk, curr_leg.pk,
            )
            continue
        if risk:
            out[(prev_leg.pk, curr_leg.pk)] = risk
    return out


def recent_moves(target_date, now=None, window_hours=MOVE_WINDOW_HOURS, legs=None,
                 include_acknowledged=False, min_minutes=0):
    """{leg_id: Move} for pickups on the date that moved recently.

    Two ways out of this list, and a move only needs one. Acknowledging it on
    the board clears it, which is the tidy path. Otherwise it simply ages out of
    ``MOVE_WINDOW_HOURS`` — because acknowledging happens on a page the schedule
    people do not use, and a row that can only be cleared by a click nobody
    makes is a row that is permanently red.

    ``include_acknowledged`` keeps a move that somebody has already ticked.
    That is the right window for a BROKEN TURN, which stays broken until it is
    actually fixed: acknowledging a time change says "I have seen this moved",
    never "I have re-seated the chauffeur". Ghosts follow the tick; brackets
    follow the turn.
    """
    now = now or timezone.now()
    cutoff = now - timedelta(hours=window_hours)

    moves = {}
    for leg in (_chain_legs(target_date) if legs is None else legs):
        if leg.pickup_time_was is None or leg.pickup_time_changed_at is None:
            continue
        if not include_acknowledged and not leg.has_unacked_time_change:
            continue
        if leg.pickup_time_changed_at < cutoff:
            continue
        move = Move(
            leg_id=leg.pk,
            was_time=leg.pickup_time_was,
            now_time=leg.pickup_time,
            was_date=leg.pickup_date_was,
            now_date=leg.pickup_date,
        )
        if min_minutes and abs(move.minutes) < min_minutes:
            continue
        moves[leg.pk] = move
    return moves


def clashes_from_moves(target_date, limit=CLASH_LIMIT, now=None):
    """Turns that a just-moved pickup broke or tightened, worst first.

    This is the eye-scan, done by the machine: for every chauffeur holding a leg
    whose pickup just moved, classify the chain as it stands and as it stood,
    and keep only the turns that got worse.
    """
    from .shift_checks import driver_name

    # One pass over the board, not two. This runs inside the schedule board's
    # own render, which is already a ~2s page on a 240-leg day.
    legs = _chain_legs(target_date)
    # A broken turn outlives the acknowledgement — see recent_moves.
    moves = recent_moves(target_date, now=now, legs=legs, include_acknowledged=True,
                         min_minutes=0)
    if not moves:
        return []

    found = []

    for driver_id, group in groupby(legs, key=lambda l: l.driver_id):
        chain = list(group)
        if len(chain) < 2:
            continue
        touched = [leg for leg in chain if leg.pk in moves]
        if not touched:
            continue

        after = _turns(chain, target_date)
        if not after:
            continue

        # The same chain as it stood before the moves. A leg that moved ONTO
        # this date was not on this board at all, so it leaves the before-chain
        # rather than sitting in it at a time it never had.
        before_chain = []
        for leg in chain:
            move = moves.get(leg.pk)
            if move is None:
                before_chain.append(leg)
            elif move.was_date and move.was_date != target_date:
                continue
            else:
                before_chain.append(_rewound(leg, move))
        before = _turns(before_chain, target_date)

        for (prev_id, curr_id), risk in after.items():
            if prev_id not in moves and curr_id not in moves:
                continue  # a standing conflict, not one these moves touched
            was = before.get((prev_id, curr_id))
            late_before = was["late"] if was else (0 if before_chain else None)
            if was is None and not any(
                leg.pk in (prev_id, curr_id) for leg in before_chain
            ):
                late_before = None  # the pair did not exist before — cannot compare
            if late_before is not None and late_before >= risk["late"]:
                continue  # no worse than it already was

            by_id = {leg.pk: leg for leg in chain}
            found.append(Clash(
                driver_id=driver_id,
                driver_name=driver_name(by_id[curr_id].driver),
                prev_leg=by_id[prev_id],
                curr_leg=by_id[curr_id],
                tier=risk["tier"],
                late_now=risk["late"],
                late_before=late_before,
                moves=[moves[i] for i in (prev_id, curr_id) if i in moves],
            ))

    found.sort(key=lambda c: (c.tier != "red", -c.late_now))
    return found[:limit]


# ── The change set ────────────────────────────────────────────────────
#
# Computed ONCE per refresh, not once per render. The board is a ~2s page on a
# 240-leg day and this has to be invisible inside it.
#
# The cache key carries a fingerprint of the moves themselves — the newest
# ``pickup_time_changed_at`` on the date plus how many legs carry one — so a
# refresh that moves anything invalidates it by definition and one that moves
# nothing reuses it. That beats a timed cache in both directions: no stale
# board after a match, no recompute on a quiet board. The TTL underneath is
# only a backstop for the window ageing out.

CHANGE_SET_TTL = 15 * 60


def _fingerprint(target_date):
    """A cheap stamp that changes exactly when the moves do."""
    from django.db.models import Count, Max
    from reservations.models import Leg

    row = (
        Leg.objects.filter(
            pickup_date=target_date, pickup_time_changed_at__isnull=False,
        )
        .aggregate(last=Max("pickup_time_changed_at"),
                   n=Count("id"),
                   acked=Count("pickup_change_ack_at"))
    )
    last = row["last"].isoformat() if row["last"] else "-"
    return f"{last}|{row['n']}|{row['acked']}"


@dataclass
class ChangeSet:
    """Everything the board needs to draw what changed, in one object."""

    target_date: object
    #: leg_id -> Move, UNACKNOWLEDGED only. Drives the ghost and the delta chip.
    moves: dict = field(default_factory=dict)
    #: Broken turns, acknowledged or not. Drives the brackets.
    breaks: list = field(default_factory=list)
    #: driver_id -> {"breaks": int, "moved": bool}
    rows: dict = field(default_factory=dict)
    #: leg_id -> {"move": Move|None, "in_break": bool}
    legs: dict = field(default_factory=dict)
    refreshed_at: object = None
    #: Stamp of the moves behind this set. The board uses it to remember that a
    #: dispatcher dismissed THIS refresh — a later one brings the strip back.
    fingerprint: str = ""

    @property
    def is_empty(self):
        return not self.moves and not self.breaks

    @property
    def break_count(self):
        return len(self.breaks)

    @property
    def moved_count(self):
        return len(self.moves)

    def row(self, driver_id):
        return self.rows.get(driver_id)

    def leg(self, leg_id):
        return self.legs.get(leg_id)


def _build_change_set(target_date, now=None):
    legs = _chain_legs(target_date)
    breaks = clashes_from_moves(target_date, now=now)

    # Worth pointing at = big enough to matter, OR small but it broke something.
    ghosts = recent_moves(target_date, now=now, legs=legs,
                          min_minutes=MIN_MOVE_MINUTES)
    broke = {leg.pk for clash in breaks for leg in (clash.prev_leg, clash.curr_leg)}
    if broke:
        for leg_id, move in recent_moves(target_date, now=now, legs=legs).items():
            if leg_id in broke:
                ghosts.setdefault(leg_id, move)

    rows, per_leg = {}, {}
    for leg_id, move in ghosts.items():
        per_leg.setdefault(leg_id, {"move": None, "in_break": False})["move"] = move

    driver_of = {leg.pk: leg.driver_id for leg in legs}
    for leg_id in ghosts:
        driver_id = driver_of.get(leg_id)
        if driver_id is not None:
            rows.setdefault(driver_id, {"breaks": 0, "moved": False})["moved"] = True

    for clash in breaks:
        mark = rows.setdefault(clash.driver_id, {"breaks": 0, "moved": False})
        mark["breaks"] += 1
        for leg in (clash.prev_leg, clash.curr_leg):
            # Both legs in the overlap render at full strength — neither half of
            # a broken turn is context for the other.
            per_leg.setdefault(leg.pk, {"move": None, "in_break": False})["in_break"] = True

    refreshed_at = None
    if ghosts or breaks:
        stamps = [
            leg.pickup_time_changed_at for leg in legs
            if leg.pickup_time_changed_at is not None
        ]
        refreshed_at = max(stamps) if stamps else None

    return ChangeSet(
        target_date=target_date, moves=ghosts, breaks=breaks,
        rows=rows, legs=per_leg, refreshed_at=refreshed_at,
        fingerprint=_fingerprint(target_date),
    )


def change_set(target_date, now=None, use_cache=True):
    """What changed on ``target_date``, ready for the board to draw."""
    from django.core.cache import cache

    if not use_cache:
        return _build_change_set(target_date, now=now)

    try:
        key = f"moveimpact:{target_date:%Y-%m-%d}:{_fingerprint(target_date)}"
        hit = cache.get(key)
        if hit is not None:
            return hit
    except Exception:
        # A cache that misbehaves must not cost the board its change set.
        logger.warning("Move-impact cache unavailable for %s", target_date)
        return _build_change_set(target_date, now=now)

    built = _build_change_set(target_date, now=now)
    try:
        cache.set(key, built, CHANGE_SET_TTL)
    except Exception:
        logger.warning("Could not cache the move impact for %s", target_date)
    return built
