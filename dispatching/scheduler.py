"""
Daily Capacity Planner - Core Scheduling Logic

Provides feasibility checking, assignment suggestions, and batching detection
for optimizing in-house driver coverage.
"""

import logging
import os
from datetime import datetime, timedelta, time, date
from decimal import Decimal
from typing import List, Dict, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ============================================================================
# HARDCODED ROUTE TIMING ESTIMATES (minutes)
# Based on Orlando-area geography. These serve as fallbacks when
# RouteTimingMetric has insufficient data.
# ============================================================================

DRIVE_TIME_ESTIMATES = {
    ('MCO Terminal', 'Disney Resort'): 30,
    ('Disney Resort', 'MCO Terminal'): 30,
    ('MCO Terminal', 'Universal Resort'): 25,
    ('Universal Resort', 'MCO Terminal'): 25,
    ('MCO Terminal', 'Port Canaveral Area'): 55,
    ('Port Canaveral Area', 'MCO Terminal'): 55,
    ('MCO Terminal', 'Other Hotel'): 25,
    ('Other Hotel', 'MCO Terminal'): 25,
    ('MCO Terminal', 'Residential'): 30,
    ('Residential', 'MCO Terminal'): 30,
    ('MCO Terminal', 'Airport Hotel'): 12,
    ('Airport Hotel', 'MCO Terminal'): 12,
    ('Disney Resort', 'Port Canaveral Area'): 72,
    ('Port Canaveral Area', 'Disney Resort'): 72,
    ('Disney Resort', 'Universal Resort'): 28,
    ('Universal Resort', 'Disney Resort'): 28,
    ('Disney Resort', 'Other Hotel'): 25,
    ('Other Hotel', 'Disney Resort'): 25,
    ('Disney Resort', 'Disney Resort'): 12,
    ('MCO Terminal', 'MCO Terminal'): 2,
    ('SFB Terminal', 'SFB Terminal'): 2,
    ('Airport Hotel', 'Airport Hotel'): 10,
    ('Other Hotel', 'Other Hotel'): 15,
    ('Residential', 'Residential'): 15,
    ('Port Canaveral Area', 'Port Canaveral Area'): 10,
    ('Other', 'Other'): 20,
    ('Universal Resort', 'Port Canaveral Area'): 60,
    ('Port Canaveral Area', 'Universal Resort'): 60,
    ('Universal Resort', 'Other Hotel'): 15,
    ('Other Hotel', 'Universal Resort'): 15,
    ('Universal Resort', 'Universal Resort'): 10,
    ('SFB Terminal', 'Disney Resort'): 60,
    ('Disney Resort', 'SFB Terminal'): 60,
    ('SFB Terminal', 'Universal Resort'): 49,
    ('SFB Terminal', 'Port Canaveral Area'): 70,
    ('Port Canaveral Area', 'SFB Terminal'): 70,
    ('Airport Hotel', 'Disney Resort'): 25,
    ('Disney Resort', 'Airport Hotel'): 25,
    ('Airport Hotel', 'Universal Resort'): 20,
    ('Universal Resort', 'Airport Hotel'): 20,
    ('SFB Terminal', 'MCO Terminal'): 60,
    ('MCO Terminal', 'SFB Terminal'): 60,
    ('SFB Terminal', 'Other Hotel'): 55,
    ('Other Hotel', 'SFB Terminal'): 55,
    ('SFB Terminal', 'Airport Hotel'): 45,
    ('Airport Hotel', 'SFB Terminal'): 45,
    ('SFB Terminal', 'Residential'): 55,
    ('Residential', 'SFB Terminal'): 55,
    # Hotel → Port Canaveral cruise runs (2026-06 founder-brain C4): these pairs had NO
    # entry and fell back to the 35-min default vs ~55 real, so every to-port chain was
    # scored ~20 min optimistic (made David's 9:00→10:00 port chain look +15 when it is
    # realistically −5 on 6/14).
    ('Airport Hotel', 'Port Canaveral Area'): 55,
    ('Port Canaveral Area', 'Airport Hotel'): 55,
    ('Other Hotel', 'Port Canaveral Area'): 55,
    ('Port Canaveral Area', 'Other Hotel'): 55,
}

DEFAULT_DRIVE_TIME = 35  # fallback for unknown routes

# Live-distance fallback. The category table above only knows Orlando landmarks; a stop it
# can't place (Tampa, a random residential/other address) lands in one of these buckets and
# would otherwise get the ~35-min guess. For routes touching one of these, pull a real,
# traffic-aware, 2h-cached Google Maps drive time (drivers.utils.get_drive_time) on the raw
# addresses instead — so far/unknown rides aren't hallucinated. Known landmarks keep the
# instant table estimate. Flip USE_LIVE_DISTANCE off to disable entirely.
# Default OFF (2026-05-31 hotfix). The live lookup is a SYNCHRONOUS Google Distance Matrix HTTP
# call, and resolve_drive_minutes runs in the per-request render path — the capacity planner and
# driver dashboard annotate every leg's cleared time on every page load, OUTSIDE the page cache.
# On the single sync gunicorn worker those serial 5s-timeout calls blocked page loads and
# contributed to a capacity-planner WORKER TIMEOUT. Until live distance is re-introduced as a
# precomputed / offline-cached matrix (no in-request network), prod uses the instant category
# table. Set env USE_LIVE_DISTANCE=1 to re-enable the live path (e.g. offline analysis harnesses).
USE_LIVE_DISTANCE = os.environ.get("USE_LIVE_DISTANCE", "0") == "1"
LIVE_DISTANCE_UNKNOWN_CATS = {'Other', 'Residential', 'Other Hotel'}
# Clusters whose internal spread the category table can't capture: it bills every
# "Disney Resort -> Disney Resort" hop as one ~20-min average even between the SAME resort
# (~0) or adjacent ones (~5). For an intra-cluster reposition we use live road distance on
# the real addresses instead (falls back to the table). This stops the engine over-charging
# same/near-resort turnarounds and falsely farming the leg.
INTRA_CLUSTER_LIVE_CATS = {'Disney Resort', 'Universal Resort', 'Port Canaveral Area'}

# Auto pre-farm swap pass: after auto-assign builds the board, try to recover each would-be-
# FARMED leg in-house by cascading existing assignments (find_swaps), before sending it to an
# affiliate. The greedy build is single-leg and can't rearrange; this pass can. Guard-safe
# (find_swaps re-validates feasibility on every move). Flip to False to disable.
AUTO_PREFARM_SWAP_PASS = True

# Gap-compaction relocation pass: after the board is fully covered, relocate an ALREADY-
# COVERED leg from a donor driver to a driver with a big internal hole, when doing so heals
# more gap than it opens — the founder's manual "give David the 6:15 that Roberto holds so his
# morning isn't one long hole; Roberto just starts later" move. Coverage is preserved (a leg
# only changes driver, never gets farmed); manual/pinned legs are never moved. Flip to False
# to disable. See compact_gaps_via_relocation().
AUTO_GAP_COMPACT_PASS = True
# Span-cap coverage rescue (Span Governor): after the build + swap passes, any residual leg
# whose ONLY blocker everywhere was the duty-span cap is assigned anyway (loud RED preview
# warning) instead of landing in Need Affiliates — founder priority #1: the cap may never
# cost an in-house job. A dispatcher's explicitly-TYPED modal "Max hrs" is STRICT and never
# lifted (the leg stays residual with a named reason); only the global default / stub /
# DB-availability caps are rescue-relaxable. Flip to False to make all caps strict.
SPAN_COVERAGE_RESCUE = True
# Shared-car occupancy gate: when two working drivers hold the SAME physical unit for the
# day (Day Setup planned AM/PM share, or an advisor freed-unit accept), every insert for
# one of them must not overlap the partner's jobs +/- this pad. The planned windows alone
# cannot be airtight: modal End is a LAST-PICKUP bound (a 14:50 pickup clears ~16:20 while
# the PM partner may already pick up at 15:05 - one car, two jobs, same time).
# Founder rule (2026-06-10): the pad must cover the AM driver RETURNING the car to the
# warehouse + wash/fuel (~30-40 min after his last clear) + the PM driver's drive OUT to
# his first pickup — "done by 3:00 means the car is ready at base ~3:30-3:40, first PM
# job ~4:30". 60 is the founder's minimum; a geography-aware split (car_ready = clear +
# drive_to_base + service; PM pickup >= car_ready + drive_out) needs a base-location
# concept the engine doesn't have yet — see the smarter-handoff arc.
VEHICLE_SHARE_PAD_MIN = 60
# Span-trim relocation pass (Span Governor Phase 3): after coverage is settled, actively
# SHORTEN over-long days — peel a long driver's FIRST or LAST leg (the only span-shrinking
# legs) onto a driver with room: the founder's "Roberto just starts later" move applied to
# day length. Coverage preserved by construction (a leg only changes driver, never farmed);
# manual/pinned/seeded legs locked; deterministic; read-only board math.
AUTO_SPAN_TRIM_PASS = True
SPAN_TRIM_RAW_MAX_HOURS = 15.0   # also trim a day this long even when a break credit keeps
                                 # its effective span legal (founder raw p90 = 15.2h)
SPAN_TRIM_MIN_RELIEF_MIN = 45    # a move must shorten the donor's day by at least this.
                                 # Receiver stretch is NOT netted against relief — handing a
                                 # tail leg to a short-day driver always stretches him a lot,
                                 # harmlessly (the under-both-limits gate is the protection);
                                 # stretch only breaks ties (prefer the least-stretched receiver)
SPAN_TRIM_MAX_MOVES = 12         # bound the hill-climb
SPAN_TRIM_MAX_PER_DONOR = 3      # peels per donor per run
SPAN_TRIM_MAX_RECEIVE = 2        # receives per receiver per run (no dogpiling)
GAP_COMPACT_MIN_GAP = 120        # only try to fill an internal gap at least this many minutes
GAP_COMPACT_MIN_NET_GAIN = 60    # require (receiver gap healed − donor gap opened) ≥ this (min)
GAP_COMPACT_MAX_MOVES = 25       # safety cap on relocations per run (also bounds the hill-climb)
GAP_COMPACT_PROTECT_DONOR_MAX_JOBS = 3  # never pull a job from a donor with this many jobs or fewer
                                        # (keep light drivers' work intact — founder's "give Steven more")

# ── Founder-brain value rules (2026-06: docs/scheduler-automation/founder-brain-implementation.md) ──
# Evict-to-farm pass (R2 — "an assigned leg is not sacred"): after the greedy + swap passes,
# a residual leg may displace an engine-proposed ARRIVAL when the residual is strictly more
# valuable (leg_value). Arrivals are the farm-out currency (R1): affiliates do MCO
# meet-and-greets fine; a farmed fixed-time departure that no-shows means a missed flight.
# True departures are never evicted (farm-out optimizer parity via is_departure()).
# Knobs displacement_min_value_gain / max_displacements_per_run live on SchedulerSettings.
AUTO_EVICT_TO_FARM_PASS = True

# Class-match-first candidate banding (R3 downward — "a vehicle serves its own booked class
# first"): among feasible candidates for a leg, a driver whose paired vehicle class EXACTLY
# matches the leg's booked class wins over any higher-class driver (who remains a fallback
# when no exact-class driver fits). Pushes towncar arrivals onto the towncar, keeping SUVs/
# vans free for their own class — the founder rebuilt the towncar driver's whole afternoon
# this way on 6/14. Unconditional (NOT gated by the Pass-0 scarcity rule).
CLASS_MATCH_FIRST = True

# Class-match guard (R3 upward — same arc): a driver whose paired class is C is HARD-skipped
# for a lower-class leg when an unassigned class-C leg conflicts in time with it AND no other
# compatible driver could still take that class-C leg — never let the highest-class vehicle
# run a lower-class job while a same-class job at a conflicting time goes unassigned. On 6/14
# the V14 type wasn't "scarce" by the Pass-0 rule, so nothing protected the sprinter job (M5).
CLASS_MATCH_GUARD = True

# Arrival clear-time static floor (founder-brain C4 — the sereen 6:01→7:00 hole): an airport
# arrival's estimated end is anchored on the flight's live ETA + RouteTimingMetric p75
# buckets, BOTH of which can run optimistic at decision time (an early-trending flight, a
# thin time-bucket). On 6/14 that admitted a 7:00 AM fixed-time departure chained at zero
# slack off a 6:01 arrival the static planning model says clears 7:16 (buffer −16). For
# CHAIN feasibility only, an arrival's clear time is floored at the founder's static model
# (pickup_time + 45-min dwell + category drive) — the same model .analysis/analyze_sunday.py
# scores with. A genuinely-delayed flight (dynamic estimate LATER than the floor) keeps the
# dynamic value. Display/board estimates are untouched.
ARRIVAL_CLEAR_STATIC_FLOOR = True
STATIC_FLOOR_DWELL_MIN = 45

# Chain feasibility runs on the FOUNDER'S STATIC PLANNING MODEL (founder-brain 2026-06):
# clear = pickup + 45-min dwell (arrivals / airport-pickup cruises) + CATEGORY-TABLE drive,
# pushed later by any live flight delay; repositioning between jobs = category-table drive.
# The metric path (RouteTimingMetric p75 buckets) stays for DISPLAY/board estimates but is
# the wrong tool for chain math: p75 of observed in-job drives (MCO→Disney 43 min vs the
# founder's 30) silently taxed every chain 10–20 min of slack, so back-to-back days the
# founder builds by hand (+0/+3 buffers on 6/14 — Steven 19312, Raymond 20799) were
# rejected as impossible. "Tight turns that work in reality must NOT be farmed as
# 'impossible'" is the project's prime directive; .analysis/analyze_sunday.py scores with
# exactly this model. Flip off to chain on the metric estimates again.
CHAIN_STATIC_TIMING = True

# build_smart_schedule optional-fill strategy. False = legacy first-fit (seat the first
# feasible leg in tier order). True = best-fit (each round seat the highest-SCORING feasible
# candidate).
# DEFAULT False: an A/B on real data (2026-05-31) showed best-fit is WORSE — it greedily
# grabs high-scoring RETURN legs without accounting for the empty deadhead needed to reach
# each return's pickup, producing a deadhead-heavy, low-utilization day (util 66%->46%).
# Greedy-on-the-current-score doesn't encode the founder's objective (max jobs, round-trips,
# min deadhead/gaps). Best-fit only becomes useful once the SCORE penalizes empty
# repositioning / rewards round-trip continuation. Kept behind this flag for that future work.
BUILDER_BEST_FIT = False

# Buffer between jobs (repositioning uncertainty + personal break)
INTER_JOB_BUFFER = 5  # minutes

# Airport arrivals: passengers deplane + collect bags, so driver can arrive
# up to this many minutes after the pickup_time and still be on time.
# This is the fallback default; prefer SchedulerSettings.arrival_grace_minutes.
ARRIVAL_GRACE_MINUTES = 15


# ============================================================================
# VEHICLE TIER HIERARCHY
# Index = tier level. Higher tier can fulfill all jobs at its level and below.
# ============================================================================

VEHICLE_TIER_ORDER = ['towncar', 'mini_van', 'suv', 'van', 'Van(14 Pax)']


def get_vehicle_tier(vehicle_type_str: str) -> int:
    """Return tier index (0=towncar, 4=van14pax). -1 if unknown."""
    if not vehicle_type_str:
        return -1
    try:
        return VEHICLE_TIER_ORDER.index(vehicle_type_str)
    except ValueError:
        return -1


def get_compatible_vehicle_types(driver_vehicle_type: str) -> list:
    """Return list of vehicle types this driver can fulfill (own tier + all below)."""
    tier = get_vehicle_tier(driver_vehicle_type)
    if tier < 0:
        return list(VEHICLE_TIER_ORDER)  # unknown = allow all
    return VEHICLE_TIER_ORDER[:tier + 1]


# ── Founder leg value (R1/R3/R4) ─────────────────────────────────────────────
# Band widths chosen so each term can NEVER outrank the one above it:
# one class tier step (10000) > any type premium (≤3000); one type step (1000) > the
# revenue clamp (≤999); one revenue dollar (1) > the max pax tiebreak (14 × 0.01).
LEG_VALUE_TIER_WEIGHT = 10000
LEG_VALUE_TYPE_PREMIUM = {'return': 3000, 'cruise': 2000, 'other': 1000, 'arrival': 0}
LEG_VALUE_REVENUE_CLAMP = 999


def leg_value(leg) -> float:
    """Founder value of keeping this leg in-house, for ordering and eviction decisions.

    Priority order (founder rules R1/R3/R4):
      1. BOOKED vehicle-class tier — revenue and the coverage obligation follow the booked
         class; no lower class can cover the job. A Van(14 Pax) booking with ONE passenger
         outranks a Van booking with eight (R3 — never passenger count).
      2. Trip type — departures/returns are the in-house core (fixed pickup, ~30 driver-min,
         driver ends at MCO, a farmed no-show means a missed flight); arrivals are the
         farm-out currency (flight-variable, ~75 driver-min, driver ends stranded) (R1).
      3. revenue_share, when populated (clamped below one type step).
      4. Passenger count — FINAL tiebreak only (R4: class first, pax second, never reverse).
    """
    vtype = leg.effective_vehicle_type
    tier = get_vehicle_tier(str(vtype)) if vtype else -1
    trip = leg.get_trip_type()
    premium = LEG_VALUE_TYPE_PREMIUM.get(trip, LEG_VALUE_TYPE_PREMIUM['other'])
    try:
        rev = float(leg.revenue_share or 0)
    except (TypeError, ValueError):
        rev = 0.0
    rev = min(max(rev, 0.0), LEG_VALUE_REVENUE_CLAMP)
    try:
        pax = int(getattr(leg, 'effective_passenger_count', 0) or 0)
    except (TypeError, ValueError):
        pax = 0
    return max(tier, 0) * LEG_VALUE_TIER_WEIGHT + premium + rev + min(pax, 99) * 0.01


def get_driver_vehicle_type(driver_id: int, target_date: date) -> Optional[str]:
    """Get the vehicle type string for a driver on a specific date.
    Returns None if no vehicle assignment exists."""
    from drivers.models import DriverVehicleAssignment
    assignment = DriverVehicleAssignment.objects.select_related(
        'vehicle__vehicle_type'
    ).filter(driver_id=driver_id, date=target_date).first()
    if assignment and assignment.vehicle and assignment.vehicle.vehicle_type:
        return assignment.vehicle.vehicle_type.vehicle_type
    return None


def load_all_driver_vtypes(target_date: date) -> Dict[int, str]:
    """Load vehicle type strings for ALL drivers assigned on a given date.
    Returns {driver_id: vehicle_type_str} dict. Single DB query."""
    from drivers.models import DriverVehicleAssignment
    assignments = DriverVehicleAssignment.objects.select_related(
        'vehicle__vehicle_type'
    ).filter(date=target_date)
    result = {}
    for a in assignments:
        if a.vehicle and a.vehicle.vehicle_type:
            result[a.driver_id] = a.vehicle.vehicle_type.vehicle_type
    return result


def compute_leg_scarcity(legs, all_driver_vtypes: Dict[int, str], exclude_driver_id: int = None) -> Dict[int, int]:
    """For each leg, count how many drivers have a compatible vehicle type.

    A leg that only 1 driver can handle is "scarce" — that driver should
    prioritize it over legs that many drivers could take.

    Args:
        legs: iterable of Leg objects
        all_driver_vtypes: {driver_id: vehicle_type_str} for all assigned drivers
        exclude_driver_id: if set, don't count this driver (useful for build_smart_schedule
                          where we want to know how many OTHER drivers could take it)

    Returns:
        {leg_id: eligible_driver_count}
    """
    # Pre-compute: for each vehicle type, which drivers can handle it
    # (i.e., their tier >= the type's tier)
    vtype_eligible_counts = {}
    driver_list = [
        (did, vtype) for did, vtype in all_driver_vtypes.items()
        if did != exclude_driver_id
    ]

    for vtype in VEHICLE_TIER_ORDER:
        count = 0
        for did, dvtype in driver_list:
            if vtype in get_compatible_vehicle_types(dvtype):
                count += 1
        vtype_eligible_counts[vtype] = count

    result = {}
    for leg in legs:
        leg_vtype = leg.effective_vehicle_type
        if leg_vtype:
            result[leg.id] = vtype_eligible_counts.get(leg_vtype, len(driver_list))
        else:
            # No vehicle type on leg — any driver can do it
            result[leg.id] = len(driver_list)
    return result


# ============================================================================
# ROUTE TIMING CACHE — avoids N+1 queries during scheduling
# ============================================================================

_timing_cache = None       # (pickup, dropoff, time_cat, day_cat) -> metric dict
_timing_cache_agg = None   # (pickup, dropoff) -> metric dict (best-sample fallback)


def _metric_dict(m):
    return {
        'sample_count': m.sample_count,
        'median_drive_time': m.median_drive_time,
        'p75_drive_time': m.p75_drive_time,
        'avg_drive_time': m.avg_drive_time,
        'median_airport_dwell_time': m.median_airport_dwell_time,
        'p75_airport_dwell_time': m.p75_airport_dwell_time,
        'avg_airport_dwell_time': m.avg_airport_dwell_time,
        'trip_type': m.trip_type,
    }


def preload_timing_cache():
    """
    Load all RouteTimingMetric records into memory.
    Call this once before running scheduling functions to avoid
    hundreds of individual DB queries.

    Stores two caches:
    - _timing_cache: keyed by (pickup, dropoff, time_of_day, day_type) for precise lookups
    - _timing_cache_agg: keyed by (pickup, dropoff) with highest-sample record as fallback
    """
    global _timing_cache, _timing_cache_agg
    from reservations.models import RouteTimingMetric

    _timing_cache = {}
    _timing_cache_agg = {}
    for m in RouteTimingMetric.objects.filter(sample_count__gte=1):
        # Specific key (time-of-day + day-type aware)
        specific_key = (
            m.pickup_location_category, m.dropoff_location_category,
            m.time_of_day_category, m.day_type,
        )
        existing = _timing_cache.get(specific_key)
        if existing is None or m.sample_count > existing['sample_count']:
            _timing_cache[specific_key] = _metric_dict(m)

        # Aggregate key (route-only fallback, keep highest sample count)
        agg_key = (m.pickup_location_category, m.dropoff_location_category)
        agg_existing = _timing_cache_agg.get(agg_key)
        if agg_existing is None or m.sample_count > agg_existing['sample_count']:
            _timing_cache_agg[agg_key] = _metric_dict(m)


