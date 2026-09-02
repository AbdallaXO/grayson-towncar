"""The reservation wizard's final audit.

Step 5 re-checks the whole booking rather than trusting that each step was
clean when the dispatcher left it — legs get edited, vehicles get swapped, and
a booking that was fine at step 2 can stop being fine by step 4.

Two severities, and the difference matters:

  * ``crit``  — blocking. The reservation cannot be created.
  * ``warn``  — worth confirming with the guest. Never blocks; it only asks
                the dispatcher to tick the acknowledgement box.

Deliberately NOT flagged: a base price that differs from the standard rate.
Dispatchers override prices on purpose, and flagging that is pure noise.
"""

from datetime import datetime, timedelta

from django.utils import timezone

# Two legs closer together than this are probably a typo, not a plan.
TIGHT_TURNAROUND_MINUTES = 90


def _as_date(value):
    if hasattr(value, "toordinal"):
        return value
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _as_time(value):
    if hasattr(value, "hour"):
        return value
    if not value:
        return None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(str(value).strip(), fmt).time()
        except (ValueError, TypeError):
            continue
    return None


def _int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _money(value):
    try:
        return float(str(value).replace("$", "").replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_clock(value):
    """Session strings ('10:30:00') back to a time. Shared with the pricing step."""
    return _as_time(value)


def _flag(tone, text, step):
    return {"tone": tone, "text": text, "step": step}


def customer_flags(customer):
    out = []
    if not customer:
        return [_flag("crit", "First and last name are required.", 1)]
    if not (customer.first_name or "").strip() or not (customer.last_name or "").strip():
        out.append(_flag("crit", "First and last name are required.", 1))
    phone = (customer.phone_number or "").strip()
    digits = "".join(ch for ch in phone if ch.isdigit())
    if not phone:
        out.append(_flag(
            "crit",
            "A phone number is required — the driver texts the guest on arrival.",
            1,
        ))
    elif len(digits) < 10 or len(digits) > 11:
        out.append(_flag(
            "warn",
            f"That phone number has {len(digits)} digits — US numbers have 10.",
            1,
        ))
    email = (customer.email or "").strip()
    if not email:
        out.append(_flag("crit", "An email address is required for the confirmation.", 1))
    elif "@" not in email or "." not in email.split("@")[-1]:
        out.append(_flag("warn", "That email address does not look complete.", 1))
    return out


def ride_flags(reservation_data, vehicle):
    out = []
    rd = reservation_data or {}
    pax = _int(rd.get("passenger_count"), 0)
    bags = _int(rd.get("luggage_count"), 0)
    seats = (
        _int(rd.get("rf_carseats"))
        + _int(rd.get("ff_carseats"))
        + _int(rd.get("booster_seats"))
    )

    if pax < 1:
        out.append(_flag("crit", "Passenger count is 0.", 2))
    if vehicle is None:
        out.append(_flag("crit", "No vehicle has been selected.", 2))
    else:
        name = vehicle.get_vehicle_type_display()
        if pax > vehicle.capacity:
            out.append(_flag(
                "crit",
                f"{name} seats {vehicle.capacity}. {pax} passengers will not fit.",
                2,
            ))
        if bags > vehicle.luggage_capacity:
            out.append(_flag(
                "warn",
                f"{name} holds about {vehicle.luggage_capacity} bags — {bags} may "
                f"not fit with passengers aboard.",
                2,
            ))
    if pax > 0 and seats > pax:
        out.append(_flag(
            "warn", f"{seats} car seats for {pax} passengers — confirm the count.", 2
        ))
    return out


def leg_flags(legs_data, flights_data=None):
    """Per-leg checks, plus the cross-leg ordering and turnaround checks."""
    out = []
    flights_data = flights_data or []
    today = timezone.localdate()
    stamps = []

    for i, leg in enumerate(legs_data or []):
        label = f"Leg {i + 1}"
        d = _as_date(leg.get("pickup_date"))
        t = _as_time(leg.get("pickup_time"))
        pickup = (leg.get("pickup_location") or "").strip()
        dropoff = (leg.get("dropoff_location") or "").strip()

        if not pickup or not dropoff:
            out.append(_flag(
                "crit", f"{label}: pickup and drop-off addresses are both required.", 3
            ))
        if t is None:
            out.append(_flag("crit", f"{label}: the pickup time is not a valid clock time.", 3))
        if d is None:
            out.append(_flag("crit", f"{label}: the pickup date is missing.", 3))
        elif d < today:
            # %-d is not portable to Windows, so the day is spelled out by hand.
            spoken_date = f"{d.strftime('%A, %B')} {d.day}, {d.year}"
            out.append(_flag(
                "crit", f"{label}: the pickup date is in the past — {spoken_date}.", 3
            ))
        if t is not None and t.hour < 5:
            spoken = t.strftime("%I:%M %p").lstrip("0")
            out.append(_flag(
                "warn",
                f"{label}: {spoken} is the middle of the night. Confirm the guest "
                f"did not mean PM.",
                3,
            ))

        flight = flights_data[i] if i < len(flights_data) else {}
        flight_no = ((flight or {}).get("flight_number") or "").strip()
        if ("mco" in pickup.lower() or "mco" in dropoff.lower()) and not flight_no:
            out.append(_flag(
                "warn",
                f"{label}: airport leg with no flight number — the driver cannot "
                f"track a delay.",
                3,
            ))

        stamps.append(datetime.combine(d, t) if (d and t) else None)

    for i in range(1, len(stamps)):
        a, b = stamps[i - 1], stamps[i]
        if a is None or b is None:
            continue
        if b < a:
            out.append(_flag(
                "crit", f"Leg {i + 1} is scheduled before leg {i}. Check the dates.", 3
            ))
        elif b - a < timedelta(minutes=TIGHT_TURNAROUND_MINUTES):
            minutes = int((b - a).total_seconds() // 60)
            out.append(_flag(
                "warn", f"Only {minutes} minutes between leg {i} and leg {i + 1}.", 3
            ))
    return out


def price_flags(pricing_data, legs_data=None):
    from reservations.utils import AFTERHOURS_FEE_AMOUNT, is_afterhours_time

    out = []
    pd = pricing_data or {}
    if _money(pd.get("total_price")) <= 0:
        out.append(_flag("crit", "No price has been set.", 4))

    # A late-night pickup carries a flat surcharge. It is easy to price the
    # route and forget the fee, and nobody notices until the money is missing.
    late = []
    for i, leg in enumerate(legs_data or []):
        t = _as_time(leg.get("pickup_time"))
        if is_afterhours_time(t):
            late.append((i + 1, t))
    if late and _money(pd.get("additional_charges")) < float(AFTERHOURS_FEE_AMOUNT) * len(late):
        legs = ", ".join(
            f"leg {n} at {t.strftime('%I:%M %p').lstrip('0')}" for n, t in late
        )
        owed = AFTERHOURS_FEE_AMOUNT * len(late)
        out.append(_flag(
            "warn",
            f"After-hours pickup ({legs}) — the ${owed:.2f} after-hours fee is not "
            f"in this price.",
            4,
        ))
    return out


def review_flags(*, customer, reservation_data, vehicle, legs_data, flights_data, pricing_data):
    """Every flag on the booking, in the order the dispatcher would fix them."""
    return (
        customer_flags(customer)
        + ride_flags(reservation_data, vehicle)
        + leg_flags(legs_data, flights_data)
        + price_flags(pricing_data, legs_data)
    )
