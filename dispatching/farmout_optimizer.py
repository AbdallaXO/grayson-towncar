"""
Farm-Out Opportunity-Cost Optimizer — read-only recommendations.

Answers the one comparison the founder makes by hand and that NOTHING in the codebase does
today: **"is it cheaper to farm this leg, or to keep it in-house and farm something else
instead?"** Farming is expensive ($70–230 vs $25–50 in-house — a $45–180 premium per leg,
``docs/scheduler-automation/scheduler_automation_phase2_5_findings.md`` §A.4), so *which* legs
we farm is real money.

POSTURE: THIS MODULE is strictly read-only. No model writes, no migrations — exactly like
``dispatching/fleet_intel.py`` (whose cost primitives this module reuses). It is a RETROSPECTIVE
grading tool — it judges a PAST day's farm decisions on the information available WHEN THE SCHEDULE
WAS BUILT (scheduled/decision-time flight arrival + each driver's real worked-day availability) and
NEVER suggests un-farming a committed leg. See the loud header below.

ACTING ON RECOMMENDATIONS: each Recommendation carries a ready-to-POST plan in
``detail["apply"]`` (ids + expected-assignment staleness map only — never dollars). The page's
Apply/Farm buttons send it to the SEPARATE write path ``dispatching/farmout_actions.py``, which
re-validates current DB state and writes through ``dispatching.assignment.set_leg_driver`` (the
sandbox front door). The analysis/pricing engine here stays write-free.

THE OBJECTIVE (design doc ``~/.claude/plans/you-are-continuing-the-composed-goose.md`` §3.1):
For a residual would-be-farmed "target" leg, compare two end-states that cover the SAME legs:
  (A) farm the target directly;
  (B) keep the target in-house by displacing an in-house leg and farming THAT instead.
Recommend B iff it preserves in-house coverage AND lowers total farm spend by >= a threshold
(default $20). Because guest revenue is identical in both states (every leg is served either
way) the comparison collapses to driver cost, and the per-leg decision reduces to:

    net_opportunity(B over A) = recovered_margin(target) - SUM recovered_margin(displaced)
    where recovered_margin(leg) = farm_base(leg) - inhouse_base(leg)   (>0 = expensive to farm)

i.e. keep the expensive-to-farm legs in-house, farm the cheap-to-farm ones — evaluated as a
realized board state, never a sum of independent per-leg claims (the marginal-vs-total fallacy
``fleet_intel.py`` documents at its top).

HARD RULES (never crossed):
  * VIP legs (``Reservation.is_vip`` OR the Small World Big Fun agency) are immovable BOTH ways —
    never farmed, never displaced. Resolved up front to a protected leg-id set (the ``Leg.is_vip``
    property returns False when the agency FK isn't loaded, so we never call it mid-search).
  * Never farm a TRUE "departure" (a non-Port/non-Sanford leg whose DROPOFF is an airport, e.g.
    hotel->MCO). A departure may move between in-house drivers but may never land in a farmed bundle;
    a farmed departure that CAN come in-house is surfaced as a sub-threshold "policy" rescue.
    PORT CANAVERAL & SANFORD are their OWN categories (Step 3) — NOT departures — judged purely on the
    net-spend math (see ``is_departure``).
  * Every placement is re-validated with the real ``scheduler.check_feasibility`` (turnaround +
    window). Vehicle-tier capability is enforced separately (per-affiliate ``max_vehicle_tier`` in
    ``_price_one_leg``) — note check_feasibility itself has NO vehicle gate.
  * Uncomputable economics (no route, no in-house base pay, no affiliate card) => ABSTAIN, never $0.

ROSTER (Architecture B): the farm-cost waterfall prices each leg against the WHOLE carded affiliate
roster, picking the cheapest eligible one. Per-affiliate capability / capacity / route-permit facts
live as DATA in ``drivers.models.AffiliateProfile`` (rates already live in ``DriverPayRate``). See the
loud header below.

PHASE 2 SCOPE: this module computes recommendations and powers the offline ``analyze_farmout_savings``
command. Tier-2 displacement is implemented at DEPTH 1 (displace one in-house leg to make room for
the target) — the clean, explainable "keep the arrival / farm the return" case that is ~90% of the
value. Deeper cascades and the dispatch-board panel are later phases. The waterfall + bundle data
structures already accept multi-leg sets so extending to deeper bundles is search, not re-architecture.
"""

from __future__ import annotations

import math
from copy import copy as _shallow_copy
from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal
from typing import Dict, List, Optional

from dispatching import fleet_intel as fi
from dispatching.analytics import categorize_location, is_airport_location
from dispatching.scheduler import (
    LIVE_DISTANCE_UNKNOWN_CATS,
    DriverDaySchedule,
    build_driver_schedules,
    check_feasibility,
    estimate_job_end_time,
    get_compatible_vehicle_types,
    get_vehicle_tier,
    load_all_driver_vtypes,
    preload_timing_cache,
    resolve_drive_minutes,
)
from dispatching.feasibility_guards import is_airport_arrival, required_turnaround
from dispatching.swap_optimizer import _get_conflicting_slots, _leg_to_slot, find_swaps

ZERO = Decimal("0.00")


# ═══════════════════════════════════════════════════════════════════════════════════════════
# ARCHITECTURE B — DATA-DRIVEN AFFILIATE ROSTER — READ BEFORE TRUSTING ANY NUMBER
# ═══════════════════════════════════════════════════════════════════════════════════════════
# The farm-cost WATERFALL prices each would-be farm-out against the WHOLE carded affiliate roster
# (the Waleed-only validation pass is over). For each leg it picks the CHEAPEST ELIGIBLE affiliate,
# each quoted from their REAL ``DriverPayRate`` rows via ``pay_calc._find_rate`` (vehicle + direction
# aware — handles flat, per-vehicle, and per-direction cards identically). The per-affiliate facts the
# pricing layer CANNOT infer from rates — capability, capacity, and route/permit rules — now live as
# DATA in ``drivers.models.AffiliateProfile`` (one row per affiliate), NOT as code:
#   • CAPABILITY (``max_vehicle_tier``): the highest vehicle class the affiliate can serve. LOAD-BEARING
#     for FLAT all-vehicle cards (one NULL-vehicle row matches EVERY class incl. 14-pax, so without a
#     tier cap a sedan-only affiliate would be wrongly quoted for a van). PER-VEHICLE cards self-gate
#     (a missing vehicle row → _find_rate None → ineligible), so a tier is optional for them.
#   • CAPACITY (``capacity_mode`` + ``daily_cap``): single_chain (one physical vehicle, limited by the
#     feasibility chain — Oualid), count_cap (finite seats/day — Anthony=12), or fleet (treated as a
#     higher count cap; true N-parallel-chains deferred). Replaces the old hardcoded
#     ``ANTHONY_MAX_LEGS_PER_DAY`` constant and ``oualid_chain``.
#   • ROUTE/PERMIT (``no_pickup_at_port_sanford``): drop-off-only at Port Canaveral / Sanford, never a
#     pickup (Waleed's permit rule) — excludes any leg ORIGINATING at Port/Sanford.
# ROSTER SCOPE (founder decision): only RATE-READY affiliates (≥1 DriverPayRate row) enter the roster.
# A carded affiliate with NO ``AffiliateProfile`` is still priced, but with no capability cap — safe
# for per-vehicle cards, a mispricing risk for flat cards; both are SURFACED in the command's roster
# audit (alongside affiliates that received farm-out legs but have no card at all) so the founder sees
# exactly which config/cards to add next. Uncarded affiliates are never invented a price — abstain.
# DATA-QUALITY: a ``base_pay`` of $0 (e.g. an unset van row) is treated as UNCARDED, never quoted as a
# free farm-out.
#
# ── RETROSPECTIVE TOOL — grades PAST decisions on DECISION-TIME information ──────────────────────
# This is NOT a live/intraday tool and NEVER suggests un-farming a committed leg (the founder can't
# take jobs back from affiliates same-day). It replays a past day and asks "was this farmed leg
# KEEPABLE in-house given what was known when the schedule was built?" Two things keep that grade fair:
#   • SCHEDULED-TIME feasibility: arrivals are anchored to the SCHEDULED (filed) flight time, not
#     best_arrival_local() (estimated/actual = hindsight). A flight DELAYED after the build no longer
#     makes a driver look retroactively free. (scheduler.USE_SCHEDULED_ARRIVAL_FOR_EVAL, set only here.)
#   • REAL driver availability: every in-house rescue is bounded to the driver's ACTUAL worked day
#     (his assigned-leg span — _worked_span_window), NOT the observed-history STUB. A rescue is only
#     proposed into a GENUINE gap in a driver who was actually working; idle/zero-leg drivers are not
#     receivers. (USE_STUB_WINDOWS stays True globally; the optimizer bypasses it via driver_windows.)
#
# PORT CANAVERAL & SANFORD ARE THEIR OWN LEG CATEGORIES: NOT "departure", NOT "arrival". They get NO
# automatic in-house protection (see ``is_departure``) — judged purely on the net-farm-spend math. Only
# a TRUE departure (non-Port/Sanford dropoff = airport) keeps its "belongs in-house" protection.
#
# The minivan==SUV pricing equivalence (_pricing_vehicles) lets a minivan leg quote an affiliate's SUV
# row as a FALLBACK (an explicit minivan row wins) — mirrored in pay_calc.calculate_driver_pay so the
# auto-filled pay on a real assignment equals the quote.
# IF AN AFFILIATE'S RATES, CAPABILITY, CAPACITY, OR PERMITS CHANGE, update their DriverPayRate rows and
# AffiliateProfile — the engine reads them live; no code change is needed.
#
# ── DRIVE-TIME REALISM (Approach A — "uncomputable, not zero" for FAR/unknown destinations) ──────
# Feasibility uses the scheduler's coarse Orlando category table (scheduler.DRIVE_TIME_ESTIMATES),
# now CALIBRATED to the founder's real drive times (MCO↔Disney 30, MCO↔Universal 25, MCO↔SFB 60,
# Disney↔Port 72). The danger: categorize_location lumps any unrecognized address into a catch-all
# bucket — LIVE_DISTANCE_UNKNOWN_CATS {Other, Residential, Other Hotel} — that can be a LOCAL Orlando
# stop OR a far/out-of-area one ("19727 Gulf Blvd, Indian Shores" → 'Residential', really ~90-120min),
# and with USE_LIVE_DISTANCE off the table prices it as a ~25-35min local hop → a PHANTOM-FEASIBLE
# reshuffle. So when a TARGET leg's pickup OR dropoff lands in one of those buckets we ABSTAIN — exclude
# it from any reshuffle/keep recommendation (``_drive_uncomputable_far``) — rather than guess. This is
# the same "uncarded → uncomputable → abstain" discipline used for rates. BROAD (founder-chosen): any
# unplaceable endpoint abstains, so zero phantom-feasible far legs (genuinely-local hotels/homes caught
# are LISTED in the report for review). RESIDUAL: far endpoints on a DISPLACED/NEIGHBOR leg still use the
# coarse table (target-only guard, to avoid gutting the tool) — closed by live-distance (Approach B),
# which is DEFERRED. This guard is OPTIMIZER-ONLY: it never touches the live scheduler's drive math.
# ═══════════════════════════════════════════════════════════════════════════════════════════
OUALID_DRIVER_ID = 7
ANTHONY_DRIVER_ID = 29

