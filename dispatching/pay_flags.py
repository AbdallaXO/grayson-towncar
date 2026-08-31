"""The one place that decides whether a leg needs a human before it is paid.

Both the Driver Payments detail page and the payroll run screen read from here,
so a flag can never mean one thing on one page and something else on the other.

A flag is a reason to LOOK, never a reason to refuse payment. Nothing in here
changes an amount; it only says which legs are worth a dispatcher's attention.
"""
from decimal import Decimal

from drivers.pay_calc import calculate_driver_pay, calculate_night_bonus

# Routes worth a second look because they have historically been mispriced by
# hand rather than by the system.
SANFORD_KEYWORDS = ["sfb", "sanford", "orlando sanford"]
CRUISE_PORT_KEYWORDS = [
    "port canaveral", "canaveral", "cruise port", "cruise terminal", "cruise ship",
]


def annotate_pay_flags(legs):
    """Annotate each leg with its review flags and return a count of each.

    ``legs`` must already have ``reservation`` selected and ``legstop_set`` and
    ``reservation__legs`` prefetched, or this will N+1.
    """
    from drivers.models import DriverPayRate
    from rates.models import Location, Route

    counts = {
        "needs_pricing": 0,
        "zero_pay": 0,
        "gratuity_over_split": 0,
        "unpaid_stop": 0,
        "pay_mismatch": 0,
        "unverified_price": 0,
        "night_bonus_missing": 0,
        "route_review": 0,
        "total_flagged": 0,
    }

    # Read once for the whole page rather than once per leg. Both tables are a
    # couple of dozen rows; a leg-by-leg read here is hundreds of queries.
    location_cache = list(Location.objects.select_related("pay_zone").all())
    route_cache = list(Route.objects.all())
    rate_cache = list(DriverPayRate.objects.all())

    for leg in legs:
        loc = f"{leg.pickup_location} {leg.dropoff_location}".lower()
        leg.is_cruise = bool(leg.cruise_information_id) or any(
            kw in loc for kw in CRUISE_PORT_KEYWORDS
        )
        leg.is_sanford = any(kw in loc for kw in SANFORD_KEYWORDS)
        leg.is_zero_pay = not leg.driver_base_pay or leg.driver_base_pay == 0

        # Nothing could price this trip: no driver rate, no route of its own, and
        # at least one endpoint in no zone. It is not that a route is missing —
        # most trips have no route and price perfectly well from their zones.
        leg.needs_pricing = bool(leg.driver_id) and leg.driver_base_pay is None

        # Stops are real work. A stop with nothing in the extra-pay box means
        # nobody has decided what the chauffeur gets for it.
        leg.pay_stops = list(leg.legstop_set.all())
        leg.has_unpaid_stop = bool(leg.pay_stops) and not (leg.driver_additional or 0)

        # The tip split treats a sibling sitting at exactly $0.00 as already
        # settled, so one leg can end up holding the whole tip. Flagged only —
        # re-dividing here would take money off this leg and strand it, because
        # the sibling that should get the other half is never re-saved.
        leg.gratuity_over_split = False
        res = leg.reservation
        if res and leg.driver_gratuity:
            tip = res.gratuity_amount or Decimal("0.00")
            if not tip and res.gratuity_percentage and res.base_price:
                tip = res.base_price * res.gratuity_percentage / Decimal("100")
            sibling_count = len(res.legs.all()) or 1
            if tip and leg.driver_gratuity > (tip / sibling_count) + Decimal("0.02"):
                leg.gratuity_over_split = True

        # Does the stored amount still agree with what the system would work out
        # today? This is the difference between "nothing is missing" and "the
        # number is right". Without it a trip carrying any number at all reads as
        # ready — which is exactly how a Clermont run to the cruise port sat at
        # $25 and looked fine. Skipped when a person typed the amount on purpose.
        leg.pay_mismatch = False
        leg.expected_base_pay = None
        if (
            leg.driver_id
            and leg.driver_base_pay is not None
            and not leg.pay_manually_set
        ):
            try:
                expected = calculate_driver_pay(
                    leg, locations=location_cache, routes=route_cache,
                    driver_rates=rate_cache,
                )
            except Exception:
                expected = None
            leg.expected_base_pay = expected
            if expected is not None and expected != leg.driver_base_pay:
                leg.pay_mismatch = True

        # Can the system actually check this number, or does it only look
        # checked? A leg carries a route as a LINK, not as a fact, and the old
        # booking-rate fallback wrote a thousand links that point at a different
        # trip. Priced off one of those, a leg agrees with itself perfectly: the
        # audit asks the wrong route what the trip costs and gets the answer
        # already stored. That is how a 70-mile run to Sebastian sits at $25 and
        # reads as ready. So say plainly when a price cannot be verified.
        leg.unverified_price = False
        leg.unverified_reason = ""
        if (
            leg.driver_id
            and leg.driver_base_pay is not None
            and not leg.pay_manually_set
            and not leg.pay_mismatch
        ):
            origin, dest = leg._resolve_location_endpoints(locations=location_cache)
            if not (origin and dest):
                leg.unverified_price = True
                leg.unverified_reason = "an address here isn't one we know"
            elif leg.route_id and {
                leg.route.origin_id, leg.route.destination_id
            } != {origin.id, dest.id}:
                # The link is wrong. Only worth a look when believing it would
                # change the money — otherwise it is tidy-up, not payroll.
                from rates.models import ZoneRate

                by_zone = ZoneRate.pay_for(origin.pay_zone_id, dest.pay_zone_id)
                if by_zone is not None and by_zone != leg.driver_base_pay:
                    leg.unverified_price = True
                    leg.unverified_reason = (
                        "linked to a different trip than the one it ran"
                    )

        # A night pickup with no bonus. Pay now follows a pickup that moves
        # across the window, but a leg priced before that landed — or one where
        # the driver's bonus changed since — can still be short.
        leg.night_bonus_missing = False
        if (
            leg.driver_id
            and leg.driver_base_pay is not None
            and not leg.pay_manually_set
            and leg.pickup_time is not None
        ):
            due = calculate_night_bonus(leg.driver, leg.pickup_time)
            if due > 0 and (leg.driver_additional or Decimal("0.00")) < due:
                leg.night_bonus_missing = True
                leg.night_bonus_due = due

        # Cruise and Sanford are shown as context, NOT as "needs a look".
        # Those keyword flags existed because the booking-rate fallback mispriced
        # exactly those routes; zones price them correctly now, so keeping them
        # in the review set flagged a Port Canaveral trip on nearly every driver
        # and buried the two that genuinely needed a decision.
        leg.needs_review = (
            leg.is_zero_pay
            or leg.needs_pricing
            or leg.gratuity_over_split
            or leg.has_unpaid_stop
            or leg.pay_mismatch
            or leg.unverified_price
            or leg.night_bonus_missing
        )

        if leg.needs_pricing:
            counts["needs_pricing"] += 1
        if leg.is_zero_pay:
            counts["zero_pay"] += 1
        if leg.gratuity_over_split:
            counts["gratuity_over_split"] += 1
        if leg.has_unpaid_stop:
            counts["unpaid_stop"] += 1
        if leg.pay_mismatch:
            counts["pay_mismatch"] += 1
        if leg.unverified_price:
            counts["unverified_price"] += 1
        if leg.night_bonus_missing:
            counts["night_bonus_missing"] += 1
        if leg.is_cruise or leg.is_sanford:
            counts["route_review"] += 1
        if leg.needs_review:
            counts["total_flagged"] += 1

    return counts


def flag_labels(leg):
    """Short reasons this leg is flagged, for a compact list."""
    out = []
    if leg.needs_pricing:
        out.append("needs a price")
    elif leg.is_zero_pay:
        out.append("$0 pay")
    if leg.has_unpaid_stop:
        n = len(leg.pay_stops)
        out.append(f"{n} extra stop{'s' if n != 1 else ''}, nothing added for it")
    if leg.gratuity_over_split:
        out.append("holds more of the tip than its share")
    if leg.pay_mismatch:
        out.append(
            f"pays ${leg.driver_base_pay} but the rates say ${leg.expected_base_pay}"
        )
    if getattr(leg, "unverified_price", False):
        out.append(f"pays ${leg.driver_base_pay}, but {leg.unverified_reason}")
    if leg.night_bonus_missing:
        out.append(f"night pickup, ${leg.night_bonus_due} bonus not on it")
    return out
