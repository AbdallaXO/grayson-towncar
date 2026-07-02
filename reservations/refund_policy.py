"""
Per-leg refund policy engine.

Time-based tiers (measured from now to the leg's pickup datetime):
  - 48h+ before pickup  -> 100% refund
  - 24-48h before pickup -> 50% refund
  - <24h before pickup   -> 0% refund
"""
from decimal import Decimal
from django.utils import timezone
from datetime import datetime


# Customer-facing cancellation policy — SINGLE SOURCE OF TRUTH for the copy.
# This sentence MUST describe the tier logic in _calculate_leg_refund below. It is
# passed into customer emails and rendered via
# users/templates/users/includes/_cancellation_policy.html, so the words a customer
# reads and the refund the engine computes can never drift apart again. If you change
# the tiers, change this sentence.
CANCELLATION_POLICY_SENTENCE = (
    "Full refund for cancellations 48 or more hours before pickup; "
    "50% refund for cancellations 24 to 48 hours before pickup; "
    "cancellations within 24 hours of pickup are non-refundable."
)


def _calculate_leg_refund(leg, revenue_per_leg):
    """
    Calculate the suggested refund amount for a single leg based on time until pickup.

    Args:
        leg: Leg instance
        revenue_per_leg: Pre-calculated revenue share (total_price / active leg count)
    """
    now = timezone.now()

    # Build pickup datetime (timezone-aware)
    pickup_naive = datetime.combine(leg.pickup_date, leg.pickup_time or datetime.min.time())
    if timezone.is_naive(pickup_naive):
        pickup_dt = timezone.make_aware(pickup_naive)
    else:
        pickup_dt = pickup_naive

    hours_until_pickup = (pickup_dt - now).total_seconds() / 3600

    if hours_until_pickup >= 48:
        pct = 100
        tier = '48h+'
    elif hours_until_pickup >= 24:
        pct = 50
        tier = '24-48h'
    else:
        pct = 0
        tier = '<24h'

    return {
        'leg_id': leg.id,
        'revenue_share': revenue_per_leg,
        'refund_percentage': pct,
        'suggested_amount': (revenue_per_leg * Decimal(pct) / Decimal(100)).quantize(Decimal('0.01')),
        'tier': tier,
        'pickup_datetime': pickup_dt,
        'pickup_location': leg.pickup_location,
        'dropoff_location': leg.dropoff_location,
    }


def calculate_refund_suggestion(reservation, leg_ids=None):
    """
    Calculate the total suggested refund for a reservation, optionally for specific legs.

    Always derives revenue per leg fresh from the reservation's current total_price
    divided by its active (non-cancelled) leg count, so stale stored values can't
    produce inflated suggestions.

    The total is capped at total_paid (or total_price if nothing paid yet) so the
    suggestion never exceeds what was actually collected.
    """
    all_legs = list(reservation.legs.all())

    if leg_ids:
        legs = [leg for leg in all_legs if leg.id in leg_ids]
    else:
        legs = [leg for leg in all_legs if leg.status != 'cancelled']

    if not legs:
        return {
            'total_suggested': Decimal('0.00'),
            'leg_details': [],
            'has_zero_refund_legs': False,
        }

    # Fresh revenue split: total_price / number of active legs
    active_leg_count = len([l for l in all_legs if l.status != 'cancelled'])
    if active_leg_count == 0:
        active_leg_count = len(all_legs) or 1
    total_price = reservation.total_price or Decimal('0.00')
    revenue_per_leg = (total_price / Decimal(active_leg_count)).quantize(Decimal('0.01'))

    details = [_calculate_leg_refund(leg, revenue_per_leg) for leg in legs]
    total = sum(d['suggested_amount'] for d in details)

    # Cap at what was actually paid (never suggest more than collected)
    max_refundable = reservation.total_paid if reservation.total_paid > 0 else total_price
    if total > max_refundable:
        total = max_refundable

    return {
        'total_suggested': total,
        'leg_details': details,
        'has_zero_refund_legs': any(d['refund_percentage'] == 0 for d in details),
    }