# Default per-day count cap for count_cap affiliates whose AffiliateProfile.daily_cap is unset
# (Anthony's calibrated ~12/day). The command reports each affiliate's ACTUAL realized load so the
# founder can tune AffiliateProfile.daily_cap against reality.
ANTHONY_MAX_LEGS_PER_DAY = 12

# Capacity-mode tokens — mirror drivers.models.AffiliateProfile.CAP_* (kept here so the hot pricing
# path needs no model import at module load).
_CAP_SINGLE_CHAIN = "single_chain"
_CAP_COUNT = "count_cap"
_CAP_FLEET = "fleet"

# Default discretionary-savings threshold ($). Founder-calibrated: real swap arbitrage is usually
# ~$20+ (e.g. a towncar return vs a slightly pricier job); $100 only ever caught extreme mismatches
# (towncar return vs a Port Canaveral van) and hid almost every useful swap. Tunable per-run/per-page.
DEFAULT_MIN_SAVINGS = Decimal("20.00")


# ── Affiliate roster resolution (Architecture B — data-driven) ─────────────────────────────────
def resolve_affiliate_roster():
    """Resolve the data-driven farm-out roster: every ACTIVE affiliate that is RATE-READY (has >=1
    ``DriverPayRate`` row), paired with its ``AffiliateProfile`` (or None). Returns
    ``(roster, warnings, profileless_flat)`` where:

      * ``roster``           = list of ``(Driver, AffiliateProfile|None)``, the waterfall candidates.
      * ``warnings``         = loud human-readable strings (surfaced by the command).
      * ``profileless_flat`` = names of carded affiliates whose card is FLAT (has a NULL-vehicle row)
        but who have NO ``max_vehicle_tier`` — a mispricing risk (their flat row matches every class),
        surfaced for the founder to add a capability cap.

    Uncarded affiliates are intentionally EXCLUDED (uncarded -> uncomputable -> abstain, never an
    invented price); the command separately reports uncarded affiliates that nonetheless received
    real farm-out legs in range, so the gap is visible, never silent."""
    from drivers.models import Driver, DriverPayRate, AffiliateProfile

    warnings: List[str] = []
    roster = []
    profileless_flat: List[str] = []

    affiliates = (Driver.objects.filter(driver_type="affiliate", is_active=True)
                  .select_related("profile").order_by("id"))
    for d in affiliates:
        rates = DriverPayRate.objects.filter(driver=d)
        if not rates.exists():
            continue  # uncarded -> not a pricing candidate (abstain)
        prof = AffiliateProfile.objects.filter(driver=d).first()
        roster.append((d, prof))
        has_flat_row = rates.filter(vehicle__isnull=True).exists()
        if has_flat_row and (prof is None or not prof.max_vehicle_tier):
            profileless_flat.append(str(d))

    if not roster:
        warnings.append("NO carded active affiliate found — ALL farm-out pricing is UNCOMPUTABLE "
                        "(abstain). Add DriverPayRate rows before trusting any recommendation.")
    return roster, warnings, profileless_flat


# ── VIP protection (resolved up front; never call leg.is_vip mid-search) ───────────────────────
def resolve_protected_vip_leg_ids(legs) -> frozenset:
    """Return the set of leg ids that are VIP (reservation flag OR Small World Big Fun agency),
    excluded from BOTH the displaceable and the farmable sets. Re-queries with the agency relation
    loaded so the query-safe ``Leg.is_vip`` property (reservations/models.py:1781) returns the
    CORRECT answer (it returns False when the agency FK isn't loaded)."""
    from reservations.models import Leg

    leg_ids = [l.id for l in legs]
    if not leg_ids:
        return frozenset()
    qs = (Leg.objects.filter(id__in=leg_ids)
          .select_related("reservation", "reservation__travel_agent",
                          "reservation__travel_agent__agency"))
    return frozenset(l.id for l in qs if l.is_vip)


_PORT_SANFORD_CATS = ("Port Canaveral Area", "SFB Terminal")


def is_port_or_sanford(text) -> bool:
    """True if a free-text location is Port Canaveral OR Sanford (SFB). Uses the single-source
    ``categorize_location`` so we agree with the scheduler's drive-time buckets. Both are their OWN
    leg categories this pass (Step 3) and drive Waleed's directional drop-off rule (Step 2)."""
    return categorize_location(text or "") in _PORT_SANFORD_CATS


def port_sanford_direction_tag(leg) -> Optional[str]:
    """INFO-only positioning signal (NOT a hard rule) for a Port/Sanford leg, for the audit + UI:
      * 'to-port'/'to-sanford'   -> DROPOFF is Port/Sanford; vehicle ends ~1h out, return work rare.
      * 'from-port'/'from-sanford' -> PICKUP is Port/Sanford; vehicle ends in Orlando (work plentiful),
        but Waleed is EXCLUDED from these (no pickup permit — Step 2).
    Returns None for non-Port/Sanford legs. The end-state feasibility chain already rewards good
    positioning because drive times are honest; this tag only labels it."""
    drop_cat = categorize_location(leg.dropoff_location or "")
    pick_cat = categorize_location(leg.pickup_location or "")
    if drop_cat == "Port Canaveral Area":
        return "to-port"
    if drop_cat == "SFB Terminal":
        return "to-sanford"
    if pick_cat == "Port Canaveral Area":
        return "from-port"
    if pick_cat == "SFB Terminal":
        return "from-sanford"
    return None


def is_departure(leg) -> bool:
    """A TRUE 'departure' = a non-Port/non-Sanford trip whose DROPOFF is an airport (hotel->MCO,
    home->MCO). Only true departures keep automatic 'belongs in-house' protection.

    Port Canaveral and Sanford are their OWN categories (Step 3): any leg whose pickup OR dropoff is
    Port/Sanford is NOT a departure and gets NO auto-protection — it's judged purely on the
    net-farm-spend math (the founder's goal: maximize in-house coverage + profit, whatever the math
    says). This de-protects what the old detector wrongly shielded: Sanford-dropoff legs (Sanford is
    an airport to ``is_airport_location``) AND Port-pickup->MCO legs (e.g. cruise terminal -> MCO)."""
    if is_port_or_sanford(leg.pickup_location) or is_port_or_sanford(leg.dropoff_location):
        return False
    return is_airport_location((leg.dropoff_location or "").lower())


def _drive_uncomputable_far(leg) -> bool:
    """Approach A — 'uncomputable, not zero' for FAR/unknown destinations (see the loud header).

    ``categorize_location`` maps any of ``LIVE_DISTANCE_UNKNOWN_CATS`` {Other, Residential, Other Hotel}
    to a catch-all bucket that can be a LOCAL Orlando address OR a far/out-of-area one (e.g.
    "19727 Gulf Blvd, Indian Shores" -> 'Residential', really ~90-120min). With USE_LIVE_DISTANCE off the
    coarse table prices it as a ~25-35min local hop -> a PHANTOM-FEASIBLE reshuffle. We cannot verify a
    reshuffle's feasibility for such a leg, so the optimizer ABSTAINS on it (excludes it from any
    reshuffle/keep recommendation) rather than guessing — the same discipline as uncarded rates. BROAD:
    EITHER endpoint unplaceable abstains. Target-only (neighbor/displaced legs still use the coarse
    table); live-distance (Approach B) closes that residual and is deferred."""
    return (categorize_location(leg.pickup_location or "") in LIVE_DISTANCE_UNKNOWN_CATS
            or categorize_location(leg.dropoff_location or "") in LIVE_DISTANCE_UNKNOWN_CATS)


# ═══════════════════════════════════════════════════════════════════════════════════════════
# CAPACITY-AWARE FARM-COST WATERFALL  (the genuinely new, capacity-safe pricing)
# ═══════════════════════════════════════════════════════════════════════════════════════════
@dataclass
class _AffiliateCapacity:
    """Per-affiliate remaining-capacity state for one day. ``single_chain`` consumes capacity by
    appending to ``chain`` (feasibility-limited); ``count_cap``/``fleet`` consume by incrementing
    ``count`` against ``cap`` (None = unlimited)."""
    driver_id: int
    name: str
    mode: str
    chain: Optional[DriverDaySchedule] = None  # single_chain only
    count: int = 0                             # count_cap / fleet
    cap: Optional[int] = None                  # count_cap / fleet legs-per-day; None = unlimited


