"""
One definition of "how far did this car go" for the whole fleet module.

Every mileage number on the Fleet pages, and every preventive-maintenance
interval that fires off odometer, resolves through this module. Same reasoning
as pickup_policy.py: when more than one place computes the number, they drift,
and a dispatcher who learns a figure can be wrong stops believing all of them.

Everything here is a pure function over already-read values: no queries, no
writes, no clock, no HTTP. That is deliberate — this file is the one that gets
over-tested BEFORE it is wired to anything, so the arithmetic is proven against
fixtures before it ever touches a real payload.

The source hierarchy
--------------------
1. ``obdOdometerMeters`` — the vehicle's real odometer off the OBD bus. Preferred
   whenever both ends of the day have it.
2. ``gpsDistanceMeters`` — a cumulative distance counter derived from GPS fixes.
   Fallback only. Under-reads slightly (it chords corners) but it is honest.
3. ``gpsOdometerMeters`` — deliberately absent from this module's inputs. It is a
   settable/calibrated value that drifts from the real clock, and accepting it as
   truth would silently poison maintenance intervals. Making it impossible to
   pass in beats a comment telling people not to.

What "unknown" means
--------------------
``meters=None`` means we do not know. It is NOT zero. Zero means the car
provably did not move — equal odometer readings at both ends of the day. Callers
must render None as an em-dash and never sum it as 0, because conflating the two
makes a dead gateway look like a parked car and quietly poisons every total
above it.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

# ════════════════════════════════════════════════════════════════════════════
# POLICY CONSTANTS — the numbers, in one place
# ════════════════════════════════════════════════════════════════════════════

METERS_PER_MILE = Decimal("1609.344")

# Hard ceiling on a single vehicle-day. Generous on purpose: a Sprinter running
# MCO -> Miami and back is ~470 mi, and a genuinely brutal double is under 700.
# Anything past this is a bad frame, an ECU reset, or a gateway that moved
# between cars — never a real day of chauffeur work.
MAX_PLAUSIBLE_DAY_MILES = Decimal("900")
MAX_PLAUSIBLE_DAY_METERS = MAX_PLAUSIBLE_DAY_MILES * METERS_PER_MILE

# An OBD odometer of exactly 0 is not a new car, it is a gateway reporting
# nothing. Real in-service units all have miles on them. GPS *distance*, by
# contrast, legitimately starts at 0 — it is a counter, not an odometer — so the
# two get different treatment.
_MIN_CREDIBLE_OBD_METERS = Decimal("1")

# --- mileage_source values (stored on the row, never inferred at render) ---
SOURCE_OBD = "obd"
SOURCE_GPS = "gps"
SOURCE_NONE = "none"


@dataclass(frozen=True)
class OdometerReading:
    """
    One vehicle's cumulative counters at a point in time, as Samsara reported
    them. Both counters are optional — a GPS-only asset gateway supplies neither
    OBD odometer nor, on some firmware, distance.

    `samsara_vehicle_id` is carried so the resolver can refuse to subtract a
    reading taken from one gateway from a reading taken from another. That is
    the single most destructive failure mode in this pipeline: one gateway moved
    between cars produces a fictional six-figure day that poisons every rollup
    above it.
    """

    samsara_vehicle_id: str
    obd_odometer_meters: Decimal | None = None
    gps_distance_meters: Decimal | None = None


@dataclass(frozen=True)
class MileageResult:
    """
    meters: distance travelled, or None for "unknown" (NEVER 0-for-missing).
    source: SOURCE_OBD | SOURCE_GPS | SOURCE_NONE — persisted alongside the value
            so the UI can mark provenance and so a later audit can tell an exact
            reading from an estimate.
    note:   short machine-ish reason, "" when clean. Surfaced in admin/logs, not
            to dispatchers.
    """

    meters: Decimal | None
    source: str
    note: str = ""

    @property
    def miles(self) -> Decimal | None:
        """Convenience for display. None stays None — never coerced to 0."""
        if self.meters is None:
            return None
        return meters_to_miles(self.meters)

    @property
    def is_known(self) -> bool:
        return self.meters is not None


def meters_to_miles(meters, places=1) -> Decimal | None:
    """Meters -> miles, rounded for display. None passes through untouched."""
    if meters is None:
        return None
    quantum = Decimal(1).scaleb(-places)  # 1 -> 0.1, 2 -> 0.01
    return (Decimal(meters) / METERS_PER_MILE).quantize(quantum)


def _clean(value, *, minimum=None):
    """
    Coerce a raw payload number to Decimal, or None if it is unusable.

    Samsara sends ints; Decimal is used end-to-end because float arithmetic on
    odometer values drifts and compounds across a year of daily deltas.
    """
    if value is None:
        return None
    try:
        dec = Decimal(str(value))
    except (ArithmeticError, ValueError, TypeError):
        return None
    if dec < 0:
        return None  # a negative cumulative counter is always a bad frame
    if minimum is not None and dec < minimum:
        return None
    return dec


def resolve_day_mileage(previous: OdometerReading | None,
                        current: OdometerReading | None) -> MileageResult:
    """
    Distance travelled between two readings of the SAME vehicle.

    Returns a MileageResult whose `meters` is None whenever we cannot say
    honestly. The rules, in order:

    1. Either reading missing            -> unknown (nothing to diff).
    2. Readings from different gateways   -> unknown, REFUSED. Never delta across
       differing samsara_vehicle_id; that is the fictional-six-figure-day bug.
    3. Both ends have a credible OBD odometer:
         - forward and plausible          -> that delta, source=obd
         - backwards (ECU reset/bad frame/swap) -> discard the OBD step and try
           GPS. Under-reporting by the discarded step is the right direction to
           be wrong in; fabricating a rollover is not.
         - implausibly large              -> discard, try GPS
    4. Otherwise fall back to the GPS distance counter, same forward/plausible
       rules. A backwards GPS counter means the counter reset (gateway swap), so
       the step is unknowable, not zero.
    5. Nothing usable                     -> unknown.
    """
    if previous is None or current is None:
        return MileageResult(None, SOURCE_NONE, "no prior reading")

    # Rule 2 — the guard that matters most. Enforced here, and by a test, rather
    # than by a comment somewhere else asking callers to be careful.
    if previous.samsara_vehicle_id != current.samsara_vehicle_id:
        return MileageResult(
            None, SOURCE_NONE,
            f"gateway changed ({previous.samsara_vehicle_id} -> "
            f"{current.samsara_vehicle_id}); refusing to diff",
        )

    prev_obd = _clean(previous.obd_odometer_meters, minimum=_MIN_CREDIBLE_OBD_METERS)
    cur_obd = _clean(current.obd_odometer_meters, minimum=_MIN_CREDIBLE_OBD_METERS)

    obd_note = ""
    if prev_obd is not None and cur_obd is not None:
        delta = cur_obd - prev_obd
        if delta < 0:
            obd_note = "obd odometer went backwards; fell back to gps"
        elif delta > MAX_PLAUSIBLE_DAY_METERS:
            obd_note = (
                f"obd delta {meters_to_miles(delta)} mi exceeds "
                f"{MAX_PLAUSIBLE_DAY_MILES} mi ceiling; fell back to gps"
            )
        else:
            return MileageResult(delta, SOURCE_OBD)

    # --- GPS fallback ---------------------------------------------------
    prev_gps = _clean(previous.gps_distance_meters)
    cur_gps = _clean(current.gps_distance_meters)

    if prev_gps is not None and cur_gps is not None:
        delta = cur_gps - prev_gps
        if delta < 0:
            return MileageResult(
                None, SOURCE_NONE,
                _join(obd_note, "gps distance counter reset; day unknowable"),
            )
        if delta > MAX_PLAUSIBLE_DAY_METERS:
            return MileageResult(
                None, SOURCE_NONE,
                _join(obd_note, f"gps delta {meters_to_miles(delta)} mi exceeds ceiling"),
            )
        return MileageResult(delta, SOURCE_GPS, obd_note)

    return MileageResult(None, SOURCE_NONE, _join(obd_note, "no usable counter on both ends"))


def _join(*notes):
    """Join the non-empty notes so a fallback keeps the reason it fell back."""
    return "; ".join(n for n in notes if n)


# ════════════════════════════════════════════════════════════════════════════
# USAGE RATE — "how hard does this car actually work?"
# ════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class UsageRate:
    """How much a vehicle is driven, averaged over the days we actually know.

    `per_day`/`per_week` are None when nothing is known — never 0. A car with a
    dead gateway has an UNKNOWN rate, and rendering that as "0 mi/day" would put
    a working car at the bottom of a utilisation comparison and push its next
    service projection out to never.
    """

    total_miles: Decimal | None
    known_days: int
    total_days: int
    per_day: Decimal | None
    per_week: Decimal | None

    @property
    def is_known(self) -> bool:
        return self.per_day is not None


def usage_rate(daily_miles, total_days=None) -> UsageRate:
    """Average miles per day and per week from a series of per-day figures.

    ``daily_miles`` is an iterable where each item is one day's mileage.

    The two kinds of blank day are treated differently, and the distinction is
    the whole point of the function:

      * ``None`` = UNKNOWN. Excluded from the sum AND from the denominator. A
        gateway that was offline for a week says nothing about how hard the car
        works, so averaging those days in as zero would understate a busy car.
      * ``0`` = the car provably did not move. Counted in the denominator. A car
        that sits every Sunday genuinely averages less over a week, and dropping
        those days would inflate the rate into a number no one can plan against.

    ``total_days`` is the size of the window asked about (default: the number of
    items given), carried through only so a caller can state coverage — it never
    changes the arithmetic.
    """
    values = list(daily_miles)
    known = [Decimal(v) for v in values if v is not None]
    span = len(values) if total_days is None else total_days

    if not known:
        return UsageRate(None, 0, span, None, None)

    total = sum(known)
    per_day = (total / len(known)).quantize(Decimal("0.1"))
    per_week = (per_day * 7).quantize(Decimal("0.1"))
    return UsageRate(total, len(known), span, per_day, per_week)


def days_to_cover(miles, per_day) -> int | None:
    """Days needed to drive ``miles`` at ``per_day``, or None when unanswerable.

    None — not a huge number — when the rate is unknown or zero. A parked car
    never reaches its next oil change, and "due in 41,000 days" is a worse
    answer than declining to give one: someone will plan a shop day around a
    projection, so it has to refuse when it cannot know.

    Negative or zero ``miles`` (already past due) returns 0 — the caller decides
    how to say "now".
    """
    if per_day is None or miles is None:
        return None
    rate = Decimal(per_day)
    if rate <= 0:
        return None
    remaining = Decimal(miles)
    if remaining <= 0:
        return 0
    return int((remaining / rate).to_integral_value(rounding="ROUND_CEILING"))
