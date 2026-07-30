"""Quote engine — the single source of truth for dispatcher price estimates.

Split out of ``dispatching.views`` so the pricing rules are testable without a
request, and so the numbers can be validated against the published rate card
(see ``tests_quote_engine.py``).

────────────────────────────────────────────────────────────────────────────
HOW A PRICE IS DECIDED
────────────────────────────────────────────────────────────────────────────
1. If the trip matches a route on the PUBLISHED RATE CARD, the card price wins
   outright and the formula never runs. Dispatchers must never quote above what
   the website sells the same transfer for. (Founder decision, 2026-07-29.)

2. Otherwise the trip is custom/off-card and the formula prices it:

       raw = base_fee + mileage_charge(miles)     # per_mile covers BOTH
                                                  # directions - see below
       raw = max(raw, committed_hours x hourly_floor)   # slow-route floor
       raw = max(raw, minimum)                          # dispatch minimum
       price = round to nearest $5

   mileage_charge uses per_mile up to 100 mi and the higher long_per_mile beyond
   it, because a genuine long haul commits a driver's whole day.

WHY per_mile LOOKS HIGH
    ``per_mile`` charges for two miles of driving per trip-mile, because an
    off-card custom trip is not chainable the way a published zone transfer is —
    the car goes out and comes back empty. Divide by two to get what we earn per
    mile actually driven (~$1.68 on a towncar), which is the number to sanity
    check against fuel + driver pay + wear + margin.

    The guest never sees this split. The dispatcher does, via
    ``QuoteResult.breakdown``, so they can defend the number if asked.
    (Founder decision, 2026-07-29.)

WHY THERE IS A TIME FLOOR
    The formula is pure mileage, so it cannot tell that 85 mi to Tampa is 90
    minutes on I-4 while 72 mi to Port Canaveral is 60 minutes on the 528. The
    floor prices driver-hours committed (out + empty return) and only binds on
    genuinely slow routes; every founder-confirmed price is set by mileage, not
    by the floor.

WHY DIRECTION DOES NOT CHANGE THE PRICE
    An outbound discount was added on 2026-07-29 from Blacklane data (same long
    route $645 outbound vs $1,053 inbound) and REMOVED the same day. That gap is
    a network property: Blacklane has supply at the far end, so their outbound
    leg costs them little. An Orlando fleet eats the empty return whichever way
    the revenue leg runs. Direction is still classified, but only to explain the
    trip in the notes.

CALIBRATION ANCHORS (confirmed by the founder — tests assert these)
    Every figure below is a FARE. Gratuity is always quoted on top.
    towncar     85 mi Orlando -> Tampa             $340
    towncar    218 mi Disney -> Port Everglades    $850   (his stated minimum)
    SUV        218 mi Disney -> Port Everglades    $920
    Sprinter   218 mi Disney -> Port Everglades  $1,400
    towncar          short custom trip             $135 minimum
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Optional

# ════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — every tunable number lives here.
# ════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class VehicleRate:
    """Pricing constants for one vehicle type.

    base_fee      Cost of showing up at all, regardless of distance.
    per_mile      Charged per trip-mile up to LONG_DISTANCE_THRESHOLD_MI; covers
                  BOTH directions of driving.
    long_per_mile Charged per trip-mile BEYOND that threshold. Higher, because a
                  genuine long haul commits a driver's whole day.
    hourly_floor  Minimum earned per hour of committed driver time.
    minimum       Dispatch minimum. A car going out at all costs this much.
    rt_multiplier Round trip = one way x this (~2 transfers, small discount).
    """

    label: str
    base_fee: Decimal
    per_mile: Decimal
    long_per_mile: Decimal
    hourly_floor: Decimal
    minimum: Decimal
    rt_multiplier: Decimal

    @property
    def per_driven_mile(self) -> Decimal:
        """What we earn per mile actually driven (per_mile covers 2 directions)."""
        return (self.per_mile / 2).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# Keys MUST match rates.Vehicle.vehicle_type values exactly.
VEHICLE_RATES: dict[str, VehicleRate] = {
    "towncar": VehicleRate(
        label="Towncar",
        base_fee=Decimal("55"),
        per_mile=Decimal("3.35"),
        long_per_mile=Decimal("3.90"),
        hourly_floor=Decimal("105"),
        minimum=Decimal("135"),
        rt_multiplier=Decimal("1.90"),
    ),
    "mini_van": VehicleRate(
        label="Mini Van",
        base_fee=Decimal("60"),
        per_mile=Decimal("3.55"),
        long_per_mile=Decimal("3.90"),
        hourly_floor=Decimal("110"),
        minimum=Decimal("135"),
        rt_multiplier=Decimal("1.85"),
    ),
    "suv": VehicleRate(
        label="SUV",
        base_fee=Decimal("65"),
        # NOTE (2026-07-29): the Blacklane check suggests SUV is underpriced on
        # long trips. Their SUV premium GROWS with distance (1.13x short ->
        # 1.35x at 230 mi) while ours SHRINKS (1.26x -> 1.13x), because our SUV
        # base is high but our per-mile gap is small. Raising per_mile to about
        # 4.19 would hold a ~1.25x premium at distance. Left at 3.85 because
        # every founder-confirmed anchor was a towncar and this is a live price
        # change — flip it deliberately, not as a side effect.
        per_mile=Decimal("3.85"),
        long_per_mile=Decimal("3.98"),
        hourly_floor=Decimal("120"),
        minimum=Decimal("170"),
        rt_multiplier=Decimal("1.90"),
    ),
    "van": VehicleRate(
        label="Van",
        base_fee=Decimal("70"),
        per_mile=Decimal("4.25"),
        long_per_mile=Decimal("4.45"),
        hourly_floor=Decimal("135"),
        minimum=Decimal("175"),
        rt_multiplier=Decimal("1.93"),
    ),
    "Van(14 Pax)": VehicleRate(
        label="Van (14 Pax)",
        base_fee=Decimal("85"),
        per_mile=Decimal("5.85"),
        long_per_mile=Decimal("6.19"),
        hourly_floor=Decimal("185"),
        minimum=Decimal("220"),
        rt_multiplier=Decimal("1.95"),
    ),
}

# Cheapest to most expensive — display order.
VEHICLE_TIER_ORDER = ["towncar", "mini_van", "suv", "van", "Van(14 Pax)"]

# ── Direction / out-of-area ────────────────────────────────────────────────
# Below this, an empty return is short enough that direction does not matter
# (Blacklane's spread at 78 mi was only 6%, inside noise).
LONG_DISTANCE_THRESHOLD_MI = Decimal("100")
# Outbound discount ramps linearly from the threshold to here, where it maxes.
OUTBOUND_DISCOUNT_FULL_AT_MI = Decimal("235")
OUTBOUND_MAX_DISCOUNT = Decimal("0.20")

# A pickup farther than this from base means we are positioning a car out of
# area to START the job — that is an INBOUND, and it carries no discount.
SERVICE_AREA_RADIUS_MI = Decimal("60")

# ── Local custom trips (in-area, off-card) ─────────────────────────────────
# Founder calibration, 2026-07-29. Grand Floridian -> 2596 Carrickton Cir
# (21.8 mi, 33 min) — a residential address a few miles from MCO:
#
#   Vehicle    MCO<->Disney card   x 1.135   founder said
#   Towncar          $105            $120        $120
#   Mini Van         $120            $135        $135
#   SUV              $140            $160        $160
#   Sprinter         $220            $250        $250
#
# All four land exactly. He priced it by ANALOGY — "this location is almost the
# same as the airport" — not from mileage, because the destination sits next to
# MCO. So a local custom trip is priced off the comparable CARD ROUTE plus a
# premium for it being a custom address rather than a zone hotel.
#
# Critically there is NO empty-return component here. Founder: "we probably
# don't need to think about the empty return for local roads." Drivers chain
# local work; the doubling in `per_mile` is for out-of-area runs only.
#
# Any premium in 12.5%–14.6% reproduces all four numbers after $5 rounding.
LOCAL_CUSTOM_PREMIUM = Decimal("0.135")

# Floor for a local custom trip. Founder: "it can be 6 miles, but I will have to
# drive from my base 10 miles, then 6 miles, then back to my base. So let's say
# $110." That is positioning cost, not trip cost. Other vehicles scale by their
# MCO<->Disney card ratio to the towncar.
LOCAL_FLOORS: dict[str, Decimal] = {
    "towncar": Decimal("110"),
    "mini_van": Decimal("125"),
    "suv": Decimal("145"),
    "van": Decimal("170"),
    "Van(14 Pax)": Decimal("230"),
}

# Gratuity is ALWAYS quoted on top of the fare, never folded into it. Founder,
# 2026-07-29: "we would let the guest know it will be nine hundred and twenty
# dollars plus twenty percent gratuity."
#
# So every price this engine computes is a FARE. What differs by regime is
# whether the 20% is obligatory:
#
#   LOCAL       — 20% SUGGESTED. The guest may or may not add it.
#   OUT OF TOWN — 20% ADDED automatically. We bill it; the total is what they pay.
#
# Internally the gratuity is margin, not a pass-through: on out-of-town work the
# driver is paid a flat or hourly rate for the job, so the gratuity is an upsell.
# That is why it is always shown as a separate line rather than buried in the
# fare — and it is internal-panel only. Dispatchers should never discuss with a
# guest how a gratuity is split.
GRATUITY_PCT = Decimal("20")

# One representative address per card zone, used to measure how far an unknown
# address is from each zone so it can be snapped to the nearest one. Zone names
# are broad categories, so these are a concrete point inside each.
ZONE_REPRESENTATIVE_ADDRESS: dict[str, str] = {
    "Orlando International Airport": "Orlando International Airport, Orlando, FL",
    "Sanford Int'l Airport": "Orlando Sanford International Airport, Sanford, FL",
    "Universal Studios Area Hotels": "Universal Studios Florida, Orlando, FL",
    "All WDW Disney Property Resorts": "Walt Disney World Resort, Lake Buena Vista, FL",
    "Disney Springs Hotels": "Disney Springs, Lake Buena Vista, FL",
    "International Drive Hotels": "9840 International Dr, Orlando, FL",
    "Kissimmee 192 Area Hotels": "W Irlo Bronson Memorial Hwy, Kissimmee, FL",
    "Omni Championsgate / Reunion": "1500 Masters Blvd, ChampionsGate, FL",
    "Port Canaveral": "Port Canaveral, FL",
    "Sea World": "SeaWorld Orlando, Orlando, FL",
}

# An address farther than this from every zone is not "in" any of them, so it
# gets the mileage formula instead of a snapped card price.
SNAP_MAX_MI = Decimal("15")

# Reference point for "how far out is the pickup".
BASE_LOCATION = "Orlando International Airport, Orlando, FL"

# ── Airport pickup surcharge ───────────────────────────────────────────────
# Founder, 2026-07-29: "for airport pickups, let's say to Miami, always add an
# additional fee in the backend for airport since we have to go thru commercial
# lane/tunnel. If it's point to point not airport that's ok, you can take that
# fee. That fee can be $40."
#
# PICKUPS only — collecting a guest means entering the commercial lane (and at
# MIA, the tunnel). Dropping at departures does not, so Disney -> Tampa Airport
# carries no fee. "In the backend" means it is built into the fare rather than
# itemised to the guest; the dispatcher sees it in the internal breakdown.
#
# Two tiers, per the founder: "$40 for long distances, $20 for short."
#
# NEVER applied to a published card price. Those are what the website charges for
# our MCO transfers and already absorb the airport's cost — adding to them would
# break rate-card precedence (D1) and quote above the website.
AIRPORT_PICKUP_FEE_LONG = Decimal("40")
AIRPORT_PICKUP_FEE_SHORT = Decimal("20")


def airport_pickup_fee(miles: Optional[Decimal]) -> Decimal:
    """Airport pickup surcharge for a trip of this length."""
    if miles is None:
        return AIRPORT_PICKUP_FEE_SHORT
    return (
        AIRPORT_PICKUP_FEE_LONG
        if Decimal(miles) > LONG_DISTANCE_THRESHOLD_MI
        else AIRPORT_PICKUP_FEE_SHORT
    )

# "Airport" as a word, but not the street name — "1234 Airport Rd" is a normal
# address and must not trigger the fee.
_AIRPORT_WORD_RE = re.compile(
    r"\bairports?\b(?!\s+(?:rd\b|road\b|blvd\b|boulevard\b|ave\b|avenue\b|"
    r"st\b|street\b|dr\b|drive\b|way\b|ln\b|lane\b|pkwy\b|parkway\b|ct\b|cir\b))",
    re.IGNORECASE,
)
# Google Places formats codes parenthesised — "Miami International Airport (MIA)".
# Matching only that form keeps three-letter words from false-positiving.
_AIRPORT_CODE_RE = re.compile(r"\(([A-Z]{3})\)")
AIRPORT_CODES = frozenset({
    "MCO", "SFB", "MIA", "FLL", "TPA", "PBI", "RSW", "JAX", "SRQ", "DAB",
    "MLB", "EYW", "PIE", "GNV", "TLH", "PNS", "ECP", "VPS", "SAV", "ATL",
    "CLT", "PGD", "OCF", "LAL", "BCT", "OPF", "TMB", "APF",
})


def is_airport_pickup(address: Optional[str]) -> bool:
    """True when a pickup address looks like an airport terminal."""
    if not address:
        return False
    if _AIRPORT_WORD_RE.search(address):
        return True
    return any(
        code in AIRPORT_CODES for code in _AIRPORT_CODE_RE.findall(address)
    )


DIRECTION_OUTBOUND = "outbound"
DIRECTION_INBOUND = "inbound"
DIRECTION_UNKNOWN = "unknown"


# ── Rate-card alias seeds ──────────────────────────────────────────────────
# The published card's Location rows are broad categories ("All WDW Disney
# Property Resorts"), and their `aliases` fields are empty — so nothing a
# dispatcher actually types would ever match, and "card wins" would never fire.
# These seeds supplement whatever is in the database.
#
# Deliberately CONSERVATIVE. A missed match falls through to the formula, which
# quotes higher and is flagged to the dispatcher — recoverable. A WRONG match
# silently quotes the wrong published price. Generic place names that span
# several card zones ("Lake Buena Vista", "Orlando") are therefore omitted.
DEFAULT_LOCATION_ALIASES: dict[str, tuple[str, ...]] = {
    "Orlando International Airport": (
        "MCO", "Orlando International", "Orlando Intl", "Orlando Airport",
    ),
    "Sanford Int'l Airport": (
        "SFB", "Sanford International", "Sanford Airport", "Orlando Sanford",
    ),
    "Universal Studios Area Hotels": (
        "Universal Studios", "Universal Orlando", "Universal's", "Universal Blvd",
        "Islands of Adventure", "Portofino Bay", "Hard Rock Hotel",
        "Royal Pacific", "Cabana Bay", "Sapphire Falls", "Aventura Hotel",
        "Endless Summer", "Stella Nova", "Terra Luna",
    ),
    "All WDW Disney Property Resorts": (
        "Walt Disney World", "Disney's", "Grand Floridian", "Contemporary Resort",
        "Polynesian Village", "Wilderness Lodge", "Animal Kingdom Lodge",
        "Caribbean Beach", "Coronado Springs", "Port Orleans", "Old Key West",
        "Saratoga Springs", "Yacht Club", "Beach Club", "BoardWalk Inn",
        "Swan and Dolphin", "Swan Reserve", "Dolphin Resort", "All-Star",
        "Pop Century", "Art of Animation", "Riviera Resort", "Bay Lake Tower",
        "Fort Wilderness", "Floridian Way", "Magic Kingdom", "EPCOT",
        "Hollywood Studios", "Animal Kingdom",
    ),
    "Disney Springs Hotels": (
        "Disney Springs", "Buena Vista Palace", "Wyndham Lake Buena Vista",
        "B Resort", "Drury Plaza Disney Springs", "Hilton Orlando Buena Vista",
    ),
    "International Drive Hotels": (
        "International Dr", "I-Drive", "Rosen Centre", "Rosen Plaza",
        "Rosen Shingle", "Hyatt Regency Orlando", "Orange County Convention",
        "OCCC", "ICON Park", "Pointe Orlando", "Castle Hotel",
    ),
    "Kissimmee 192 Area Hotels": (
        "Kissimmee", "Irlo Bronson", "Hwy 192", "Highway 192", "US 192",
    ),
    "Omni Championsgate / Reunion": (
        "ChampionsGate", "Champions Gate", "Reunion Resort", "Omni Orlando",
        "Masters Blvd",
    ),
    "Port Canaveral": (
        "Port Canaveral", "Cape Canaveral", "Cruise Terminal", "Canaveral",
    ),
    "Sea World": (
        "SeaWorld", "Sea World", "Discovery Cove", "Aquatica",
    ),
}

# Shorter than this and a keyword is too generic to trust.
MIN_KEYWORD_LEN = 3
# At or below this length, require whole-word matching so "MCO" cannot hit
# inside an unrelated word.
WORD_BOUNDARY_MAX_LEN = 5


# ════════════════════════════════════════════════════════════════════════════
# PARSING / ROUNDING HELPERS
# ════════════════════════════════════════════════════════════════════════════

_DISTANCE_RE = re.compile(
    r"^\s*([0-9]*\.?[0-9]+)\s*(mi|mile|miles|ft|feet|foot|km|kilometers?|m|meters?)?\b",
    re.IGNORECASE,
)

# Google Distance Matrix with units=imperial returns FEET under ~0.1 mi
# ("285 ft"). Parsing that as a bare number gave 285 MILES and a four-figure
# quote for a trip across a parking lot.
_UNIT_TO_MILES = {
    "mi": Decimal("1"), "mile": Decimal("1"), "miles": Decimal("1"),
    "ft": Decimal("1") / Decimal("5280"),
    "feet": Decimal("1") / Decimal("5280"),
    "foot": Decimal("1") / Decimal("5280"),
    "km": Decimal("0.621371"),
    "kilometer": Decimal("0.621371"), "kilometers": Decimal("0.621371"),
    "m": Decimal("0.000621371"),
    "meter": Decimal("0.000621371"), "meters": Decimal("0.000621371"),
}


def parse_distance_miles(distance_text: Optional[str]) -> Optional[Decimal]:
    """Miles from a Google distance string, or None if unparseable.

    Handles thousands separators ("1,234 mi") and non-mile units, notably the
    feet form Google uses for very short hops.
    """
    if not distance_text:
        return None
    cleaned = str(distance_text).replace(",", "").strip()
    match = _DISTANCE_RE.match(cleaned)
    if not match:
        return None
    try:
        value = Decimal(match.group(1))
    except Exception:
        return None
    unit = (match.group(2) or "mi").lower()
    factor = _UNIT_TO_MILES.get(unit)
    if factor is None:
        return None
    return (value * factor).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def mileage_charge(rates: VehicleRate, miles: Decimal) -> Decimal:
    """Mileage portion of a fare, with a higher rate past the long-haul threshold.

    A flat per-mile priced genuine long hauls too cheaply: Disney -> Port
    Everglades (218 mi) came out $785 for a towncar when the founder's floor for
    that trip is $850. Short and mid-range trips keep the original rate, so Tampa
    stays at $340.
    """
    miles = Decimal(miles)
    if miles <= LONG_DISTANCE_THRESHOLD_MI:
        return rates.per_mile * miles
    return (
        rates.per_mile * LONG_DISTANCE_THRESHOLD_MI
        + rates.long_per_mile * (miles - LONG_DISTANCE_THRESHOLD_MI)
    )


def round_to_5(amount: Decimal) -> Decimal:
    """Round to the nearest $5, halves upward.

    The old implementation used Python's round(), which is banker's rounding on
    Decimals — $132.50 rounded DOWN to $130 while $127.50 rounded UP to $130,
    so a dispatcher checking the math by hand disagreed with the tool half the
    time. ROUND_HALF_UP matches the rest of the codebase (reservations.utils).
    """
    return (Decimal(amount) / 5).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * 5


def _money(amount: Decimal) -> Decimal:
    return Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ════════════════════════════════════════════════════════════════════════════
# RATE-CARD MATCHING
# ════════════════════════════════════════════════════════════════════════════


def location_keywords(location) -> list[str]:
    """Every string that should identify this Location, longest first.

    Combines the Location's own name, its comma-separated ``aliases`` field, and
    the conservative seeds above.
    """
    keywords = {location.name}
    aliases = getattr(location, "aliases", "") or ""
    keywords.update(a.strip() for a in aliases.split(",") if a.strip())
    keywords.update(DEFAULT_LOCATION_ALIASES.get(location.name, ()))
    usable = [k for k in keywords if len(k) >= MIN_KEYWORD_LEN]
    return sorted(usable, key=len, reverse=True)


def _keyword_hits(keyword: str, haystack: str) -> bool:
    if len(keyword) <= WORD_BOUNDARY_MAX_LEN:
        return re.search(rf"\b{re.escape(keyword)}\b", haystack, re.IGNORECASE) is not None
    return keyword.lower() in haystack


def match_location(address: str, locations: Iterable) -> tuple[Optional[object], Optional[str]]:
    """Best Location for a typed address — LONGEST matching keyword wins.

    The previous implementation broke only out of the inner keyword loop and
    kept iterating locations, so the LAST match overwrote earlier ones and the
    detected route depended on database row order.
    """
    if not address:
        return None, None
    haystack = address.lower()
    best_loc = None
    best_kw = None
    for location in locations:
        for keyword in location_keywords(location):
            if best_kw is not None and len(keyword) <= len(best_kw):
                continue
            if _keyword_hits(keyword, haystack):
                best_loc, best_kw = location, keyword
                break
    return best_loc, best_kw


def lookup_card_rates(vehicle_type: str, pickup_location, dropoff_location):
    """(exact_direction_rate, reverse_direction_rate) for this vehicle.

    The card stores each direction as its own Route row, so a pair can carry two
    rows. Usually they agree and either will do. When they DISAGREE the card
    contradicts itself and a dispatcher could quote a different price than the
    website depending on which way round they typed the addresses — so both are
    returned and ``quote()`` flags it.
    """
    if not (pickup_location and dropoff_location):
        return None, None
    if pickup_location.pk == dropoff_location.pk:
        return None, None
    from rates.models import Rate

    base = Rate.objects.select_related(
        "route", "route__origin", "route__destination"
    ).filter(vehicle__vehicle_type=vehicle_type)
    exact = base.filter(
        route__origin=pickup_location, route__destination=dropoff_location
    ).first()
    reverse = base.filter(
        route__origin=dropoff_location, route__destination=pickup_location
    ).first()
    return exact, reverse


def lookup_card_rate(vehicle_type: str, pickup_location, dropoff_location):
    """The published Rate to use, preferring the exact travel direction."""
    exact, reverse = lookup_card_rates(vehicle_type, pickup_location, dropoff_location)
    return exact or reverse


# ════════════════════════════════════════════════════════════════════════════
# DIRECTION
# ════════════════════════════════════════════════════════════════════════════


def classify_direction(
    trip_miles: Decimal, pickup_miles_from_base: Optional[Decimal]
) -> str:
    """Whether we position the car out of area to start (inbound) or end there.

    Returns DIRECTION_UNKNOWN when the positioning distance is unavailable, in
    which case no directional adjustment is applied and pricing falls back to
    the symmetric behaviour.
    """
    if trip_miles <= LONG_DISTANCE_THRESHOLD_MI:
        return DIRECTION_UNKNOWN
    if pickup_miles_from_base is None:
        return DIRECTION_UNKNOWN
    if pickup_miles_from_base > SERVICE_AREA_RADIUS_MI:
        return DIRECTION_INBOUND
    return DIRECTION_OUTBOUND


def outbound_discount(trip_miles: Decimal) -> Decimal:
    """Discount fraction for an outbound out-of-area one-way (0 when local)."""
    if trip_miles <= LONG_DISTANCE_THRESHOLD_MI:
        return Decimal("0")
    span = OUTBOUND_DISCOUNT_FULL_AT_MI - LONG_DISTANCE_THRESHOLD_MI
    ramp = (trip_miles - LONG_DISTANCE_THRESHOLD_MI) / span
    return OUTBOUND_MAX_DISCOUNT * min(Decimal("1"), ramp)


# ════════════════════════════════════════════════════════════════════════════
# RESULT
# ════════════════════════════════════════════════════════════════════════════

SOURCE_RATE_CARD = "rate_card"
SOURCE_LOCAL_CUSTOM = "local_custom"
SOURCE_FORMULA = "formula"


def snap_to_zone(address, locations, measure_miles):
    """Nearest card zone to an address, as (Location, miles), or (None, None).

    ``measure_miles(zone_address)`` returns driving miles from ``address``, or
    None. Used when an address does not match a zone by name but sits inside one
    of their areas — a residence near MCO prices off the MCO routes, which is how
    the founder prices these by hand.

    Returns nothing if the nearest zone is farther than SNAP_MAX_MI, so a genuine
    out-of-area address falls through to the mileage formula.
    """
    if not address:
        return None, None
    best_loc, best_miles = None, None
    for location in locations:
        zone_address = ZONE_REPRESENTATIVE_ADDRESS.get(location.name)
        if not zone_address:
            continue
        miles = measure_miles(zone_address)
        if miles is None:
            continue
        if best_miles is None or miles < best_miles:
            best_loc, best_miles = location, miles
    if best_loc is None or best_miles > SNAP_MAX_MI:
        return None, None
    return best_loc, best_miles


@dataclass
class QuoteResult:
    """One priced quote.

    ``price`` is the only figure a guest should ever be shown. ``breakdown`` and
    ``notes`` are internal — they exist so a dispatcher understands where the
    number came from and can defend it if the guest pushes back.
    """

    vehicle_type: str
    vehicle_label: str
    trip_type: str
    price: Decimal
    source: str
    miles: Optional[Decimal] = None
    minutes: Optional[int] = None
    direction: str = DIRECTION_UNKNOWN
    card_route: Optional[str] = None
    card_oneway: Optional[Decimal] = None
    card_roundtrip: Optional[Decimal] = None
    oneway_price: Optional[Decimal] = None
    roundtrip_price: Optional[Decimal] = None
    breakdown: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # Every price here is a FARE; gratuity is always a separate line on top.
    # Local work suggests 20%; out-of-town work bills it automatically.
    gratuity_suggested: bool = False
    gratuity_mandatory: bool = False

    @property
    def is_rate_card(self) -> bool:
        return self.source == SOURCE_RATE_CARD

    @property
    def is_local_custom(self) -> bool:
        return self.source == SOURCE_LOCAL_CUSTOM


# ════════════════════════════════════════════════════════════════════════════
# THE ENGINE
# ════════════════════════════════════════════════════════════════════════════


def formula_oneway(
    vehicle_type: str,
    miles: Decimal,
    minutes: Optional[int] = None,
    direction: str = DIRECTION_UNKNOWN,
) -> tuple[Decimal, dict, list[str]]:
    """One-way custom price, plus the internal breakdown and dispatcher notes.

    Raises KeyError for an unknown vehicle type — the old code silently fell
    back to towncar rates, quoting a Van at towncar prices with no warning.
    """
    rates = VEHICLE_RATES[vehicle_type]
    miles = Decimal(miles)
    notes: list[str] = []

    mileage = mileage_charge(rates, miles)
    raw = rates.base_fee + mileage

    breakdown = {
        "base_fee": _money(rates.base_fee),
        "per_mile": rates.per_mile,
        "miles": miles.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP),
        "mileage_fee": _money(mileage),
        # Internal only: the guest sees one number, but a dispatcher asked
        # "why so much?" needs to know half of the mileage is the empty return.
        # Derive the second share by subtraction so the two always sum to the
        # mileage total — rounding each half independently leaves a stray penny
        # and an on-screen breakdown that does not add up.
        "revenue_leg_share": _money(mileage / 2),
        "empty_return_share": _money(mileage) - _money(mileage / 2),
        "per_driven_mile": rates.per_driven_mile,
        "subtotal_mileage": _money(raw),
        "time_floor_applied": False,
        "direction_discount_pct": Decimal("0"),
        "minimum_applied": False,
    }

    # ── Slow-route floor: price the driver-hours we actually commit ──
    committed_hours = None
    if minutes:
        committed_hours = (Decimal(minutes) * 2) / Decimal("60")
        time_floor = rates.hourly_floor * committed_hours
        breakdown["committed_hours"] = committed_hours.quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        breakdown["hourly_floor"] = rates.hourly_floor
        breakdown["time_floor_price"] = _money(time_floor)
        if time_floor > raw:
            raw = time_floor
            breakdown["time_floor_applied"] = True
            notes.append(
                f"This drive is slow for the distance — it ties the car up for "
                f"about {breakdown['committed_hours']} hours there and back. The "
                f"price is based on that time rather than the miles."
            )

    # ── Direction: informational only, it does not change the price ──
    # A 2026-07-29 outbound discount (from Blacklane pricing the same long route
    # 17-38% cheaper outbound) was REMOVED: that asymmetry is a network property.
    # Blacklane has Fort Lauderdale supply, so their outbound leg costs them
    # little. An Orlando-based fleet eats the empty return whichever way the
    # revenue leg runs. It was cutting 17.5% off exactly the trips the founder
    # prices highest — Disney -> Port Everglades quoted $650 against his $850.
    if direction == DIRECTION_OUTBOUND:
        notes.append(
            "Out of town, heading away from us. The car still has to come back "
            "empty, so the price is the same in either direction."
        )
    elif direction == DIRECTION_INBOUND:
        notes.append(
            "Out of town, coming back to us. We have to send the car all the way "
            "there empty to make the pickup on time, so there is no discount."
        )

    # ── Minimum: sending a car out at all has a cost ──
    if raw < rates.minimum:
        raw = rates.minimum
        breakdown["minimum_applied"] = True
        notes.append(
            f"Short for an out-of-town trip, so our minimum charge for a "
            f"{rates.label} applies."
        )

    breakdown["minimum"] = _money(rates.minimum)
    breakdown["raw_before_rounding"] = _money(raw)

    price = round_to_5(raw)
    if committed_hours and committed_hours > 0:
        breakdown["implied_hourly"] = _money(price / committed_hours)
    return price, breakdown, notes


def _attach_gratuity(result: QuoteResult, fare: Decimal, mandatory: bool) -> None:
    """Put the 20% gratuity ON TOP of the fare, as its own line.

    ``mandatory`` distinguishes out-of-town work, where we bill the gratuity, from
    local work, where it is only suggested. Either way the fare stays the headline
    figure — that is the number a dispatcher says first.
    """
    amount = _money(Decimal(fare) * GRATUITY_PCT / 100)
    result.breakdown["gratuity_pct"] = GRATUITY_PCT
    result.breakdown["gratuity_amount"] = amount
    result.breakdown["total_with_gratuity"] = _money(Decimal(fare) + amount)
    result.gratuity_mandatory = mandatory
    result.gratuity_suggested = not mandatory
    if mandatory:
        result.notes.append(
            f"Quote it as ${fare} plus {GRATUITY_PCT}% gratuity — say both "
            f"numbers. On out-of-town work the gratuity is added automatically, "
            f"not optional."
        )
        result.notes.append(
            f"Internal: the gratuity is margin, not a pass-through. Drivers are "
            f"paid a flat or hourly rate on these jobs. Never discuss with a "
            f"guest how a gratuity is split."
        )


def _apply_airport_fee(result: QuoteResult, is_roundtrip: bool) -> None:
    """Add the airport pickup surcharge to a NON-CARD fare, in the backend.

    One fee per trip, not per leg: a round trip collects the guest at the airport
    once and drops them at departures on the way back. Added after the $5
    rounding — the fee is a multiple of 5, so prices stay on the nickel.
    """
    fee = airport_pickup_fee(result.miles)
    if result.oneway_price is not None:
        result.oneway_price += fee
    if result.roundtrip_price is not None:
        result.roundtrip_price += fee
    result.price += fee
    result.breakdown["airport_pickup_fee"] = _money(fee)
    result.notes.append(
        f"Airport pickup — ${fee} added for commercial lane and tunnel access. "
        f"It is built into the fare, not a separate line for the guest."
    )


def quote(
    vehicle_type: str,
    trip_type: str = "oneway",
    miles: Optional[Decimal] = None,
    minutes: Optional[int] = None,
    pickup_location=None,
    dropoff_location=None,
    pickup_miles_from_base: Optional[Decimal] = None,
    snapped_pickup=None,
    snapped_dropoff=None,
    airport_pickup: bool = False,
) -> QuoteResult:
    """Price one trip.

    Precedence: an exact rate-card match wins outright; then a local custom
    address snapped to a nearby card zone; then the mileage formula.

    ``snapped_pickup`` / ``snapped_dropoff`` are the nearest card zones when an
    end did not match one by name — pass the matched Location through unchanged
    for ends that did match.
    """
    if vehicle_type not in VEHICLE_RATES:
        raise KeyError(f"No quote rates configured for vehicle type {vehicle_type!r}")
    rates = VEHICLE_RATES[vehicle_type]
    is_roundtrip = trip_type == "roundtrip"

    # ── 1. Published rate card wins ──
    exact_card, reverse_card = lookup_card_rates(
        vehicle_type, pickup_location, dropoff_location
    )
    card = exact_card or reverse_card
    if card:
        result = QuoteResult(
            vehicle_type=vehicle_type,
            vehicle_label=rates.label,
            trip_type=trip_type,
            price=_money(card.round_trip_price if is_roundtrip else card.oneway_price),
            source=SOURCE_RATE_CARD,
            miles=miles,
            minutes=minutes,
            card_route=f"{card.route.origin.name} ⇄ {card.route.destination.name}",
            card_oneway=_money(card.oneway_price),
            card_roundtrip=_money(card.round_trip_price),
            oneway_price=_money(card.oneway_price),
            roundtrip_price=_money(card.round_trip_price),
        )
        result.notes.append(
            "This is our published rate for this trip — the same price the "
            "website charges. Quote this, not a custom price."
        )
        # The card contradicting itself is a DATA problem, not a pricing one.
        # Surface it instead of silently picking a side: a guest booking the
        # return online would see the other figure.
        if (
            exact_card
            and reverse_card
            and exact_card.pk != reverse_card.pk
            and (
                exact_card.oneway_price != reverse_card.oneway_price
                or exact_card.round_trip_price != reverse_card.round_trip_price
            )
        ):
            other = _money(
                reverse_card.round_trip_price
                if is_roundtrip
                else reverse_card.oneway_price
            )
            result.breakdown["conflicting_reverse_card_price"] = other
            result.notes.append(
                f"⚠ Our published rate disagrees with itself on this trip: "
                f"${result.price} this way, ${other} the other way. Quoting the "
                f"direction they are travelling. Flag it to be fixed — a guest "
                f"booking the return online would see the other price."
            )
        if miles is not None:
            formula_price, _bd, _n = formula_oneway(vehicle_type, miles, minutes)
            result.breakdown["custom_estimate_not_used"] = formula_price
        # Card routes are all in-area, so they carry the recommended gratuity.
        _attach_gratuity(result, result.price, mandatory=False)
        return result

    # ── 2. Local: in-area work, which never carries an empty return ──
    # In-area means an end resolves to a card zone (by name or by snapping) and
    # the trip is short enough to be inside the service area. Everything here is
    # priced WITHOUT the out-and-back doubling, because local jobs chain.
    local_pickup = snapped_pickup or pickup_location
    local_dropoff = snapped_dropoff or dropoff_location
    is_local = bool(
        (local_pickup or local_dropoff)
        and miles is not None
        and Decimal(miles) <= SERVICE_AREA_RADIUS_MI
    )

    # 2a. A comparable card route exists → card price + custom premium.
    snapped_card = (
        lookup_card_rate(vehicle_type, local_pickup, local_dropoff)
        if is_local
        else None
    )
    if snapped_card:
        floor = LOCAL_FLOORS.get(vehicle_type, rates.minimum)
        fare_ow = max(
            round_to_5(snapped_card.oneway_price * (Decimal("1") + LOCAL_CUSTOM_PREMIUM)),
            floor,
        )
        fare_rt = max(
            round_to_5(
                snapped_card.round_trip_price * (Decimal("1") + LOCAL_CUSTOM_PREMIUM)
            ),
            round_to_5(fare_ow * Decimal("1.85")),
        )
        fare = fare_rt if is_roundtrip else fare_ow
        premium_pct = (LOCAL_CUSTOM_PREMIUM * 100).quantize(Decimal("0.1"))
        result = QuoteResult(
            vehicle_type=vehicle_type,
            vehicle_label=rates.label,
            trip_type=trip_type,
            price=fare,
            source=SOURCE_LOCAL_CUSTOM,
            miles=miles,
            minutes=minutes,
            card_route=(
                f"{snapped_card.route.origin.name} ⇄ "
                f"{snapped_card.route.destination.name}"
            ),
            card_oneway=_money(snapped_card.oneway_price),
            card_roundtrip=_money(snapped_card.round_trip_price),
            oneway_price=fare_ow,
            roundtrip_price=fare_rt,
        )
        result.breakdown.update({
            "comparable_route": result.card_route,
            "comparable_card_price": _money(
                snapped_card.round_trip_price
                if is_roundtrip
                else snapped_card.oneway_price
            ),
            "custom_premium_pct": premium_pct,
            "local_floor": _money(floor),
            "minimum_applied": fare_ow == floor,
        })
        result.notes.append(
            f"Priced the same as our {result.card_route} transfer, plus "
            f"{premium_pct}% because this is a private address rather than a "
            f"hotel we already run to."
        )
        if fare_ow == floor:
            result.notes.append(
                f"Short trip, so our minimum charge applies — we still send a car "
                f"out and bring it back."
            )
        if airport_pickup:
            _apply_airport_fee(result, is_roundtrip)
        _attach_gratuity(result, result.price, mandatory=False)
        return result

    # 2b. In-area but no comparable card route — most often an intra-zone trip,
    # e.g. MCO to a residence a few miles from MCO, where both ends resolve to
    # the same zone and a zone has no route to itself. Priced on ONE direction of
    # driving (no empty return) and floored at the local minimum, which is what
    # that floor is for: getting the car to the pickup and back to base.
    if is_local:
        floor = LOCAL_FLOORS.get(vehicle_type, rates.minimum)
        raw = rates.base_fee + rates.per_driven_mile * Decimal(miles)
        breakdown = {
            "base_fee": _money(rates.base_fee),
            "miles": Decimal(miles).quantize(Decimal("0.1")),
            "per_driven_mile": rates.per_driven_mile,
            "local_floor": _money(floor),
            "minimum_applied": raw < floor,
        }
        notes = [
            "Local trip with no matching published rate, so it is priced on the "
            "drive itself. Nearby work, so there is no long empty drive to cover."
        ]
        if raw < floor:
            raw = floor
            notes.append(
                f"Short trip, so our minimum charge applies — we still send a car "
                f"out and bring it back."
            )
        fare_ow = round_to_5(raw)
        fare_rt = round_to_5(fare_ow * rates.rt_multiplier)
        result = QuoteResult(
            vehicle_type=vehicle_type,
            vehicle_label=rates.label,
            trip_type=trip_type,
            price=fare_rt if is_roundtrip else fare_ow,
            source=SOURCE_LOCAL_CUSTOM,
            miles=Decimal(miles),
            minutes=minutes,
            oneway_price=fare_ow,
            roundtrip_price=fare_rt,
            breakdown=breakdown,
            notes=notes,
        )
        if airport_pickup:
            _apply_airport_fee(result, is_roundtrip)
        _attach_gratuity(result, result.price, mandatory=False)
        return result

    # ── 3. Out of area: the mileage formula prices it, empty return included ──
    if miles is None:
        raise ValueError("miles is required to price an off-card trip")

    direction = classify_direction(Decimal(miles), pickup_miles_from_base)
    oneway, breakdown, notes = formula_oneway(
        vehicle_type, Decimal(miles), minutes, direction
    )
    roundtrip = round_to_5(oneway * rates.rt_multiplier)

    result = QuoteResult(
        vehicle_type=vehicle_type,
        vehicle_label=rates.label,
        trip_type=trip_type,
        price=roundtrip if is_roundtrip else oneway,
        source=SOURCE_FORMULA,
        miles=Decimal(miles),
        minutes=minutes,
        direction=direction,
        oneway_price=oneway,
        roundtrip_price=roundtrip,
        breakdown=breakdown,
        notes=notes,
    )
    result.breakdown["rt_multiplier"] = rates.rt_multiplier
    if pickup_miles_from_base is None and Decimal(miles) > LONG_DISTANCE_THRESHOLD_MI:
        result.notes.append(
            "Could not work out how far the pickup is from us, so the price is "
            "the same in either direction."
        )
    result.notes.append(
        "Out-of-town trip with no published rate. The price covers driving the "
        "car out and bringing it back. If this should be one of our usual areas, "
        "check the address spelling before quoting."
    )
    if airport_pickup:
        _apply_airport_fee(result, is_roundtrip)
    # Out-of-town work bills the gratuity on top of the fare.
    _attach_gratuity(result, result.price, mandatory=True)
    return result


def quote_all_vehicles(**kwargs) -> list[QuoteResult]:
    """One quote per vehicle tier, cheapest first. Skips tiers with no rates."""
    kwargs.pop("vehicle_type", None)
    results = []
    for vehicle_type in VEHICLE_TIER_ORDER:
        if vehicle_type not in VEHICLE_RATES:
            continue
        results.append(quote(vehicle_type=vehicle_type, **kwargs))
    return results