def clear_timing_cache():
    """Clear the cache (e.g., after metrics are updated)."""
    global _timing_cache, _timing_cache_agg
    _timing_cache = None
    _timing_cache_agg = None


def _get_cached_metric(pickup_cat, dropoff_cat, time_cat=None, day_cat=None):
    """
    Look up a metric from cache, or return None if not cached / insufficient samples.
    Tries specific (time/day) key first, falls back to aggregate (route-only).
    """
    if _timing_cache is None:
        return None

    # Try specific lookup when time/day context is available
    if time_cat and day_cat:
        metric = _timing_cache.get((pickup_cat, dropoff_cat, time_cat, day_cat))
        if metric and metric['sample_count'] >= 5:
            return metric

    # Fall back to aggregate (best-sample for this route)
    metric = _timing_cache_agg.get((pickup_cat, dropoff_cat))
    if metric and metric['sample_count'] >= 5:
        return metric
    return None


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class ScheduleSlot:
    """A single leg in a driver's daily schedule."""
    leg_id: int
    pickup_time: time
    pickup_location: str
    pickup_category: str
    dropoff_location: str
    dropoff_category: str
    trip_type: str
    estimated_end_time: datetime
    reservation_id: int
    customer_name: str
    status: str
    has_flight: bool
    flight_info: Optional[str] = None
    revenue: Optional[Decimal] = None
    vehicle_type: Optional[str] = None
    is_paid: bool = True
    passengers: int = 1
    luggage: int = 0
    luggage_type: str = ""
    carseats_short: str = ""
    store_stop: bool = False
    # Founder static-model clear time for CHAIN math (CHAIN_STATIC_TIMING); None falls
    # back to estimated_end_time (+ the arrival static floor).
    chain_clear_dt: Optional[datetime] = None
    # Trip has a refund in-flight — flag so dispatchers don't assign it by mistake.
    pending_refund: bool = False
    # VIP reservation (manual flag or VIP agency e.g. Small World Big Fun) — gold
    # highlight on the planner timeline, mirroring the leg boards.
    is_vip: bool = False
    # Multi-stop / multi-flight indicators (default 0 keeps legacy slots unchanged)
    extra_stop_count: int = 0
    secondary_flight_count: int = 0
    # Set by view for template positioning
    position_pct: float = 0
    width_pct: float = 0

    @property
    def vehicle_abbr(self) -> str:
        _map = {'towncar': 'TC', 'suv': 'SUV', 'mini_van': 'MV', 'van': 'VAN', 'Van(14 Pax)': 'V14'}
        return _map.get(self.vehicle_type or '', '')


@dataclass
class DriverDaySchedule:
    """Full day schedule for a single driver."""
    driver_id: int
    driver_name: str
    driver_type: str
    slots: List[ScheduleSlot] = field(default_factory=list)

    @property
    def total_legs(self):
        return len(self.slots)

    @property
    def total_revenue(self):
        return sum((s.revenue or Decimal('0.00')) for s in self.slots)


@dataclass
class FeasibilityResult:
    """Result of checking if a driver can take an additional leg."""
    feasible: bool
    buffer_minutes: int
    warnings: List[str] = field(default_factory=list)
    reason: str = ""


def _make_sim_slot(leg, target_date) -> 'ScheduleSlot':
    """Build the in-memory ScheduleSlot used when simulating an assignment of `leg`
    (greedy build, class-match guard, evict-to-farm pass)."""
    from dispatching.analytics import categorize_location
    return ScheduleSlot(
        leg_id=leg.id,
        pickup_time=leg.pickup_time,
        pickup_location=leg.pickup_location,
        pickup_category=categorize_location(leg.pickup_location),
        dropoff_location=leg.dropoff_location,
        dropoff_category=categorize_location(leg.dropoff_location),
        trip_type=leg.get_trip_type(),
        estimated_end_time=estimate_job_end_time(leg, target_date),
        reservation_id=getattr(leg, 'reservation_id', None) or 0,
        customer_name="",
        status=getattr(leg, 'status', None) or 'in-progress',
        has_flight=False,
        revenue=getattr(leg, 'revenue_share', None),
        chain_clear_dt=chain_clear_dt(leg, target_date),
    )


@dataclass
class AlternativeDriver:
    """A ranked alternative driver for assignment."""
    driver_id: int
    driver_name: str
    score: float
    feasibility: FeasibilityResult
    reason: str


@dataclass
class AssignmentSuggestion:
    """Suggested driver for an unassigned leg."""
    leg_id: int
    suggested_driver_id: Optional[int]
    suggested_driver_name: Optional[str]
    feasibility: Optional[FeasibilityResult]
    reason: str
    priority: int  # 1=best fit, 0=no fit
    alternatives: List[AlternativeDriver] = field(default_factory=list)


@dataclass
class BatchingOpportunity:
    """Group of legs that could be done back-to-back by one driver."""
    legs: List[dict]
    location_category: str
    time_window_start: time
    time_window_end: time
    reason: str


# ============================================================================
# CORE FUNCTIONS
# ============================================================================

def get_drive_time(pickup_category: str, dropoff_category: str,
                   time_cat: str = None, day_cat: str = None) -> int:
    """
    Get estimated drive time between two location categories in minutes.
    Prefers P75 from RouteTimingMetric, falls back to median then avg then hardcoded.
    Uses in-memory cache when available to avoid repeated DB queries.

    When time_cat/day_cat are provided, tries a time-of-day specific metric first,
    then falls back to the best aggregate for the route.
    """
    # Try cache first
    cached = _get_cached_metric(pickup_category, dropoff_category, time_cat, day_cat)
    if cached:
        if cached['p75_drive_time']:
            return cached['p75_drive_time']
        if cached['median_drive_time']:
            return cached['median_drive_time']
        if cached['avg_drive_time']:
            return cached['avg_drive_time']
    elif _timing_cache is None:
        # Cache not loaded — fall back to DB query (single-use calls)
        from reservations.models import RouteTimingMetric
        filters = {
            'pickup_location_category': pickup_category,
            'dropoff_location_category': dropoff_category,
            'sample_count__gte': 5,
        }
        if time_cat and day_cat:
            filters['time_of_day_category'] = time_cat
            filters['day_type'] = day_cat
        metric = RouteTimingMetric.objects.filter(**filters).order_by('-sample_count').first()
        # If specific time/day query returned nothing, try without
        if metric is None and time_cat and day_cat:
            metric = RouteTimingMetric.objects.filter(
                pickup_location_category=pickup_category,
                dropoff_location_category=dropoff_category,
                sample_count__gte=5,
            ).order_by('-sample_count').first()
        if metric:
            if metric.p75_drive_time:
                return metric.p75_drive_time
            if metric.median_drive_time:
                return metric.median_drive_time
            if metric.avg_drive_time:
                return metric.avg_drive_time

    return DRIVE_TIME_ESTIMATES.get(
        (pickup_category, dropoff_category),
        DEFAULT_DRIVE_TIME,
    )


def resolve_drive_minutes(pickup_text, dropoff_text, pickup_category, dropoff_category,
                          time_cat: str = None, day_cat: str = None) -> int:
    """Drive minutes between two points.

    For routes where the category map can't place an endpoint (LIVE_DISTANCE_UNKNOWN_CATS —
    e.g. a Tampa or odd residential address), use a live, traffic-aware, 2h-cached Google
    Maps drive time on the RAW addresses so far/unknown rides aren't guessed at the ~35-min
    default. Known Orlando landmarks use the instant category estimate. Falls back to the
    category estimate if the live lookup is disabled, unavailable, or fails.
    """
    # Same exact location => already there, no repositioning drive (offline, deterministic).
    if pickup_text and dropoff_text and pickup_text.strip().lower() == dropoff_text.strip().lower():
        return 0
    if (USE_LIVE_DISTANCE and pickup_text and dropoff_text
            and (pickup_category in LIVE_DISTANCE_UNKNOWN_CATS
                 or dropoff_category in LIVE_DISTANCE_UNKNOWN_CATS
                 # intra-cluster hop the category table can't resolve (same/adjacent resort, etc.)
                 or (pickup_category == dropoff_category
                     and pickup_category in INTRA_CLUSTER_LIVE_CATS))):
        try:
            from drivers.utils import get_drive_time as _maps_drive_time
            # SPIKE TRIPWIRE: this is the PAID, synchronous Google Distance Matrix path,
            # default-OFF in prod. A harness/script that flips USE_LIVE_DISTANCE=1 can fan
            # this out across thousands of legs (see the 2026-06-10 $593 spike). Log every
            # invocation under a fixed, greppable tag so a runaway run is instantly visible:
            #   grep GTC-GOOGLE-LIVE-DISTANCE <logs>
            logger.warning(
                "GTC-GOOGLE-LIVE-DISTANCE live Distance Matrix call (USE_LIVE_DISTANCE=1): %s -> %s",
                pickup_text, dropoff_text,
            )
            info = _maps_drive_time(pickup_text, dropoff_text)
            if info and info.get("duration_seconds"):
                return max(1, round(info["duration_seconds"] / 60))
        except Exception:
            pass  # fall through to the category estimate
    return get_drive_time(pickup_category, dropoff_category, time_cat, day_cat)


def get_airport_dwell_time(pickup_category: str, dropoff_category: str,
                           time_cat: str = None, day_cat: str = None) -> int:
    """
    Get estimated airport dwell time (gate arrival → pickup) in minutes.
    Only meaningful for arrival trips. Falls back to 45 min.
    Uses in-memory cache when available.

    When time_cat/day_cat are provided, tries a time-of-day specific metric first,
    then falls back to the best aggregate for the route.
    """
    # Try cache first
    cached = _get_cached_metric(pickup_category, dropoff_category, time_cat, day_cat)
    if cached:
        if cached['p75_airport_dwell_time']:
            return cached['p75_airport_dwell_time']
        if cached['median_airport_dwell_time']:
            return cached['median_airport_dwell_time']
        if cached['avg_airport_dwell_time']:
            return cached['avg_airport_dwell_time']
    elif _timing_cache is None:
        # Cache not loaded — fall back to DB query
        from reservations.models import RouteTimingMetric
        filters = {
            'pickup_location_category': pickup_category,
            'dropoff_location_category': dropoff_category,
            'trip_type': 'arrival',
            'sample_count__gte': 5,
        }
        if time_cat and day_cat:
            filters['time_of_day_category'] = time_cat
            filters['day_type'] = day_cat
        metric = RouteTimingMetric.objects.filter(**filters).order_by('-sample_count').first()
        if metric is None and time_cat and day_cat:
            metric = RouteTimingMetric.objects.filter(
                pickup_location_category=pickup_category,
                dropoff_location_category=dropoff_category,
                trip_type='arrival',
                sample_count__gte=5,
            ).order_by('-sample_count').first()
        if metric:
            if metric.p75_airport_dwell_time:
                return metric.p75_airport_dwell_time
            if metric.median_airport_dwell_time:
                return metric.median_airport_dwell_time
            if metric.avg_airport_dwell_time:
                return metric.avg_airport_dwell_time

    return 45  # Default: 45 min airport dwell (flight land → bags → walk → in car)


PUBLIX_STOP_MINUTES = 25  # Extra time for grocery store stop

# RETROSPECTIVE-EVAL ONLY. When True, _get_best_flight_arrival returns the SCHEDULED (decision-time)
# flight arrival instead of best_arrival_local() (estimated/actual = hindsight). The live scheduler /
# web process NEVER sets this — only dispatching.farmout_optimizer flips it (set/reset around its run)
# so a past day is graded on the times the founder actually saw when building the schedule.
USE_SCHEDULED_ARRIVAL_FOR_EVAL = False


def _get_best_flight_arrival(leg) -> 'datetime | None':
    """
    Return the best available flight arrival datetime for an arrival leg,
    or None if no flight data exists. Uses naive local time.

    Delegates to dispatching.analytics.best_flight_arrival_local — the SAME anchor
    used when historical dwell is measured — so the scheduler's clearing clock and
    the measured dwell start from the identical moment (no gate-vs-runway mismatch).

    When USE_SCHEDULED_ARRIVAL_FOR_EVAL is set (retrospective grading only), uses the
    SCHEDULED arrival instead — the decision-time value, excluding later delays.
    """
    from dispatching.analytics import best_flight_arrival_local, scheduled_flight_arrival_local
    flight = getattr(leg, 'flight_information', None)
    if USE_SCHEDULED_ARRIVAL_FOR_EVAL:
        return scheduled_flight_arrival_local(flight)
    return best_flight_arrival_local(flight)


def _anchor_flight_dt(flight_dt: datetime, pickup_dt: datetime) -> datetime:
    """Place the flight's clock time on the calendar day nearest the pickup slot.

    The flight record's DATE can be a red-eye (lands just after midnight, so the
    real arrival is the next calendar day) or occasionally a stale/oddly-dated
    value. Rather than blindly combining the flight time with target_date (which
    forced a 00:30 landing to 00:30 the SAME morning — up to ~24h early, throwing
    clearing time off and scrambling slot order), pick whichever of
    prev/same/next day puts the arrival closest to the pickup slot. Keeps
    within-day delay signal intact while handling the midnight wrap correctly.
    """
    t = flight_dt.time()
    candidates = [
        datetime.combine(pickup_dt.date() + timedelta(days=d), t)
        for d in (-1, 0, 1)
    ]
    return min(candidates, key=lambda c: abs((c - pickup_dt).total_seconds()))


def estimate_job_end_time(leg, target_date: date) -> datetime:
    """
    Estimate when a driver finishes this leg.
    For arrivals: flight_arrival (or pickup_time) + dwell + drive (+ Publix stop if applicable).
    For non-arrivals: pickup_time + drive.

    Uses time-of-day and day-type aware metrics for more accurate estimates.
    """
    from dispatching.analytics import (
        categorize_location, categorize_day_type, leg_time_of_day_category,
    )

    pickup_cat = categorize_location(leg.pickup_location)
    dropoff_cat = categorize_location(leg.dropoff_location)
    # Flight-aware time bucket so we read the same bucket the metric was stored under.
    time_cat = leg_time_of_day_category(leg)
    day_cat = categorize_day_type(target_date)
    drive_minutes = resolve_drive_minutes(leg.pickup_location, leg.dropoff_location, pickup_cat, dropoff_cat, time_cat, day_cat)

    pickup_dt = datetime.combine(target_date, leg.pickup_time)

    # For arrivals, use best flight arrival time if available (dynamic clearing)
    trip_type = leg.get_trip_type()
    if trip_type == 'arrival':
        flight_dt = _get_best_flight_arrival(leg)
        # Anchor the flight time to the calendar day nearest the pickup slot so
        # red-eyes (landing just after midnight) aren't pulled ~24h early.
        start_dt = _anchor_flight_dt(flight_dt, pickup_dt) if flight_dt else pickup_dt
        dwell_minutes = get_airport_dwell_time(pickup_cat, dropoff_cat, time_cat, day_cat)
        store_stop_minutes = 0
        if hasattr(leg, 'reservation') and leg.reservation and getattr(leg.reservation, 'store_stop', False):
            store_stop_minutes = PUBLIX_STOP_MINUTES
        return start_dt + timedelta(minutes=dwell_minutes + drive_minutes + store_stop_minutes)

    # Cruise legs picking up from airport (MCO → Cruise Port) need dwell time too
    if trip_type == 'cruise' and leg.get_cruise_direction() == 'to_cruise' and leg.is_airport_pickup():
        dwell_minutes = get_airport_dwell_time(pickup_cat, dropoff_cat, time_cat, day_cat)
        return pickup_dt + timedelta(minutes=dwell_minutes + drive_minutes)

    return pickup_dt + timedelta(minutes=drive_minutes)


def get_clearing_breakdown(leg, target_date: date) -> dict:
    """
    Return a detailed breakdown of how clearing time is estimated for a leg.
    Useful for diagnosing incorrect clearing estimates.
    """
    from dispatching.analytics import (
        categorize_location, categorize_day_type, leg_time_of_day_category,
    )

    pickup_cat = categorize_location(leg.pickup_location)
    dropoff_cat = categorize_location(leg.dropoff_location)
    time_cat = leg_time_of_day_category(leg)
    day_cat = categorize_day_type(target_date)
    drive_minutes = resolve_drive_minutes(leg.pickup_location, leg.dropoff_location, pickup_cat, dropoff_cat, time_cat, day_cat)
    static_drive = DRIVE_TIME_ESTIMATES.get((pickup_cat, dropoff_cat), DEFAULT_DRIVE_TIME)

    pickup_dt = datetime.combine(target_date, leg.pickup_time)
    trip_type = leg.get_trip_type()

    breakdown = {
        'pickup_category': pickup_cat,
        'dropoff_category': dropoff_cat,
        'trip_type': trip_type,
        'time_of_day': time_cat,
        'day_type': day_cat,
        'drive_minutes': drive_minutes,
        'static_drive_minutes': static_drive,
        'drive_source': 'metric' if drive_minutes != static_drive else 'static',
    }

    if trip_type == 'arrival':
        flight_dt = _get_best_flight_arrival(leg)
        dwell_minutes = get_airport_dwell_time(pickup_cat, dropoff_cat, time_cat, day_cat)
        store_stop = 0
        if hasattr(leg, 'reservation') and leg.reservation and getattr(leg.reservation, 'store_stop', False):
            store_stop = PUBLIX_STOP_MINUTES

        start_dt = _anchor_flight_dt(flight_dt, pickup_dt) if flight_dt else pickup_dt
        end_dt = start_dt + timedelta(minutes=dwell_minutes + drive_minutes + store_stop)

        breakdown['start_time'] = start_dt.strftime('%I:%M %p').lstrip('0')
        breakdown['start_source'] = 'flight' if flight_dt else 'pickup_time'
        breakdown['flight_time'] = flight_dt.strftime('%I:%M %p').lstrip('0') if flight_dt else None
        breakdown['pickup_time'] = pickup_dt.strftime('%I:%M %p').lstrip('0')
        breakdown['dwell_minutes'] = dwell_minutes
        breakdown['store_stop_minutes'] = store_stop
        breakdown['clearing_time'] = end_dt.strftime('%I:%M %p').lstrip('0')
        breakdown['total_minutes'] = dwell_minutes + drive_minutes + store_stop
        breakdown['formula'] = (
            f"{breakdown['start_time']} ({'flight' if flight_dt else 'pickup'}) "
            f"+ {dwell_minutes}min dwell + {drive_minutes}min drive"
            + (f" + {store_stop}min store stop" if store_stop else "")
            + f" = {breakdown['clearing_time']}"
        )
    elif trip_type == 'cruise' and leg.get_cruise_direction() == 'to_cruise' and leg.is_airport_pickup():
        dwell_minutes = get_airport_dwell_time(pickup_cat, dropoff_cat, time_cat, day_cat)
        end_dt = pickup_dt + timedelta(minutes=dwell_minutes + drive_minutes)
        breakdown['start_time'] = pickup_dt.strftime('%I:%M %p').lstrip('0')
        breakdown['dwell_minutes'] = dwell_minutes
        breakdown['clearing_time'] = end_dt.strftime('%I:%M %p').lstrip('0')
        breakdown['total_minutes'] = dwell_minutes + drive_minutes
        breakdown['formula'] = (
            f"{breakdown['start_time']} (pickup) "
            f"+ {dwell_minutes}min dwell + {drive_minutes}min drive"
            f" = {breakdown['clearing_time']}"
        )
    else:
        end_dt = pickup_dt + timedelta(minutes=drive_minutes)
        breakdown['start_time'] = pickup_dt.strftime('%I:%M %p').lstrip('0')
        breakdown['dwell_minutes'] = 0
        breakdown['clearing_time'] = end_dt.strftime('%I:%M %p').lstrip('0')
        breakdown['total_minutes'] = drive_minutes
        breakdown['formula'] = (
            f"{breakdown['start_time']} (pickup) "
            f"+ {drive_minutes}min drive"
            f" = {breakdown['clearing_time']}"
        )

    return breakdown


def predict_driver_available_time(leg, target_date: date) -> datetime:
    """
    Predict when a driver will be available for the next job after this leg.
    Returns: estimated end time + inter-job buffer.
    """
    return estimate_job_end_time(leg, target_date) + timedelta(minutes=INTER_JOB_BUFFER)


def _arrival_static_floor_dt(pickup_time, pickup_category, dropoff_category, target_date):
    """Static planning clear time for an airport arrival: pickup_time + default dwell +
    category-table drive — the founder's by-hand model (and .analysis/analyze_sunday.py's).
    Used as a FLOOR on the dynamic (flight-ETA + p75-bucket) estimate in chain checks; see
    ARRIVAL_CLEAR_STATIC_FLOOR."""
    drive = DRIVE_TIME_ESTIMATES.get((pickup_category, dropoff_category), DEFAULT_DRIVE_TIME)
    return datetime.combine(target_date, pickup_time) + timedelta(
        minutes=STATIC_FLOOR_DWELL_MIN + drive)


def chain_clear_dt(leg, target_date: date) -> datetime:
    """Clear time used for CHAIN feasibility (CHAIN_STATIC_TIMING — the founder's
    planning model): pickup + 45-min dwell (arrivals and airport-pickup cruises) +
    category-table drive (+ Publix stop), with the anchor pushed LATER by a live flight
    delay (a flight trending past its scheduled slot still protects the next pickup).
    An early-trending flight never pulls the clear time earlier — that optimism is what
    admitted the sereen 6:01→7:00 pair (C4)."""
    from dispatching.analytics import categorize_location
    pickup_cat = categorize_location(leg.pickup_location)
    dropoff_cat = categorize_location(leg.dropoff_location)
    drive = DRIVE_TIME_ESTIMATES.get((pickup_cat, dropoff_cat), DEFAULT_DRIVE_TIME)
    anchor = datetime.combine(target_date, leg.pickup_time)
    trip = leg.get_trip_type()
    dwell = 0
    store_stop = 0
    if trip == 'arrival':
        dwell = STATIC_FLOOR_DWELL_MIN
        flight_dt = _get_best_flight_arrival(leg)
        if flight_dt:
            anchored = _anchor_flight_dt(flight_dt, anchor)
            if anchored > anchor:
                anchor = anchored
        if (getattr(leg, 'reservation', None) is not None
                and getattr(leg.reservation, 'store_stop', False)):
            store_stop = PUBLIX_STOP_MINUTES
    elif trip == 'cruise' and leg.get_cruise_direction() == 'to_cruise' and leg.is_airport_pickup():
        dwell = STATIC_FLOOR_DWELL_MIN
    return anchor + timedelta(minutes=dwell + drive + store_stop)