@dataclass
class WaterfallLedger:
    """Per-day shared affiliate capacity, keyed by driver id. Pricing a bundle consumes capacity here
    so the same single-vehicle slot / count seat is never counted twice across a day's recommendations.
    Built from the resolved roster so each affiliate's capacity model comes from its AffiliateProfile."""

    caps: Dict[int, _AffiliateCapacity] = field(default_factory=dict)

    @classmethod
    def for_roster(cls, roster) -> "WaterfallLedger":
        """``roster`` = list of (Driver, AffiliateProfile|None). Affiliates without a profile default
        to single_chain (one physical vehicle) — the conservative assumption."""
        caps: Dict[int, _AffiliateCapacity] = {}
        for drv, prof in roster:
            mode = prof.capacity_mode if prof else _CAP_SINGLE_CHAIN
            cap = prof.daily_cap if (prof and prof.capacity_mode != _CAP_SINGLE_CHAIN) else None
            if mode == _CAP_COUNT and cap is None:
                cap = ANTHONY_MAX_LEGS_PER_DAY  # calibrated default for an uncapped count affiliate
            chain = (DriverDaySchedule(drv.id, str(drv), "affiliate")
                     if mode == _CAP_SINGLE_CHAIN else None)
            caps[drv.id] = _AffiliateCapacity(drv.id, str(drv), mode, chain, 0, cap)
        return cls(caps)

    def copy(self) -> "WaterfallLedger":
        new: Dict[int, _AffiliateCapacity] = {}
        for did, c in self.caps.items():
            chain = (DriverDaySchedule(c.chain.driver_id, c.chain.driver_name,
                                       c.chain.driver_type, list(c.chain.slots))
                     if c.chain is not None else None)
            new[did] = _AffiliateCapacity(c.driver_id, c.name, c.mode, chain, c.count, c.cap)
        return WaterfallLedger(new)

    def load_by_name(self) -> Dict[str, int]:
        """{affiliate_name: realized legs this day} — chain length for single_chain, count otherwise."""
        return {c.name: (len(c.chain.slots) if c.chain is not None else c.count)
                for c in self.caps.values()}


# Per-leg waterfall outcome statuses.
_OK = "ok"
_UNCARDED = "uncarded"        # neither curated affiliate cards this route -> uncomputable (abstain)
_OVER_CAP = "over_capacity"   # would exceed Anthony's cap -> bundle infeasible (reject)


def _find_rate(driver, route, vehicle, direction):
    """Thin wrapper around pay_calc._find_rate (the EXACT vehicle+direction-aware priority lookup
    Leg.save uses) so the quoted affiliate price equals what would actually be paid IF that affiliate
    were assigned. Handles BOTH rate shapes: flat all-vehicle (Oualid/Anthony) AND per-vehicle-class/
    per-direction (e.g. Cheapo Limo). We NEVER hardcode a per-affiliate rate constant."""
    from drivers.pay_calc import _find_rate as _fr
    return _fr(driver, route, vehicle, direction)


_SUV_VEHICLE_CACHE: list = []  # [Vehicle|None] — lazy, command-process scoped


def _suv_vehicle():
    """The canonical rates.Vehicle of type 'suv', used for the minivan->SUV pricing collapse."""
    if not _SUV_VEHICLE_CACHE:
        from rates.models import Vehicle
        _SUV_VEHICLE_CACHE.append(Vehicle.objects.filter(vehicle_type="suv").first())
    return _SUV_VEHICLE_CACHE[0]


def _pricing_vehicles(leg):
    """``(primary, fallback)`` rates.Vehicles to PRICE this leg at. PRICING-TIER EQUIVALENCE:
    minivan == SUV for farm-out pricing (distinct from VEHICLE_TIER_ORDER capability tiers) — a
    minivan leg whose affiliate has NO minivan row is quoted at their SUV rate. Implemented as a
    FALLBACK (primary = the raw reservation vehicle, exactly what pay_calc books; SUV second) so
    an explicit minivan row always wins and the quote equals the pay Leg.save will actually
    auto-fill — ``pay_calc.calculate_driver_pay`` applies the SAME fallback. Flat all-vehicle
    affiliates (NULL-vehicle row) are unaffected either way."""
    base_vehicle = leg.reservation.vehicle if leg.reservation_id else None
    if (leg.effective_vehicle_type or "") == "mini_van":
        return base_vehicle, _suv_vehicle()
    return base_vehicle, None


def _commit_chain(capst, leg, day):
    """Closure that appends ``leg`` to a single_chain affiliate's growing day chain."""
    def _c():
        capst.chain.slots.append(_leg_to_slot(leg, day))
    return _c


def _commit_count(capst):
    """Closure that consumes one count_cap/fleet seat."""
    def _c():
        capst.count += 1
    return _c


def _leg_pricing_ctx(leg) -> dict:
    """Per-leg facts the affiliate gates need, computed once per leg (route / direction /
    pricing-vehicle collapse / capability tier / Port-Sanford origination)."""
    from drivers.pay_calc import _determine_direction

    pveh, pveh_fallback = _pricing_vehicles(leg)  # raw vehicle first, SUV fallback for minivan
    return {
        "route": leg.route if leg.route_id else None,
        "direction": _determine_direction(leg),
        "pveh": pveh,
        "pveh_fallback": pveh_fallback,
        "tier": get_vehicle_tier(leg.effective_vehicle_type or ""),
        "pickup_port_sanford": is_port_or_sanford(leg.pickup_location),
    }


# Tier index of 'van' in VEHICLE_TIER_ORDER — van and Van(14 Pax) are the special-capacity
# classes a flat card must never auto-claim (see _gate_affiliate step 1b).
_VAN_TIER = get_vehicle_tier("van")


def _has_explicit_vehicle_rate(driver, route, vehicle) -> bool:
    """True if the affiliate cards THIS route + THIS vehicle class with a dedicated row —
    the proof of both capability and a real (non-collapsed) price for van-class jobs."""
    from drivers.models import DriverPayRate
    if vehicle is None or route is None:
        return False
    return DriverPayRate.objects.filter(driver=driver, route=route, vehicle=vehicle).exists()


def _gate_affiliate(ctx, drv, prof):
    """The CAPABILITY / PERMIT / RATE gates (steps 1-3 of the waterfall), shared by the engine
    (_price_one_leg), the page's override picker (quote_affiliate_options), and the apply
    endpoint's server-side re-validation — one implementation so they can never disagree.
    Returns ``(base, None)`` when the affiliate can be quoted, else ``(None, reason)`` with
    reason in {'vehicle_tier', 'van_unproven', 'port_pickup_permit', 'no_rate'}."""
    # 1. CAPABILITY tier cap (explicit; load-bearing for flat all-vehicle cards).
    if prof and prof.max_vehicle_tier:
        ptier = get_vehicle_tier(prof.max_vehicle_tier)
        if ctx["tier"] == -1 or ptier == -1 or ctx["tier"] > ptier:
            return None, "vehicle_tier"
    elif ctx["tier"] >= _VAN_TIER:
        # 1b. NO capability cap on file (no profile, or blank tier): NEVER assume van / 14-pax
        # capability. A flat all-vehicle row would otherwise quote an SUV-only affiliate for a
        # van job at the SUV price (the Shaq case — wrong vehicle AND wrong rate). An EXPLICIT
        # van-class rate row is the proof of both; otherwise the founder opts the affiliate in
        # by setting AffiliateProfile.max_vehicle_tier. Classes below van keep matching the
        # flat row (that's what a flat sedan/SUV card means).
        if not _has_explicit_vehicle_rate(drv, ctx["route"], ctx["pveh"]):
            return None, "van_unproven"
    # 2. PERMIT — drop-off-only at Port/Sanford => never originate there.
    if prof and prof.no_pickup_at_port_sanford and ctx["pickup_port_sanford"]:
        return None, "port_pickup_permit"
    # 3. RATE from the real card (positive only; a $0 row is dirty data -> treated as uncarded).
    # Primary lookup uses the RAW reservation vehicle (= what pay_calc books); the minivan->SUV
    # equivalence is a FALLBACK, mirrored in pay_calc, so quote == auto-filled pay.
    base = _find_rate(drv, ctx["route"], ctx["pveh"], ctx["direction"])
    if (base is None or base <= 0) and ctx["pveh_fallback"] is not None:
        base = _find_rate(drv, ctx["route"], ctx["pveh_fallback"], ctx["direction"])
    if base is None or base <= 0:
        return None, "no_rate"
    return base, None


def _price_one_leg(leg, day, ledger, roster) -> dict:
    """Price ONE leg by the CHEAPEST ELIGIBLE affiliate across the whole roster, each quoted from
    their REAL DriverPayRate rows via _find_rate (vehicle+direction aware) with the minivan->SUV
    pricing collapse. MUTATES ``ledger`` only on a successful assignment. Returns
    {status, affiliate, affiliate_id, base, night, total, leg_id}.

    ``roster`` = list of (Driver, AffiliateProfile|None). Eligibility per affiliate: the shared
    ``_gate_affiliate`` CAPABILITY / PERMIT / RATE gates, then
      4. CAPACITY — single_chain: the leg must fit the growing feasibility chain (check_feasibility);
         count_cap/fleet: remaining seats > 0.
    Cheapest base (then cheapest night bonus) wins. check_feasibility has NO vehicle gate, so the
    capability gate is load-bearing."""
    from drivers.pay_calc import calculate_night_bonus

    ctx = _leg_pricing_ctx(leg)
    if ctx["route"] is None:
        return {"status": _UNCARDED, "leg_id": leg.id, "affiliate": None, "affiliate_id": None,
                "base": None, "night": None, "total": None}

    eligible = []            # (base, affiliate_name, night, commit_callable, driver_id)
    carded_but_full = False  # some affiliate cards the route but has no remaining capacity

    for drv, prof in roster:
        base, _reason = _gate_affiliate(ctx, drv, prof)
        if base is None:
            continue
        capst = ledger.caps.get(drv.id)
        if capst is None:
            continue
        night = calculate_night_bonus(drv, leg.pickup_time)
        # 4. CAPACITY.
        if capst.mode == _CAP_SINGLE_CHAIN:
            if check_feasibility(capst.chain, leg, day).feasible:
                eligible.append((base, capst.name, night, _commit_chain(capst, leg, day), drv.id))
            else:
                carded_but_full = True
        else:  # count_cap / fleet
            if capst.cap is None or capst.count < capst.cap:
                eligible.append((base, capst.name, night, _commit_count(capst), drv.id))
            else:
                carded_but_full = True

    if eligible:
        eligible.sort(key=lambda c: (c[0], c[2]))  # cheapest base, then cheapest night
        base, name, night, commit, drv_id = eligible[0]
        commit()
        return {"status": _OK, "leg_id": leg.id, "affiliate": name, "affiliate_id": drv_id,
                "base": base, "night": night, "total": base + night}
    if carded_but_full:
        return {"status": _OVER_CAP, "leg_id": leg.id, "affiliate": None, "affiliate_id": None,
                "base": None, "night": None, "total": None}
    return {"status": _UNCARDED, "leg_id": leg.id, "affiliate": None, "affiliate_id": None,
            "base": None, "night": None, "total": None}


