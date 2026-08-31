"""The one place that decides whether a leg needs a human before it is paid.

Both the Driver Payments detail page and the payroll run screen read from here,
so a flag can never mean one thing on one page and something else on the other.

A flag is a reason to LOOK, never a reason to refuse payment. Nothing in here
changes an amount; it only says which legs are worth a dispatcher's attention.

Two tiers, and the difference matters more than any single check:

  * ``needs_review`` — a person has to decide something. Keep this list short
    and true. Every entry on it costs a Sunday minute, and a list that cries
    wolf gets skimmed, which is worse than no list.
  * context — worth showing on the row, never worth stopping for. An address we
    have not listed on a trip whose price looks perfectly ordinary belongs here,
    not above. The 8/30 run put ~22 of those in front of a person; almost all of
    them were "this is fine, we just haven't typed the property in yet".
"""
import re
from decimal import Decimal

from drivers.pay_calc import calculate_driver_pay, calculate_night_bonus

# Shown as context on the row so a dispatcher can see at a glance what kind of
# trip it is. NOT review reasons — as review reasons these fired on nearly every
# driver and buried the trips that genuinely needed a decision.
SANFORD_KEYWORDS = ["sfb", "sanford", "orlando sanford"]
CRUISE_PORT_KEYWORDS = [
    "port canaveral", "canaveral", "cruise port", "cruise terminal", "cruise ship",
]

# A stop at a shop is not a destination. The chauffeur waits a few minutes and
# drives on, and Abdalla does not pay for it — so it must not sit in the review
# list every week asking to be dismissed. Shown as context only.
STORE_STOP_KEYWORDS = [
    "publix", "walmart", "wal-mart", "target", "grocery", "groceries",
    "supermarket", "super market", "winn-dixie", "winn dixie", "aldi",
    "whole foods", "trader joe", "costco", "sam's club", "sams club",
    "cvs", "walgreens", "pharmacy", "drug store", "drugstore",
    "abc fine wine", "liquor", "7-eleven", "7 eleven", "convenience",
    "wawa", "circle k", "dollar general", "dollar tree",
]

# A driver saying the tip never turned up. Checked against the tip column rather
# than taken on faith.
TIP_NOTE_PATTERN = re.compile(
    r"\b(gratuit|tip)\w*\b[^.]{0,40}\b(not|no|missing|never|without|didn'?t)\b"
    r"|\b(not|no|missing|never|didn'?t)\b[^.]{0,40}\b(gratuit|tip)\w*\b",
    re.IGNORECASE,
)

# Local↔Local tops out at 30.5 miles across every completed trip with a real
# Google distance behind it. A trip priced in the local band that is much longer
# than the local band has ever been is the Clermont bug wearing a new hat: the
# number reads fine and the drive was nothing like it. Headroom on purpose —
# this must fire on 80 miles, never on 33.
LOCAL_BAND_MILES_CEILING = 40.0

_MILES = re.compile(r"([\d.,]+)\s*mi", re.IGNORECASE)


def _distance_miles(leg, distance_cache):
    """Cached Google drive distance for this leg's exact address pair, or None."""
    if distance_cache is None:
        return None
    return distance_cache.get(
        ((leg.pickup_location or "").strip(), (leg.dropoff_location or "").strip())
    )


def build_distance_cache():
    """Every resolved pickup→dropoff distance, both directions, keyed by text.

    A few thousand rows read once. Returns miles as a float.
    """
    from reservations.models import RouteDistanceCache

    out = {}
    rows = RouteDistanceCache.objects.filter(status="ok").values_list(
        "pickup_text", "dropoff_text", "distance_text"
    )
    for pickup, dropoff, text in rows:
        match = _MILES.match((text or "").strip())
        if not match:
            continue
        try:
            miles = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        pickup, dropoff = (pickup or "").strip(), (dropoff or "").strip()
        out[(pickup, dropoff)] = miles
        out.setdefault((dropoff, pickup), miles)
    return out