def chain_repo_minutes(from_text, to_text, from_category, to_category) -> int:
    """Repositioning drive for CHAIN feasibility (CHAIN_STATIC_TIMING): same exact
    address → 0, else the category table — the founder's planning numbers, NOT the p75
    metric (p75 of observed in-job drives over-prices an empty reposition and was
    silently rejecting chains the founder builds by hand). Far/unknown endpoints keep
    the live-distance path when enabled (the table can't place them at all)."""
    if from_text and to_text and from_text.strip().lower() == to_text.strip().lower():
        return 0
    if USE_LIVE_DISTANCE and (from_category in LIVE_DISTANCE_UNKNOWN_CATS
                              or to_category in LIVE_DISTANCE_UNKNOWN_CATS):
        return resolve_drive_minutes(from_text, to_text, from_category, to_category)
    return DRIVE_TIME_ESTIMATES.get((from_category, to_category), DEFAULT_DRIVE_TIME)


def _slot_chain_end(slot, target_date: date) -> datetime:
    """A slot's clear time for chain math: the precomputed founder static-model value
    when present (CHAIN_STATIC_TIMING), else the dynamic estimate raised to the arrival
    static floor (legacy fallback for callers that build bare slots)."""
    if CHAIN_STATIC_TIMING and slot.chain_clear_dt is not None:
        return slot.chain_clear_dt
    from dispatching import feasibility_guards as fg
    end = slot.estimated_end_time
    if ARRIVAL_CLEAR_STATIC_FLOOR and fg.is_airport_arrival(slot.trip_type, slot.pickup_category):
        end = max(end, _arrival_static_floor_dt(
            slot.pickup_time, slot.pickup_category, slot.dropoff_category, target_date))
    return end


def check_feasibility(
    driver_schedule: DriverDaySchedule,
    new_leg,
    target_date: date,
    inter_job_buffer: int = None,   # deprecated: superseded by context turnaround (Guard B)
    arrival_grace: int = None,      # deprecated: superseded by DEPLANING_GRACE_MIN (Guard B)
    driver_window: dict = None,     # Guard C: per-driver window {start,end,max_hours,flexible} (None => skip)
    deplaning_grace: int = None,    # Guard B override (default feasibility_guards.DEPLANING_GRACE_MIN)
    safety_pad: int = None,         # Guard B override (default feasibility_guards.SAFETY_PAD_MIN)
) -> FeasibilityResult:
    """
    Check whether a driver can fit a new leg into their schedule.

    Checks:
    1. No overlaps with existing jobs.
    2/3. Context-dependent turnaround (Guard B) to the preceding and following jobs.
    4. Guard C — per-driver window (start / clear-by end / max-hours span).

    Guard C only runs when the caller supplies `driver_window` (so other callers keep
    their behavior). Guard B turnaround applies always. (Guard A / physical-capacity was
    removed — booking-time validation already enforces party/luggage/car-seat fit.)
    """
    from dispatching.analytics import categorize_location
    from dispatching import feasibility_guards as fg

    new_pickup_dt = datetime.combine(target_date, new_leg.pickup_time)
    new_end_dt = estimate_job_end_time(new_leg, target_date)
    new_pickup_cat = categorize_location(new_leg.pickup_location)
    new_dropoff_cat = categorize_location(new_leg.dropoff_location)
    new_is_arrival = fg.is_airport_arrival(new_leg.get_trip_type(), new_pickup_cat)

    # Founder-brain C4 (the sereen 6:01→7:00 hole) + CHAIN_STATIC_TIMING: chain math
    # runs on the founder's static planning model, not the flight-ETA/p75 estimate —
    # optimistic estimates admitted fixed-time follow-ups at zero real slack, and
    # pessimistic p75 buckets rejected chains the founder builds by hand. A live flight
    # DELAY still pushes the chain clear time later (never earlier).
    if CHAIN_STATIC_TIMING:
        new_chain_end_dt = chain_clear_dt(new_leg, target_date)
    else:
        new_chain_end_dt = new_end_dt
        if ARRIVAL_CLEAR_STATIC_FLOOR and new_is_arrival:
            new_chain_end_dt = max(new_end_dt, _arrival_static_floor_dt(
                new_leg.pickup_time, new_pickup_cat, new_dropoff_cat, target_date))

    # ── Guard C: per-driver window (start / clear-by / max-hours span) ──
    if driver_window is not None:
        if driver_schedule.slots:
            first_pickup_dt = datetime.combine(
                target_date, min([s.pickup_time for s in driver_schedule.slots] + [new_leg.pickup_time]))
            last_end = max([s.estimated_end_time for s in driver_schedule.slots] + [new_end_dt])
            span_after = (last_end - first_pickup_dt).total_seconds() / 3600
            _fp_before = datetime.combine(target_date, min(s.pickup_time for s in driver_schedule.slots))
            _le_before = max(s.estimated_end_time for s in driver_schedule.slots)
            span_before = (_le_before - _fp_before).total_seconds() / 3600
        else:
            span_after = (new_end_dt - new_pickup_dt).total_seconds() / 3600
            span_before = 0.0
        ok, reason = fg.window_check(driver_window, new_leg.pickup_time, new_end_dt, span_after,
                                     target_date=target_date, span_hours_before=span_before)
        if not ok:
            return FeasibilityResult(feasible=False, buffer_minutes=-999,
                                     reason=f"Outside driver window: {reason}")

    if not driver_schedule.slots:
        return FeasibilityResult(feasible=True, buffer_minutes=999, reason="Available - no jobs yet")

    warnings = []
    sorted_slots = sorted(driver_schedule.slots, key=lambda s: s.pickup_time)

    # Find preceding and following slots
    preceding = None
    following = None
    for slot in sorted_slots:
        slot_pickup_dt = datetime.combine(target_date, slot.pickup_time)
        if slot_pickup_dt <= new_pickup_dt:
            preceding = slot
        elif following is None:
            following = slot

    buffer_minutes = 999

    # Check against preceding slot — context-dependent turnaround (Guard B)
    if preceding:
        preceding_end = _slot_chain_end(preceding, target_date)
        if CHAIN_STATIC_TIMING:
            reposition = chain_repo_minutes(preceding.dropoff_location, new_leg.pickup_location,
                                            preceding.dropoff_category, new_pickup_cat)
        else:
            reposition = resolve_drive_minutes(preceding.dropoff_location, new_leg.pickup_location, preceding.dropoff_category, new_pickup_cat)
        req = fg.required_turnaround(
            reposition, new_is_arrival,
            same_terminal=(preceding.dropoff_category == new_pickup_cat),
            deplaning_grace=deplaning_grace, safety_pad=safety_pad,
        )
        earliest_available = preceding_end + timedelta(minutes=req)
        buffer_minutes = int((new_pickup_dt - earliest_available).total_seconds() / 60)

        if buffer_minutes < 0:
            end_str = preceding_end.strftime('%I:%M %p').lstrip('0')
            return FeasibilityResult(
                feasible=False,
                buffer_minutes=buffer_minutes,
                reason=f"Needs {abs(buffer_minutes)} more min. Previous job ends ~{end_str}, "
                       f"+{req}min turnaround required.",
            )
        if buffer_minutes < 15:
            warnings.append(f"Tight: {buffer_minutes}min after previous job")

    # Check against following slot — context-dependent turnaround (Guard B)
    if following:
        following_pickup_dt = datetime.combine(target_date, following.pickup_time)
        following_is_arrival = fg.is_airport_arrival(following.trip_type, following.pickup_category)
        if CHAIN_STATIC_TIMING:
            reposition = chain_repo_minutes(new_leg.dropoff_location, following.pickup_location,
                                            new_dropoff_cat, following.pickup_category)
        else:
            reposition = resolve_drive_minutes(new_leg.dropoff_location, following.pickup_location, new_dropoff_cat, following.pickup_category)
        req = fg.required_turnaround(
            reposition, following_is_arrival,
            same_terminal=(new_dropoff_cat == following.pickup_category),
            deplaning_grace=deplaning_grace, safety_pad=safety_pad,
        )
        earliest_for_next = new_chain_end_dt + timedelta(minutes=req)
        following_buffer = int((following_pickup_dt - earliest_for_next).total_seconds() / 60)

        if following_buffer < 0:
            next_str = following.pickup_time.strftime('%I:%M %p').lstrip('0')
            return FeasibilityResult(
                feasible=False,
                buffer_minutes=following_buffer,
                reason=f"Conflicts with next job at {next_str}.",
            )
        if following_buffer < 15:
            warnings.append(f"Tight: {following_buffer}min before next job")
        buffer_minutes = min(buffer_minutes, following_buffer)

    return FeasibilityResult(
        feasible=True,
        buffer_minutes=buffer_minutes,
        warnings=warnings,
        reason=f"{buffer_minutes}min buffer",
    )


def build_driver_schedules(legs, drivers, target_date: date) -> Dict[int, DriverDaySchedule]:
    """
    Build a DriverDaySchedule for each driver from the day's assigned legs.
    """
    from dispatching.analytics import categorize_location

    schedules = {}
    for driver in drivers:
        schedules[driver.id] = DriverDaySchedule(
            driver_id=driver.id,
            driver_name=str(driver),
            driver_type=driver.driver_type,
        )

    for leg in legs:
        if not leg.driver or leg.driver.id not in schedules:
            continue

        pickup_cat = categorize_location(leg.pickup_location)
        dropoff_cat = categorize_location(leg.dropoff_location)
        # Reuse pre-computed end time if available (avoids redundant recalculation)
        end_time = getattr(leg, '_estimated_end_dt', None) or estimate_job_end_time(leg, target_date)

        customer_name = ""
        if leg.reservation and leg.reservation.customer:
            customer_name = leg.reservation.customer.get_full_name()

        flight_info = None
        has_flight = bool(leg.flight_information_id if hasattr(leg, 'flight_information_id') else False)
        try:
            if leg.flight_information:
                has_flight = True
                flight_info = str(leg.flight_information)
        except Exception:
            pass

        leg_vtype = leg.effective_vehicle_type

        # Build a compact car-seat summary (e.g. "1 rf, 2 ff, 1 b") for popups.
        _carseat_parts = []
        try:
            if leg.effective_need_carseats:
                if leg.effective_rf_carseats:
                    _carseat_parts.append(f"{leg.effective_rf_carseats} rf")
                if leg.effective_ff_carseats:
                    _carseat_parts.append(f"{leg.effective_ff_carseats} ff")
                if leg.effective_booster_seats:
                    _carseat_parts.append(f"{leg.effective_booster_seats} b")
        except Exception:
            pass
        _carseats_short = ", ".join(_carseat_parts)

        # Count extra stops + secondary flights. Uses the prefetched collection when the
        # caller prefetched the relation (build_driver_schedules runs MANY times across the
        # auto-assign passes — without the prefetch this was a per-leg COUNT × every rebuild,
        # the dominant N+1 on assign-all). `_result_cache` lives on the prefetched QuerySet,
        # NOT the RelatedManager (the old check was always False → always .count()); read
        # Django's `_prefetched_objects_cache` instead. Falls back to .count() unprefetched.
        _pf_cache = getattr(leg, '_prefetched_objects_cache', None) or {}
        try:
            _legstop_count = (len(_pf_cache['legstop_set']) if 'legstop_set' in _pf_cache
                              else leg.legstop_set.count())
        except Exception:
            _legstop_count = 0
        try:
            _legflight_count = (len(_pf_cache['legflight_set']) if 'legflight_set' in _pf_cache
                                else leg.legflight_set.count())
        except Exception:
            _legflight_count = 0
        _secondary_flights = max(_legflight_count - 1, 0) if _legflight_count else 0

        slot = ScheduleSlot(
            leg_id=leg.id,
            pickup_time=leg.pickup_time,
            pickup_location=leg.pickup_location,
            pickup_category=pickup_cat,
            dropoff_location=leg.dropoff_location,
            dropoff_category=dropoff_cat,
            trip_type=leg.get_trip_type(),
            estimated_end_time=end_time,
            reservation_id=leg.reservation_id,
            customer_name=customer_name,
            status=leg.status or 'in-progress',
            has_flight=has_flight,
            flight_info=flight_info,
            revenue=leg.revenue_share,
            vehicle_type=str(leg_vtype) if leg_vtype else None,
            is_paid=(leg.reservation.payment_status == 'paid') if leg.reservation else True,
            passengers=int(leg.effective_passenger_count or 1),
            luggage=int(leg.effective_luggage_count or 0),
            luggage_type=leg.effective_luggage_type or "",
            carseats_short=_carseats_short,
            store_stop=bool(leg.reservation.store_stop) if (leg.reservation and leg.get_trip_type() == 'arrival') else False,
            pending_refund=bool(leg.reservation.has_pending_refund) if leg.reservation else False,
            is_vip=leg.is_vip,
            extra_stop_count=_legstop_count,
            secondary_flight_count=_secondary_flights,
            chain_clear_dt=chain_clear_dt(leg, target_date),
        )
        schedules[leg.driver.id].slots.append(slot)

    for schedule in schedules.values():
        schedule.slots.sort(key=lambda s: s.pickup_time)

    return schedules


def cluster_legs_by_time(legs, target_date: date, gap_minutes: int = 120) -> List[list]:
    """Group legs into natural time clusters using gap-based splitting.

    Sorts legs by pickup_time and starts a new cluster when the gap
    between consecutive legs exceeds gap_minutes.

    Returns list of clusters, each a list of legs sorted by pickup_time.
    """
    if not legs:
        return []
    sorted_by_time = sorted(legs, key=lambda l: l.pickup_time)
    clusters = [[sorted_by_time[0]]]
    for leg in sorted_by_time[1:]:
        prev = clusters[-1][-1]
        gap = (datetime.combine(target_date, leg.pickup_time) -
               datetime.combine(target_date, prev.pickup_time)).total_seconds() / 60
        if gap > gap_minutes:
            clusters.append([leg])
        else:
            clusters[-1].append(leg)
    return clusters