@dataclass
class WaterfallQuote:
    feasible: bool                 # all legs priced AND none over capacity
    uncomputable: bool             # at least one leg is uncarded (abstain)
    total_base: Optional[Decimal]  # sum of card base_pay (used for the decision; night excluded)
    total_with_night: Optional[Decimal]  # real spend incl. night bonus (display)
    per_leg: List[dict] = field(default_factory=list)
    by_affiliate: Dict[str, int] = field(default_factory=dict)  # {'oualid': n, 'anthony': m}


def price_farm_waterfall(legs, day, ledger, roster) -> WaterfallQuote:
    """Price a SET of hypothetical farm-outs as ONE shared allocation against ``ledger``.
    Sorts by pickup time so each single-vehicle chain is built chronologically. Operate on a ledger
    COPY if you don't want to commit capacity (the caller passes ledger.copy() to price an alternative).

    A bundle is INFEASIBLE if any leg exceeds an affiliate's capacity with no cheaper-or-equal
    alternative (reject per the hard capacity rule); UNCOMPUTABLE if any leg is uncarded (abstain —
    never price as $0)."""
    legs_sorted = sorted(legs, key=lambda l: l.pickup_time or time.min)
    per_leg, by_aff = [], {}
    base_sum, night_sum = ZERO, ZERO
    over_cap = uncarded = False
    for leg in legs_sorted:
        r = _price_one_leg(leg, day, ledger, roster)
        per_leg.append(r)
        if r["status"] == _OK:
            base_sum += r["base"]
            night_sum += (r["night"] or ZERO)
            by_aff[r["affiliate"]] = by_aff.get(r["affiliate"], 0) + 1
        elif r["status"] == _OVER_CAP:
            over_cap = True
        else:
            uncarded = True
    feasible = not over_cap and not uncarded
    return WaterfallQuote(
        feasible=feasible,
        uncomputable=uncarded,
        total_base=base_sum if feasible else None,
        total_with_night=(base_sum + night_sum) if feasible else None,
        per_leg=per_leg,
        by_affiliate=by_aff,
    )


def cheapest_affiliate_for_leg(leg, day, ledger, roster) -> dict:
    """Single-leg convenience: what the waterfall would charge to farm this one leg, given remaining
    day capacity in ``ledger`` (priced on a COPY — does not consume capacity). Returns the per-leg
    waterfall dict (status/affiliate/base/night/total)."""
    return _price_one_leg(leg, day, ledger.copy(), roster)


def quote_affiliate_options(leg, day, ledger, roster):
    """EVERY eligible affiliate's quote for farming ``leg`` against the remaining day capacity in
    ``ledger`` — READ-ONLY (nothing committed). The engine's own decisions stay cheapest-first
    (_price_one_leg); this exists so the founder can deliberately farm to a NON-cheapest affiliate
    from the page (a human override, fully priced). Returns ``(options, skipped)``:

      * ``options`` = [{driver_id, name, base, night, total}] sorted (base, night) — same
        eligibility gates AND capacity check as _price_one_leg, so anything listed here is a
        choice the waterfall itself would have accepted.
      * ``skipped`` = [{driver_id, name, reason}] with reason in {'no_route', 'vehicle_tier',
        'port_pickup_permit', 'no_rate', 'over_capacity'} — audit/display only.
    """
    from drivers.pay_calc import calculate_night_bonus

    ctx = _leg_pricing_ctx(leg)
    options, skipped = [], []
    if ctx["route"] is None:
        return options, [{"driver_id": d.id, "name": str(d), "reason": "no_route"}
                         for d, _ in roster]
    for drv, prof in roster:
        base, reason = _gate_affiliate(ctx, drv, prof)
        if base is not None:
            capst = ledger.caps.get(drv.id)
            has_capacity = (capst is not None
                            and (check_feasibility(capst.chain, leg, day).feasible
                                 if capst.mode == _CAP_SINGLE_CHAIN
                                 else (capst.cap is None or capst.count < capst.cap)))
            if has_capacity:
                night = calculate_night_bonus(drv, leg.pickup_time)
                options.append({"driver_id": drv.id, "name": capst.name,
                                "base": base, "night": night, "total": base + night})
                continue
            reason = "over_capacity"
        skipped.append({"driver_id": drv.id, "name": str(drv), "reason": reason})
    options.sort(key=lambda o: (o["base"], o["night"]))
    return options, skipped


# ═══════════════════════════════════════════════════════════════════════════════════════════
# OPPORTUNITY-COST EVALUATION  (per target leg, on a replayed read-only board)
# ═══════════════════════════════════════════════════════════════════════════════════════════
@dataclass
class Recommendation:
    target_leg_id: int
    kind: str                       # 'free_rescue' | 'opportunity_swap' | 'policy_departure_rescue'
    target_is_departure: bool
    # what changes
    keep_in_house_driver_id: Optional[int]   # who would take the target in-house
    farmed_leg_ids: List[int]                # legs newly farmed in state B ([] for free rescue)
    farm_affiliate_mix: Dict[str, int]       # {'oualid': n, 'anthony': m} for the farmed bundle
    # economics (base-only decision figures; *_with_night for display)
    state_a_farm_base: Optional[Decimal]     # cost to farm the target (counterfactual, waterfall)
    state_b_farm_base: Optional[Decimal]     # cost to farm the displaced bundle (waterfall)
    net_savings: Optional[Decimal]           # state_a_farm_base - state_b_farm_base  (>0 = B cheaper)
    target_actual_farm_cost: Optional[Decimal]  # what we REALLY paid to farm the target (stored; None
    #                                             for an UNASSIGNED leftover — nothing was paid)
    reason: str                              # plain-English, dollars shown, no score
    # Decision-support mode: True when the target is an UNASSIGNED leftover (founder hand-built the
    # schedule and left it for the tool to place vs farm), False for a retrospective affiliate-farmed
    # target. Drives mode-aware wording ("would cost ~$X to farm" vs "actually paid").
    target_is_unassigned: bool = False
    target_hypothetical_farm_cost: Optional[Decimal] = None  # cheapest-affiliate waterfall quote to
    #                                             farm an unassigned target (the State-A basis here)
    detail: dict = field(default_factory=dict)


def _recovered_margin(farm_base: Optional[Decimal], leg) -> Optional[Decimal]:
    """farm_base - inhouse_base. None (abstain) if either side is uncomputable."""
    inh = fi.inhouse_counterfactual_cost(leg)
    if farm_base is None or inh is None:
        return None
    return farm_base - inh


def _apply_swap_solution(board, solution, legs_by_id, day):
    """COMMIT a tier-1 find_swaps solution to the shared in-house board so subsequent targets see
    the consumed capacity (the fix for the marginal-vs-total fallacy among rescues). Each move
    re-homes a leg onto an in-house driver; nothing is farmed (find_swaps guarantees this)."""
    move_to = {mv.leg_id: mv.to_driver_id for mv in solution.moves}
    affected = set(move_to.values())
    for sched in board.values():                       # pull every moved leg from wherever it sits
        sched.slots = [s for s in sched.slots if s.leg_id not in move_to]
    for leg_id, to_did in move_to.items():             # and seat it on its destination driver
        leg = legs_by_id.get(leg_id)
        if leg is None or to_did not in board:
            continue
        board[to_did].slots.append(_leg_to_slot(leg, day))
    for did in affected:
        if did in board:
            board[did].slots.sort(key=lambda s: s.pickup_time)


def _apply_tier2_to_board(board, target, displaced_id, keep_driver_id, day):
    """COMMIT a depth-1 displace-and-farm move to the shared board: remove the displaced leg from
    its driver (it becomes farmed) and seat the target in-house on keep_driver_id."""
    for sched in board.values():
        sched.slots = [s for s in sched.slots if s.leg_id != displaced_id]
    if keep_driver_id in board:
        board[keep_driver_id].slots.append(_leg_to_slot(target, day))
        board[keep_driver_id].slots.sort(key=lambda s: s.pickup_time)


def _fmt_time(t) -> str:
    """Portable 12-hour time format (avoids platform-specific strftime %-I/%#I)."""
    if t is None:
        return ""
    h12 = ((t.hour - 1) % 12) + 1
    return f"{h12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def _driver_name(ctx, did) -> str:
    """Human-readable driver name from the replayed board (read-only; no query)."""
    sched = ctx.board.get(did)
    return sched.driver_name if sched is not None else f"driver {did}"


def _leg_display(leg) -> dict:
    """Compact human-readable leg fields for the report/UI (read-only). Customer is read from the
    already-loaded reservation->customer relation; guarded so it never N+1s or raises."""
    cust = ""
    try:
        if leg.reservation_id and leg.reservation and leg.reservation.customer:
            cust = leg.reservation.customer.get_full_name()
    except Exception:
        cust = ""
    return {
        "leg_id": leg.id,
        "pickup": leg.pickup_location,
        "dropoff": leg.dropoff_location,
        "time": _fmt_time(leg.pickup_time),
        "customer": cust,
        "vehicle_type": leg.effective_vehicle_type or "",
        "direction_tag": port_sanford_direction_tag(leg),
    }


# Arrivals auto-bump their stored pickup_time to TRACK flight delays (flight_verify_views.py fires
# at >= 15 min moves), so a delayed arrival's pickup is itself a HINDSIGHT time. The 15-min threshold
# below mirrors that auto-bump trigger — below it the pickup was not bumped, so we must not shift it.
_ARRIVAL_PICKUP_BUMP_MIN = 15