def _zone_floor(zone_id, zone_rates):
    """The least this zone can ever cost, whatever it is paired with.

    A trip touching Port Canaveral cannot be a $25 trip: the cheapest pairing
    the port has is $40. So one known endpoint is enough to call a price too
    low, even when the other end is somewhere we have never listed. That is the
    whole difference between "$25 to a Disney resort we haven't typed in" — fine,
    say nothing — and "$25 to the cruise port" — that is money.
    """
    if zone_id is None:
        return None
    prices = [
        rate.inhouse_base_pay
        for rate in zone_rates
        if rate.zone_a_id == zone_id or rate.zone_b_id == zone_id
    ]
    return min(prices) if prices else None


def annotate_pay_flags(legs, distance_cache=None):
    """Annotate each leg with its review flags and return a count of each.

    ``legs`` must already have ``reservation`` selected and ``legstop_set`` and
    ``reservation__legs`` prefetched, or this will N+1.
    """
    from drivers.models import DriverPayRate
    from rates.models import Location, Route, ZoneRate

    counts = {
        "needs_pricing": 0,
        "zero_pay": 0,
        "negative_pay": 0,
        "gratuity_over_split": 0,
        "extra_destination": 0,
        "pay_mismatch": 0,
        "below_zone_floor": 0,
        "distance_mismatch": 0,
        "stale_link": 0,
        "tip_missing": 0,
        "night_bonus_missing": 0,
        # context only, never part of total_flagged
        "address_unlisted": 0,
        "store_stop": 0,
        "route_review": 0,
        "total_flagged": 0,
    }

    # Read once for the whole page rather than once per leg. These tables are a
    # few dozen rows; a leg-by-leg read here is hundreds of queries.
    location_cache = list(Location.objects.select_related("pay_zone").all())
    route_cache = list(Route.objects.all())
    rate_cache = list(DriverPayRate.objects.all())
    zone_rates = list(ZoneRate.objects.all())
    if distance_cache is None:
        distance_cache = build_distance_cache()

    floors = {}

    for leg in legs:
        loc = f"{leg.pickup_location} {leg.dropoff_location}".lower()
        leg.is_cruise = bool(leg.cruise_information_id) or any(
            kw in loc for kw in CRUISE_PORT_KEYWORDS
        )
        leg.is_sanford = any(kw in loc for kw in SANFORD_KEYWORDS)
        leg.is_zero_pay = not leg.driver_base_pay or leg.driver_base_pay == 0

        # No component of a chauffeur's pay can be below zero. Nothing should be
        # able to produce one, but "nothing should" is what was believed about
        # the price borrowed from the booking too.
        leg.negative_pay = any(
            (part or Decimal("0.00")) < 0
            for part in (leg.driver_base_pay, leg.driver_gratuity, leg.driver_additional)
        )

        # Nothing could price this trip. Note this is now genuinely rare: a leg
        # that merely lacks a Route row prices from its zone, and since the
        # auto-fill gate was narrowed to base pay alone, a leg that picked up a
        # tip before it had a rate is no longer stuck without one forever.
        leg.needs_pricing = bool(leg.driver_id) and leg.driver_base_pay is None

        # ── stops ────────────────────────────────────────────────────────────
        # A shop run and a second drop-off are not the same thing. One is a few
        # minutes' wait that nobody pays for; the other is a destination, and
        # what it pays is Abdalla's call every time.
        leg.pay_stops = list(leg.legstop_set.all())
        leg.store_stops, leg.destination_stops = [], []
        for stop in leg.pay_stops:
            name = (getattr(stop, "display_location", "") or str(stop) or "").lower()
            (leg.store_stops if any(kw in name for kw in STORE_STOP_KEYWORDS)
             else leg.destination_stops).append(stop)
        leg.has_store_stop = bool(leg.store_stops)
        leg.has_unpaid_stop = bool(leg.destination_stops) and not (
            leg.driver_additional or 0
        )

        # ── tip ──────────────────────────────────────────────────────────────
        # The tip split treats a sibling sitting at exactly $0.00 as already
        # settled, so one leg can end up holding the whole tip. Flagged only —
        # re-dividing here would take money off this leg and strand it.
        leg.gratuity_over_split = False
        res = leg.reservation
        booking_tip = Decimal("0.00")
        if res:
            booking_tip = res.gratuity_amount or Decimal("0.00")
            if not booking_tip and res.gratuity_percentage and res.base_price:
                booking_tip = res.base_price * res.gratuity_percentage / Decimal("100")
        if res and leg.driver_gratuity and booking_tip:
            sibling_count = len(res.legs.all()) or 1
            if leg.driver_gratuity > (booking_tip / sibling_count) + Decimal("0.02"):
                leg.gratuity_over_split = True

        # The chauffeur says the tip never arrived. Two different things hide
        # behind that note: the guest genuinely did not tip, or a tip the guest
        # DID pay never reached the leg. Only the second is a defect, so say
        # which one this is rather than making someone open the booking.
        leg.tip_missing = False
        leg.tip_note_but_booking_has_one = False
        if leg.driver_notes and TIP_NOTE_PATTERN.search(leg.driver_notes):
            if not (leg.driver_gratuity or 0):
                leg.tip_missing = True
                attributed = sum(
                    (sib.driver_gratuity or Decimal("0.00"))
                    for sib in (res.legs.all() if res else [])
                )
                leg.tip_note_but_booking_has_one = booking_tip > attributed

        # ── is the stored price right, and can we even tell? ─────────────────
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

        leg.below_zone_floor = False
        leg.distance_mismatch = False
        leg.address_unlisted = False
        leg.stale_link = False
        leg.floor_zone_name = ""
        leg.leg_miles = None
        # Which address we could not place, and a sensible name for it. The whole
        # point of naming it here is that the person answering the price can
        # place it in the same breath, instead of the trip coming back next week.
        leg.unlisted_text = ""
        leg.unlisted_name = ""
        if (
            leg.driver_id
            and leg.driver_base_pay is not None
            and not leg.pay_manually_set
            and not leg.pay_mismatch
        ):
            origin, dest = leg._resolve_location_endpoints(locations=location_cache)
            leg.leg_miles = _distance_miles(leg, distance_cache)

            if origin and dest:
                pair = {origin.id, dest.id}
                if leg.route_id and {
                    leg.route.origin_id, leg.route.destination_id
                } != pair:
                    from rates.models import ZoneRate as _ZR

                    by_zone = _ZR.pay_for(origin.pay_zone_id, dest.pay_zone_id)
                    if by_zone is not None and by_zone != leg.driver_base_pay:
                        leg.stale_link = True
            else:
                # One end is a place we have never listed. Use the end we DO
                # know rather than throwing up our hands: it sets a floor.
                known = origin or dest
                unknown_text = (
                    leg.dropoff_location if origin else leg.pickup_location
                ) or ""
                leg.unlisted_text = unknown_text.strip()
                # Everything before the first comma is almost always the property
                # name and almost never the street number.
                leg.unlisted_name = leg.unlisted_text.split(",")[0].strip()[:60]
                floor = None
                if known is not None and known.pay_zone_id is not None:
                    if known.pay_zone_id not in floors:
                        floors[known.pay_zone_id] = _zone_floor(
                            known.pay_zone_id, zone_rates
                        )
                    floor = floors[known.pay_zone_id]
                if floor is not None and leg.driver_base_pay < floor:
                    leg.below_zone_floor = True
                    leg.floor_zone_name = known.pay_zone.name if known.pay_zone else ""
                    leg.expected_base_pay = floor
                else:
                    # Nothing looks wrong. Say so on the row and move on — this
                    # is a property to list some rainy afternoon, not a payroll
                    # decision. It stops appearing the moment it is listed.
                    leg.address_unlisted = True

            # A price can clear every floor and still be plainly wrong when the
            # drive was four times longer than that price has ever covered.
            # Only where an end is unlisted. With both ends placed the zone
            # table has already had its say and pay_mismatch is the check that
            # matters; second-guessing it here would argue with the rate card.
            both_placed = bool(
                origin and origin.pay_zone_id and dest and dest.pay_zone_id
            )
            local_rate = (
                min(r.inhouse_base_pay for r in zone_rates)
                if zone_rates and not both_placed
                else None
            )
            if (
                not leg.below_zone_floor
                and leg.leg_miles is not None
                and local_rate is not None
                and leg.driver_base_pay <= local_rate
                and leg.leg_miles > LOCAL_BAND_MILES_CEILING
            ):
                leg.distance_mismatch = True
                leg.address_unlisted = False

        # A night pickup with no bonus. The window starts at 22:01 on purpose —
        # see calculate_night_bonus — so a 22:00 pickup carrying nothing is
        # correct and must never appear here.
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

        # Cruise/Sanford keywords, an unlisted address on an otherwise ordinary
        # price, and a shop stop are CONTEXT. None of them is a decision.
        leg.needs_review = (
            leg.is_zero_pay
            or leg.negative_pay
            or leg.needs_pricing
            or leg.gratuity_over_split
            or leg.has_unpaid_stop
            or leg.pay_mismatch
            or leg.below_zone_floor
            or leg.distance_mismatch
            or leg.stale_link
            or leg.tip_missing
            or leg.night_bonus_missing
        )

        for name, fired in (
            ("needs_pricing", leg.needs_pricing),
            ("zero_pay", leg.is_zero_pay),
            ("negative_pay", leg.negative_pay),
            ("gratuity_over_split", leg.gratuity_over_split),
            ("extra_destination", leg.has_unpaid_stop),
            ("pay_mismatch", leg.pay_mismatch),
            ("below_zone_floor", leg.below_zone_floor),
            ("distance_mismatch", leg.distance_mismatch),
            ("stale_link", leg.stale_link),
            ("tip_missing", leg.tip_missing),
            ("night_bonus_missing", leg.night_bonus_missing),
            ("address_unlisted", leg.address_unlisted),
            ("store_stop", leg.has_store_stop),
            ("route_review", leg.is_cruise or leg.is_sanford),
        ):
            if fired:
                counts[name] += 1
        if leg.needs_review:
            counts["total_flagged"] += 1

    return counts


