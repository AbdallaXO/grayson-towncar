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


def calculate_leg_refund_suggestion(leg):
    """
    Calculate the suggested refund amount for a single leg based on time until pickup.

    Returns:
        dict: {
            'leg_id': int,
            'revenue_share': Decimal,
            'refund_percentage': int (0, 50, or 100),
            'suggested_amount': Decimal,
            'tier': str ('48h+', '24-48h', '<24h'),
            'pickup_datetime': datetime,
            'pickup_location': str,
            'dropoff_location': str,
        }
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

    revenue = leg.revenue_share or leg.calculate_revenue_share()

    return {
        'leg_id': leg.id,
        'revenue_share': revenue,
        'refund_percentage': pct,
        'suggested_amount': (revenue * Decimal(pct) / Decimal(100)).quantize(Decimal('0.01')),
        'tier': tier,
        'pickup_datetime': pickup_dt,
        'pickup_location': leg.pickup_location,
        'dropoff_location': leg.dropoff_location,
    }


def calculate_refund_suggestion(reservation, leg_ids=None):
    """
    Calculate the total suggested refund for a reservation, optionally for specific legs.

    Args:
        reservation: Reservation instance (with prefetched legs ideally)
        leg_ids: Optional list of specific leg IDs. If None, all non-cancelled legs.

    Returns:
        dict: {
            'total_suggested': Decimal,
            'leg_details': list of per-leg dicts,
            'has_zero_refund_legs': bool (True if any leg gets 0%),
        }
    """
    legs = reservation.legs.all()
    if leg_ids:
        legs = [leg for leg in legs if leg.id in leg_ids]
    else:
        # Exclude already-cancelled legs
        legs = [leg for leg in legs if leg.status != 'cancelled']

    details = [calculate_leg_refund_suggestion(leg) for leg in legs]
    total = sum(d['suggested_amount'] for d in details)

    return {
        'total_suggested': total,
        'leg_details': details,
        'has_zero_refund_legs': any(d['refund_percentage'] == 0 for d in details),
    }