def _apply_decision_time_pickups(day_legs, day) -> None:
    """Rewrite each ARRIVAL leg's pickup_time IN-MEMORY (never saved) to the DECISION-TIME pickup the
    dispatcher saw when the schedule was built. The stored pickup tracks the flight's best (delayed)
    arrival 1:1; the build-time pickup = scheduled arrival + that same offset
    = stored_pickup - (best_arrival - scheduled_arrival). Without this, a delayed arrival sits in the
    board at its delayed pickup and falsely frees the slot it really occupied at build time (the
    leg-18332/12367 case: 18332 scheduled 11:50, delayed/stored 12:28 -> must sit at 11:50).

    Paired with the scheduled CLEAR anchor (scheduler.USE_SCHEDULED_ARRIVAL_FOR_EVAL), this makes
    arrival legs fully decision-time on BOTH ends. Shift only when the delta meets the 15-min auto-bump
    threshold, and skip a (rare) midnight-crossing shift rather than corrupt the time-only field."""
    from datetime import datetime, timedelta
    from dispatching.analytics import best_flight_arrival_local, scheduled_flight_arrival_local

    for leg in day_legs:
        if leg.pickup_time is None or leg.get_trip_type() != "arrival":
            continue
        f = getattr(leg, "flight_information", None)
        if not f:
            continue
        best = best_flight_arrival_local(f)
        sched = scheduled_flight_arrival_local(f)
        if not best or not sched:
            continue
        delta_min = (best - sched).total_seconds() / 60.0
        if abs(delta_min) < _ARRIVAL_PICKUP_BUMP_MIN:
            continue
        dec_dt = datetime.combine(day, leg.pickup_time) - timedelta(minutes=delta_min)
        if dec_dt.date() != day:  # crossed midnight — leave stored pickup rather than corrupt it
            continue
        leg.pickup_time = dec_dt.time()


def _worked_span_window(sched) -> Optional[dict]:
    """A driver's REAL worked day, reconstructed from the legs ACTUALLY ASSIGNED to him (his real
    chain already on the board) — the trustworthy proxy for "when was he genuinely working" used to
    keep retrospective rescues honest (the configured DriverWeeklySchedule data is documented-dirty:
    placeholder driver, OFF-marked-but-working, default-flexible windows that don't bind).

    Returns a non-flexible Guard-C window {start, end, max_hours, flexible} so it actually binds:
    pickups must be >= his first real pickup hour and clears must be <= ceil(his last real clear).
    The internal gaps between his real legs are still governed by Guard B turnaround. Returns None
    for an idle/zero-leg driver (no worked legs => no evidence he was available => not a receiver)."""
    if not sched.slots:
        return None
    first = min(s.pickup_time for s in sched.slots)
    last_clear = max(s.estimated_end_time for s in sched.slots)
    end_hour = min(math.ceil(last_clear.hour + last_clear.minute / 60), 23)
    return {"start": first.hour, "end": end_hour, "max_hours": None, "flexible": False}


def _placement_feasibility(after_slots, placed, day) -> dict:
    """Mirror of check_feasibility Guard B (scheduler.py:777-821) for DISPLAY ONLY — reproduces the
    EXACT drive/turnaround/slack the optimizer used at this insertion, so the founder can sanity-check
    the drive-time assumptions (the NEXT #7 realism question, surfaced visually). ``placed`` is the
    inserted ScheduleSlot; ``after_slots`` is the driver's full proposed day (incl. placed). Returns
    {'preceding': {...}|None, 'following': {...}|None} with the same numbers check_feasibility computed."""
    from datetime import datetime, timedelta

    others = sorted((s for s in after_slots if s.leg_id != placed.leg_id), key=lambda s: s.pickup_time)
    new_pickup_dt = datetime.combine(day, placed.pickup_time)
    prec = foll = None
    for s in others:
        if datetime.combine(day, s.pickup_time) <= new_pickup_dt:
            prec = s
        elif foll is None:
            foll = s

    out = {"preceding": None, "following": None}
    placed_is_arr = is_airport_arrival(placed.trip_type, placed.pickup_category)
    if prec is not None:
        rep = resolve_drive_minutes(prec.dropoff_location, placed.pickup_location,
                                    prec.dropoff_category, placed.pickup_category)
        req = required_turnaround(rep, placed_is_arr,
                                  same_terminal=(prec.dropoff_category == placed.pickup_category))
        earliest = prec.estimated_end_time + timedelta(minutes=req)
        out["preceding"] = {
            "other_leg_id": prec.leg_id, "other_time": prec.estimated_end_time.strftime("%H:%M"),
            "drive_min": rep, "drive_from": prec.dropoff_location, "drive_to": placed.pickup_location,
            "turnaround_min": req, "slack_min": int((new_pickup_dt - earliest).total_seconds() / 60)}
    if foll is not None:
        foll_is_arr = is_airport_arrival(foll.trip_type, foll.pickup_category)
        rep = resolve_drive_minutes(placed.dropoff_location, foll.pickup_location,
                                    placed.dropoff_category, foll.pickup_category)
        req = required_turnaround(rep, foll_is_arr,
                                  same_terminal=(placed.dropoff_category == foll.pickup_category))
        foll_pickup_dt = datetime.combine(day, foll.pickup_time)
        earliest_for_next = placed.estimated_end_time + timedelta(minutes=req)
        out["following"] = {
            "other_leg_id": foll.leg_id, "other_time": foll.pickup_time.strftime("%H:%M"),
            "drive_min": rep, "drive_from": placed.dropoff_location, "drive_to": foll.pickup_location,
            "turnaround_min": req, "slack_min": int((foll_pickup_dt - earliest_for_next).total_seconds() / 60)}
    return out


def _capture_boards(ctx, day, *, target_id, inhouse_moves, farmed_out, protected_ids, legs_by_id) -> list:
    """Build per-affected-driver board rows (the PROPOSED/after state) for the HTML report so a human
    can SEE and CHECK the reshuffle. Read-only; call AFTER the move is committed to ctx.board.
      inhouse_moves: [(leg_id, from_did, to_did)] in-house reshuffles (from_did None = target from farm).
      farmed_out:    [(donor_did, leg_id, affiliate)] displaced legs that leave the board to an affiliate.
    Each driver's day is listed in time order; the target is 'kept', reshuffled legs show 'moved_out' on
    the donor AND 'moved_in' on the receiver, farmed displaced legs show 'farmed_out', the rest
    'existing'. 'kept'/'moved_in' rows carry the feasibility math. VIP from protected_ids (never leg.is_vip)."""
    moved_in = {(leg_id, to_did) for (leg_id, _f, to_did) in inhouse_moves if leg_id != target_id}
    # donor name per moved-in leg, so the receiver row can show "<- from <driver>" (the mirror of the
    # donor's "-> <receiver>" moved_out note) -- makes a multi-leg cascade traceable across boards.
    moved_in_from = {leg_id: _driver_name(ctx, f_did)
                     for (leg_id, f_did, _t) in inhouse_moves
                     if f_did is not None and leg_id != target_id}
    moved_out: Dict[int, list] = {}
    for (leg_id, f_did, to_did) in inhouse_moves:
        if f_did is not None and leg_id != target_id:
            moved_out.setdefault(f_did, []).append((leg_id, _driver_name(ctx, to_did)))
    farmed_by_donor: Dict[int, list] = {}
    for (donor, leg_id, aff) in farmed_out:
        farmed_by_donor.setdefault(donor, []).append((leg_id, aff))

    affected = set()
    affected.update(to_did for (_l, _f, to_did) in inhouse_moves)
    affected.update(f for (_l, f, _t) in inhouse_moves if f is not None)
    affected.update(farmed_by_donor.keys())

    def _slot_row(slot, role, sched, note=""):
        return {"leg_id": slot.leg_id, "_sort": slot.pickup_time,
                "pickup": _fmt_time(slot.pickup_time), "clear": _fmt_time(slot.estimated_end_time.time()),
                "from": slot.pickup_location, "to": slot.dropoff_location,
                "vehicle": slot.vehicle_type or "", "role": role,
                "vip": slot.leg_id in protected_ids, "note": note,
                "feas": _placement_feasibility(sched.slots, slot, day) if role in ("kept", "moved_in") else None}

    def _leg_row(leg, role, note):
        end = estimate_job_end_time(leg, day)
        return {"leg_id": leg.id, "_sort": leg.pickup_time,
                "pickup": _fmt_time(leg.pickup_time), "clear": _fmt_time(end.time()),
                "from": leg.pickup_location, "to": leg.dropoff_location,
                "vehicle": leg.effective_vehicle_type or "", "role": role,
                "vip": leg.id in protected_ids, "note": note, "feas": None}

    boards = []
    for did in sorted(affected):
        sched = ctx.board.get(did)
        if sched is None:
            continue
        rows = []
        for slot in sched.slots:
            note = ""
            if slot.leg_id == target_id:
                role = "kept"
            elif (slot.leg_id, did) in moved_in:
                role = "moved_in"
                donor = moved_in_from.get(slot.leg_id)
                note = f"← {donor}" if donor else ""
            else:
                role = "existing"
            rows.append(_slot_row(slot, role, sched, note))
        for (leg_id, receiver_name) in moved_out.get(did, []):
            leg = legs_by_id.get(leg_id)
            if leg is not None:
                rows.append(_leg_row(leg, "moved_out", f"→ {receiver_name}"))
        for (leg_id, aff) in farmed_by_donor.get(did, []):
            leg = legs_by_id.get(leg_id)
            if leg is not None:
                rows.append(_leg_row(leg, "farmed_out", f"→ farmed ({aff})"))
        rows.sort(key=lambda r: r["_sort"])
        for r in rows:
            r.pop("_sort", None)
        boards.append({"driver_id": did, "driver_name": _driver_name(ctx, did),
                       "vehicle": ctx.dvtypes.get(did) or "", "rows": rows})
    return boards