def assign_drivers_to_clusters(
    clusters: List[list],
    working: Dict[int, DriverDaySchedule],
    driver_hours: Dict[int, tuple],
    driver_vtypes: Dict[int, str],
    target_date: date,
) -> Dict[int, List[int]]:
    """Assign drivers to time clusters based on availability overlap and vehicle compatibility.

    Returns {driver_id: [cluster_indices]} — which clusters each driver
    should preferentially work in. Drivers may appear in multiple clusters
    if their availability spans them.
    """
    if not clusters or not working:
        return {}

    # Compute each cluster's hour span
    cluster_hours = []
    for cluster in clusters:
        start_h = cluster[0].pickup_time.hour
        last_end = max(estimate_job_end_time(l, target_date) for l in cluster)
        end_h = last_end.hour
        cluster_hours.append((start_h, end_h))

    # Count how many drivers can cover each cluster (cluster supply)
    cluster_supply = [0] * len(clusters)
    driver_eligible_clusters = {}  # {did: [(cluster_idx, compatible_job_count)]}

    for did in working:
        if driver_hours and did in driver_hours:
            dh_start, dh_end = driver_hours[did]
        else:
            dh_start, dh_end = 0, 23

        dvtype = driver_vtypes.get(did)
        compatible = get_compatible_vehicle_types(dvtype) if dvtype else None

        eligible = []
        for ci, (cs_h, ce_h) in enumerate(cluster_hours):
            # Driver must be available during the cluster's time span
            if dh_start > ce_h or dh_end < cs_h:
                continue
            # Count vehicle-compatible legs in this cluster
            match_count = 0
            for leg in clusters[ci]:
                leg_vtype = leg.effective_vehicle_type
                if not compatible or not leg_vtype or leg_vtype in compatible:
                    match_count += 1
            if match_count > 0:
                eligible.append((ci, match_count))
                cluster_supply[ci] += 1

        driver_eligible_clusters[did] = eligible

    # Greedy assignment: for each cluster (prioritizing scarce ones),
    # assign the best-fit drivers. Each driver gets their top cluster(s).
    # Sort clusters by supply (scarce first)
    cluster_order = sorted(range(len(clusters)), key=lambda ci: cluster_supply[ci])

    result = {did: [] for did in working}
    driver_assigned_count = {did: 0 for did in working}

    for ci in cluster_order:
        demand = len(clusters[ci])
        # Estimate how many drivers we need for this cluster
        # (~3-4 jobs per driver is a reasonable target)
        needed = max(1, (demand + 2) // 3)

        # Rank drivers for this cluster: prefer those with fewer assignments
        # and more compatible jobs
        candidates = []
        for did in working:
            for eci, match_count in driver_eligible_clusters.get(did, []):
                if eci == ci:
                    candidates.append((did, match_count, driver_assigned_count[did]))
                    break

        # Sort: fewer existing assignments first, then more compatible jobs
        candidates.sort(key=lambda x: (x[2], -x[1]))

        assigned = 0
        for did, _mc, _ac in candidates:
            if assigned >= needed:
                break
            if ci not in result[did]:
                result[did].append(ci)
                driver_assigned_count[did] += 1
                assigned += 1

    return result


def suggest_assignments_clustered(
    unassigned_legs,
    inhouse_schedules: Dict[int, DriverDaySchedule],
    target_date: date,
    driver_hours: Dict[int, tuple] = None,
    driver_preferences: Dict[int, str] = None,
    driver_vtypes: Dict[int, str] = None,
    flexible_drivers: set = None,
    driver_max_hours: Dict[int, float] = None,
    sharer_partners: Dict[int, set] = None,
    prev_end_by_driver: Dict[int, datetime] = None,
) -> List[AssignmentSuggestion]:
    """Cluster-aware assignment wrapper.

    Groups legs into time clusters, assigns drivers to clusters for shift
    coherence, then runs the enhanced greedy suggest_assignments() with
    cluster hints so drivers are preferentially scored for jobs in their
    assigned time blocks.

    Falls back to standard mode for single-cluster days.
    """
    from dispatching.models import SchedulerSettings
    cfg = SchedulerSettings.get_settings()

    gap_minutes = getattr(cfg, 'cluster_gap_minutes', 120)
    clusters = cluster_legs_by_time(unassigned_legs, target_date, gap_minutes=gap_minutes)

    if len(clusters) <= 1:
        # Single cluster or empty: no shift modeling needed
        return suggest_assignments(
            unassigned_legs, inhouse_schedules, target_date,
            driver_hours=driver_hours, driver_preferences=driver_preferences,
            driver_vtypes=driver_vtypes, flexible_drivers=flexible_drivers,
            driver_max_hours=driver_max_hours, sharer_partners=sharer_partners,
            prev_end_by_driver=prev_end_by_driver,
        )

    if driver_vtypes is None:
        driver_vtypes = load_all_driver_vtypes(target_date)

    # Build working driver set (same filter as suggest_assignments)
    working_dids = {did for did in inhouse_schedules if did in driver_vtypes}

    cluster_hints = assign_drivers_to_clusters(
        clusters,
        {did: inhouse_schedules[did] for did in working_dids},
        driver_hours,
        driver_vtypes,
        target_date,
    )

    return suggest_assignments(
        unassigned_legs, inhouse_schedules, target_date,
        driver_hours=driver_hours, driver_preferences=driver_preferences,
        driver_vtypes=driver_vtypes, cluster_hints=cluster_hints,
        clusters=clusters, flexible_drivers=flexible_drivers,
        driver_max_hours=driver_max_hours, sharer_partners=sharer_partners,
        prev_end_by_driver=prev_end_by_driver,
    )


def suggest_assignments(
    unassigned_legs,
    inhouse_schedules: Dict[int, DriverDaySchedule],
    target_date: date,
    driver_hours: Dict[int, tuple] = None,
    driver_preferences: Dict[int, str] = None,
    driver_vtypes: Dict[int, str] = None,
    cluster_hints: Dict[int, List[int]] = None,
    clusters: List[list] = None,
    flexible_drivers: set = None,
    driver_max_hours: Dict[int, float] = None,
    sharer_partners: Dict[int, set] = None,
    prev_end_by_driver: Dict[int, datetime] = None,
) -> List[AssignmentSuggestion]:
    """
    Greedy algorithm: assign unassigned legs to best-fit in-house drivers.
    Legs that don't fit any in-house driver are marked for affiliate.
    Vehicle-aware: only assigns legs that match the driver's vehicle tier.

    Optional cluster_hints: {driver_id: [cluster_indices]} from
    suggest_assignments_clustered(). When provided, drivers get a shift
    coherence bonus for jobs in their assigned clusters.

    Optional flexible_drivers: set of driver IDs that skip the hard time
    window filter. Their schedules are kept compact via scoring penalties
    (idle gap, span) rather than hard cutoffs.
    """
    from dispatching.analytics import categorize_location
    from dispatching.models import SchedulerSettings
    cfg = SchedulerSettings.get_settings()

    from dispatching import feasibility_guards as fg

    # Guard C — pass the REAL configured window as a fallback so flipping
    # USE_STUB_WINDOWS=False switches to live windows instead of silently disabling the guard.
    def _configured_window(did):
        if driver_hours and did in driver_hours:
            s, e = driver_hours[did]
            return {"start": s, "end": e,
                    "max_hours": (driver_max_hours or {}).get(did),
                    "flexible": bool(flexible_drivers and did in flexible_drivers)}
        return None
    _driver_windows = {did: fg.get_effective_window(did, configured=_configured_window(did))
                       for did in inhouse_schedules}

    suggestions = []

    # Sort legs strategically: returns/cruise BEFORE arrivals within each hour.
    # Tunable via SchedulerSettings (founder-brain C3); defaults return:0 cruise:1
    # other:2 arrival:3.
    _TYPE_PRIORITY = {
        'return': getattr(cfg, 'type_priority_return', 0),
        'cruise': getattr(cfg, 'type_priority_cruise', 1),
        'other': getattr(cfg, 'type_priority_other', 2),
        'arrival': getattr(cfg, 'type_priority_arrival', 3),
    }
    _TYPE_PRIORITY_DEFAULT = _TYPE_PRIORITY['other']

    # Founder value per leg (R3/R4): orders legs WITHIN each (pass, hour, type) bucket —
    # the higher booked class first, then revenue, then pax — so when two jobs tie on
    # (hour, type) the Van(14 Pax)-class booking deterministically reaches the V14 driver
    # before the Van-class one (M5), and the 3-pax towncar beats the 2-pax (R4 tiebreak).
    _value_map = {leg.id: leg_value(leg) for leg in unassigned_legs}

    def _assignment_sort_key(leg):
        trip_type = leg.get_trip_type()
        return (leg.pickup_time.hour, _TYPE_PRIORITY.get(trip_type, _TYPE_PRIORITY_DEFAULT),
                -_value_map[leg.id], leg.pickup_time)

    sorted_legs = sorted(unassigned_legs, key=_assignment_sort_key)

    # Pre-load vehicle assignments for all drivers on this date (single query)
    if driver_vtypes is None:
        driver_vtypes = load_all_driver_vtypes(target_date)

    # Work on copies so we can simulate assignments
    working = {}
    for did, sched in inhouse_schedules.items():
        # Skip drivers with no vehicle assignment for this date
        if did not in driver_vtypes:
            continue
        working[did] = DriverDaySchedule(
            driver_id=sched.driver_id,
            driver_name=sched.driver_name,
            driver_type=sched.driver_type,
            slots=list(sched.slots),
        )

    # Pre-compute scarcity: how many eligible drivers per leg
    scarcity_map = compute_leg_scarcity(sorted_legs, driver_vtypes)

    # Pre-compute chain opportunities
    chain_map = {}
    grace = cfg.arrival_grace_minutes
    for leg in sorted_legs:
        dropoff_cat = categorize_location(leg.dropoff_location)
        leg_end_est = estimate_job_end_time(leg, target_date)
        chain_count = 0
        for other in sorted_legs:
            if other.id == leg.id:
                continue
            other_pickup_cat = categorize_location(other.pickup_location)
            if other_pickup_cat == dropoff_cat:
                drive_between = 0
            else:
                drive_between = DRIVE_TIME_ESTIMATES.get(
                    (dropoff_cat, other_pickup_cat), DEFAULT_DRIVE_TIME
                )
                if drive_between > cfg.chain_drive_threshold:
                    continue
            other_pickup_dt = datetime.combine(target_date, other.pickup_time)
            # Airport arrivals: flight time != pickup time, pax still deplaning
            other_is_arrival = (
                other.get_trip_type() == 'arrival'
                and other_pickup_cat in ('MCO Terminal', 'SFB Terminal')
            )
            effective_pickup = other_pickup_dt + timedelta(minutes=grace) if other_is_arrival else other_pickup_dt
            gap_minutes = (effective_pickup - leg_end_est).total_seconds() / 60
            if cfg.chain_time_min <= gap_minutes <= cfg.chain_time_max:
                chain_count += 1
        chain_map[leg.id] = chain_count

    # Pre-compute vehicle reservation: for each driver, count upcoming scarce
    # jobs that MATCH their exact vehicle type. If they have matching jobs
    # waiting, penalize assigning them to a different vehicle type.
    #
    # IMPORTANT: We use "exact type scarcity" here, NOT the general scarcity_map.
    # scarcity_map counts all COMPATIBLE drivers (e.g., Van(14 Pax) can do Van jobs),
    # but for reservation we need to know how many drivers have the EXACT vehicle
    # type. Example: 1 Van + 2 Van(14 Pax) → scarcity_map says Van jobs have 3
    # eligible drivers, but exact_type_count says only 1 driver IS a Van. The Van
    # driver should be saved for Van jobs even though Van(14 Pax) drivers COULD help.
    #
    # Pre-count: how many drivers have each exact vehicle type
    exact_type_driver_counts = {}
    for dvtype in driver_vtypes.values():
        if dvtype:
            exact_type_driver_counts[dvtype] = exact_type_driver_counts.get(dvtype, 0) + 1

    driver_reserved_count = {}
    for did, dvtype in driver_vtypes.items():
        if not dvtype:
            continue
        count = 0
        for leg_check in sorted_legs:
            leg_check_vtype = leg_check.effective_vehicle_type
            if leg_check_vtype and str(leg_check_vtype) == dvtype:
                # This job matches the driver's exact vehicle type.
                # Use exact-type driver count (not general scarcity) to decide
                # if this job is rare enough to warrant saving this driver.
                exact_drivers = exact_type_driver_counts.get(dvtype, len(working))
                if exact_drivers <= cfg.reserve_max_scarcity:
                    count += 1
        driver_reserved_count[did] = count

    # ── Pre-compute time scarcity: demand/supply ratio per hour ────
    # Hours with more legs than available drivers are "time-scarce" and
    # should be processed before slack hours to prevent early-bird drivers
    # from being wasted on afternoon work.
    _hour_demand = {}
    for _leg in sorted_legs:
        h = _leg.pickup_time.hour
        _hour_demand[h] = _hour_demand.get(h, 0) + 1

    _hour_supply = {}
    for _did in working:
        if driver_hours and _did in driver_hours:
            _dh_start, _dh_end = driver_hours[_did]
        else:
            _dh_start, _dh_end = 0, 23
        for h in range(_dh_start, _dh_end + 1):
            _hour_supply[h] = _hour_supply.get(h, 0) + 1

    time_scarcity_map = {}
    for _leg in sorted_legs:
        h = _leg.pickup_time.hour
        demand = _hour_demand.get(h, 0)
        supply = max(_hour_supply.get(h, 1), 1)
        time_scarcity_map[_leg.id] = demand / supply

    # ── Three-pass processing order ───────────────────────────────────
    # Pass 0: Legs whose vehicle type is TRULY scarce — few exact-type
    #   drivers AND few total eligible drivers. This ensures specialized
    #   drivers (e.g., the only Van driver) get their matching jobs BEFORE
    #   being consumed as fallback for general jobs.
    #
    #   Both conditions must be met:
    #   a) Exact-type driver count ≤ reserve_max_scarcity (few drivers ARE this type)
    #   b) Total eligible drivers ≤ half the fleet (few drivers CAN do this type)
    #
    # Pass 1: Time-scarce legs (demand/supply > 1.5). These hours have
    #   more jobs than comfortably available drivers, so assigning them
    #   first prevents constrained drivers from being consumed by slack hours.
    #
    # Pass 2: Everything else (types with many eligible drivers, slack hours).
    #
    # Within each pass, the original sort order is preserved:
    #   (hour, type_priority [returns→cruise→other→arrivals], pickup_time)
    _TYPE_PRIORITY_REF = _TYPE_PRIORITY
    half_fleet = max(len(working) // 2, 3)

    def _multi_pass_sort_key(leg):
        trip_type = leg.get_trip_type()
        leg_vtype = leg.effective_vehicle_type
        pass_priority = 2  # Pass 2 (normal)
        # Check vehicle scarcity (Pass 0)
        if leg_vtype:
            exact_count = exact_type_driver_counts.get(str(leg_vtype), 0)
            eligible = scarcity_map.get(leg.id, len(working))
            if 0 < exact_count <= cfg.reserve_max_scarcity and eligible <= half_fleet:
                pass_priority = 0  # Pass 0 (vehicle-scarce)
        # Check time scarcity (Pass 1) — only if not already Pass 0
        if pass_priority == 2 and time_scarcity_map.get(leg.id, 0) > 1.5:
            pass_priority = 1  # Pass 1 (time-scarce)
        return (pass_priority, leg.pickup_time.hour,
                _TYPE_PRIORITY_REF.get(trip_type, _TYPE_PRIORITY_DEFAULT),
                -_value_map[leg.id], leg.pickup_time)

    sorted_legs = sorted(sorted_legs, key=_multi_pass_sort_key)

    # Pre-compute leg-to-cluster mapping for shift coherence scoring
    _leg_cluster_map = {}  # {leg_id: cluster_index}
    if cluster_hints and clusters:
        for ci, cluster in enumerate(clusters):
            for cl_leg in cluster:
                _leg_cluster_map[cl_leg.id] = ci

    # ── Class-match guard (R3 upward) support structures ──
    # Per exact class: processing-order indices of legs BOOKED in that class, so a
    # higher-class driver candidate can ask "does one of MY OWN class's still-pending
    # jobs conflict with this lower-class leg?". `_consumed_leg_ids` tracks legs already
    # decided (assigned or farmed) as the loop advances.
    _legs_idx_by_class = {}
    for _i, _lg in enumerate(sorted_legs):
        _lv = _lg.effective_vehicle_type
        if _lv:
            _legs_idx_by_class.setdefault(str(_lv), []).append(_i)
    _consumed_leg_ids = set()
    _ijb = cfg.inter_job_buffer
    _grace = cfg.arrival_grace_minutes

    def _within_modal_hours(did, pickup_time):
        """Greedy pickup-hour pre-filter (same semantics as the main candidate loop)."""
        if not (driver_hours and did in driver_hours):
            return True
        if flexible_drivers and did in flexible_drivers:
            return True
        dh_start, dh_end = driver_hours[did]
        return time(dh_start, 0) <= pickup_time <= time(dh_end, 59)

    def _class_guard_blocks(did, driver_vtype, sched, leg, leg_idx):
        """True when giving `leg` (a LOWER class than this driver's) to him would push a
        pending leg of HIS exact class off his board — the class-C leg wins,
        UNCONDITIONALLY (founder R3). No "someone else can cover it" escape: that test
        is optimistic at decision time (every other class-C driver still looks free,
        each gets released into lower-class work in turn, and by the class-C leg's own
        turn nobody can reach it — exactly how the 9:15 V14 port cruise stranded on
        6/14). A driver who cannot serve the class-C leg anyway is not barred."""
        for xi in _legs_idx_by_class.get(driver_vtype, ()):
            if xi <= leg_idx:
                continue
            X = sorted_legs[xi]
            if X.id in _consumed_leg_ids or X.id == leg.id:
                continue
            if not _within_modal_hours(did, X.pickup_time):
                continue
            feas_now = check_feasibility(sched, X, target_date, inter_job_buffer=_ijb,
                                         arrival_grace=_grace, driver_window=_driver_windows.get(did))
            if not feas_now.feasible:
                continue   # X doesn't fit this driver anyway — `leg` isn't what blocks it
            # Would X still fit once `leg` is on this driver's board?
            sim = DriverDaySchedule(
                driver_id=sched.driver_id, driver_name=sched.driver_name,
                driver_type=sched.driver_type,
                slots=sorted(sched.slots + [_make_sim_slot(leg, target_date)],
                             key=lambda s: s.pickup_time))
            feas_with = check_feasibility(sim, X, target_date, inter_job_buffer=_ijb,
                                          arrival_grace=_grace, driver_window=_driver_windows.get(did))
            if not feas_with.feasible:
                return True   # `leg` would push the class-C job off this driver
        return False

    _value_weight = getattr(cfg, 'auto_assign_value_weight', 0)

    # Rest Advisor: soft overnight-rest deficit penalty, charged once per candidate when
    # the leg would become that driver's FIRST pickup. Off unless a prev-day end map was
    # threaded in AND the gap/penalty knobs are both > 0 (gap 0 disables the feature).
    _rest_min_gap_h = (getattr(cfg, 'rest_min_gap_minutes', 0) or 0) / 60.0
    _rest_pen = getattr(cfg, 'rest_penalty_per_hour', 0) or 0
    _rest_on = bool(prev_end_by_driver) and _rest_min_gap_h > 0 and _rest_pen > 0

    for _leg_idx, leg in enumerate(sorted_legs):
        best_id = None
        best_score = float('-inf')
        best_feasibility = None
        # Class-match-first band (R3 downward): a feasible EXACT-class driver beats any
        # higher-class driver, which stays available as fallback.
        best_exact_id = None
        best_exact_score = float('-inf')
        best_exact_feasibility = None
        # Fallback: reserved-mismatch drivers (only used if no non-reserved driver fits)
        best_reserved_id = None
        best_reserved_score = float('-inf')
        best_reserved_feasibility = None
        # Collect all scored candidates for alternatives
        all_candidates = []
        pickup_cat = categorize_location(leg.pickup_location)
        leg_vtype = leg.effective_vehicle_type
        eligible_drivers = scarcity_map.get(leg.id, len(working))

        for did, sched in working.items():
            # Per-driver time window check (flexible drivers skip this)
            if driver_hours and did in driver_hours:
                if not (flexible_drivers and did in flexible_drivers):
                    dh_start, dh_end = driver_hours[did]
                    if leg.pickup_time < time(dh_start, 0) or leg.pickup_time > time(dh_end, 59):
                        continue

            # Max hours enforcement: skip driver if already at or over their limit
            if driver_max_hours and did in driver_max_hours:
                if sched.slots:
                    first_pickup_dt = datetime.combine(target_date, min(s.pickup_time for s in sched.slots))
                    last_end_dt = max(s.estimated_end_time for s in sched.slots)
                    span_hours = (last_end_dt - first_pickup_dt).total_seconds() / 3600
                    if span_hours >= driver_max_hours[did]:
                        continue

            # Vehicle compatibility check
            driver_vtype = driver_vtypes.get(did)
            if driver_vtype and leg_vtype:
                compatible = get_compatible_vehicle_types(driver_vtype)
                if leg_vtype not in compatible:
                    continue

            feas = check_feasibility(sched, leg, target_date, inter_job_buffer=cfg.inter_job_buffer, arrival_grace=cfg.arrival_grace_minutes,
                                     driver_window=_driver_windows.get(did))
            if not feas.feasible:
                continue

            # Shared-car occupancy gate: never give a sharer a job overlapping his
            # partner's jobs (one physical unit; planned windows alone are not airtight).
            if sharers_conflict(leg, did, sharer_partners, working, target_date):
                continue

            # ── Class-match guard (R3 upward, unconditional) ──
            # Never let a higher-class driver take this lower-class leg when one of his
            # OWN class's pending jobs conflicts with it and nobody else could cover that
            # job. NOT gated by the Pass-0 scarcity rule (on 6/14 V14 wasn't "scarce" and
            # the sprinter job went unprotected — M5).
            if CLASS_MATCH_GUARD and driver_vtype and leg_vtype:
                _d_tier_g = get_vehicle_tier(driver_vtype)
                _l_tier_g = get_vehicle_tier(str(leg_vtype))
                if (_d_tier_g > _l_tier_g >= 0
                        and _class_guard_blocks(did, driver_vtype, sched, leg, _leg_idx)):
                    continue

            # Check if this driver is reserved for matching jobs but this
            # leg doesn't match their type. These drivers are HARD SKIPPED
            # and only used as fallback if no non-reserved driver is feasible.
            is_reserved_mismatch = False
            if driver_vtype and leg_vtype:
                d_tier = get_vehicle_tier(driver_vtype)
                l_tier = get_vehicle_tier(str(leg_vtype))
                if d_tier > l_tier and driver_reserved_count.get(did, 0) > 0:
                    is_reserved_mismatch = True

            score = 0
            buf = feas.buffer_minutes

            # Buffer quality
            if 20 <= buf <= 30:
                score += cfg.buffer_perfect
            elif 30 < buf <= 60:
                score += cfg.buffer_sweet_spot
            elif 10 <= buf < 20:
                score += cfg.buffer_tight
            elif 60 < buf <= 120:
                score += cfg.buffer_good
            elif buf > 120:
                score += cfg.buffer_loose
            else:
                score += cfg.buffer_risky

            # Vehicle tier preference
            if driver_vtype and leg_vtype:
                d_tier = get_vehicle_tier(driver_vtype)
                l_tier = get_vehicle_tier(leg_vtype)
                tier_diff = d_tier - l_tier
                if tier_diff == 0:
                    score += cfg.tier_exact
                elif tier_diff == 1:
                    score += cfg.tier_1_down
                elif tier_diff == 2:
                    score += cfg.tier_2_down
                else:
                    score += cfg.tier_4_down

            # Scarcity bonus
            if eligible_drivers <= 1:
                score += cfg.scarcity_1
            elif eligible_drivers == 2:
                score += cfg.scarcity_2
            elif eligible_drivers == 3:
                score += cfg.scarcity_3
            elif eligible_drivers == 4:
                score += cfg.scarcity_4

            # Location proximity
            if sched.slots:
                last = sched.slots[-1]
                if last.dropoff_category == pickup_cat:
                    score += cfg.loc_same_area
                else:
                    repo = DRIVE_TIME_ESTIMATES.get((last.dropoff_category, pickup_cat))
                    if repo and repo <= 15:
                        score += cfg.loc_close
            else:
                score += cfg.loc_first_job

            # Schedule flow
            leg_trip_type = leg.get_trip_type()
            if sched.slots:
                consecutive_arrivals = 0
                for slot in reversed(sched.slots):
                    if slot.trip_type == 'arrival':
                        consecutive_arrivals += 1
                    else:
                        break

                if leg_trip_type == 'arrival' and consecutive_arrivals >= 2:
                    score += cfg.flow_3rd_arrival  # negative value
                elif leg_trip_type == 'arrival' and consecutive_arrivals == 1:
                    score += cfg.flow_2nd_arrival  # negative value
                elif leg_trip_type in ('return', 'cruise') and consecutive_arrivals >= 1:
                    score += cfg.flow_break_bonus

            # In-house retention bonus
            if leg_trip_type in ('return', 'cruise'):
                score += cfg.retention_bonus

            # Founder value term (R1/R3): booked class › trip type › revenue › pax,
            # scaled so one class tier step ≈ 10·weight points. Constant across drivers
            # for a given leg, so it never distorts driver CHOICE — it makes leg value
            # visible in candidate scores and steers any cross-leg score comparison
            # (e.g. best-fit builders). Gated by auto_assign_value_weight (0 disables).
            if _value_weight:
                score += int(_value_map[leg.id] / 1000 * _value_weight)

            # Chain bonus
            chains = chain_map.get(leg.id, 0)
            if chains >= 3:
                score += cfg.chain_3_plus
            elif chains == 2:
                score += cfg.chain_2
            elif chains == 1:
                score += cfg.chain_1

            # Backward chain: does this driver's existing schedule chain INTO this job?
            backward_bonus = getattr(cfg, 'backward_chain_bonus', 40)
            if sched.slots and backward_bonus > 0:
                last_slot = sorted(sched.slots, key=lambda s: s.pickup_time)[-1]
                last_dropoff = last_slot.dropoff_category
                if last_dropoff == pickup_cat:
                    drive_to_pickup = 0
                else:
                    drive_to_pickup = DRIVE_TIME_ESTIMATES.get(
                        (last_dropoff, pickup_cat), DEFAULT_DRIVE_TIME
                    )
                if drive_to_pickup <= cfg.chain_drive_threshold:
                    gap_from_last = (datetime.combine(target_date, leg.pickup_time) - last_slot.estimated_end_time).total_seconds() / 60
                    if cfg.chain_time_min <= gap_from_last <= cfg.chain_time_max:
                        score += backward_bonus

            # Shift coherence: bonus when job is in driver's assigned cluster
            if cluster_hints and _leg_cluster_map:
                leg_cluster = _leg_cluster_map.get(leg.id)
                driver_clusters = cluster_hints.get(did, [])
                if leg_cluster is not None and leg_cluster in driver_clusters:
                    score += getattr(cfg, 'shift_coherence_bonus', 50)

            # Load balance (exponential: heavier penalty as jobs accumulate)
            n_jobs = len(sched.slots)
            if n_jobs > 0:
                score -= int(cfg.load_balance_multiplier * (n_jobs ** getattr(cfg, 'load_balance_exponent', 1.5)))

            # Idle gap penalty: penalize large gaps between consecutive jobs
            idle_threshold = getattr(cfg, 'idle_gap_threshold', 120)
            idle_penalty_rate = getattr(cfg, 'idle_gap_penalty_per_min', 2)
            if sched.slots and idle_threshold > 0:
                new_pickup_dt_gap = datetime.combine(target_date, leg.pickup_time)
                new_end_dt_gap = estimate_job_end_time(leg, target_date)
                sorted_slots_gap = sorted(sched.slots, key=lambda s: s.pickup_time)
                # Find insertion point
                insert_idx = len(sorted_slots_gap)
                for idx_g, slot_g in enumerate(sorted_slots_gap):
                    if datetime.combine(target_date, slot_g.pickup_time) > new_pickup_dt_gap:
                        insert_idx = idx_g
                        break
                # Check gap from preceding slot to new leg
                if insert_idx > 0:
                    prev_slot = sorted_slots_gap[insert_idx - 1]
                    gap_before = (new_pickup_dt_gap - prev_slot.estimated_end_time).total_seconds() / 60
                    if gap_before > idle_threshold:
                        score -= int((gap_before - idle_threshold) * idle_penalty_rate)
                # Check gap from new leg to following slot
                if insert_idx < len(sorted_slots_gap):
                    next_slot = sorted_slots_gap[insert_idx]
                    next_pickup_dt = datetime.combine(target_date, next_slot.pickup_time)
                    gap_after = (next_pickup_dt - new_end_dt_gap).total_seconds() / 60
                    if gap_after > idle_threshold:
                        score -= int((gap_after - idle_threshold) * idle_penalty_rate)

            # Schedule span penalty: penalize overly long driver days.
            # Span Governor (fg.ENFORCE_SPAN_CAPS + SPAN_SOFT_PRICING): marginal effective-span
            # pricing — charge only the day-stretch THIS leg adds, progressively (free under
            # 12h effective, mild to 13.5h, steep beyond), so late legs prefer already-late or
            # fresh drivers and the old 30/hr flat penalty (routinely outweighed by chain/
            # scarcity bonuses) stops minting 15-18h days. Legacy block kept behind the flag.
            if fg.ENFORCE_SPAN_CAPS and fg.SPAN_SOFT_PRICING:
                if sched.slots:
                    new_pickup_dt_span = datetime.combine(target_date, leg.pickup_time)
                    new_end_dt_span = estimate_job_end_time(leg, target_date)
                    score -= marginal_span_penalty(sched.slots, target_date,
                                                   new_pickup_dt_span, new_end_dt_span)
            else:
                span_threshold = getattr(cfg, 'span_threshold_hours', 13)
                span_penalty_rate = getattr(cfg, 'span_penalty_per_hour', 30)
                if sched.slots and span_threshold > 0:
                    sorted_slots_span = sorted(sched.slots, key=lambda s: s.pickup_time)
                    first_start = datetime.combine(target_date, sorted_slots_span[0].pickup_time)
                    last_end = sorted_slots_span[-1].estimated_end_time
                    new_pickup_dt_span = datetime.combine(target_date, leg.pickup_time)
                    new_end_dt_span = estimate_job_end_time(leg, target_date)
                    effective_start = min(first_start, new_pickup_dt_span)
                    effective_end = max(last_end, new_end_dt_span)
                    span_hours = (effective_end - effective_start).total_seconds() / 3600
                    if span_hours > span_threshold:
                        score -= int((span_hours - span_threshold) * span_penalty_rate)

            # Rest Advisor (overnight rest): soft penalty when THIS leg would become the
            # driver's FIRST pickup and he didn't get the minimum rest since yesterday's
            # last drop-off. MARGINAL (first-pickup only — mid-day legs never charged) and
            # SOFT (a score nudge, never a hard skip): a tired driver who is the ONLY option
            # still covers the leg; he just loses the early-morning TIE to a rested peer.
            if _rest_on and (not sched.slots
                             or leg.pickup_time < min(s.pickup_time for s in sched.slots)):
                _prev_end = prev_end_by_driver.get(did)
                if _prev_end is not None:
                    _rest_h = (datetime.combine(target_date, leg.pickup_time)
                               - _prev_end).total_seconds() / 3600
                    _deficit = _rest_min_gap_h - _rest_h
                    if _deficit > 0:
                        score -= int(_deficit * _rest_pen)

            # Per-driver trip type preference
            if driver_preferences and did in driver_preferences:
                pref_str = driver_preferences[did]
                parts = pref_str.split('_', 1)
                p_mode, p_type = (parts[0], parts[1]) if len(parts) == 2 else ('prefer', pref_str)
                if p_mode == 'only' and leg_trip_type != p_type:
                    continue  # hard skip — driver only wants this type
                elif p_mode == 'heavy' and leg_trip_type == p_type:
                    score += cfg.trip_pref_match * 2
                elif p_mode == 'heavy' and leg_trip_type != p_type:
                    score += cfg.trip_pref_mismatch * 2
                elif p_type and leg_trip_type == p_type:
                    score += cfg.trip_pref_match
                elif p_type and leg_trip_type != p_type:
                    score += cfg.trip_pref_mismatch

            if is_reserved_mismatch:
                # Reserved driver on mismatched job — track as fallback only.
                # Apply penalty so the best fallback is still ranked sensibly.
                score += cfg.reserve_penalty
                if score > best_reserved_score:
                    best_reserved_score = score
                    best_reserved_id = did
                    best_reserved_feasibility = feas
            else:
                # Class-match-first band (R3 downward): an EXACT-class candidate
                # unconditionally outranks higher-class ones (tracked separately).
                if (CLASS_MATCH_FIRST and driver_vtype and leg_vtype
                        and str(leg_vtype) == driver_vtype and score > best_exact_score):
                    best_exact_score = score
                    best_exact_id = did
                    best_exact_feasibility = feas
                if score > best_score:
                    best_score = score
                    best_id = did
                    best_feasibility = feas

            # Track all candidates for alternatives list
            all_candidates.append((did, score, feas, is_reserved_mismatch))

        # Class-match-first: when any exact-class driver fits, he wins outright; the
        # higher-class candidates remain visible as alternatives.
        if best_exact_id:
            best_id = best_exact_id
            best_score = best_exact_score
            best_feasibility = best_exact_feasibility

        # Fallback: if no non-reserved driver fits, use the best reserved one.
        # This prevents jobs from going unassigned when only reserved drivers
        # are available, while still preferring non-reserved drivers first.
        if not best_id and best_reserved_id:
            best_id = best_reserved_id
            best_score = best_reserved_score
            best_feasibility = best_reserved_feasibility

        if best_id:
            same_area = ""
            if working[best_id].slots and working[best_id].slots[-1].dropoff_category == pickup_cat:
                same_area = " (same area)"

            # Build alternatives list: top candidates excluding the best pick
            alternatives = []
            sorted_candidates = sorted(all_candidates, key=lambda c: c[1], reverse=True)
            for cand_id, cand_score, cand_feas, cand_reserved in sorted_candidates:
                if cand_id == best_id:
                    continue
                if len(alternatives) >= 4:
                    break
                cand_area = ""
                if working[cand_id].slots and working[cand_id].slots[-1].dropoff_category == pickup_cat:
                    cand_area = " (same area)"
                cand_reason = cand_feas.reason + cand_area if cand_feas.buffer_minutes < 999 else f"Available{cand_area}"
                if cand_reserved:
                    cand_reason += " [reserved]"
                alternatives.append(AlternativeDriver(
                    driver_id=cand_id,
                    driver_name=working[cand_id].driver_name,
                    score=cand_score,
                    feasibility=cand_feas,
                    reason=cand_reason,
                ))

            suggestion = AssignmentSuggestion(
                leg_id=leg.id,
                suggested_driver_id=best_id,
                suggested_driver_name=working[best_id].driver_name,
                feasibility=best_feasibility,
                reason=best_feasibility.reason + same_area if best_feasibility.buffer_minutes < 999 else f"Available{same_area}",
                priority=1,
                alternatives=alternatives,
            )

            # Simulate the assignment
            sim_slot = _make_sim_slot(leg, target_date)
            working[best_id].slots.append(sim_slot)
            working[best_id].slots.sort(key=lambda s: s.pickup_time)

            # Update reservation counts: this leg is now assigned, so
            # decrement for all drivers whose exact type matched this leg
            if leg_vtype:
                assigned_vtype = str(leg_vtype)
                exact_drivers = exact_type_driver_counts.get(assigned_vtype, len(working))
                if exact_drivers <= cfg.reserve_max_scarcity:
                    for did_r, dvtype_r in driver_vtypes.items():
                        if dvtype_r == assigned_vtype and driver_reserved_count.get(did_r, 0) > 0:
                            driver_reserved_count[did_r] -= 1
        else:
            suggestion = AssignmentSuggestion(
                leg_id=leg.id,
                suggested_driver_id=None,
                suggested_driver_name=None,
                feasibility=None,
                reason="No in-house driver available",
                priority=0,
            )

        _consumed_leg_ids.add(leg.id)   # decided (assigned or farmed) — class guard ignores it
        suggestions.append(suggestion)

    return suggestions


def recover_residuals_via_swaps(final_assignments, candidate_leg_ids, legs_by_id,
                                drivers, drivers_by_id, target_date, dvtypes,
                                locked_leg_ids=None, driver_windows=None,
                                driver_hours=None, flexible_drivers=None,
                                sharer_partners=None):
    """Pre-farm swap pass. After the greedy build, try to pull each would-be-FARMED candidate
    leg back in-house by cascading existing assignments via find_swaps (which the single-leg
    greedy can't do). Read-only wrt the DB — operates on an in-memory assignment map.

    Args:
        final_assignments: {leg_id: driver_id} the build produced (auto + manual). Mutated + returned.
        candidate_leg_ids: ids of legs eligible for in-house (the auto pool); any not in
            final_assignments is a farm residual we try to recover.
        legs_by_id / drivers_by_id: lookups. drivers: list of eligible Driver objects.
        dvtypes: {driver_id: vehicle_type} (load_all_driver_vtypes).
        locked_leg_ids: legs that must NOT be relocated (manual / pre-existing assignments);
            any swap solution that would move one is rejected.
        driver_windows: optional {driver_id: window_dict} forwarded to find_swaps — the
            auto-assign view passes its CAP-CLAMPED, modal-aware windows so cascade
            destinations respect the duty-span caps. MUST contain an entry for EVERY working
            driver (find_swaps restricts its receiver pool to the dict's keys).
        driver_hours / flexible_drivers: the modal's hard Start/End map + flexible set. When
            given, every move in an accepted solution is post-validated against the receiving
            driver's modal pickup-hour window (greedy parity) — find_swaps' window_check uses
            stub start/end under USE_STUB_WINDOWS, so without this a cascade could land a leg
            on a driver outside the hours the dispatcher typed.

    Returns (final_assignments, recovered_leg_ids).
    """
    if not AUTO_PREFARM_SWAP_PASS:
        return final_assignments, []
    from dispatching.swap_optimizer import find_swaps

    locked = set(locked_leg_ids or [])
    # Full in-house board = the proposed assignments PLUS any pre-existing assignment already on
    # a leg (so driver occupancy is correct). Pre-existing ones are implicitly locked too.
    def board_map():
        m = dict(final_assignments)
        for lid, leg in legs_by_id.items():
            if lid not in m and getattr(leg, "driver_id", None):
                m[lid] = leg.driver_id
                locked.add(lid)
        return m

    farmed = [legs_by_id[lid] for lid in candidate_leg_ids
              if lid not in final_assignments and lid in legs_by_id]
    farmed.sort(key=lambda l: -float(getattr(l, "revenue_share", 0) or 0))  # highest value first
    recovered = []

    def build_and_run(target):
        # Temporarily reflect the current board onto the leg objects, build schedules, run find_swaps.
        saved, ih = {}, []
        for lid, did in board_map().items():
            leg = legs_by_id.get(lid); dr = drivers_by_id.get(did)
            if leg is None or dr is None:
                continue
            saved[lid] = (leg.driver, getattr(leg, "driver_id", None))
            leg.driver = dr; leg.driver_id = did
            ih.append(leg)
        sch = build_driver_schedules(ih, drivers, target_date)
        for s in sch.values():
            s._date = target_date
        try:
            return find_swaps(target, sch, {l.id: l for l in ih}, dvtypes, target_date,
                              driver_windows=driver_windows,
                              sharer_partners=sharer_partners)
        finally:
            for lid, (drv, drvid) in saved.items():
                legs_by_id[lid].driver = drv; legs_by_id[lid].driver_id = drvid

    def _within_modal_hours(leg_id, did):
        """Greedy-parity modal window check (scheduler pickup-hour pre-filter semantics)."""
        if not driver_hours or did not in driver_hours:
            return True
        if flexible_drivers and did in flexible_drivers:
            return True
        leg = legs_by_id.get(leg_id)
        if leg is None:
            return True
        dh_start, dh_end = driver_hours[did]
        return time(dh_start, 0) <= leg.pickup_time <= time(dh_end, 59)

    for target in farmed:
        try:
            res = build_and_run(target)
        except Exception:
            res = None
        if not (res and getattr(res, "solutions", None)):
            continue
        sol = res.solutions[0]
        if {mv.leg_id for mv in sol.moves} & locked:
            continue  # never disturb a locked (manual/pre-existing) assignment
        if not all(_within_modal_hours(mv.leg_id, mv.to_driver_id) for mv in sol.moves):
            continue  # a cascade destination falls outside the dispatcher's typed hours
        if not _within_modal_hours(target.id, sol.target_driver_id):
            continue
        if sharer_partners:
            # Shared-car post-validation: every move destination (and the rescued target)
            # must not overlap its partner's CURRENT jobs. Approximate (pre-cascade board)
            # but safe-conservative for the rare share days.
            saved2, ih2 = {}, []
            for lid2, did2 in board_map().items():
                lg2 = legs_by_id.get(lid2); dr2 = drivers_by_id.get(did2)
                if lg2 is None or dr2 is None:
                    continue
                saved2[lid2] = (lg2.driver, getattr(lg2, "driver_id", None))
                lg2.driver = dr2; lg2.driver_id = did2
                ih2.append(lg2)
            try:
                cur_board = build_driver_schedules(ih2, drivers, target_date)
            finally:
                for lid2, (drv2, drvid2) in saved2.items():
                    legs_by_id[lid2].driver = drv2; legs_by_id[lid2].driver_id = drvid2
            moves_ok = all(
                not sharers_conflict(legs_by_id[mv.leg_id], mv.to_driver_id,
                                     sharer_partners, cur_board, target_date)
                for mv in sol.moves if mv.leg_id in legs_by_id)
            if not moves_ok or sharers_conflict(target, sol.target_driver_id,
                                                sharer_partners, cur_board, target_date):
                continue
        for mv in sol.moves:
            final_assignments[mv.leg_id] = mv.to_driver_id
        final_assignments[target.id] = sol.target_driver_id
        recovered.append(target.id)

    return final_assignments, recovered


def _chain_ok(driver_schedule, target_date, driver_window=None):
    """End-to-end revalidation of a (simulated) day: every consecutive turnaround must be
    feasible under the same Guard-B math check_feasibility uses (incl. the arrival
    static floor), and every slot must respect the driver window + max-hours span.
    Removing a leg only relaxes a chain, but the evict-to-farm pass also INSERTS one —
    so the full day is replayed rather than trusting a single pairwise check."""
    from dispatching import feasibility_guards as fg
    slots = sorted(driver_schedule.slots, key=lambda s: s.pickup_time)
    for prev, nxt in zip(slots, slots[1:]):
        prev_end = _slot_chain_end(prev, target_date)
        nxt_is_arr = fg.is_airport_arrival(nxt.trip_type, nxt.pickup_category)
        if CHAIN_STATIC_TIMING:
            repo = chain_repo_minutes(prev.dropoff_location, nxt.pickup_location,
                                      prev.dropoff_category, nxt.pickup_category)
        else:
            repo = resolve_drive_minutes(prev.dropoff_location, nxt.pickup_location,
                                         prev.dropoff_category, nxt.pickup_category)
        req = fg.required_turnaround(repo, nxt_is_arr,
                                     same_terminal=(prev.dropoff_category == nxt.pickup_category))
        if datetime.combine(target_date, nxt.pickup_time) < prev_end + timedelta(minutes=req):
            return False
    if driver_window and slots:
        first_pickup = datetime.combine(target_date, slots[0].pickup_time)
        last_end = max(s.estimated_end_time for s in slots)
        span = (last_end - first_pickup).total_seconds() / 3600
        for s in slots:
            ok, _ = fg.window_check(driver_window, s.pickup_time, s.estimated_end_time,
                                    span, target_date=target_date)
            if not ok:
                return False
    return True


def evict_to_farm_for_value(final_assignments, candidate_leg_ids, legs_by_id,
                            drivers, drivers_by_id, target_date, dvtypes,
                            locked_leg_ids=None, driver_windows=None,
                            driver_hours=None, flexible_drivers=None,
                            sharer_partners=None, free_insert_only=False):
    """Evict-to-farm value pass (founder brain, rules R1+R2 —
    docs/scheduler-automation/founder-brain-implementation.md).

    An assigned leg is not sacred: when demand beats driver supply, in-house drivers
    belong on DEPARTURES/returns (fixed pickup, ~30 driver-min, driver ends at the MCO
    demand hub) and ARRIVALS are the farm-out currency (flight-variable, ~75 driver-min,
    driver ends stranded at a resort; affiliates do MCO meet-and-greets fine, while a
    farmed fixed-time hotel pickup that no-shows means a missed flight).

    For each residual (would-be-farmed) leg U in DESCENDING founder value (leg_value),
    find a driver where U fits either directly (free insertion — the board changed since
    the greedy ran) or once exactly ONE assigned leg A is removed. An eviction is
    accepted only when ALL hold:
      * A is farmable: trip_type == 'arrival', never a true departure (farm-out
        optimizer parity via is_departure()), never locked (manual/seeded/pre-existing);
      * leg_value(U) − leg_value(A) ≥ SchedulerSettings.displacement_min_value_gain
        (1000 ≈ one trip-type step, 10000 ≈ one booked-class step);
      * the modified day re-passes the guards END TO END (every turnaround via the
        Guard-B math incl. the arrival static floor, plus window/max-hours), AND the
        greedy-parity gates hold (modal pickup-hour window, vehicle tier compatibility,
        shared-car occupancy).
    The evicted A returns to the residual pool — the span-rescue pass that runs directly
    after this one re-seats it anywhere it still fits, otherwise it farms exactly like
    the founder does by hand. Evictions are bounded by max_displacements_per_run and an
    evicted leg never re-enters as a target (no ping-pong). Deterministic; read-only wrt
    the DB. Returns (final_assignments, moves); each move carries a human-readable
    reason for the preview/provenance trail.

    free_insert_only=True restricts the pass to its free-insertion clause (no
    evictions) — used as a FINAL sweep after the trim/gap passes, whose relocations can
    open seats that did not exist when coverage was settled ("leave no leg farmed that
    fits the final board as-is" — the answer key's missed-free-insertion flaw).
    """
    if not AUTO_EVICT_TO_FARM_PASS:
        return final_assignments, []
    from dispatching.models import SchedulerSettings
    from dispatching.farmout_optimizer import is_departure  # lazy: farmout imports scheduler
    from dispatching import feasibility_guards as fg
    cfg = SchedulerSettings.get_settings()
    min_gain = float(getattr(cfg, 'displacement_min_value_gain', 500))
    max_moves = int(getattr(cfg, 'max_displacements_per_run', 10))
    ijb = cfg.inter_job_buffer
    grace = cfg.arrival_grace_minutes

    locked = set(locked_leg_ids or [])
    # Full board = proposed assignments PLUS pre-existing assignments (occupancy must be
    # real; pre-existing ones are implicitly locked — never evicted).
    board = dict(final_assignments)
    for lid, leg in legs_by_id.items():
        if lid not in board and getattr(leg, 'driver_id', None):
            board[lid] = leg.driver_id
            locked.add(lid)

    residual_ids = [lid for lid in candidate_leg_ids if lid not in board and lid in legs_by_id]
    if not residual_ids:
        return final_assignments, []
    residuals = sorted((legs_by_id[lid] for lid in residual_ids),
                       key=lambda l: (-leg_value(l), l.id))

    # Window context: prefer the caller's cap-clamped, modal-aware windows (the
    # auto-assign view passes them); else fall back to the saved-availability funnel
    # (gap-pass parity) so a programmatic caller still gets the duty-span caps.
    if driver_windows:
        windows = driver_windows
    else:
        windows = {}
        for dr in drivers:
            try:
                eff = dr.get_effective_availability(target_date)
                mh = eff.get("max_hours")
                cfgw = {"start": eff.get("start_hour"), "end": eff.get("end_hour"),
                        "max_hours": (float(mh) if mh else None),
                        "flexible": bool(eff.get("flexible"))}
            except Exception:
                cfgw = None
            windows[dr.id] = fg.get_effective_window(dr.id, configured=cfgw)

    def build_schedules():
        """Reflect the current board onto the leg objects, build schedules, then restore."""
        saved, ih = {}, []
        for lid, did in board.items():
            leg = legs_by_id.get(lid); dr = drivers_by_id.get(did)
            if leg is None or dr is None:
                continue
            saved[lid] = (leg.driver, getattr(leg, 'driver_id', None))
            leg.driver = dr; leg.driver_id = did
            ih.append(leg)
        try:
            return build_driver_schedules(ih, drivers, target_date)
        finally:
            for lid, (drv, drvid) in saved.items():
                legs_by_id[lid].driver = drv; legs_by_id[lid].driver_id = drvid

    def _within_modal(did, pickup_time):
        if not (driver_hours and did in driver_hours):
            return True
        if flexible_drivers and did in flexible_drivers:
            return True
        dh_start, dh_end = driver_hours[did]
        return time(dh_start, 0) <= pickup_time <= time(dh_end, 59)

    def _desc(leg):
        vt = leg.effective_vehicle_type
        try:
            pax = int(getattr(leg, 'effective_passenger_count', 0) or 0)
        except (TypeError, ValueError):
            pax = 0
        return (f"leg {leg.id} ({leg.pickup_time.strftime('%I:%M %p').lstrip('0')} "
                f"{leg.get_trip_type()}, {pax} pax, {vt or 'any vehicle'})")

    moves = []
    evictions = 0
    evicted_ids = set()
    schedules = build_schedules()

    for U in residuals:
        if evictions >= max_moves:
            break
        if U.id in evicted_ids or U.id in board:
            continue
        u_val = leg_value(U)
        u_vtype = U.effective_vehicle_type
        best = None  # (sort_key, driver_id, evicted_leg_id|None, gain)
        for dr in sorted(drivers, key=lambda d: d.id):
            did = dr.id
            sched = schedules.get(did)
            if sched is None:
                continue
            # Greedy-parity hard gates: modal pickup-hour window, vehicle tier, sharers.
            if not _within_modal(did, U.pickup_time):
                continue
            dvt = dvtypes.get(did)
            if dvt and u_vtype and str(u_vtype) not in get_compatible_vehicle_types(dvt):
                continue
            if sharers_conflict(U, did, sharer_partners, schedules, target_date):
                continue
            # 1) Free insertion — U fits as-is (the board changed since the greedy ran).
            feas0 = check_feasibility(sched, U, target_date, inter_job_buffer=ijb,
                                      arrival_grace=grace, driver_window=windows.get(did))
            if feas0.feasible:
                key = (1, 0.0, feas0.buffer_minutes, -did, 0)  # band 1: free always wins
                if best is None or key > best[0]:
                    best = (key, did, None, 0.0)
                continue
            if free_insert_only:
                continue
            # 2) Eviction — U fits if exactly ONE farmable proposed arrival is removed.
            for slot in sorted(sched.slots, key=lambda s: s.pickup_time):
                a_id = slot.leg_id
                if a_id in locked or a_id not in final_assignments:
                    continue   # only engine-proposed legs from THIS run are evictable
                A = legs_by_id.get(a_id)
                if A is None:
                    continue
                if A.get_trip_type() != 'arrival':
                    continue   # arrivals are the farm currency (R1)
                if is_departure(A):
                    continue   # double guard: a true departure is NEVER farmed
                gain = u_val - leg_value(A)
                if gain < min_gain:
                    continue
                sim = DriverDaySchedule(
                    driver_id=sched.driver_id, driver_name=sched.driver_name,
                    driver_type=sched.driver_type,
                    slots=[s for s in sched.slots if s.leg_id != a_id])
                feas = check_feasibility(sim, U, target_date, inter_job_buffer=ijb,
                                         arrival_grace=grace, driver_window=windows.get(did))
                if not feas.feasible:
                    continue
                # End-to-end revalidation of the modified day (turnarounds + window/cap).
                sim.slots = sorted(sim.slots + [_make_sim_slot(U, target_date)],
                                   key=lambda s: s.pickup_time)
                if not _chain_ok(sim, target_date, windows.get(did)):
                    continue
                key = (0, gain, feas.buffer_minutes, -did, -a_id)
                if best is None or key > best[0]:
                    best = (key, did, a_id, gain)

        if best is None:
            continue
        _, did, evict_lid, gain = best
        dname = str(drivers_by_id.get(did, did))
        if evict_lid is not None:
            A = legs_by_id.get(evict_lid)
            final_assignments.pop(evict_lid, None)
            board.pop(evict_lid, None)
            evicted_ids.add(evict_lid)
            evictions += 1
            reason = (f"evicted {_desc(A)} from {dname} to the farm pool to cover "
                      f"{_desc(U)} (value gain +{gain:.0f}; an assigned arrival is the "
                      f"farm-out currency, the higher-value leg keeps the driver)")
            moves.append({"kind": "evict", "leg_id": U.id, "driver_id": did,
                          "evicted_leg_id": evict_lid, "value_gain": round(gain, 2),
                          "reason": reason})
        else:
            reason = f"free insertion: {_desc(U)} fits {dname} as-is on the settled board"
            moves.append({"kind": "free_insert", "leg_id": U.id, "driver_id": did,
                          "evicted_leg_id": None, "value_gain": None, "reason": reason})
        final_assignments[U.id] = did
        board[U.id] = did
        schedules = build_schedules()

    return final_assignments, moves


def rescue_span_blocked_residuals(final_assignments, candidate_leg_ids, legs_by_id,
                                  drivers, drivers_by_id, target_date, dvtypes,
                                  capped_windows, driver_hours=None, flexible_drivers=None,
                                  strict_cap_driver_ids=None, locked_leg_ids=None,
                                  sharer_partners=None):
    """Span-cap coverage rescue (Span Governor escalation step). For each residual leg the
    build + swap passes left unassigned, test every working driver twice:

      1. under his CAP-CLAMPED window — if feasible NOW (the board changed since the greedy
         ran), assign normally (no warning);
      2. under the same window with max_hours LIFTED — if feasible only then, the duty-span
         cap was the leg's sole blocker, so assign it anyway (priority #1: never lose an
         in-house job to the cap) and emit a RED warning naming the overage.

    Drivers in strict_cap_driver_ids (dispatcher explicitly TYPED a Max hrs in the modal)
    are never lifted — if only strict drivers could take the leg, it stays residual and a
    warning names the blocking cap ("the modal is authoritative").

    Greedy-parity candidate filters are replicated here because check_feasibility checks
    NEITHER of them: vehicle-tier compatibility and the modal pickup-hour window for
    non-flexible drivers. Deterministic: residuals by (-revenue, id); candidates by
    (lowest resulting raw span, highest buffer, driver id). Read-only wrt the DB.

    Returns (final_assignments, rescued_leg_ids, warnings) — warnings are dicts:
    {"kind": "rescued"|"strict_blocked", "leg_id", "driver_id"|None, "driver_name",
     "span_after", "cap_hours", "pickup"}.
    """
    if not SPAN_COVERAGE_RESCUE:
        return final_assignments, [], []
    from dispatching import feasibility_guards as fg
    if not fg.ENFORCE_SPAN_CAPS:
        return final_assignments, [], []

    strict = set(strict_cap_driver_ids or [])
    residuals = [legs_by_id[lid] for lid in candidate_leg_ids
                 if lid not in final_assignments and lid in legs_by_id]
    residuals.sort(key=lambda l: (-float(getattr(l, "revenue_share", 0) or 0), l.id))
    if not residuals:
        return final_assignments, [], []

    # Board = proposed assignments PLUS pre-existing assignments (occupancy must be real).
    board = dict(final_assignments)
    for lid, leg in legs_by_id.items():
        if lid not in board and getattr(leg, "driver_id", None):
            board[lid] = leg.driver_id

    def build_schedules():
        saved, ih = {}, []
        for lid, did in board.items():
            leg = legs_by_id.get(lid); dr = drivers_by_id.get(did)
            if leg is None or dr is None:
                continue
            saved[lid] = (leg.driver, getattr(leg, "driver_id", None))
            leg.driver = dr; leg.driver_id = did
            ih.append(leg)
        try:
            return build_driver_schedules(ih, drivers, target_date)
        finally:
            for lid, (drv, drvid) in saved.items():
                legs_by_id[lid].driver = drv; legs_by_id[lid].driver_id = drvid

    rescued, warnings = [], []
    schedules = build_schedules()
    for leg in residuals:
        leg_vtype = leg.effective_vehicle_type
        best = None          # (raw_span_after, -buffer, did) -> normal assign
        best_lifted = None   # same key -> cap-lifted assign
        strict_only_block = None
        ceiling_block = None  # span was the sole blocker but even the ceiling can't admit it
        for dr in sorted(drivers, key=lambda d: d.id):
            did = dr.id
            sched = schedules.get(did)
            if sched is None:
                continue
            # Modal pickup-hour window (greedy parity, scheduler pre-filter semantics).
            if driver_hours and did in driver_hours and not (flexible_drivers and did in flexible_drivers):
                dh_start, dh_end = driver_hours[did]
                if not (time(dh_start, 0) <= leg.pickup_time <= time(dh_end, 59)):
                    continue
            # Vehicle-tier compatibility (greedy parity).
            driver_vtype = dvtypes.get(did)
            if driver_vtype and leg_vtype and leg_vtype not in get_compatible_vehicle_types(driver_vtype):
                continue
            if sharers_conflict(leg, did, sharer_partners, schedules, target_date):
                continue
            window = (capped_windows or {}).get(did)
            feas = check_feasibility(sched, leg, target_date, driver_window=window)
            new_end = estimate_job_end_time(leg, target_date)
            new_pickup_dt = datetime.combine(target_date, leg.pickup_time)
            if sched.slots:
                span_after = (max([s.estimated_end_time for s in sched.slots] + [new_end])
                              - datetime.combine(target_date,
                                                 min([s.pickup_time for s in sched.slots] + [leg.pickup_time]))
                              ).total_seconds() / 3600
            else:
                span_after = (new_end - new_pickup_dt).total_seconds() / 3600
            key = (round(span_after, 2), -feas.buffer_minutes, did)
            if feas.feasible:
                if best is None or key < best[0]:
                    best = (key, did, span_after)
                continue
            # Only retry with the cap lifted when span was plausibly the blocker.
            if window is None or window.get("max_hours") is None:
                continue
            probe_w = dict(window); probe_w["max_hours"] = None
            probe = check_feasibility(sched, leg, target_date, driver_window=probe_w)
            if not probe.feasible:
                continue  # blocked by turnaround/window too — not a span rescue
            # Span IS the sole blocker. The lift is NOT unbounded: it stops at the
            # absolute ceiling (founder 2026-06-10: "no driver ever gets an inhumane
            # day"). A leg that fits nobody under the ceiling farms — LOUDLY, via the
            # ceiling_blocked warning below, never silently.
            ceiling = float(fg.SPAN_RESCUE_CEILING_HOURS)
            win_cap = float(window["max_hours"])
            if win_cap >= ceiling:
                # The window already reaches (or, via a typed Max hrs, exceeds) the
                # automatic rescue ceiling — there is nothing to lift. A TYPED cap
                # is the binding constraint and must be NAMED (strict_blocked with
                # HIS number, e.g. 16h), never misreported as the policy ceiling.
                if did in strict:
                    if strict_only_block is None or did < strict_only_block[0]:
                        strict_only_block = (did, span_after, win_cap)
                else:
                    if ceiling_block is None or did < ceiling_block[0]:
                        ceiling_block = (did, span_after, ceiling)
                continue
            probe_c = dict(window); probe_c["max_hours"] = ceiling
            within_ceiling = check_feasibility(
                sched, leg, target_date, driver_window=probe_c).feasible
            if not within_ceiling:
                if ceiling_block is None or did < ceiling_block[0]:
                    ceiling_block = (did, span_after, ceiling)
                continue
            if did in strict:
                if strict_only_block is None or did < strict_only_block[0]:
                    strict_only_block = (did, span_after, float(window["max_hours"]))
                continue
            if best_lifted is None or key < best_lifted[0]:
                best_lifted = (key, did, span_after, float(window["max_hours"]))

        if best is not None:
            _, did, span_after = best
            board[leg.id] = did
            final_assignments[leg.id] = did
            rescued.append(leg.id)
            schedules = build_schedules()
        elif best_lifted is not None:
            _, did, span_after, cap_h = best_lifted
            board[leg.id] = did
            final_assignments[leg.id] = did
            rescued.append(leg.id)
            warnings.append({
                "kind": "rescued", "leg_id": leg.id, "driver_id": did,
                "driver_name": str(drivers_by_id.get(did, did)),
                "span_after": round(span_after, 1), "cap_hours": cap_h,
                "pickup": leg.pickup_time.strftime("%I:%M %p").lstrip("0"),
            })
            schedules = build_schedules()
        elif strict_only_block is not None:
            did, span_after, cap_h = strict_only_block
            warnings.append({
                "kind": "strict_blocked", "leg_id": leg.id, "driver_id": did,
                "driver_name": str(drivers_by_id.get(did, did)),
                "span_after": round(span_after, 1), "cap_hours": cap_h,
                "pickup": leg.pickup_time.strftime("%I:%M %p").lstrip("0"),
            })
        elif ceiling_block is not None:
            did, span_after, ceil_h = ceiling_block
            warnings.append({
                "kind": "ceiling_blocked", "leg_id": leg.id, "driver_id": did,
                "driver_name": str(drivers_by_id.get(did, did)),
                "span_after": round(span_after, 1), "cap_hours": ceil_h,
                "pickup": leg.pickup_time.strftime("%I:%M %p").lstrip("0"),
            })

    return final_assignments, rescued, warnings


def _max_internal_gap_minutes(slots, target_date: date) -> float:
    """Largest idle gap (minutes) between consecutive jobs in a slot list. 0 if < 2 jobs."""
    if not slots or len(slots) < 2:
        return 0
    ordered = sorted(slots, key=lambda s: s.pickup_time)
    worst = 0
    for prev, nxt in zip(ordered, ordered[1:]):
        gap = (datetime.combine(target_date, nxt.pickup_time) - prev.estimated_end_time).total_seconds() / 60
        if gap > worst:
            worst = gap
    return worst


def _span_gap_credit_minutes(slots, target_date: date) -> float:
    """Off-duty break credit: the largest PRE-EXISTING internal gap, when it is a real break
    (>= fg.SPAN_GAP_CREDIT_MIN_MIN), capped at fg.SPAN_GAP_CREDIT_MAX_MIN. Computed from the
    schedule BEFORE any candidate insert, so the engine can never earn credit by minting a
    new hole with the insert being priced."""
    from dispatching import feasibility_guards as fg
    gap = _max_internal_gap_minutes(slots, target_date)
    if gap < fg.SPAN_GAP_CREDIT_MIN_MIN:
        return 0.0
    return float(min(gap, fg.SPAN_GAP_CREDIT_MAX_MIN))


def _span_cost_points(effective_hours: float) -> float:
    """Progressive duty-span price (score points): free under SPAN_SOFT_FREE_HOURS,
    SPAN_SOFT_RATE/hr up to the (strictly-greater) SPAN_SOFT_EFFECTIVE_HOURS target,
    SPAN_STEEP_RATE/hr beyond it."""
    from dispatching import feasibility_guards as fg
    free, target = fg.SPAN_SOFT_FREE_HOURS, fg.SPAN_SOFT_EFFECTIVE_HOURS
    return (fg.SPAN_SOFT_RATE * max(0.0, min(effective_hours, target) - free)
            + fg.SPAN_STEEP_RATE * max(0.0, effective_hours - target))


def marginal_span_penalty(slots, target_date: date, new_pickup_dt, new_end_dt) -> int:
    """Marginal effective-span price of adding a leg: cost(after) − cost(before), where
    effective span = raw span (first pickup → last clear) minus the pre-existing break
    credit. A candidate is charged only for the day-stretch THIS leg adds, so a late leg
    is cheap on an already-late (or fresh) driver and expensive on a 4 AM starter —
    early/late shift structure emerges from the price with no templates. An insert that
    lands inside the existing day stretches nothing and costs 0 (gap-filling stays free)."""
    if not slots:
        return 0
    first = datetime.combine(target_date, min(s.pickup_time for s in slots))
    last = max(s.estimated_end_time for s in slots)
    raw_before = (last - first).total_seconds() / 3600
    raw_after = (max(last, new_end_dt) - min(first, new_pickup_dt)).total_seconds() / 3600
    credit_h = _span_gap_credit_minutes(slots, target_date) / 60.0
    return int(round(_span_cost_points(max(0.0, raw_after - credit_h))
                     - _span_cost_points(max(0.0, raw_before - credit_h))))


def effective_span_hours(slots, target_date: date):
    """(raw_span_hours, effective_span_hours) for a built day — the preview/badge metric.
    Effective = raw minus the break credit (the founder's split-days are judged by their
    continuous duty, not wall-clock span). Returns (0.0, 0.0) for an empty day."""
    if not slots:
        return 0.0, 0.0
    first = datetime.combine(target_date, min(s.pickup_time for s in slots))
    last = max(s.estimated_end_time for s in slots)
    raw = (last - first).total_seconds() / 3600
    return raw, max(0.0, raw - _span_gap_credit_minutes(slots, target_date) / 60.0)


def trim_spans_via_relocation(final_assignments, legs_by_id, drivers, drivers_by_id,
                              target_date, dvtypes, locked_leg_ids=None,
                              driver_hours=None, flexible_drivers=None, capped_windows=None,
                              sharer_partners=None):
    """Span-trim pass (Span Governor Phase 3). For each driver whose built day runs over the
    soft target (effective span > SPAN_SOFT_EFFECTIVE_HOURS) or is simply too long raw
    (> SPAN_TRIM_RAW_MAX_HOURS), try to relocate his FIRST or LAST leg — the only legs whose
    removal shrinks the span — onto another driver with room. The founder's "Roberto just
    starts later" move, applied to day length.

    A move of leg L (donor D -> receiver R) is accepted iff ALL hold:
      * L is unlocked (manual / seeded / pre-existing assignments never move) and is D's
        first or last leg;
      * removing L shortens D's raw span by >= SPAN_TRIM_MIN_RELIEF_MIN minutes;
      * R is vehicle-tier compatible, inside his modal hard window (unless flexible), and
        check_feasibility passes under his cap-clamped window ("never late" preserved);
      * R stays under BOTH limits after the insert (raw <= SPAN_TRIM_RAW_MAX_HOURS and
        effective <= the soft target) — trimming may never mint a new long day. Receiver
        stretch is deliberately NOT netted against relief (it would veto the founder's own
        move — a tail leg handed to a short-day driver stretches him a lot, harmlessly);
        stretch only breaks ties so the least-stretched receiver wins.
    Per round the single best move applies (worst-excess donor first; ties by relief,
    stretch, tier fit, leg id). Moved legs are LOCKED (no ping-pong; views also locks them against
    gap compaction). Donors peel <= SPAN_TRIM_MAX_PER_DONOR, receivers gain <=
    SPAN_TRIM_MAX_RECEIVE, <= SPAN_TRIM_MAX_MOVES total. Farms nothing by construction —
    the assignment keyset cannot change. Returns (final_assignments, moves).
    """
    if not AUTO_SPAN_TRIM_PASS:
        return final_assignments, []
    from dispatching import feasibility_guards as fg
    if not fg.ENFORCE_SPAN_CAPS:
        return final_assignments, []

    locked = set(locked_leg_ids or [])
    board = dict(final_assignments)
    for lid, leg in legs_by_id.items():
        if lid not in board and getattr(leg, "driver_id", None):
            board[lid] = leg.driver_id
            locked.add(lid)
    keyset_before = set(final_assignments.keys())

    if capped_windows is not None:
        windows = capped_windows
    else:
        windows = {}
        for dr in drivers:
            try:
                eff = dr.get_effective_availability(target_date)
                mh = eff.get("max_hours")
                cfgw = {"start": eff.get("start_hour"), "end": eff.get("end_hour"),
                        "max_hours": (float(mh) if mh else None), "flexible": bool(eff.get("flexible"))}
            except Exception:
                cfgw = None
            windows[dr.id] = fg.get_effective_window(dr.id, configured=cfgw)

    def build_schedules():
        saved, ih = {}, []
        for lid, did in board.items():
            leg = legs_by_id.get(lid); dr = drivers_by_id.get(did)
            if leg is None or dr is None:
                continue
            saved[lid] = (leg.driver, getattr(leg, "driver_id", None))
            leg.driver = dr; leg.driver_id = did
            ih.append(leg)
        try:
            return build_driver_schedules(ih, drivers, target_date)
        finally:
            for lid, (drv, drvid) in saved.items():
                legs_by_id[lid].driver = drv; legs_by_id[lid].driver_id = drvid

    target_eff = fg.SPAN_SOFT_EFFECTIVE_HOURS
    moves, peels, receives = [], {}, {}
    for _ in range(SPAN_TRIM_MAX_MOVES):
        schedules = build_schedules()
        donors = []
        for did, s in schedules.items():
            if len(s.slots) < 2:
                continue
            raw, eff = effective_span_hours(s.slots, target_date)
            excess = max(eff - target_eff, raw - SPAN_TRIM_RAW_MAX_HOURS)
            if excess > 0 and peels.get(did, 0) < SPAN_TRIM_MAX_PER_DONOR:
                donors.append((excess, did))
        if not donors:
            break
        donors.sort(key=lambda t: (-t[0], t[1]))

        best = None   # (key, leg, donor_id, receiver_id)
        for excess, don_id in donors:
            ordered = sorted(schedules[don_id].slots, key=lambda s: s.pickup_time)
            d_first = datetime.combine(target_date, ordered[0].pickup_time)
            d_last = max(s.estimated_end_time for s in ordered)
            raw_before_min = (d_last - d_first).total_seconds() / 60
            for slot in (ordered[0], ordered[-1]):
                if slot.leg_id in locked or board.get(slot.leg_id) != don_id:
                    continue
                leg = legs_by_id.get(slot.leg_id)
                if leg is None:
                    continue
                remaining = [s for s in ordered if s.leg_id != slot.leg_id]
                r_first = datetime.combine(target_date, min(s.pickup_time for s in remaining))
                r_last = max(s.estimated_end_time for s in remaining)
                relief = raw_before_min - (r_last - r_first).total_seconds() / 60
                if relief < SPAN_TRIM_MIN_RELIEF_MIN:
                    continue
                leg_vtype = leg.effective_vehicle_type
                new_end = estimate_job_end_time(leg, target_date)
                np_dt = datetime.combine(target_date, leg.pickup_time)
                for rec in sorted(drivers, key=lambda d: d.id):
                    rid = rec.id
                    if rid == don_id or receives.get(rid, 0) >= SPAN_TRIM_MAX_RECEIVE:
                        continue
                    rsched = schedules.get(rid)
                    if rsched is None:
                        continue
                    if (driver_hours and rid in driver_hours
                            and not (flexible_drivers and rid in flexible_drivers)):
                        sh, eh = driver_hours[rid]
                        if not (time(sh, 0) <= leg.pickup_time <= time(eh, 59)):
                            continue
                    rv = dvtypes.get(rid)
                    if rv and leg_vtype and leg_vtype not in get_compatible_vehicle_types(rv):
                        continue
                    if sharers_conflict(leg, rid, sharer_partners, schedules, target_date):
                        continue
                    feas = check_feasibility(rsched, leg, target_date,
                                             driver_window=(windows or {}).get(rid))
                    if not feas.feasible:
                        continue
                    if rsched.slots:
                        nr_first = min([datetime.combine(target_date, s.pickup_time)
                                        for s in rsched.slots] + [np_dt])
                        nr_last = max([s.estimated_end_time for s in rsched.slots] + [new_end])
                        or_first = datetime.combine(target_date,
                                                    min(s.pickup_time for s in rsched.slots))
                        or_last = max(s.estimated_end_time for s in rsched.slots)
                        r_raw_before_min = (or_last - or_first).total_seconds() / 60
                        credit_h = _span_gap_credit_minutes(rsched.slots, target_date) / 60.0
                    else:
                        nr_first, nr_last = np_dt, new_end
                        r_raw_before_min, credit_h = 0.0, 0.0
                    r_raw_after = (nr_last - nr_first).total_seconds() / 3600
                    if (r_raw_after > SPAN_TRIM_RAW_MAX_HOURS
                            or max(0.0, r_raw_after - credit_h) > target_eff):
                        continue
                    stretch = max(0.0, r_raw_after * 60 - r_raw_before_min)
                    tier_waste = ((get_vehicle_tier(rv) - get_vehicle_tier(str(leg_vtype)))
                                  if (rv and leg_vtype) else 0)
                    key = (-excess, -relief, stretch, tier_waste, slot.leg_id, rid)
                    if best is None or key < best[0]:
                        best = (key, leg, don_id, rid, relief, stretch)
            if best is not None:
                break   # worst-excess donor first: take his best move this round

        if best is None:
            break
        _, leg, don_id, rid, relief, stretch = best
        board[leg.id] = rid
        final_assignments[leg.id] = rid
        locked.add(leg.id)
        peels[don_id] = peels.get(don_id, 0) + 1
        receives[rid] = receives.get(rid, 0) + 1
        moves.append({"leg_id": leg.id, "from": don_id, "to": rid,
                      "relief_min": round(relief), "stretch_min": round(stretch)})

    assert set(final_assignments.keys()) == keyset_before, "trim pass must never change coverage"
    return final_assignments, moves


def build_sharer_partners(driver_ids, target_date):
    """Map {driver_id: {other drivers sharing the SAME physical vehicle that date}}.

    Built from DriverVehicleAssignment: a vehicle held by >1 working driver is one
    physical unit split across shifts (Day Setup AM/PM share or an advisor freed-unit
    accept). Feed the result to sharers_conflict() to gate any insert against the
    car-share partner's jobs. Mirrors the inline construction in suggest_assignments()."""
    from drivers.models import DriverVehicleAssignment

    working = set(driver_ids)
    unit_holders = {}
    for dva in DriverVehicleAssignment.objects.filter(
            date=target_date, vehicle__isnull=False, driver_id__in=working):
        unit_holders.setdefault(dva.vehicle_id, []).append(dva.driver_id)
    partners = {}
    for holders in unit_holders.values():
        if len(holders) > 1:
            for did in holders:
                partners.setdefault(did, set()).update(
                    h for h in holders if h != did)
    return partners


def sharers_conflict(leg, driver_id, sharer_partners, schedules, target_date,
                     pad_min=None):
    """True if giving `leg` to `driver_id` would overlap his car-share PARTNER's jobs
    (one physical unit). Interval = [pickup - pad, est_end + pad] vs every partner slot.
    schedules: {driver_id: DriverDaySchedule} for the CURRENT board state."""
    partners = (sharer_partners or {}).get(driver_id)
    if not partners:
        return False
    pad = timedelta(minutes=VEHICLE_SHARE_PAD_MIN if pad_min is None else pad_min)
    start = datetime.combine(target_date, leg.pickup_time) - pad
    end = estimate_job_end_time(leg, target_date) + pad
    for pid in partners:
        psched = schedules.get(pid)
        if psched is None:
            continue
        for s in psched.slots:
            s_start = datetime.combine(target_date, s.pickup_time)
            if start < s.estimated_end_time and s_start < end:
                return True
    return False


def compact_gaps_via_relocation(final_assignments, legs_by_id, drivers, drivers_by_id,
                                target_date, dvtypes, locked_leg_ids=None,
                                driver_hours=None, flexible_drivers=None,
                                sharer_partners=None):
    """Gap-compaction pass. Relocate an ALREADY-COVERED leg from a donor driver to a driver
    with a large internal gap, when doing so heals more gap than it opens — the founder's
    manual "give David the job sitting in his hole; the other driver just starts later" move.

    Coverage is preserved: a leg only changes driver, never gets farmed. Manual / pinned /
    pre-existing assignments are locked and never moved. Read-only wrt the DB — mutates and
    returns the in-memory {leg_id: driver_id} map. Fully deterministic.

    A move of leg L (donor D -> receiver R) is accepted iff ALL hold:
      * L is not locked, R's vehicle is compatible, and R can feasibly insert L
        (turnaround + window via check_feasibility — "we are never late" is preserved);
      * L's pickup lands inside R's SINGLE LARGEST internal gap, which is >= GAP_COMPACT_MIN_GAP;
      * D is not a protected light driver (> GAP_COMPACT_PROTECT_DONOR_MAX_JOBS jobs); and
      * receiver_gap_healed - donor_gap_opened >= GAP_COMPACT_MIN_NET_GAIN, where a first/last
        job opens NO donor gap (D simply starts later / finishes earlier) and a middle job only
        passes when the hole it opens on D is smaller than the hole it heals on R.

    Founder-calibrated behavior:
      * each driver is filled at most once — only their biggest hole, then left alone;
      * light donors (<= GAP_COMPACT_PROTECT_DONOR_MAX_JOBS jobs) are never stripped;
      * a tier-matched receiver wins over a higher-tier one, so the scarce van is used to fill a
        small-vehicle job only when no smaller-vehicle driver can take it.

    Deadhead is NOT a gate (per the founder: fill the hole, any deadhead) — it only breaks ties
    so an on-route fill is preferred. Each round applies the single best move and recomputes;
    bounded by GAP_COMPACT_MAX_MOVES. Returns (final_assignments, moves) — moves for logging.
    """
    if not AUTO_GAP_COMPACT_PASS:
        return final_assignments, []

    from dispatching.analytics import categorize_location
    from dispatching.models import SchedulerSettings
    from dispatching import feasibility_guards as fg
    cfg = SchedulerSettings.get_settings()
    ijb = cfg.inter_job_buffer
    grace = cfg.arrival_grace_minutes

    locked = set(locked_leg_ids or [])

    # Full board = proposed assignments PLUS any pre-existing manual assignment already on a leg
    # (those are also locked — never relocated, but they DO occupy their driver).
    board = dict(final_assignments)
    for lid, leg in legs_by_id.items():
        if lid not in board and getattr(leg, "driver_id", None):
            board[lid] = leg.driver_id
            locked.add(lid)

    # Receiving-driver window context (same construction the swap pass uses).
    windows = {}
    for dr in drivers:
        try:
            eff = dr.get_effective_availability(target_date)
            mh = eff.get("max_hours")
            cfgw = {"start": eff.get("start_hour"), "end": eff.get("end_hour"),
                    "max_hours": (float(mh) if mh else None), "flexible": bool(eff.get("flexible"))}
        except Exception:
            cfgw = None
        windows[dr.id] = fg.get_effective_window(dr.id, configured=cfgw)

    def build_schedules():
        """Reflect the current board onto the leg objects, build schedules, then restore."""
        saved, ih = {}, []
        for lid, did in board.items():
            leg = legs_by_id.get(lid); dr = drivers_by_id.get(did)
            if leg is None or dr is None:
                continue
            saved[lid] = (leg.driver, getattr(leg, "driver_id", None))
            leg.driver = dr; leg.driver_id = did
            ih.append(leg)
        try:
            sch = build_driver_schedules(ih, drivers, target_date)
        finally:
            for lid, (drv, drvid) in saved.items():
                legs_by_id[lid].driver = drv; legs_by_id[lid].driver_id = drvid
        return sch

    moves = []
    received = set()  # a driver is filled at most once — only their biggest hole, then left alone
    for _ in range(GAP_COMPACT_MAX_MOVES):
        schedules = build_schedules()
        # Each driver's current largest internal gap (baseline for the donor-cost comparison).
        cur_max_gap = {did: _max_internal_gap_minutes(s.slots, target_date)
                       for did, s in schedules.items()}

        best = None  # (sort_key, leg_id, receiver_id, donor_id, net_gain, deadhead, healed, opened)
        for rid, rsched in schedules.items():
            if rid in received:
                continue
            slots = sorted(rsched.slots, key=lambda s: s.pickup_time)
            if len(slots) < 2:
                continue
            # Target only this receiver's SINGLE LARGEST internal gap ("just the biggest hole").
            prev, nxt = max(zip(slots, slots[1:]),
                            key=lambda pr: (datetime.combine(target_date, pr[1].pickup_time)
                                            - pr[0].estimated_end_time).total_seconds())
            nxt_pickup_dt = datetime.combine(target_date, nxt.pickup_time)
            gap = (nxt_pickup_dt - prev.estimated_end_time).total_seconds() / 60
            if gap < GAP_COMPACT_MIN_GAP:
                continue
            rvtype = dvtypes.get(rid)
            r_tier = get_vehicle_tier(rvtype) if rvtype else -1
            # Candidate legs: covered, movable, on a DIFFERENT driver, pickup inside this gap.
            for lid, did in board.items():
                if did == rid or lid in locked:
                    continue
                leg = legs_by_id.get(lid)
                if leg is None:
                    continue
                # Protect light donors — never strip a driver who already has few jobs.
                dsched = schedules.get(did)
                if dsched is None or len(dsched.slots) <= GAP_COMPACT_PROTECT_DONOR_MAX_JOBS:
                    continue
                pickup_dt = datetime.combine(target_date, leg.pickup_time)
                if pickup_dt <= prev.estimated_end_time or pickup_dt >= nxt_pickup_dt:
                    continue
                # Vehicle compatibility with the receiver.
                lvtype = leg.effective_vehicle_type
                if rvtype and lvtype and str(lvtype) not in get_compatible_vehicle_types(rvtype):
                    continue
                # Receiver's MODAL hard window (parity with the greedy/trim/rescue passes —
                # critical for Day Setup shared cars, where two drivers' partitioned windows
                # are the only thing keeping the physical unit single-booked).
                if (driver_hours and rid in driver_hours
                        and not (flexible_drivers and rid in flexible_drivers)):
                    _sh, _eh = driver_hours[rid]
                    if not (time(_sh, 0) <= leg.pickup_time <= time(_eh, 59)):
                        continue
                if sharers_conflict(leg, rid, sharer_partners, schedules, target_date):
                    continue
                # Receiver can feasibly insert L? (turnaround + window; never late)
                feas = check_feasibility(rsched, leg, target_date, inter_job_buffer=ijb,
                                         arrival_grace=grace, driver_window=windows.get(rid))
                if not feas.feasible:
                    continue
                # Receiver gap healed = original hole minus its worse remaining half.
                l_end = estimate_job_end_time(leg, target_date)
                gap_before = (pickup_dt - prev.estimated_end_time).total_seconds() / 60
                gap_after = (nxt_pickup_dt - l_end).total_seconds() / 60
                receiver_gap_healed = gap - max(gap_before, gap_after)
                if receiver_gap_healed <= 0:
                    continue
                # Donor gap opened = increase in donor's largest internal gap once L leaves.
                donor_after = [s for s in dsched.slots if s.leg_id != lid]
                donor_gap_opened = max(0, _max_internal_gap_minutes(donor_after, target_date)
                                       - cur_max_gap.get(did, 0))
                net_gain = receiver_gap_healed - donor_gap_opened
                if net_gain < GAP_COMPACT_MIN_NET_GAIN:
                    continue
                # Tier waste: how far above the job's vehicle tier this receiver is. Prefer a
                # tier-matched receiver so the scarce van fills a small job only when nothing
                # smaller can (founder: "prefer same/lower-tier receivers"). Primary sort term.
                l_tier = get_vehicle_tier(str(lvtype)) if lvtype else -1
                tier_waste = (r_tier - l_tier) if (l_tier >= 0 and r_tier > l_tier) else 0
                # Deadhead added on the receiver (tiebreak only — "any deadhead" is allowed).
                dh_in = resolve_drive_minutes(prev.dropoff_location, leg.pickup_location,
                                              prev.dropoff_category, categorize_location(leg.pickup_location))
                dh_out = resolve_drive_minutes(leg.dropoff_location, nxt.pickup_location,
                                               categorize_location(leg.dropoff_location), nxt.pickup_category)
                deadhead = dh_in + dh_out
                # Prefer pulling a donor's first/last job — the clean "starts later" move.
                donor_slots = sorted(dsched.slots, key=lambda s: s.pickup_time)
                is_boundary = lid in (donor_slots[0].leg_id, donor_slots[-1].leg_id)
                sort_key = (-tier_waste, net_gain, -deadhead, 1 if is_boundary else 0, -lid)
                if best is None or sort_key > best[0]:
                    best = (sort_key, lid, rid, did, net_gain, deadhead,
                            receiver_gap_healed, donor_gap_opened)

        if best is None:
            break
        _, lid, rid, did, net_gain, deadhead, healed, opened = best
        board[lid] = rid
        final_assignments[lid] = rid
        locked.add(lid)     # a relocated leg is final for this pass — bounds the hill-climb,
                            # prevents a leg ping-ponging between two drivers across rounds.
        received.add(rid)   # fill each driver's biggest hole once, then leave him alone.
        moves.append({
            "leg_id": lid, "from_driver_id": did, "to_driver_id": rid,
            "net_gain_min": round(net_gain), "deadhead_added_min": round(deadhead),
            "receiver_gap_healed_min": round(healed), "donor_gap_opened_min": round(opened),
        })

    return final_assignments, moves


def find_batching_opportunities(legs, target_date: date) -> List[BatchingOpportunity]:
    """
    Find legs in the same location category within 30 min that could be batched.
    """
    from dispatching.analytics import categorize_location

    opportunities = []
    seen = set()

    by_category = {}
    for leg in legs:
        cat = categorize_location(leg.pickup_location)
        by_category.setdefault(cat, []).append(leg)

    for category, cat_legs in by_category.items():
        if len(cat_legs) < 2:
            continue

        cat_legs.sort(key=lambda l: l.pickup_time)

        for i, leg_a in enumerate(cat_legs):
            group = [leg_a]
            for j in range(i + 1, len(cat_legs)):
                leg_b = cat_legs[j]
                dt_a = datetime.combine(target_date, leg_a.pickup_time)
                dt_b = datetime.combine(target_date, leg_b.pickup_time)
                if abs((dt_b - dt_a).total_seconds()) / 60 <= 30:
                    if not (leg_a.driver and leg_b.driver and leg_a.driver_id == leg_b.driver_id):
                        group.append(leg_b)
                else:
                    break

            if len(group) >= 2:
                key = frozenset(l.id for l in group)
                if key not in seen:
                    seen.add(key)
                    opportunities.append(BatchingOpportunity(
                        legs=[{
                            'id': l.id,
                            'pickup_time': l.pickup_time,
                            'pickup_location': l.pickup_location,
                            'dropoff_location': l.dropoff_location,
                            'driver_name': str(l.driver) if l.driver else 'Unassigned',
                            'trip_type': l.get_trip_type(),
                        } for l in group],
                        location_category=category,
                        time_window_start=group[0].pickup_time,
                        time_window_end=group[-1].pickup_time,
                        reason=f"{len(group)} jobs at {category} within 30min",
                    ))

    return opportunities


def build_smart_schedule(
    driver_id: int,
    driver_name: str,
    available_legs,
    target_date: date,
    start_hour: int = 0,
    end_hour: int = 23,
    pinned_leg_ids: List[int] = None,
    preferred_trip_type: str = None,
    existing_schedule: DriverDaySchedule = None,
    excluded_leg_ids: List[int] = None,
    vehicle_pref_mode: str = None,
    preferred_vehicle_types: List[str] = None,
    max_hours: float = None,
) -> dict:
    """
    Build an optimal schedule for a single driver within a time window.

    Parameters:
        driver_id: The driver to build the schedule for
        driver_name: Display name
        available_legs: Queryset/list of unassigned legs on this date
        target_date: The date being planned
        start_hour: Earliest hour the driver is available (0-23)
        end_hour: Latest hour the driver should finish by (0-23)
        pinned_leg_ids: Leg IDs that MUST be included (e.g., a cruise the dispatcher wants this driver to have)
        preferred_trip_type: If set, prefer legs of this type ('arrival', 'return', 'cruise', 'other')
        existing_schedule: If the driver already has assigned jobs, pass them here

    Returns:
        {
            'driver_id': int,
            'driver_name': str,
            'schedule': [ScheduleSlot, ...],
            'pinned_included': [int, ...],  # which pinned legs were included
            'pinned_failed': [int, ...],    # which pinned legs couldn't fit
            'total_legs': int,
            'total_revenue': Decimal,
            'utilization_pct': float,
            'warnings': [str, ...],
        }
    """
    from dispatching.analytics import categorize_location

    from dispatching.models import SchedulerSettings
    cfg = SchedulerSettings.get_settings()

    from dispatching import feasibility_guards as fg
    from drivers.models import Driver as _Driver
    from drivers.availability import resolve_effective_availability, is_pickup_within_window
    # The schedule builder is a MANUAL tool: obey the DISPATCHER's selected from/until
    # (start_hour/end_hour) as the window — NOT the observed-history stub. Resolve the
    # driver's REAL availability so a flexible driver (who works/finishes anytime) is not
    # window-bound, consistent with drivers.availability (the canonical resolver).
    _drv = (_Driver.objects.filter(id=driver_id)
            .prefetch_related("weekly_schedule", "date_overrides").first())
    _eff = resolve_effective_availability(_drv, target_date) if _drv else None
    _is_flexible = bool(_eff and _eff.get("status") == "flexible")
    # max_hours: per-driver hard duty-span cap (Span Governor). None was a hole — Build-1st
    # seeding had NO span bound at all, so a wide dispatcher window built 15-18h days.
    _dwindow = {"start": start_hour, "end": end_hour, "max_hours": max_hours, "flexible": _is_flexible}

    pinned_leg_ids = pinned_leg_ids or []
    excluded_leg_ids = excluded_leg_ids or []
    warnings = []
    # Track timing details for each newly added slot: leg_id -> {prev_dropoff, drive_time, buffer, ...}
    slot_timing_details = {}

    # Look up driver's vehicle type for this date
    driver_vtype = get_driver_vehicle_type(driver_id, target_date)
    driver_tier = get_vehicle_tier(driver_vtype) if driver_vtype else -1
    compatible_types = get_compatible_vehicle_types(driver_vtype) if driver_vtype else None

    # Load all driver vehicle types for scarcity calculation
    all_driver_vtypes = load_all_driver_vtypes(target_date)

    if not driver_vtype:
        # No vehicle assignment — return empty schedule with warning
        warnings.append("No vehicle assigned for this date. Assign a vehicle first.")
        return {
            'driver_id': driver_id, 'driver_name': driver_name,
            'schedule': [], 'pinned_included': [], 'pinned_failed': pinned_leg_ids or [],
            'total_legs': 0, 'total_revenue': Decimal('0'), 'utilization_pct': 0,
            'warnings': warnings, 'existing_count': 0, 'new_count': 0,
            'slot_timing_details': {},
        }

    # Build working schedule (start with existing assignments, minus excluded)
    existing_slots = list(existing_schedule.slots) if existing_schedule else []
    if excluded_leg_ids:
        existing_slots = [s for s in existing_slots if s.leg_id not in excluded_leg_ids]
    working = DriverDaySchedule(
        driver_id=driver_id,
        driver_name=driver_name,
        driver_type='inhouse',
        slots=existing_slots,
    )

    # Time window boundaries
    window_start = time(start_hour, 0)
    window_end = time(end_hour, 59)

    # Filter available legs: within time window, not excluded, AND vehicle-compatible
    window_legs = []
    for leg in available_legs:
        if not (window_start <= leg.pickup_time <= window_end):
            continue
        if leg.id in excluded_leg_ids:
            continue
        # Vehicle compatibility filter
        leg_vtype = leg.effective_vehicle_type
        if leg_vtype and compatible_types and leg_vtype not in compatible_types:
            continue
        window_legs.append(leg)

    # Separate pinned legs (must-do) from optional legs
    # Pinned legs bypass vehicle filter — they're mandatory
    pinned_legs = [l for l in window_legs if l.id in pinned_leg_ids]
    optional_legs = [l for l in window_legs if l.id not in pinned_leg_ids]

    # Also check pinned legs outside window (user pinned something at 6 AM but window is 8-16)
    for leg in available_legs:
        if leg.id in pinned_leg_ids and leg.id not in excluded_leg_ids and leg not in pinned_legs:
            pinned_legs.append(leg)
            if not (window_start <= leg.pickup_time <= window_end):
                warnings.append(f"Pinned leg #{leg.id} ({leg.pickup_time.strftime('%I:%M %p').lstrip('0')}) is outside the {start_hour}:00-{end_hour}:00 window")

    pinned_included = []
    pinned_failed = []

    # Step 1: Insert pinned legs first (they're mandatory)
    # A pin is an explicit dispatcher override. Only a PHYSICAL time conflict (Guard B
    # overlap/turnaround) may drop it — the per-driver window (Guard C) is ADVISORY here,
    # so we pass driver_window=None and instead surface any window issue as a warning
    # (founder: "if it extends a driver's time, flag it but still do it").
    pinned_sorted = sorted(pinned_legs, key=lambda l: l.pickup_time)
    for leg in pinned_sorted:
        t = leg.pickup_time.strftime('%I:%M %p').lstrip('0')
        feas = check_feasibility(working, leg, target_date, inter_job_buffer=cfg.inter_job_buffer, arrival_grace=cfg.arrival_grace_minutes,
                                 driver_window=None)
        if feas.feasible:
            _add_leg_to_schedule(working, leg, target_date)
            pinned_included.append(leg.id)
            # Advisory only (leg is kept regardless): flag a pin that sits outside the
            # driver's REAL availability, or that finishes past the selected end. A
            # flexible driver works/finishes anytime, so it never gets a window flag.
            if not _is_flexible:
                leg_end = estimate_job_end_time(leg, target_date)
                wok, wreason = (is_pickup_within_window(_eff, leg.pickup_time, dropoff_dt=leg_end)
                                if _eff is not None else (True, ""))
                end_sel_dt = datetime.combine(target_date, time(min(end_hour, 23), 0))
                if not wok:
                    warnings.append(f"Pinned leg #{leg.id} at {t} kept (override) — {wreason}")
                elif leg_end > end_sel_dt:
                    warnings.append(
                        f"Pinned leg #{leg.id} at {t} kept (override) — finishes "
                        f"~{leg_end.strftime('%I:%M %p').lstrip('0')}, past your selected {end_hour}:00 end.")
        else:
            pinned_failed.append(leg.id)
            warnings.append(f"Pinned leg #{leg.id} at {t} can't fit — time conflict: {feas.reason}")

    # Step 2: Fill remaining slots with optional legs, scored by preference
    # Parse preference mode and trip type
    pref_mode = 'prefer'  # default
    pref_type = None
    if preferred_trip_type:
        parts = preferred_trip_type.split('_', 1)
        if len(parts) == 2 and parts[0] in ('prefer', 'heavy', 'only'):
            pref_mode, pref_type = parts
        else:
            # Backward compatibility: bare type like 'arrival'
            pref_type = preferred_trip_type

    # For "only" mode: hard-filter to matching trip type
    if pref_mode == 'only' and pref_type:
        pre_filter_count = len(optional_legs)
        optional_legs = [l for l in optional_legs if l.get_trip_type() == pref_type]
        if len(optional_legs) == 0:
            warnings.append(f"No {pref_type} legs available in this window. Try a different preference or time range.")
        elif pre_filter_count > len(optional_legs):
            warnings.append(f"Strict mode: showing {len(optional_legs)} {pref_type} leg(s) ({pre_filter_count - len(optional_legs)} other type(s) excluded)")

    # VEHICLE-TYPE preference (optional) — mirrors the trip-type preference above.
    # 'only' hard-filters to the selected vehicle type(s); prefer/heavy nudge ordering below.
    _veh_pref_set = {str(v) for v in (preferred_vehicle_types or []) if v}
    if vehicle_pref_mode == 'only' and _veh_pref_set:
        _pre_v = len(optional_legs)
        optional_legs = [l for l in optional_legs if str(l.effective_vehicle_type) in _veh_pref_set]
        _label = ", ".join(sorted(_veh_pref_set))
        if len(optional_legs) == 0:
            warnings.append(f"No {_label} jobs available in this window. Try different vehicle types or time range.")
        elif _pre_v > len(optional_legs):
            warnings.append(f"Vehicle-only mode: showing {len(optional_legs)} {_label} job(s) ({_pre_v - len(optional_legs)} other vehicle type(s) excluded)")

    # Pre-compute scarcity: how many OTHER drivers can handle each leg
    scarcity_map = compute_leg_scarcity(optional_legs, all_driver_vtypes, exclude_driver_id=driver_id)

    # Pre-compute chain opportunities for this driver's candidate legs
    chain_map = {}
    grace = cfg.arrival_grace_minutes
    for leg in optional_legs:
        dropoff_cat = categorize_location(leg.dropoff_location)
        leg_end_est = estimate_job_end_time(leg, target_date)
        chain_count = 0
        for other in optional_legs:
            if other.id == leg.id:
                continue
            other_pickup_cat = categorize_location(other.pickup_location)
            if other_pickup_cat == dropoff_cat:
                drive_between = 0
            else:
                drive_between = DRIVE_TIME_ESTIMATES.get(
                    (dropoff_cat, other_pickup_cat), DEFAULT_DRIVE_TIME
                )
                if drive_between > cfg.chain_drive_threshold:
                    continue
            other_pickup_dt = datetime.combine(target_date, other.pickup_time)
            # Airport arrivals: flight time != pickup time, pax still deplaning
            other_is_arrival = (
                other.get_trip_type() == 'arrival'
                and other_pickup_cat in ('MCO Terminal', 'SFB Terminal')
            )
            effective_pickup = other_pickup_dt + timedelta(minutes=grace) if other_is_arrival else other_pickup_dt
            gap_minutes = (effective_pickup - leg_end_est).total_seconds() / 60
            if cfg.chain_time_min <= gap_minutes <= cfg.chain_time_max:
                chain_count += 1
        chain_map[leg.id] = chain_count

    # Pre-compute vehicle reservation count for this driver
    # Use exact-type driver count (how many drivers have this EXACT vehicle),
    # not general scarcity (which counts all compatible higher-tier drivers).
    exact_type_driver_counts = {}
    for dvtype in all_driver_vtypes.values():
        if dvtype:
            exact_type_driver_counts[dvtype] = exact_type_driver_counts.get(dvtype, 0) + 1

    reserved_count = 0
    if driver_vtype:
        exact_drivers_for_type = exact_type_driver_counts.get(driver_vtype, len(all_driver_vtypes))
        for leg_check in optional_legs:
            leg_check_vtype = leg_check.effective_vehicle_type
            if leg_check_vtype and str(leg_check_vtype) == driver_vtype:
                if exact_drivers_for_type <= cfg.reserve_max_scarcity:
                    reserved_count += 1

    # Sort by vehicle tier descending, with preference-aware ordering
    if pref_type and pref_mode == 'heavy':
        # Heavy mode: preference match is PRIMARY sort key
        # All matching-type legs processed before any non-matching
        def _leg_sort_key(leg):
            tt = leg.get_trip_type()
            match = 0 if tt == pref_type else 1
            vtype = leg.effective_vehicle_type
            tier = get_vehicle_tier(vtype) if vtype else 0
            return (match, -tier, leg.pickup_time)
        optional_sorted = sorted(optional_legs, key=_leg_sort_key)
    elif pref_type and pref_mode == 'prefer':
        # Prefer mode: tier primary, preference as tiebreaker within same tier
        def _leg_sort_key(leg):
            tt = leg.get_trip_type()
            match = 0 if tt == pref_type else 1
            vtype = leg.effective_vehicle_type
            tier = get_vehicle_tier(vtype) if vtype else 0
            return (-tier, match, leg.pickup_time)
        optional_sorted = sorted(optional_legs, key=_leg_sort_key)
    else:
        # No preference or "only" mode (already filtered): sort by tier
        def _leg_sort_key(leg):
            vtype = leg.effective_vehicle_type
            tier = get_vehicle_tier(vtype) if vtype else 0
            return (-tier, leg.pickup_time)
        optional_sorted = sorted(optional_legs, key=_leg_sort_key)

    # Vehicle prefer/heavy: stable nudge so preferred-vehicle-type jobs are considered
    # first within the existing order (doesn't alter scoring — just ordering, like trip prefer).
    if _veh_pref_set and vehicle_pref_mode in ('prefer', 'heavy'):
        optional_sorted = sorted(
            optional_sorted,
            key=lambda l: 0 if str(l.effective_vehicle_type) in _veh_pref_set else 1,
        )

    def _score_candidate(leg):
        """Feasibility-gated score for a candidate, or None if it can't be seated now.
        Honors the dispatcher's selected window + finish-by grace, then Guard B feasibility."""
        if not (window_start <= leg.pickup_time <= window_end):
            return None
        est_end = estimate_job_end_time(leg, target_date)
        if est_end.hour > end_hour + 1:  # allow 1 hour grace for last job
            return None
        feas = check_feasibility(working, leg, target_date, inter_job_buffer=cfg.inter_job_buffer, arrival_grace=cfg.arrival_grace_minutes,
                                 driver_window=_dwindow)
        if not feas.feasible:
            return None
        # Score this leg (with vehicle tier + scarcity + chain awareness)
        # Pass pref_type for scoring; skip preference scoring in "only" mode (all legs match)
        eligible_others = scarcity_map.get(leg.id, len(all_driver_vtypes))
        chains = chain_map.get(leg.id, 0)
        scoring_pref = pref_type if pref_mode != 'only' else None
        return _score_leg_for_smart_schedule(
            leg, working, feas, scoring_pref, target_date, driver_tier,
            eligible_others, chains, cfg, reserved_count=reserved_count)

    if BUILDER_BEST_FIT:
        # Best-fit: each round score every still-feasible candidate against the CURRENT
        # schedule and seat the single highest-scoring one, then re-check the rest (so a
        # high-tier empty-deadhead leg can't be seated ahead of — and block — a
        # higher-scoring paid leg). Ties keep (-tier, pickup_time) order (we replace only
        # on a STRICTLY higher score while iterating optional_sorted). One seat/round → terminates.
        remaining = list(optional_sorted)
        while remaining:
            best_leg = None
            best_score = 0
            for leg in remaining:
                score = _score_candidate(leg)
                if score is not None and score > best_score:
                    best_score = score
                    best_leg = leg
            if best_leg is None:   # nothing feasible scores > 0 → done
                break
            _add_leg_to_schedule(working, best_leg, target_date)
            remaining = [l for l in remaining if l.id != best_leg.id]
    else:
        # Legacy first-fit: single pass, seat the first feasible leg with score > 0.
        for leg in optional_sorted:
            score = _score_candidate(leg)
            if score is not None and score > 0:
                _add_leg_to_schedule(working, leg, target_date)

    # Recalculate ALL timing details after schedule is fully built.
    # During greedy insertion, legs aren't added in chronological order,
    # so timing captured at insertion time references wrong preceding jobs.
    slot_timing_details = _recalculate_timing_details(working, target_date, inter_job_buffer=cfg.inter_job_buffer)

    # Safety net: greedy insertion (tier-ordered, not chronological) can occasionally seat
    # a leg that turns out infeasible once the full day is ordered. The buffers above are
    # now context-aware (match check_feasibility), so a NEGATIVE one means the driver would
    # genuinely be late — surface it (don't silently keep it looking fine).
    for _sl in sorted(working.slots, key=lambda s: s.pickup_time):
        _d = slot_timing_details.get(_sl.leg_id, {})
        _b = _d.get('buffer_minutes')
        if _b is not None and _b < 0:
            warnings.append(
                f"⚠ Leg #{_sl.leg_id} at {_sl.pickup_time.strftime('%I:%M %p').lstrip('0')}: "
                f"driver may be ~{abs(_b)} min late repositioning "
                f"{_d.get('reposition_from')} → {_d.get('reposition_to')}.")

    # Calculate utilization
    total_window_minutes = (end_hour - start_hour) * 60
    active_minutes = 0
    for slot in working.slots:
        pickup_dt = datetime.combine(target_date, slot.pickup_time)
        duration = (slot.estimated_end_time - pickup_dt).total_seconds() / 60
        active_minutes += duration

    utilization = round(active_minutes / total_window_minutes * 100, 1) if total_window_minutes > 0 else 0

    return {
        'driver_id': driver_id,
        'driver_name': driver_name,
        'schedule': working.slots,
        'pinned_included': pinned_included,
        'pinned_failed': pinned_failed,
        'total_legs': len(working.slots),
        'total_revenue': working.total_revenue,
        'utilization_pct': utilization,
        'warnings': warnings,
        'existing_count': len(existing_schedule.slots) if existing_schedule else 0,
        'new_count': len(working.slots) - (len(existing_schedule.slots) if existing_schedule else 0),
        'slot_timing_details': slot_timing_details,
    }


def _recalculate_timing_details(schedule: DriverDaySchedule, target_date: date, inter_job_buffer: int = None) -> dict:
    """
    Recalculate timing details for ALL slots after the schedule is fully built.
    This ensures each slot references the correct preceding job, since greedy
    insertion doesn't add legs in chronological order.
    """
    if inter_job_buffer is None:
        inter_job_buffer = INTER_JOB_BUFFER

    from dispatching import feasibility_guards as fg

    details_map = {}
    sorted_slots = sorted(schedule.slots, key=lambda s: s.pickup_time)

    for i, slot in enumerate(sorted_slots):
        pickup_cat = slot.pickup_category
        dropoff_cat = slot.dropoff_category
        drive_time = resolve_drive_minutes(slot.pickup_location, slot.dropoff_location, pickup_cat, dropoff_cat)
        est_end = slot.estimated_end_time

        details = {
            'pickup_category': pickup_cat,
            'dropoff_category': dropoff_cat,
            'job_drive_time': drive_time,
            'est_end_time': est_end.strftime('%I:%M %p').lstrip('0'),
        }

        if i == 0:
            details['prev_job'] = None
            details['reposition_from'] = None
            details['reposition_drive_time'] = None
            details['buffer_minutes'] = None
            details['reasoning'] = (
                f"First job. Drive: {pickup_cat} \u2192 {dropoff_cat} ({drive_time} min)"
            )
        else:
            preceding = sorted_slots[i - 1]
            new_pickup_dt = datetime.combine(target_date, slot.pickup_time)
            # Match check_feasibility's chain inputs exactly (founder static model when
            # CHAIN_STATIC_TIMING) so the displayed buffer == the gate's buffer \u2014 a leg the
            # gate admits never reads as "late" here, and the prev-end shown is the same
            # static clear the founder computes by hand.
            if CHAIN_STATIC_TIMING:
                repo_drive = chain_repo_minutes(preceding.dropoff_location, slot.pickup_location, preceding.dropoff_category, pickup_cat)
            else:
                repo_drive = resolve_drive_minutes(preceding.dropoff_location, slot.pickup_location, preceding.dropoff_category, pickup_cat)
            cur_is_arrival = fg.is_airport_arrival(slot.trip_type, pickup_cat)
            same_terminal = (preceding.dropoff_category == pickup_cat)
            req_turn = fg.required_turnaround(repo_drive, cur_is_arrival, same_terminal=same_terminal)
            prev_end = _slot_chain_end(preceding, target_date)
            earliest = prev_end + timedelta(minutes=req_turn)
            buffer = int((new_pickup_dt - earliest).total_seconds() / 60)
            grace_note = (f" (already at {pickup_cat}, \u2212{fg.DEPLANING_GRACE_MIN}min deplaning grace)"
                          if cur_is_arrival and same_terminal
                          else (f" (full {repo_drive}min drive in, no deplaning credit off-airport)"
                                if cur_is_arrival else ""))

            details['prev_job'] = {
                'leg_id': preceding.leg_id,
                'end_time': prev_end.strftime('%I:%M %p').lstrip('0'),
                'dropoff_category': preceding.dropoff_category,
            }
            details['reposition_from'] = preceding.dropoff_category
            details['reposition_to'] = pickup_cat
            details['reposition_drive_time'] = repo_drive
            details['required_turnaround'] = max(0, req_turn)  # display: negative = deplaning slack
            details['buffer_minutes'] = buffer
            details['reasoning'] = (
                f"Prev job ends ~{prev_end.strftime('%I:%M %p').lstrip('0')} "
                f"at {preceding.dropoff_category}. "
                f"Turnaround needed: {max(0, req_turn)} min{grace_note} = {buffer} min spare. "
                f"Job drive: {pickup_cat} \u2192 {dropoff_cat} ({drive_time} min)"
            )

        details_map[slot.leg_id] = details

    return details_map


def _capture_timing_details(schedule: DriverDaySchedule, new_leg, target_date: date, inter_job_buffer: int = None) -> dict:
    """
    Capture the timing reasoning for adding this leg to the schedule.
    Returns dict with drive time, buffer, and route info so the user can see
    why the algorithm chose this job and spot incorrect drive time estimates.
    """
    from dispatching.analytics import categorize_location
    from dispatching import feasibility_guards as fg

    if inter_job_buffer is None:
        inter_job_buffer = INTER_JOB_BUFFER

    new_pickup_cat = categorize_location(new_leg.pickup_location)
    new_dropoff_cat = categorize_location(new_leg.dropoff_location)
    drive_time = resolve_drive_minutes(new_leg.pickup_location, new_leg.dropoff_location, new_pickup_cat, new_dropoff_cat)
    est_end = estimate_job_end_time(new_leg, target_date)

    details = {
        'pickup_category': new_pickup_cat,
        'dropoff_category': new_dropoff_cat,
        'job_drive_time': drive_time,
        'est_end_time': est_end.strftime('%I:%M %p').lstrip('0'),
    }

    if not schedule.slots:
        details['prev_job'] = None
        details['reposition_from'] = None
        details['reposition_drive_time'] = None
        details['buffer_minutes'] = None
        details['reasoning'] = f"First job. Drive: {new_pickup_cat} → {new_dropoff_cat} ({drive_time} min)"
    else:
        # Find the preceding slot
        new_pickup_dt = datetime.combine(target_date, new_leg.pickup_time)
        sorted_slots = sorted(schedule.slots, key=lambda s: s.pickup_time)
        preceding = None
        for slot in sorted_slots:
            if datetime.combine(target_date, slot.pickup_time) <= new_pickup_dt:
                preceding = slot

        if preceding:
            # Match check_feasibility's chain inputs (founder static model when
            # CHAIN_STATIC_TIMING) so the displayed buffer == the gate's buffer.
            if CHAIN_STATIC_TIMING:
                repo_drive = chain_repo_minutes(preceding.dropoff_location, new_leg.pickup_location, preceding.dropoff_category, new_pickup_cat)
            else:
                repo_drive = resolve_drive_minutes(preceding.dropoff_location, new_leg.pickup_location, preceding.dropoff_category, new_pickup_cat)
            cur_is_arrival = fg.is_airport_arrival(new_leg.get_trip_type(), new_pickup_cat)
            same_terminal = (preceding.dropoff_category == new_pickup_cat)
            req_turn = fg.required_turnaround(repo_drive, cur_is_arrival, same_terminal=same_terminal)
            prev_end = _slot_chain_end(preceding, target_date)
            earliest = prev_end + timedelta(minutes=req_turn)
            buffer = int((new_pickup_dt - earliest).total_seconds() / 60)
            grace_note = (f" (already at {pickup_cat}, −{fg.DEPLANING_GRACE_MIN}min deplaning grace)"
                          if cur_is_arrival and same_terminal
                          else (f" (full {repo_drive}min drive in, no deplaning credit off-airport)"
                                if cur_is_arrival else ""))

            details['prev_job'] = {
                'leg_id': preceding.leg_id,
                'end_time': prev_end.strftime('%I:%M %p').lstrip('0'),
                'dropoff_category': preceding.dropoff_category,
            }
            details['reposition_from'] = preceding.dropoff_category
            details['reposition_to'] = new_pickup_cat
            details['reposition_drive_time'] = repo_drive
            details['required_turnaround'] = max(0, req_turn)  # display: negative = deplaning slack
            details['buffer_minutes'] = buffer
            details['reasoning'] = (
                f"Prev job ends ~{prev_end.strftime('%I:%M %p').lstrip('0')} "
                f"at {preceding.dropoff_category}. "
                f"Turnaround needed: {max(0, req_turn)} min{grace_note} = {buffer} min spare. "
                f"Job drive: {new_pickup_cat} → {new_dropoff_cat} ({drive_time} min)"
            )
        else:
            details['prev_job'] = None
            details['reposition_from'] = None
            details['reposition_drive_time'] = None
            details['buffer_minutes'] = None
            details['reasoning'] = f"First job in window. Drive: {new_pickup_cat} → {new_dropoff_cat} ({drive_time} min)"

    return details


def _score_leg_for_smart_schedule(
    leg, schedule: DriverDaySchedule, feasibility: FeasibilityResult,
    preferred_trip_type: str, target_date: date, driver_tier: int = -1,
    eligible_others: int = -1, chain_count: int = 0, cfg=None,
    reserved_count: int = 0
) -> int:
    """Score a leg for smart schedule insertion. Higher = better fit."""
    from dispatching.analytics import categorize_location

    if cfg is None:
        from dispatching.models import SchedulerSettings
        cfg = SchedulerSettings.get_settings()

    score = cfg.base_score

    # Vehicle tier scoring
    if driver_tier >= 0:
        leg_vtype = leg.effective_vehicle_type
        leg_tier = get_vehicle_tier(leg_vtype) if leg_vtype else 0
        tier_diff = driver_tier - leg_tier

        if tier_diff == 0:
            score += cfg.tier_exact
        elif tier_diff == 1:
            score += cfg.tier_1_down
        elif tier_diff == 2:
            score += cfg.tier_2_down
        elif tier_diff == 3:
            score += cfg.tier_3_down
        else:
            score += cfg.tier_4_down

        # Vehicle reservation penalty
        if tier_diff > 0 and reserved_count > 0:
            score += cfg.reserve_penalty  # negative value

    # Scarcity bonus
    if eligible_others >= 0:
        if eligible_others == 0:
            score += cfg.scarcity_1
        elif eligible_others == 1:
            score += cfg.scarcity_2
        elif eligible_others == 2:
            score += cfg.scarcity_3
        elif eligible_others == 3:
            score += cfg.scarcity_4

    # Trip type preference
    trip_type = leg.get_trip_type()
    if preferred_trip_type and trip_type == preferred_trip_type:
        score += cfg.trip_pref_match
    elif preferred_trip_type and trip_type != preferred_trip_type:
        score += cfg.trip_pref_mismatch

    # Buffer quality
    buf = feasibility.buffer_minutes
    if 20 <= buf <= 30:
        score += cfg.sb_buffer_perfect
    elif 30 < buf <= 60:
        score += cfg.sb_buffer_sweet_spot
    elif 10 <= buf < 20:
        score += cfg.sb_buffer_tight
    elif 60 < buf <= 120:
        score += cfg.sb_buffer_good
    elif buf >= 999:
        score += cfg.sb_buffer_first_job

    # Location proximity
    if schedule.slots:
        pickup_cat = categorize_location(leg.pickup_location)
        last = schedule.slots[-1]
        if last.dropoff_category == pickup_cat:
            score += cfg.sb_loc_same_area

    # Schedule flow
    if schedule.slots:
        consecutive_arrivals = 0
        for slot in reversed(schedule.slots):
            if slot.trip_type == 'arrival':
                consecutive_arrivals += 1
            else:
                break

        if trip_type == 'arrival' and consecutive_arrivals >= 2:
            score += cfg.sb_flow_3rd_arrival  # negative value
        elif trip_type == 'arrival' and consecutive_arrivals == 1:
            score += cfg.sb_flow_2nd_arrival  # negative value
        elif trip_type in ('return', 'cruise') and consecutive_arrivals >= 1:
            score += cfg.sb_flow_break_bonus

    # Chain bonus
    if chain_count >= 3:
        score += cfg.chain_3_plus
    elif chain_count == 2:
        score += cfg.chain_2
    elif chain_count == 1:
        score += cfg.chain_1

    # Revenue bonus
    if leg.revenue_share and leg.revenue_share > 0:
        score += min(int(leg.revenue_share / cfg.revenue_divisor), cfg.revenue_cap)

    # Duty-span pricing (Span Governor): same marginal price as the general builder, at
    # SPAN_SEEDER_RATE_SCALE — the seeder seats only on score>0, so a full-rate term could
    # silently drop borderline legs from a Build-1st day the dispatcher explicitly asked for.
    from dispatching import feasibility_guards as fg
    if fg.ENFORCE_SPAN_CAPS and fg.SPAN_SOFT_PRICING and schedule.slots:
        _np_dt = datetime.combine(target_date, leg.pickup_time)
        _ne_dt = estimate_job_end_time(leg, target_date)
        score -= int(marginal_span_penalty(schedule.slots, target_date, _np_dt, _ne_dt)
                     * fg.SPAN_SEEDER_RATE_SCALE)

    return score


def _add_leg_to_schedule(schedule: DriverDaySchedule, leg, target_date: date):
    """Helper to add a leg to a working schedule."""
    from dispatching.analytics import categorize_location

    pickup_cat = categorize_location(leg.pickup_location)
    dropoff_cat = categorize_location(leg.dropoff_location)
    end_time = estimate_job_end_time(leg, target_date)

    customer_name = ""
    if leg.reservation and leg.reservation.customer:
        customer_name = leg.reservation.customer.get_full_name()

    leg_vtype = leg.effective_vehicle_type
    slot = ScheduleSlot(
        leg_id=leg.id,
        pickup_time=leg.pickup_time,
        pickup_location=leg.pickup_location,
        pickup_category=pickup_cat,
        dropoff_location=leg.dropoff_location,
        dropoff_category=dropoff_cat,
        trip_type=leg.get_trip_type(),
        estimated_end_time=end_time,
        reservation_id=leg.reservation_id,
        customer_name=customer_name,
        status=leg.status or 'in-progress',
        has_flight=False,
        revenue=leg.revenue_share,
        vehicle_type=str(leg_vtype) if leg_vtype else None,
        chain_clear_dt=chain_clear_dt(leg, target_date),
    )
    schedule.slots.append(slot)
    schedule.slots.sort(key=lambda s: s.pickup_time)


def update_drive_time_estimate(from_cat: str, to_cat: str, minutes: int) -> bool:
    """
    Update a drive time estimate in DRIVE_TIME_ESTIMATES.
    Updates both directions.
    Returns True if updated.
    """
    DRIVE_TIME_ESTIMATES[(from_cat, to_cat)] = minutes
    DRIVE_TIME_ESTIMATES[(to_cat, from_cat)] = minutes
    return True


def get_all_drive_time_categories() -> List[str]:
    """Return all unique location categories used in drive time estimates."""
    cats = set()
    for (a, b) in DRIVE_TIME_ESTIMATES.keys():
        cats.add(a)
        cats.add(b)
    return sorted(cats)


def get_coverage_stats(legs) -> dict:
    """Calculate in-house vs affiliate vs unassigned coverage."""
    stats = {
        'total': 0,
        'inhouse': 0,
        'affiliate': 0,
        'unassigned': 0,
        'inhouse_pct': 0,
        'affiliate_pct': 0,
        'unassigned_pct': 0,
        'inhouse_revenue': Decimal('0.00'),
        'affiliate_revenue': Decimal('0.00'),
        'unassigned_revenue': Decimal('0.00'),
        'total_revenue': Decimal('0.00'),
    }

    for leg in legs:
        stats['total'] += 1
        rev = leg.revenue_share or Decimal('0.00')
        stats['total_revenue'] += rev

        if not leg.driver:
            stats['unassigned'] += 1
            stats['unassigned_revenue'] += rev
        elif leg.driver.driver_type == 'inhouse':
            stats['inhouse'] += 1
            stats['inhouse_revenue'] += rev
        else:
            stats['affiliate'] += 1
            stats['affiliate_revenue'] += rev

    total = stats['total']
    if total > 0:
        stats['inhouse_pct'] = round(stats['inhouse'] / total * 100)
        stats['affiliate_pct'] = round(stats['affiliate'] / total * 100)
        stats['unassigned_pct'] = round(stats['unassigned'] / total * 100)

    return stats
