"""The Publix stop, as a thing that actually happened rather than a flat 25 minutes.

WHY THIS EXISTS
---------------
`Reservation.store_stop` is a checkbox on the booking form. Until now it was also
the entire timing model: an arrival leg carrying it got `PUBLIX_STOP_MINUTES = 25`
bolted onto the DIRECT airport→destination drive, forever, no matter what the
trip did. Three separate things were wrong with that:

  1. The route is wrong. The van drives airport → Publix → resort, which is a
     different road than airport → resort. Depending on which side of town the
     destination is on, the flat constant can be badly short (see
     docs/store-stop-timing-refactor.md).
  2. The stop is a PREDICTION, and the founder's rule is that guests skip it and
     guests add it last-minute when we have the slack. A checkbox ticked at
     booking is not evidence about a trip already under way.
  3. Nothing was ever measured. The driver taps "Picked Up" and then vanishes
     until "Complete" — so between those two taps the board had no idea whether
     he was in Publix or already at the resort, and every clearing time in that
     window was a guess presented as a fact.

That last one is what produced fake driver-conflict tasks: the scanner priced a
driver as busy until 2:55 PM when he'd been rolling since 1:27 PM.

THE MODEL
---------
The booking flag is the prediction. The driver's taps are the truth, and each one
retires a guess:

    picked up            → drive-to-store, shopping and drive-from-store all estimated
    tapped At Publix     → drive-to-store is now a FACT; shopping + final drive estimated
    tapped Leaving       → only the final drive is still an estimate
    tapped No stop       → the store is gone from the math entirely
    never flagged, but
    driver taps At Publix→ an ad-hoc stop we never planned; timing picks it up anyway

So a leg's clearing time is not one number, it is a number that gets sharper three
times over the course of the trip. That is the point.

WHAT THIS MODULE WILL NOT DO
----------------------------
* It never writes. Callers resolve state and pass it down.
* It never touches money. A driver adding a stop at the guest's request does NOT
  set `Reservation.store_stop` and does NOT create a stop fee — billing stays a
  human decision, surfaced to dispatch (see `adhoc` in `StoreStopState`).
* It never hits the network. Every minute here comes from the static category
  table, the same source `chain_clear_dt` plans with. A GET that renders a board
  must not be able to spend money on the Distance Matrix API.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

# ── LegStatus values ────────────────────────────────────────────────────────
# These live in LegStatus history ONLY. `Leg.status` stays "picked-up" through
# the whole store stop: ~60 files filter, colour and count off that value, and a
# guest standing in a grocery aisle is still, in every sense the rest of the
# system cares about, picked up.
STATUS_ARRIVED = "store-arrived"
STATUS_DEPARTED = "store-departed"
STATUS_SKIPPED = "store-skipped"

STORE_STATUSES = (STATUS_ARRIVED, STATUS_DEPARTED, STATUS_SKIPPED)

STORE_STATUS_CHOICES = [
    (STATUS_ARRIVED, "At Store"),
    (STATUS_DEPARTED, "Left Store"),
    (STATUS_SKIPPED, "Store Stop Skipped"),
]

# ── Timing constants ────────────────────────────────────────────────────────
# The category the Publix waypoint routes as. Never a leg pickup or dropoff, so
# `categorize_location` is deliberately NOT taught about it — it exists only as
# an intermediate hop in this module's math.
PUBLIX_CATEGORY = "Publix Store"

# Time inside the store, doors to doors. Distinct from the old flat 25, which was
# shopping AND the detour rolled into one — those are now priced separately, so
# double-counting the detour here would re-introduce the bug from the other side.
PUBLIX_DWELL_MINUTES = 18

# Drive minutes to/from Publix at Lake Cay Commons (9930 Universal Blvd), the
# store these runs actually use. Same shape and spirit as
# scheduler.DRIVE_TIME_ESTIMATES; merged into it at import so every existing
# lookup path can price a Publix hop without knowing this module exists.
PUBLIX_DRIVE_ESTIMATES = {
    ("MCO Terminal", PUBLIX_CATEGORY): 20,
    (PUBLIX_CATEGORY, "MCO Terminal"): 20,
    ("SFB Terminal", PUBLIX_CATEGORY): 50,
    (PUBLIX_CATEGORY, "SFB Terminal"): 50,
    (PUBLIX_CATEGORY, "Disney Resort"): 25,
    ("Disney Resort", PUBLIX_CATEGORY): 25,
    (PUBLIX_CATEGORY, "Universal Resort"): 8,
    ("Universal Resort", PUBLIX_CATEGORY): 8,
    (PUBLIX_CATEGORY, "Port Canaveral Area"): 60,
    (PUBLIX_CATEGORY, "Other Hotel"): 15,
    (PUBLIX_CATEGORY, "Residential"): 25,
    (PUBLIX_CATEGORY, "Airport Hotel"): 18,
    (PUBLIX_CATEGORY, PUBLIX_CATEGORY): 0,
}

# Fallback when the segmented route can't be priced at all. This is the OLD flat
# constant, kept so a category pair nobody anticipated degrades to exactly the
# behaviour that shipped rather than to zero.
LEGACY_FLAT_MINUTES = 25


# ── State ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StoreStopState:
    """What we know, right now, about this leg's grocery stop.

    `expected` is the booking flag; `arrived_at`/`departed_at`/`skipped` are the
    driver's taps. Deliberately a plain value object with no DB handle: it gets
    resolved once per board and handed down into the scheduler primitives, which
    are also called with synthetic slot shims that have no status history.
    """

    expected: bool = False
    arrived_at: datetime = None      # naive local, first tap wins
    departed_at: datetime = None     # naive local, first tap wins
    skipped: bool = False

    @property
    def adhoc(self):
        """Driver stopped at a store the booking never asked for.

        Surfaced to dispatch because it may be billable, and because a stop
        nobody planned is exactly the kind of thing a dispatcher wants to see on
        the board rather than discover in a pay dispute.
        """
        return bool(self.arrived_at) and not self.expected

    @property
    def happening(self):
        """Is a store stop part of this trip's remaining time?"""
        if self.skipped:
            return False
        return bool(self.expected or self.arrived_at)

    @property
    def resolved(self):
        """Do we KNOW, rather than predict? A resolved state is one the driver
        settled by tapping something — either he's been to the store or he's told
        us there's no stop."""
        return bool(self.arrived_at or self.skipped)

    @property
    def phase(self):
        """none | expected | shopping | rolling | skipped — what to show a
        dispatcher, and which branch of the timing math applies."""
        if self.skipped:
            return "skipped"
        if self.departed_at:
            return "rolling"
        if self.arrived_at:
            return "shopping"
        if self.expected:
            return "expected"
        return "none"

    @property
    def shopped_minutes(self):
        """Measured door-to-door time in the store, or None while still inside."""
        if self.arrived_at and self.departed_at:
            return max(0, int((self.departed_at - self.arrived_at).total_seconds() // 60))
        return None


NO_STORE = StoreStopState()


# A store tap is only reachable once the guest is in the car, so a leg that
# hasn't got there yet cannot have one. Used to skip the history read entirely on
# planning paths, which walk every future leg on the board.
TAPPABLE_STATUSES = frozenset({"picked-up", "completed"})


def _prefetched_rows(leg):
    """Status rows already in memory, or None if reading them costs a query."""
    cache = getattr(leg, "_prefetched_objects_cache", None) or {}
    rows = cache.get("status_history")
    return list(rows) if rows is not None else None


def resolve_store_state(leg, status_rows=None):
    """Build a `StoreStopState` for one leg.

    Reads `leg.status_history` when `status_rows` isn't supplied. `leg.shows_store_stop`
    supplies `expected`, so the flag only counts on the leg the grocery run
    actually rides (never the guest's departure leg back to MCO).

    QUERY BUDGET: this is called from `chain_clear_dt`, which auto-assign runs
    over every leg on the board — so it must not become an N+1. Two escapes, in
    order: prefetched rows are used as-is, and a leg that has not reached
    "picked-up" is answered from the booking flag without touching the DB at all
    (it cannot have taps yet). What's left is the handful of trips actually under
    way, and the dispatcher paths prefetch those anyway.
    """
    from django.utils import timezone

    expected = False
    try:
        expected = bool(leg.shows_store_stop)
    except Exception:
        # Slot shims and bare synthetic legs: fall back to the raw flag.
        res = getattr(leg, "reservation", None)
        expected = bool(getattr(res, "store_stop", False)) if res is not None else False

    if status_rows is None:
        status_rows = _prefetched_rows(leg)
    if status_rows is None:
        if str(getattr(leg, "status", "") or "") not in TAPPABLE_STATUSES:
            return StoreStopState(expected=expected)
        history = getattr(leg, "status_history", None)
        if history is None:
            return StoreStopState(expected=expected)
        try:
            status_rows = list(history.all())
        except Exception:
            return StoreStopState(expected=expected)

    arrived = departed = None
    skipped = False
    for row in status_rows:
        status = getattr(row, "status", None)
        if status not in STORE_STATUSES:
            continue
        stamp = getattr(row, "timestamp", None)
        if stamp is None:
            continue
        local = timezone.localtime(stamp).replace(tzinfo=None) if timezone.is_aware(stamp) else stamp
        if status == STATUS_SKIPPED:
            skipped = True
        elif status == STATUS_ARRIVED:
            # Rows arrive newest-first on the prefetched paths and oldest-first
            # elsewhere; keeping the earliest either way makes the resolved state
            # independent of query ordering.
            arrived = local if arrived is None else min(arrived, local)
        elif status == STATUS_DEPARTED:
            departed = local if departed is None else min(departed, local)

    # A driver who reached the store settles the question, even if he tapped
    # "skip" earlier and then changed his mind at the guest's request.
    if arrived is not None:
        skipped = False

    return StoreStopState(
        expected=expected, arrived_at=arrived, departed_at=departed, skipped=skipped,
    )


def store_states_for_legs(legs):
    """{leg_id: StoreStopState} for a board's worth of legs.

    Query-free given the `status_history` prefetch the dispatcher paths already
    carry (conflict_advisor, capacity planner, legs list).
    """
    return {leg.id: resolve_store_state(leg) for leg in legs}


# ── Timing ──────────────────────────────────────────────────────────────────

def _drive(from_cat, to_cat):
    """Static category drive minutes, Publix pairs included. No network, ever."""
    from dispatching.scheduler import DRIVE_TIME_ESTIMATES, DEFAULT_DRIVE_TIME
    return DRIVE_TIME_ESTIMATES.get((from_cat, to_cat), DEFAULT_DRIVE_TIME)


def in_job_minutes(pickup_cat, dropoff_cat, state=None):
    """Minutes from "guest is in the car" to "driver is free", for planning.

    With no store stop this is just the drive. With one it is the real segmented
    route — pickup → Publix → destination — plus shopping, instead of the direct
    drive plus a flat 25 that assumed the store was free to reach.

    `state=None` means "no store stop". A caller that only holds the booking flag
    should pass `StoreStopState(expected=True)`.
    """
    direct = _drive(pickup_cat, dropoff_cat)
    state = state or NO_STORE
    if not state.happening:
        return direct

    to_store = _drive(pickup_cat, PUBLIX_CATEGORY)
    from_store = _drive(PUBLIX_CATEGORY, dropoff_cat)
    segmented = to_store + PUBLIX_DWELL_MINUTES + from_store

    # Guard against a category pair the Publix table doesn't cover: if both hops
    # fell through to the generic default the "segmented" number is fiction, so
    # use the shipped flat-25 behaviour rather than a confident wrong answer.
    from dispatching.scheduler import DRIVE_TIME_ESTIMATES
    priced = (
        (pickup_cat, PUBLIX_CATEGORY) in DRIVE_TIME_ESTIMATES
        and (PUBLIX_CATEGORY, dropoff_cat) in DRIVE_TIME_ESTIMATES
    )
    if not priced:
        return direct + LEGACY_FLAT_MINUTES

    # Floor: a stop can never cost less than the time spent inside the store. A
    # thin table entry that made the detour look free (or negative) would hand
    # the board slack it does not have — the failure mode this whole module is
    # here to remove, just from the optimistic side.
    return max(segmented, direct + PUBLIX_DWELL_MINUTES)


def detour_minutes(pickup_cat, dropoff_cat, state=None):
    """Minutes a store stop ADDS on top of driving straight to the destination.

    The existing clear-time formulas are all shaped `dwell + drive + X`, where
    `drive` may be a live RouteTimingMetric p75 rather than the table value. X is
    this — a pure static differential — so it stays dimensionally correct
    whichever direct-drive number the caller already committed to.
    """
    state = state or NO_STORE
    if not state.happening:
        return 0
    direct = _drive(pickup_cat, dropoff_cat)
    return max(0, in_job_minutes(pickup_cat, dropoff_cat, state) - direct)


def clear_dt_from_pickup(pickup_cat, dropoff_cat, picked_up_dt, state=None):
    """The live clearing time for a leg under way — the "second clearing time".

    Each driver tap moves the anchor forward onto something measured, so what
    remains to estimate shrinks:

        no taps yet   picked_up  + drive→store + shop + drive→dest   (or direct)
        At Publix     arrived_at + shop + drive→dest
        Leaving       departed_at + drive→dest
        No stop       picked_up  + direct drive

    Returns `(clear_dt, basis)` where basis is one of ``projected``,
    ``at-store``, ``left-store``, ``no-store`` — so a surface can say WHY it
    believes a time rather than just printing one.
    """
    state = state or NO_STORE

    if state.departed_at is not None:
        # Out of the store and rolling. Only the last drive is unknown.
        return (state.departed_at + timedelta(minutes=_drive(PUBLIX_CATEGORY, dropoff_cat)),
                "left-store")

    if state.arrived_at is not None:
        # Inside. The detour is spent; shopping + the final drive remain.
        remaining = PUBLIX_DWELL_MINUTES + _drive(PUBLIX_CATEGORY, dropoff_cat)
        return state.arrived_at + timedelta(minutes=remaining), "at-store"

    if state.skipped:
        return (picked_up_dt + timedelta(minutes=_drive(pickup_cat, dropoff_cat)),
                "no-store")

    basis = "projected" if state.happening else "no-store"
    return (picked_up_dt + timedelta(minutes=in_job_minutes(pickup_cat, dropoff_cat, state)),
            basis)


def _clock(dt):
    """"1:27 PM". `%-I` is a glibc extension that raises on Windows, and this
    module is read by tests and harnesses that run on the founder's laptop."""
    return dt.strftime("%I:%M %p").lstrip("0") if dt else ""


def describe(state):
    """One short dispatcher-facing phrase, or None when there's nothing to say."""
    if state is None:
        return None
    if state.phase == "skipped":
        return "Store stop skipped"
    if state.phase == "shopping":
        return f"At Publix since {_clock(state.arrived_at)}".rstrip()
    if state.phase == "rolling":
        shopped = state.shopped_minutes
        tail = f" ({shopped} min in store)" if shopped is not None else ""
        return f"Left Publix {_clock(state.departed_at)}{tail}".rstrip()
    if state.phase == "expected":
        return "Publix stop to come"
    return None