def evaluate_target(target, ctx, ledger, roster, *,
                    protected_ids: frozenset, min_savings: Decimal,
                    legs_by_id: dict, departure_rescue_max_premium: Decimal = ZERO,
                    stats: Optional[dict] = None,
                    driver_windows: Optional[dict] = None) -> Optional[Recommendation]:
    """Evaluate ONE target leg for an opportunity-cost recommendation. The target is EITHER an
    affiliate-farmed leg (retrospective grading: "was this past farm decision keepable in-house?")
    OR an UNASSIGNED leftover (decision support: the founder hand-built the in-house schedule and
    left this job for the tool to keep-in-house vs farm). Read-only. ``ctx`` is a fleet_intel
    DayContext (replayed in-house board). Returns the best Recommendation or None. Commits the chosen
    bundle's capacity to ``ledger`` (so later targets see consumed affiliate capacity — the day-level
    over-loading guard).

    ``driver_windows`` = {driver_id: real worked-span window} bounds every in-house rescue to the
    driver's REAL worked day (tier-1 find_swaps + tier-2 displacement), replacing the stub windows."""
    day = ctx.day
    target_dep = is_departure(target)

    # STATE A = the cost to farm the target directly. Two modes, one per-leg basis:
    #   • AFFILIATE-FARMED target (retrospective): what we REALLY paid (stored driver_base_pay). A
    #     Waleed/roster re-quote would flatten signal and abstain on legs we KNOW the paid cost of.
    #   • UNASSIGNED leftover (decision support): nothing was paid, so BOTH states are hypothetical —
    #     State A = the cheapest-eligible-affiliate waterfall quote. Priced up front (read-only, on a
    #     ledger COPY) so tier-1 can use it too. ``requote_base`` is kept as a secondary display figure
    #     for affiliate targets (their actual-paid basis stays primary).
    is_unassigned = not getattr(target, "driver_id", None)
    requote = price_farm_waterfall([target], day, ledger.copy(), roster)
    requote_base = requote.total_base if requote.feasible else None
    target_state_a = requote_base if is_unassigned else fi.affiliate_base_cost(target)
    # Display-only "actually paid": real for an affiliate target, None for a leftover (nothing paid).
    target_paid = None if is_unassigned else fi.affiliate_base_cost(target)

    # ── Tier 1: FREE reshuffle (wrap find_swaps unchanged — it never farms anything). ──
    # CRITICAL: ctx.board contains ONLY deployable drivers (a FleetVehicle that day), and every
    # accepted rescue is COMMITTED to ctx.board below, so the next target sees the consumed slot.
    # Without both, find_swaps would say "fits in-house" for almost every farmed leg independently
    # (they'd all pile onto the same idle driver) — the marginal-vs-total fallacy this project avoids.
    try:
        sw = find_swaps(target, ctx.board, ctx.legs_by_id, ctx.dvtypes, day,
                        driver_windows=driver_windows)
    except Exception:
        sw = None
    if sw and sw.solutions:
        # reject any solution that would disturb a protected (VIP/locked) leg
        for sol in sw.solutions:
            if not ({mv.leg_id for mv in sol.moves} & protected_ids):
                # Value avoided by keeping the target in-house for free: the actual paid cost for an
                # affiliate target, or the hypothetical farm cost for an unassigned leftover.
                avoided = target_state_a
                _apply_swap_solution(ctx.board, sol, ctx.legs_by_id, day)  # COMMIT to shared board
                _boards = _capture_boards(
                    ctx, day, target_id=target.id,
                    inhouse_moves=[(mv.leg_id, mv.from_driver_id, mv.to_driver_id) for mv in sol.moves],
                    farmed_out=[], protected_ids=protected_ids, legs_by_id=ctx.legs_by_id)
                _verb = "avoids the ~" if is_unassigned else "saves the whole farm cost (~"
                _dep_txt = ((". This is a leftover DEPARTURE -- belongs in-house." if is_unassigned
                             else ". This is a DEPARTURE -- belongs in-house.") if target_dep else ".")
                return Recommendation(
                    target_leg_id=target.id, kind="free_rescue", target_is_departure=target_dep,
                    keep_in_house_driver_id=sol.target_driver_id, farmed_leg_ids=[],
                    farm_affiliate_mix={}, state_a_farm_base=None, state_b_farm_base=ZERO,
                    net_savings=None,
                    target_actual_farm_cost=target_paid,  # None for an unassigned leftover
                    target_is_unassigned=is_unassigned,
                    target_hypothetical_farm_cost=requote_base,
                    reason=(f"Keep leg {target.id} in-house (driver {sol.target_driver_id}) via a "
                            f"free reshuffle of {len(sol.moves) - 1} other leg(s) -- nothing farmed, "
                            + (f"{_verb}{_m(avoided)} farm cost" if avoided is not None
                               else "saves the whole farm cost")
                            + _dep_txt),
                    detail={
                        "moves": [(mv.leg_id, mv.to_driver_id) for mv in sol.moves],
                        "boards": _boards,
                        # Ready-to-POST plan for the page's Apply button (ids only — the apply
                        # endpoint re-validates state and re-derives every dollar server-side).
                        # ``expected`` = each touched leg's CURRENT driver, the staleness guard:
                        # apply is rejected if the live board moved on since this was computed.
                        "apply": {
                            "kind": "free_rescue",
                            "target_leg_id": target.id,
                            "keep_driver_id": sol.target_driver_id,
                            "moves": [[mv.leg_id, mv.to_driver_id] for mv in sol.moves],
                            "expected": {
                                str(mv.leg_id): ((target.driver_id or None)
                                                 if mv.leg_id == target.id
                                                 else mv.from_driver_id)
                                for mv in sol.moves
                            },
                        },
                        "target_current": {
                            "driver_id": target.driver_id or None,
                            "name": (str(target.driver)
                                     if getattr(target, "driver_id", None) else ""),
                        },
                        "display": {
                            "target": _leg_display(target),
                            "keep_driver_name": _driver_name(ctx, sol.target_driver_id),
                            # the driver's ACTUAL vehicle that day — shown next to "Run on X" so a
                            # towncar-class job seated in a (higher-tier) van reads as intended
                            "keep_driver_vehicle": ctx.dvtypes.get(sol.target_driver_id) or "",
                            "reshuffled": [
                                {"leg": (_leg_display(ctx.legs_by_id[mv.leg_id])
                                         if mv.leg_id in ctx.legs_by_id else {"leg_id": mv.leg_id}),
                                 "to_driver": _driver_name(ctx, mv.to_driver_id)}
                                for mv in sol.moves if mv.leg_id != target.id
                            ],
                        },
                    },
                )

    # ── Tier 2: DEPTH-1 displace-and-farm. STATE A = ``target_state_a`` (computed up front): the
    # actual paid cost for an affiliate-farmed target, the hypothetical cheapest-affiliate quote for an
    # unassigned leftover. For an affiliate target a roster re-quote would flatten signal and abstain on
    # legs we KNOW the paid cost of; for a leftover the re-quote IS the only available basis (nothing
    # was paid). ABSTAIN (rm_target None) when the basis is uncomputable. ``requote_base`` stays a
    # secondary display figure for affiliate targets.
    rm_target = _recovered_margin(target_state_a, target)

    best = None  # (net_savings, Recommendation, committed-bundle legs)
    compat_inhouse = [d for d in ctx.inhouse_drivers
                      if (target.effective_vehicle_type or "") in
                      get_compatible_vehicle_types(ctx.dvtypes.get(d.id) or "")]

    for drv in compat_inhouse:
        sched = ctx.board.get(drv.id)
        if sched is None:
            continue
        # slots whose INDIVIDUAL removal makes the target feasible on this driver — bounded by
        # the receiver's REAL worked-span window (same Guard C as tier-1), not the stub.
        _recv_window = driver_windows.get(drv.id) if driver_windows else None
        for slot, _buf in _get_conflicting_slots(sched, target, day, driver_window=_recv_window):
            displaced = legs_by_id.get(slot.leg_id)
            if displaced is None:
                continue
            if displaced.id in protected_ids:          # never displace VIP/locked
                continue
            if is_departure(displaced):                # never FARM a departure
                continue
            # Coverage (depth-1): target moves in-house, displaced moves to farm -> count unchanged.
            b_quote = price_farm_waterfall([displaced], day, ledger.copy(), roster)
            if not b_quote.feasible:                   # over-capacity bundle -> reject (#2)
                continue
            rm_disp = _recovered_margin(b_quote.total_base, displaced)
            if rm_disp is None:                        # uncomputable displaced economics -> abstain
                continue
            if rm_target is None:                      # can't price keeping the target -> abstain
                # ...unless this is a mandatory departure rescue (kept in-house regardless of $).
                if not target_dep:
                    continue
                net = None
            else:
                net = rm_target - rm_disp              # >0 => B cheaper

            cand = Recommendation(
                target_leg_id=target.id,
                kind="policy_departure_rescue" if target_dep else "opportunity_swap",
                target_is_departure=target_dep,
                keep_in_house_driver_id=drv.id,
                farmed_leg_ids=[displaced.id],
                farm_affiliate_mix=dict(b_quote.by_affiliate),
                state_a_farm_base=target_state_a,  # paid (affiliate) or hypothetical (leftover) basis
                state_b_farm_base=b_quote.total_base,
                net_savings=net,
                target_actual_farm_cost=target_paid,  # None for an unassigned leftover (nothing paid)
                target_is_unassigned=is_unassigned,
                target_hypothetical_farm_cost=requote_base,
                reason="",  # filled below once chosen
                detail={"displaced_pickup": str(slot.pickup_time),
                        "displaced_route": f"{displaced.pickup_location} -> {displaced.dropoff_location}",
                        "b_with_night": b_quote.total_with_night,
                        "requote_base": requote_base,  # secondary: cheapest-affiliate re-quote of the target
                        # Ready-to-POST plan (ids only; server re-validates everything). The page
                        # may override suggested_affiliate_id with any quote_affiliate_options pick.
                        "apply": {
                            "kind": "policy_departure_rescue" if target_dep else "opportunity_swap",
                            "target_leg_id": target.id,
                            "keep_driver_id": drv.id,
                            "farm_leg_id": displaced.id,
                            "suggested_affiliate_id": b_quote.per_leg[0].get("affiliate_id"),
                            "expected": {str(target.id): target.driver_id or None,
                                         str(displaced.id): drv.id},
                        },
                        "target_current": {
                            "driver_id": target.driver_id or None,
                            "name": (str(target.driver)
                                     if getattr(target, "driver_id", None) else ""),
                        },
                        "display": {
                            "target": _leg_display(target),
                            "displaced": _leg_display(displaced),
                            "keep_driver_name": _driver_name(ctx, drv.id),
                            "keep_driver_vehicle": ctx.dvtypes.get(drv.id) or "",
                            "affiliate": next(iter(b_quote.by_affiliate), "an affiliate"),
                        }},
            )
            key = net if net is not None else Decimal("-1")
            if best is None or key > best[0]:
                best = (key, cand)

    if best is None:
        return None
    rec = best[1]

    # ── Decision gate: hard-rule (departure) rescues bypass the $ threshold; others need >= min. ──
    def _accept(reason_fn):
        rec.reason = reason_fn()
        # Affiliate-override picker for the page: every eligible alternative for the displaced leg,
        # priced BEFORE this bundle's capacity is committed (so the suggested affiliate is listed).
        # ``farm_skipped`` = the rest of the roster WITH the exclusion reason (permit / tier / no
        # rate / capacity), surfaced on the page so "why isn't X offered?" answers itself.
        rec.detail["farm_options"], rec.detail["farm_skipped"] = quote_affiliate_options(
            legs_by_id[rec.farmed_leg_ids[0]], day, ledger, roster)
        # commit AFFILIATE capacity (displaced bundle now farmed) AND the in-house BOARD change
        # (displaced leg leaves the board; target seated in-house) so later targets stay honest.
        _commit(ledger, [legs_by_id[lid] for lid in rec.farmed_leg_ids], day, roster)
        _apply_tier2_to_board(ctx.board, target, rec.farmed_leg_ids[0],
                              rec.keep_in_house_driver_id, day)
        _aff = next(iter(rec.farm_affiliate_mix), "an affiliate")
        rec.detail["boards"] = _capture_boards(
            ctx, day, target_id=target.id, inhouse_moves=[],
            farmed_out=[(rec.keep_in_house_driver_id, rec.farmed_leg_ids[0], _aff)],
            protected_ids=protected_ids, legs_by_id=legs_by_id)
        return rec

    if rec.kind == "policy_departure_rescue":
        # Keep a departure in-house only when doing so is FREE (tier-1, handled above) or costs no
        # more than departure_rescue_max_premium. A departure rescuable ONLY by farming a much more
        # expensive non-departure was correctly farmed in the first place — do not recommend
        # spending more to undo a correct call.
        if rec.net_savings is not None and rec.net_savings >= -departure_rescue_max_premium:
            return _accept(lambda: _departure_reason(rec, target))
        if stats is not None and rec.net_savings is not None:  # record what a higher premium buys
            stats["suppressed_departures"] = stats.get("suppressed_departures", 0) + 1
            stats["suppressed_departure_premium"] = (
                stats.get("suppressed_departure_premium", ZERO) + (-rec.net_savings))
        return None
    if rec.net_savings is not None and rec.net_savings >= min_savings:
        return _accept(lambda: _swap_reason(rec, target, legs_by_id))
    return None


