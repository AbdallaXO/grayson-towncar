"""
One-time cleanup for after-hours legs booked before the pricing screen asked.

``leg.afterhours_fee`` records that a leg's $20 is collected. For years it was
only set when the money happened to land in ``additional_charges``, so a fee
folded into a hand-quoted price left it at zero and the trip card kept asking a
dispatcher to collect what the guest had already paid.

Most of those legs no longer need touching — ``Leg.afterhours_fee_outstanding``
now reads the booking's itemised charges directly. What remains is the folded-in
case, where the only evidence is arithmetic: the reservation's total equals the
standard rate for its route plus a fee for every late leg on it.

That proof is the whole bar for stamping. Everything else is left alone and keeps
flagging, because roughly 160 of these really were never billed, and marking
those "collected" would write off money on trips that can still be charged.
"""

import logging
from decimal import Decimal

from django.db import transaction

from .utils import AFTERHOURS_FEE_AMOUNT, is_afterhours_time

logger = logging.getLogger(__name__)

NOTE = (
    f"${AFTERHOURS_FEE_AMOUNT:.2f} After-Hours Fee marked collected — the booking "
    f"total matches the standard rate plus the fee"
)


def _standard_price(reservation):
    """The rate-sheet price for this reservation's route, or None if it has no rate."""
    rate = reservation.rate
    if rate is None:
        return None
    trip_type = (reservation.trip_type or "").replace("-", "_")
    price = (
        rate.round_trip_price if trip_type.startswith("round") else rate.oneway_price
    )
    return Decimal(price) if price is not None else None


def provable_legs(reservations=None):
    """Legs whose after-hours fee is provably already in the booking total.

    Returns a list of Leg objects. Read-only — callers decide whether to stamp.
    """
    from .models import Leg, Reservation

    qs = reservations
    if qs is None:
        qs = Reservation.objects.exclude(status="cancelled")
    qs = qs.select_related("rate").prefetch_related("legs")

    out = []
    for reservation in qs:
        standard = _standard_price(reservation)
        if standard is None:
            continue

        legs = [lg for lg in reservation.legs.all() if lg.status != "cancelled"]
        late = [lg for lg in legs if is_afterhours_time(lg.pickup_time)]
        if not late:
            continue

        total = Decimal(reservation.total_price or 0)
        expected = standard + AFTERHOURS_FEE_AMOUNT * len(late)
        if total != expected:
            continue

        for leg in late:
            if Decimal(leg.afterhours_fee or 0) < AFTERHOURS_FEE_AMOUNT:
                out.append(leg)
    return out


def apply_backfill(reservations=None):
    """Stamp every provable leg. Returns how many were changed.

    Safe to re-run: a stamped leg no longer qualifies, so a second pass is a
    no-op.
    """
    legs = provable_legs(reservations=reservations)
    if not legs:
        return 0

    with transaction.atomic():
        for leg in legs:
            leg.afterhours_fee = AFTERHOURS_FEE_AMOUNT
            leg.private_notes = (
                f"{leg.private_notes}\n{NOTE}" if leg.private_notes else NOTE
            )
            leg.save(update_fields=["afterhours_fee", "private_notes"])

    logger.info(f"After-hours backfill stamped {len(legs)} leg(s)")
    return len(legs)
