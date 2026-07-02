"""
Quote engine — pure pricing logic for hourly charters and city-to-city
transfers. Every number comes from the admin-editable models in this app; there
are no hardcoded prices here.

Public entry point: compute_quote(...) -> QuoteResult.

Distance discipline: a named CityRoute is priced from CityRoutePrice and NEVER
triggers a distance lookup. The formula path resolves miles through
pricing.distance (cache first, live Distance Matrix only as a last resort).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_cls
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from .distance import DistanceUnavailable, get_loaded_miles, normalize_place
from .models import (
    CityRoute,
    CityRoutePrice,
    FallbackFormula,
    HourlyRate,
    PeakDate,
    PricingConfig,
    VehicleClass,
)

CENT = Decimal("0.01")
DOLLAR = Decimal("1")


class QuoteError(Exception):
    """A validation problem the customer can fix (unknown class, missing
    destination, hours below minimum, …). Carries a machine `code` and the
    offending `field` for the API."""

    def __init__(self, message: str, code: str = "invalid", field: str | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.field = field


@dataclass
class QuoteResult:
    service_type: str
    vehicle_class: VehicleClass
    service_date: date_cls
    base_price: Decimal
    peak_adjustment: Decimal | None
    gratuity: Decimal
    total: Decimal
    all_inclusive: bool
    price_source: str  # route_table | formula | hourly
    # context
    origin: str = ""
    destination: str = ""
    city_route: CityRoute | None = None
    loaded_miles: Decimal | None = None
    hours: Decimal | None = None
    minimum_hours: Decimal | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def effective_base(self) -> Decimal:
        """Base including any peak adjustment — the figure gratuity is taken on."""
        return self.base_price + (self.peak_adjustment or Decimal("0"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _round(value: Decimal, config: PricingConfig) -> Decimal:
    quant = DOLLAR if config.round_to_whole_dollars else CENT
    return Decimal(value).quantize(quant, rounding=ROUND_HALF_UP)


def get_vehicle_class(key: str) -> VehicleClass:
    try:
        return VehicleClass.objects.get(key=key, is_active=True)
    except VehicleClass.DoesNotExist:
        raise QuoteError(
            f"Unknown vehicle class '{key}'.", code="unknown_class", field="vehicle_class"
        )


def peak_multiplier_for_date(d: date_cls, config: PricingConfig | None = None) -> tuple[Decimal, PeakDate | None]:
    """Return (multiplier, matching PeakDate or None). 1.0 when not a peak date."""
    config = config or PricingConfig.load()
    for pd in PeakDate.objects.filter(is_active=True):
        if pd.covers(d):
            return (pd.multiplier or config.peak_multiplier), pd
    return Decimal("1"), None


def resolve_city_route(
    destination: str, origin: str = "", route_id: int | None = None
) -> CityRoute | None:
    """Match a quote to a named CityRoute. Returns None for an unlisted route."""
    if route_id:
        route = CityRoute.objects.filter(id=route_id, is_active=True).first()
        if route:
            return route
    dest = normalize_place(destination)
    if not dest:
        return None
    for route in CityRoute.objects.filter(is_active=True):
        for alias in route.alias_list():
            alias_n = normalize_place(alias)
            if alias_n and (alias_n in dest or dest in alias_n):
                return route
    return None


def _apply_peak_gratuity(
    raw_base: Decimal, d: date_cls, config: PricingConfig
) -> tuple[Decimal, Decimal | None, Decimal, Decimal]:
    """Given a pre-peak base, return (base_price, peak_adjustment, gratuity, total),
    all rounded per config. base_price is the pre-peak figure; the gratuity and
    total include any peak adjustment."""
    mult, _peak = peak_multiplier_for_date(d, config)
    base_price = _round(raw_base, config)
    if mult != Decimal("1"):
        peaked = _round(raw_base * mult, config)
        peak_adjustment = peaked - base_price
        effective_base = peaked
    else:
        peak_adjustment = None
        effective_base = base_price
    gratuity = _round(effective_base * config.gratuity_percentage / Decimal("100"), config)
    total = effective_base + gratuity
    return base_price, peak_adjustment, gratuity, total


# ---------------------------------------------------------------------------
# City-to-city
# ---------------------------------------------------------------------------
@dataclass
class TripBasis:
    """A city-to-city trip resolved to its pricing geometry ONCE — either a
    matched named route, or loaded miles for the formula path. Computed a single
    time so an all-classes quote does a single distance lookup, not one per
    class.

    `distance_error` is set (instead of raising) when miles could not be
    resolved for an unlisted route, so an all-classes caller can surface one
    trip-level message rather than five identical per-class failures.
    """

    route: CityRoute | None
    loaded_miles: Decimal | None
    origin_label: str
    dest_label: str
    distance_error: QuoteError | None = None


def resolve_trip_basis(
    origin: str, destination: str, route_id: int | None = None
) -> TripBasis:
    """Resolve (origin, destination) to a TripBasis with at most one distance
    lookup. A named route never triggers a distance call."""
    route = resolve_city_route(destination, origin, route_id)
    if route:
        loaded_miles = Decimal(route.approx_miles) if route.approx_miles else None
        return TripBasis(
            route=route,
            loaded_miles=loaded_miles,
            origin_label=origin or route.origin_label,
            dest_label=route.name,
        )
    try:
        loaded_miles, _src = get_loaded_miles(origin, destination)
    except DistanceUnavailable:
        return TripBasis(
            route=None,
            loaded_miles=None,
            origin_label=origin,
            dest_label=destination,
            distance_error=QuoteError(
                "We service this route, but it isn't set up for an instant "
                "online quote yet. Please call (407) 212-7190 and we'll quote it "
                "right away.",
                code="distance_unavailable",
                field="destination",
            ),
        )
    return TripBasis(
        route=None,
        loaded_miles=loaded_miles,
        origin_label=origin,
        dest_label=destination,
    )


def _price_route_class(
    basis: TripBasis,
    vehicle_class: VehicleClass,
    service_date: date_cls,
    config: PricingConfig,
) -> QuoteResult:
    """Authoritative named-route price for one class — NO distance lookup, NO
    deadhead (the flat price already accounts for the return trip)."""
    price_row = CityRoutePrice.objects.filter(
        city_route=basis.route, vehicle_class=vehicle_class
    ).first()
    if not price_row:
        raise QuoteError(
            f"{vehicle_class.display_name} is not available for "
            f"{basis.route.name}. Please call us for a quote.",
            code="class_unavailable_for_route",
            field="vehicle_class",
        )
    base_price, peak_adjustment, gratuity, total = _apply_peak_gratuity(
        price_row.price, service_date, config
    )
    return QuoteResult(
        service_type="city_to_city",
        vehicle_class=vehicle_class,
        service_date=service_date,
        base_price=base_price,
        peak_adjustment=peak_adjustment,
        gratuity=gratuity,
        total=total,
        all_inclusive=True,
        price_source="route_table",
        origin=basis.origin_label,
        destination=basis.dest_label,
        city_route=basis.route,
        loaded_miles=basis.loaded_miles,
        notes=[config.all_inclusive_note],
    )


def _price_formula_class(
    basis: TripBasis,
    vehicle_class: VehicleClass,
    service_date: date_cls,
    config: PricingConfig,
) -> QuoteResult:
    """Per-mile formula price for one class on an unlisted route. The mileage
    component is inflated by the class deadhead_factor to cover the empty return
    of our owned vehicle; the customer-facing loaded_miles stays the true
    one-way distance."""
    formula = _get_formula(vehicle_class)
    chargeable_miles = basis.loaded_miles * formula.deadhead_factor
    raw_base = formula.base + (chargeable_miles * formula.per_mile)
    if raw_base < formula.minimum:
        raw_base = formula.minimum
    base_price, peak_adjustment, gratuity, total = _apply_peak_gratuity(
        raw_base, service_date, config
    )
    return QuoteResult(
        service_type="city_to_city",
        vehicle_class=vehicle_class,
        service_date=service_date,
        base_price=base_price,
        peak_adjustment=peak_adjustment,
        gratuity=gratuity,
        total=total,
        all_inclusive=True,
        price_source="formula",
        origin=basis.origin_label,
        destination=basis.dest_label,
        city_route=None,
        loaded_miles=basis.loaded_miles,
        notes=[config.all_inclusive_note],
    )


def quote_city_to_city(
    vehicle_class: VehicleClass,
    service_date: date_cls,
    origin: str,
    destination: str,
    route_id: int | None = None,
    config: PricingConfig | None = None,
) -> QuoteResult:
    config = config or PricingConfig.load()
    if not destination or not destination.strip():
        raise QuoteError(
            "Please enter a destination.", code="missing_destination", field="destination"
        )

    basis = resolve_trip_basis(origin, destination, route_id)
    if basis.route:
        return _price_route_class(basis, vehicle_class, service_date, config)
    if basis.distance_error:
        raise basis.distance_error
    return _price_formula_class(basis, vehicle_class, service_date, config)


def _get_formula(vehicle_class: VehicleClass) -> FallbackFormula:
    try:
        return vehicle_class.fallback_formula
    except FallbackFormula.DoesNotExist:
        raise QuoteError(
            f"No fallback pricing configured for {vehicle_class.display_name}.",
            code="config_missing",
            field="vehicle_class",
        )


# ---------------------------------------------------------------------------
# Hourly
# ---------------------------------------------------------------------------
def quote_hourly(
    vehicle_class: VehicleClass,
    service_date: date_cls,
    hours: Decimal,
    config: PricingConfig | None = None,
) -> QuoteResult:
    config = config or PricingConfig.load()
    try:
        rate = vehicle_class.hourly_rate
    except HourlyRate.DoesNotExist:
        raise QuoteError(
            f"No hourly rate configured for {vehicle_class.display_name}.",
            code="config_missing",
            field="vehicle_class",
        )

    hours = Decimal(hours)
    # 30-minute increments only.
    increment = Decimal(config.overtime_increment_minutes) / Decimal("60")
    if increment > 0 and (hours / increment) % 1 != 0:
        raise QuoteError(
            f"Hours must be in {config.overtime_increment_minutes}-minute "
            "increments.",
            code="bad_increment",
            field="hours",
        )

    # Peak may raise the minimum (e.g. Sprinter 4 hr on peak).
    _mult, peak = peak_multiplier_for_date(service_date, config)
    min_hours = rate.minimum_hours
    if peak and rate.peak_minimum_hours:
        min_hours = rate.peak_minimum_hours

    if hours < min_hours:
        raise QuoteError(
            f"{vehicle_class.display_name} has a {min_hours:g}-hour minimum"
            + (" on this date." if peak and rate.peak_minimum_hours else "."),
            code="below_minimum",
            field="hours",
        )

    raw_base = rate.hourly_rate * hours
    base_price, peak_adjustment, gratuity, total = _apply_peak_gratuity(
        raw_base, service_date, config
    )

    notes = [config.hourly_tolls_note, f"{min_hours:g}-hour minimum."]
    return QuoteResult(
        service_type="hourly",
        vehicle_class=vehicle_class,
        service_date=service_date,
        base_price=base_price,
        peak_adjustment=peak_adjustment,
        gratuity=gratuity,
        total=total,
        all_inclusive=False,
        price_source="hourly",
        hours=hours,
        minimum_hours=min_hours,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------
def compute_quote(
    *,
    service_type: str,
    vehicle_class_key: str,
    service_date: date_cls,
    origin: str = "",
    destination: str = "",
    route_id: int | None = None,
    hours: Decimal | None = None,
) -> QuoteResult:
    """Validate inputs and return a QuoteResult. Raises QuoteError on bad input."""
    if service_date is None:
        raise QuoteError("Please choose a date.", code="missing_date", field="date")
    if service_date < timezone.localdate():
        raise QuoteError("Please choose a date in the future.", code="past_date", field="date")

    vehicle_class = get_vehicle_class(vehicle_class_key)

    if service_type == "hourly":
        if hours is None:
            raise QuoteError("Please enter the number of hours.", code="missing_hours", field="hours")
        return quote_hourly(vehicle_class, service_date, hours)
    elif service_type == "city_to_city":
        return quote_city_to_city(
            vehicle_class, service_date, origin, destination, route_id
        )
    raise QuoteError(
        f"Unknown service type '{service_type}'.", code="unknown_service", field="service_type"
    )


# ---------------------------------------------------------------------------
# All-classes quote (Blacklane-style: enter the trip once, price every class)
# ---------------------------------------------------------------------------
@dataclass
class ClassQuote:
    """One vehicle class's outcome within an all-classes quote: either a priced
    QuoteResult, or unavailable with a human reason (e.g. the named route has no
    price for this class)."""

    vehicle_class: VehicleClass
    available: bool
    result: QuoteResult | None = None
    unavailable_reason: str = ""


def quote_all_classes(
    *,
    service_type: str,
    service_date: date_cls,
    origin: str = "",
    destination: str = "",
    route_id: int | None = None,
    hours: Decimal | None = None,
    config: PricingConfig | None = None,
) -> list[ClassQuote]:
    """Price EVERY active vehicle class for a single trip, doing the distance
    lookup at most once. Trip-level problems (bad date, missing destination, no
    resolvable distance) raise QuoteError; a problem with one class only (no
    route price for that class) marks just that class unavailable.
    """
    config = config or PricingConfig.load()
    if service_date is None:
        raise QuoteError("Please choose a date.", code="missing_date", field="date")
    if service_date < timezone.localdate():
        raise QuoteError("Please choose a date in the future.", code="past_date", field="date")

    classes = list(VehicleClass.objects.filter(is_active=True).order_by("sort_order"))
    out: list[ClassQuote] = []

    if service_type == "city_to_city":
        if not destination or not destination.strip():
            raise QuoteError(
                "Please enter a destination.", code="missing_destination", field="destination"
            )
        basis = resolve_trip_basis(origin, destination, route_id)
        if basis.distance_error:
            raise basis.distance_error  # trip-level: nothing can be priced
        for vc in classes:
            try:
                result = (
                    _price_route_class(basis, vc, service_date, config)
                    if basis.route
                    else _price_formula_class(basis, vc, service_date, config)
                )
                out.append(ClassQuote(vc, True, result))
            except QuoteError as exc:
                out.append(ClassQuote(vc, False, None, exc.message))
    elif service_type == "hourly":
        if hours is None:
            raise QuoteError("Please enter the number of hours.", code="missing_hours", field="hours")
        for vc in classes:
            try:
                out.append(ClassQuote(vc, True, quote_hourly(vc, service_date, hours, config)))
            except QuoteError as exc:
                out.append(ClassQuote(vc, False, None, exc.message))
    else:
        raise QuoteError(
            f"Unknown service type '{service_type}'.", code="unknown_service", field="service_type"
        )

    return out