def _commit(ledger, farmed_legs, day, roster):
    """Commit the accepted bundle's capacity to the REAL day ledger (best-effort; pricing copies
    were used for the decision). Keeps later targets honest about remaining affiliate capacity."""
    price_farm_waterfall(farmed_legs, day, ledger, roster)


# ── Reason strings (plain English, dollars shown, NO score) ────────────────────────────────────
def _m(d) -> str:
    return f"${d:,.0f}" if d is not None else "?"


def _swap_reason(rec: Recommendation, target, legs_by_id) -> str:
    disp = legs_by_id.get(rec.farmed_leg_ids[0]) if rec.farmed_leg_ids else None
    aff = ", ".join(f"{n}x {a}" for a, n in rec.farm_affiliate_mix.items()) or "an affiliate"
    disp_txt = (f"{disp.pickup_location} -> {disp.dropoff_location}" if disp else "the displaced leg")
    # Basis wording: affiliate target compares to the actual paid cost; an unassigned leftover has no
    # paid cost, so compare to the hypothetical cost of farming it directly.
    if rec.target_is_unassigned:
        basis = f"vs the ~{_m(rec.target_hypothetical_farm_cost)} it would cost to farm the target."
    else:
        basis = f"vs the {_m(rec.target_actual_farm_cost)} actually paid to farm the target."
    return (f"Keep leg {target.id} ({target.pickup_location} -> {target.dropoff_location}) in-house "
            f"on driver {rec.keep_in_house_driver_id}; farm the {disp_txt} to {aff} for "
            f"{_m(rec.state_b_farm_base)} instead -- same legs covered, saves {_m(rec.net_savings)} "
            + basis)


def _departure_reason(rec: Recommendation, target) -> str:
    delta = ("" if rec.net_savings is None
             else (f" (also saves {_m(rec.net_savings)})" if rec.net_savings >= 0
                   else f" (costs {_m(-rec.net_savings)} more, but policy keeps departures in-house)"))
    # An unassigned leftover was never farmed — keeping it in-house is the default, not an "undo".
    status = "is an unassigned leftover DEPARTURE" if rec.target_is_unassigned else "is a DEPARTURE and was farmed"
    return (f"POLICY: leg {target.id} {status} ({target.pickup_location} -> "
            f"{target.dropoff_location}) -- keep it in-house on driver "
            f"{rec.keep_in_house_driver_id} by farming a non-departure instead{delta}.")