def flag_labels(leg):
    """Short reasons this leg needs a decision, for a compact list.

    Only reasons a person must act on. Context lives in ``context_labels``.
    """
    out = []
    if getattr(leg, "negative_pay", False):
        out.append("part of this pay is below zero")
    if leg.needs_pricing:
        out.append("needs a price")
    elif leg.is_zero_pay:
        out.append("$0 pay")
    if getattr(leg, "below_zone_floor", False):
        out.append(
            f"pays ${leg.driver_base_pay} on a {leg.floor_zone_name} run — "
            f"nothing touching that zone is under ${leg.expected_base_pay}"
        )
    if getattr(leg, "distance_mismatch", False):
        out.append(
            f"pays ${leg.driver_base_pay}, the local rate, for a "
            f"{leg.leg_miles:.0f}-mile drive"
        )
    if getattr(leg, "stale_link", False):
        out.append("linked to a different trip than the one it ran")
    if leg.has_unpaid_stop:
        n = len(leg.destination_stops)
        out.append(f"{n} extra destination{'s' if n != 1 else ''}, nothing added for it")
    if leg.gratuity_over_split:
        out.append("holds more of the tip than its share")
    if getattr(leg, "tip_missing", False):
        if getattr(leg, "tip_note_but_booking_has_one", False):
            out.append("driver says no tip, but the booking is holding one")
        else:
            out.append("driver says the guest left no tip")
    if leg.pay_mismatch:
        out.append(
            f"pays ${leg.driver_base_pay} but the rates say ${leg.expected_base_pay}"
        )
    if leg.night_bonus_missing:
        out.append(f"night pickup, ${leg.night_bonus_due} bonus not on it")
    return out


def context_labels(leg):
    """Things worth seeing on the row that are NOT a reason to stop."""
    out = []
    if getattr(leg, "address_unlisted", False):
        out.append("address not listed yet — price looks normal")
    if getattr(leg, "has_store_stop", False):
        n = len(leg.store_stops)
        out.append(f"{n} shop stop{'s' if n != 1 else ''} (not paid)")
    return out