# ═══════════════════════════════════════════════════════════════════════════════════════════
# RANGE SUMMARY  (offline harness entry point — mirrors fleet_intel.summarize_range)
# ═══════════════════════════════════════════════════════════════════════════════════════════
def summarize_savings_range(start: date, end: date, *,
                            min_savings: Decimal = DEFAULT_MIN_SAVINGS,
                            anthony_cap: Optional[int] = None,
                            departure_rescue_max_premium: Decimal = ZERO) -> dict:
    """Read-only opportunity-cost recommendations for [start, end] by service date.

    For each day: replay the actual in-house board, take every ACTUALLY-FARMED leg as a target, and
    evaluate keep-in-house-by-farming-something-cheaper against the whole carded affiliate roster.
    Maintains a per-day capacity ledger so the recommendations don't collectively over-load any single
    affiliate. Returns a nested dict with per-day recommendations, per-affiliate load maxima (the
    founder's calibration signal), a roster audit (capability/card gaps), and a behavioral audit.
    NO writes. ``anthony_cap`` (optional) overrides Anthony's daily count cap for what-if calibration."""
    from collections import Counter, defaultdict
    from drivers.models import Driver, DriverPayRate

    # Architecture B: data-driven roster (every rate-ready active affiliate + its AffiliateProfile).
    roster, aff_warnings, profileless_flat = resolve_affiliate_roster()
    # Roster descriptor for the report header (name / capacity mode / capability / card size).
    roster_desc = []
    for drv, prof in roster:
        roster_desc.append({
            "driver_id": drv.id, "name": str(drv),
            "mode": (prof.capacity_mode if prof else _CAP_SINGLE_CHAIN),
            "max_vehicle_tier": (prof.max_vehicle_tier if prof and prof.max_vehicle_tier else ""),
            "daily_cap": (prof.daily_cap if prof else None),
            "no_pickup_port_sanford": bool(prof.no_pickup_at_port_sanford) if prof else False,
            "has_profile": prof is not None,
            "rate_rows": DriverPayRate.objects.filter(driver=drv).count(),
        })

    legs = list(fi.legs_for_range(start, end))
    inhouse_drivers = list(Driver.objects.filter(driver_type="inhouse", is_active=True))
    preload_timing_cache()

    protected_ids = resolve_protected_vip_leg_ids(legs)

    legs_by_date: Dict[date, list] = defaultdict(list)
    for leg in legs:
        legs_by_date[leg.pickup_date].append(leg)

    # Gap-flagging: count the ACTUAL farm-out legs each affiliate received in range, so the command can
    # surface affiliates that got real work but have NO/partial card (uncomputable -> abstained here).
    farm_counts: Counter = Counter()

    days_out = []
    totals = {"targets": 0, "recommendations": 0, "free_rescue": 0, "opportunity_swap": 0,
              "policy_departure_rescue": 0, "abstained_uncomputable": 0,
              "abstained_uncomputable_far": 0,  # Approach A: target leg touches a far/unknown location
              "vip_protected": len(protected_ids), "est_savings": ZERO,
              "free_rescue_avoided": ZERO,  # full ACTUAL farm cost avoided by free affiliate-leg rescues
              # Decision-support mode (UNASSIGNED leftovers): targets that the founder hand-left and the
              # tool evaluated. ``unassigned_targets`` = leftovers seen; ``farm_only`` = evaluated targets
              # with NO keep-in-house rec (must be farmed); ``stuck`` = an UNASSIGNED leftover that is
              # both unplaceable in-house AND unpriceable to farm (operational alert);
              # ``free_rescue_avoided_hypothetical`` = hypothetical farm cost of free leftover rescues
              # (kept separate from the real-money headline above).
              "unassigned_targets": 0, "farm_only": 0, "stuck": 0,
              "free_rescue_avoided_hypothetical": ZERO,
              "suppressed_departures": 0, "suppressed_departure_premium": ZERO}
    # Behavioral audit — Port/Sanford are now their OWN categories (Step 3), NOT departures. We track
    # true departures kept protected, Port/Sanford volume + direction (info), the Port/Sanford PICKUPS
    # Waleed is excluded from, and VIP targets seen.
    audit = {"true_departures_protected": 0,
             "port_to": 0, "port_from": 0, "sanford_to": 0, "sanford_from": 0,
             "waleed_excluded_pickups": 0, "vip_targets_seen": 0}

    # RETROSPECTIVE GRADING: replay every day on the times the founder saw WHEN THE SCHEDULE WAS
    # BUILT — scheduled (decision-time) flight arrival, not best_arrival_local() (estimated/actual =
    # hindsight). Flip the scheduler's eval toggle for the whole replay; ALWAYS restore it (finally)
    # so the flag never leaks to any other caller in-process.
    import dispatching.scheduler as _sched
    _prev_sched_arr = _sched.USE_SCHEDULED_ARRIVAL_FOR_EVAL
    _sched.USE_SCHEDULED_ARRIVAL_FOR_EVAL = True
    try:
        for day, day_legs in sorted(legs_by_date.items()):
            # DEPLOYABLE drivers only = in-house drivers with a FleetVehicle assigned that day. This is
            # how fleet_intel defines a deployable unit (a driver with no vehicle isn't one) and it
            # naturally excludes the placeholder driver, whose empty schedule would otherwise "absorb"
            # every farmed leg in find_swaps (the marginal-vs-total fallacy).
            dvtypes = load_all_driver_vtypes(day)
            deployable = [d for d in inhouse_drivers if dvtypes.get(d.id) is not None]
            # Decision-time arrival PICKUPS (undo the flight-delay auto-bump) before anything reads
            # the board — pairs with the scheduled CLEAR anchor so arrivals are decision-time on both ends.
            _apply_decision_time_pickups(day_legs, day)
            board = build_driver_schedules(day_legs, deployable, day)
            # REAL driver availability: bound every in-house rescue to each driver's actual worked
            # day (his assigned-leg span, scheduled-time clears), replacing the stub windows. Idle/
            # zero-leg drivers get no window => excluded as receivers (no phantom-idle absorption).
            worked_windows = {did: w for did, s in board.items()
                              if (w := _worked_span_window(s)) is not None}
            legs_by_id = {l.id: l for l in day_legs}
            ctx = fi.DayContext(day, board, dvtypes, legs_by_id, deployable)
            ledger = WaterfallLedger.for_roster(roster)
            # Optional what-if override of Anthony's count cap (back-compat with the --anthony-cap flag).
            if anthony_cap is not None and ANTHONY_DRIVER_ID in ledger.caps:
                _ac = ledger.caps[ANTHONY_DRIVER_ID]
                _ac.mode, _ac.chain, _ac.cap = _CAP_COUNT, None, anthony_cap

            # TARGETS = legs to evaluate keep-in-house vs farm. Two kinds, handled per-leg:
            #   • FARM_OUT  — already farmed to an affiliate (retrospective grading of a PAST decision).
            #   • UNASSIGNED — a leftover the founder hand-left with no driver (decision support: the
            #     primary live workflow — build the in-house schedule, run this, decide what to keep).
            # A mixed day (some already farmed, some still unassigned) is handled naturally.
            targets = [l for l in day_legs if fi.fulfillment_of(l) in (fi.FARM_OUT, fi.UNASSIGNED)]

            # Ordering cost basis (high-cost-to-farm first → keep the expensive-to-farm legs in-house):
            # an affiliate target uses its actual paid cost; a leftover has none, so price its
            # hypothetical cheapest-affiliate farm cost (on a COPY — read-only). Like the affiliate sort
            # this prices against full day capacity (not intra-day consumption) — fine for a sort key.
            def _target_sort_cost(l):
                if getattr(l, "driver_id", None):
                    return float(fi.affiliate_base_cost(l) or 0)
                q = cheapest_affiliate_for_leg(l, day, ledger, roster)
                return float(q["base"]) if q.get("status") == _OK and q.get("base") else 0.0

            # departures first (mandatory rescue priority), then high-cost-to-farm legs first.
            targets.sort(key=lambda l: (0 if is_departure(l) else 1, -_target_sort_cost(l)))

            recs, abstained, abstained_far = [], 0, []
            day_unassigned = 0
            for t in targets:
                totals["targets"] += 1
                if getattr(t, "driver_id", None):       # gap-flagging: who actually got this farm-out
                    farm_counts[t.driver_id] += 1
                else:                                    # decision-support: an unassigned leftover
                    day_unassigned += 1
                    totals["unassigned_targets"] += 1
                # behavioral audit (Port/Sanford = own categories; only TRUE departures stay protected)
                if is_departure(t):
                    audit["true_departures_protected"] += 1
                tag = port_sanford_direction_tag(t)
                if tag == "to-port":
                    audit["port_to"] += 1
                elif tag == "from-port":
                    audit["port_from"] += 1
                    audit["waleed_excluded_pickups"] += 1   # Waleed can't pick up at Port (Step 2)
                elif tag == "to-sanford":
                    audit["sanford_to"] += 1
                elif tag == "from-sanford":
                    audit["sanford_from"] += 1
                    audit["waleed_excluded_pickups"] += 1   # Waleed can't pick up at Sanford (Step 2)
                if t.id in protected_ids:
                    audit["vip_targets_seen"] += 1
                # Approach A: a target whose pickup/dropoff is far/unknown has an UNCOMPUTABLE drive
                # time (see _drive_uncomputable_far) — we can't verify any reshuffle's feasibility, so
                # ABSTAIN (exclude from reshuffle/keep recs) rather than guess a ~30min local hop.
                if _drive_uncomputable_far(t):
                    totals["abstained_uncomputable_far"] += 1
                    abstained_far.append({
                        "leg_id": t.id,
                        "route": f"{t.pickup_location} -> {t.dropoff_location}",
                        "pickup_cat": categorize_location(t.pickup_location or ""),
                        "dropoff_cat": categorize_location(t.dropoff_location or ""),
                    })
                    continue
                try:
                    rec = evaluate_target(t, ctx, ledger, roster,
                                          protected_ids=protected_ids, min_savings=min_savings,
                                          legs_by_id=legs_by_id,
                                          departure_rescue_max_premium=departure_rescue_max_premium,
                                          stats=totals, driver_windows=worked_windows)
                except Exception:  # never let one leg crash the day
                    rec = None
                    abstained += 1
                    continue
                if rec is not None:
                    recs.append(rec)

            for rec in recs:
                totals["recommendations"] += 1
                totals[rec.kind] += 1
                if rec.net_savings is not None and rec.net_savings > 0:
                    totals["est_savings"] += rec.net_savings
                # A free rescue farms NOTHING -> it avoids the WHOLE farm cost of the target. That value
                # is not an apples-to-apples "net" (so it's kept out of est_savings), but it is the real
                # money saved, so report it separately rather than letting the headline read ~$0. For an
                # affiliate target that is the ACTUAL paid cost; for an unassigned leftover nothing was
                # paid, so the avoided value is HYPOTHETICAL — tracked separately so the real-money
                # headline stays real.
                if rec.kind == "free_rescue":
                    if rec.target_actual_farm_cost is not None:
                        totals["free_rescue_avoided"] += rec.target_actual_farm_cost
                    elif rec.target_is_unassigned and rec.target_hypothetical_farm_cost is not None:
                        totals["free_rescue_avoided_hypothetical"] += rec.target_hypothetical_farm_cost

            # FARM-ONLY / STUCK (decision support): of the evaluated targets, which got NO keep-in-house
            # rec (=> must be farmed)? Among those, an UNASSIGNED leftover that ALSO can't be priced to
            # farm by any affiliate (fresh-capacity quote not OK) is "stuck" — unplaceable in-house AND
            # unfarmable: a real operational alert. far/unknown-drive abstains are excluded (own bucket).
            rec_ids = {r.target_leg_id for r in recs}
            far_ids = {a["leg_id"] for a in abstained_far}
            day_farm_only = day_stuck = 0
            _fresh_ledger = WaterfallLedger.for_roster(roster)
            farm_items = []
            for t in targets:
                if t.id in rec_ids:
                    continue
                is_far = t.id in far_ids
                if not is_far:
                    day_farm_only += 1
                    if not getattr(t, "driver_id", None):
                        q = cheapest_affiliate_for_leg(t, day, _fresh_ledger, roster)
                        if q.get("status") != _OK:
                            day_stuck += 1
                # ACTIONABLE farm list (the page's write actions): every evaluated target that got
                # NO keep-in-house rec, incl. far-abstained ones (their reshuffle feasibility was
                # uncomputable, but a farm PRICE is still real and the job still has to be served).
                # Options are quoted against the END-state ledger so capacity already consumed by
                # accepted recommendations is respected. VIP / departure legs are listed but the
                # page renders them action-less (never farmed — hard rules).
                options, skipped = quote_affiliate_options(t, day, ledger, roster)
                farm_items.append({
                    "leg_id": t.id,
                    "display": _leg_display(t),
                    "current_driver_id": t.driver_id or None,
                    "current_driver_name": (str(t.driver) if t.driver_id else ""),
                    "already_farmed": bool(t.driver_id),
                    "is_departure": is_departure(t),
                    "vip": t.id in protected_ids,
                    "abstained_far": is_far,
                    "options": options,
                    "skipped": skipped,  # ineligible affiliates + reason (page transparency)
                })
            farm_items.sort(key=lambda it: ((legs_by_id[it["leg_id"]].pickup_time or time.min),
                                            it["leg_id"]))
            totals["farm_only"] += day_farm_only
            totals["stuck"] += day_stuck

            days_out.append({
                "day": day,
                "legs": len(day_legs),
                "evaluated": len(targets),           # legs evaluated (farmed + unassigned leftovers)
                "unassigned_targets": day_unassigned,  # the founder's hand-left leftovers
                "farmed_targets": len(targets) - day_unassigned,  # affiliate-farmed targets (past-day)
                "farm_only": day_farm_only,          # evaluated targets with no keep-in-house rec
                "stuck": day_stuck,                  # leftover: unplaceable in-house AND unfarmable
                "inhouse_deployable": len(deployable),
                "inhouse_total": len(inhouse_drivers),
                "recommendations": recs,
                "farm_items": farm_items,           # actionable no-rec targets (page farm buttons)
                "ledger_load": ledger.load_by_name(),  # {affiliate_name: legs farmed to them this day}
                "abstained": abstained,
                "abstained_far": abstained_far,  # Approach A: targets skipped (far/unknown drive time)
            })
    finally:
        _sched.USE_SCHEDULED_ARRIVAL_FOR_EVAL = _prev_sched_arr

    # Roster audit: carded affiliates that received real farm-out legs but lack a usable card/config.
    roster_ids = {drv.id for drv, _ in roster}
    gap_ids = [did for did in farm_counts if did not in roster_ids]
    gap_names = {d.id: str(d) for d in Driver.objects.filter(id__in=gap_ids).select_related("profile")}
    uncarded_with_volume = sorted(
        ((gap_names.get(did, f"driver {did}"), farm_counts[did]) for did in gap_ids),
        key=lambda x: -x[1])

    return {
        "range": {"start": start, "end": end, "days": (end - start).days + 1},
        "min_savings": min_savings,
        "anthony_cap": anthony_cap,
        "departure_rescue_max_premium": departure_rescue_max_premium,
        "affiliate_warnings": aff_warnings,
        "roster": roster_desc,
        "roster_audit": {
            "profileless_flat": profileless_flat,       # flat card, no capability cap -> mispricing risk
            "uncarded_with_volume": uncarded_with_volume,  # got farm-outs but no card -> abstained here
        },
        "totals": totals,
        "audit": audit,
        "days": days_out,
    }
