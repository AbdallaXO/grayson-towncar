"""Commission check: unpaid commissions a person should look at before paying.

Two lists, both read-only until someone clicks:

  * Possible personal trips: the rider looks like the agent (same name, same
    last name), the notes mention a discount, or the price is well under the
    normal rate. Agents get no commission on their own discounted trips.
  * Possible duplicates: the same agent booked the same rider for the same day
    more than once, so commission would be paid twice.

Deliberately NOT a signal: the rider's email or phone being the agent's own.
Agents put their own contact on clients' bookings all the time.

Nothing is changed automatically. "Not commissionable" sets the same flag the
reservation page uses; "It's fine" / "All real" is saved as a CommissionCheck so
the booking never comes back to the list.
"""
import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction

from users.eligibility import STATUS_PENDING, STATUS_READY, STATUS_REVIEW, get_commission_eligibility

# Under the normal rate by at least this much counts as discounted.
DISCOUNT_SHARE = Decimal("0.15")
PERSONAL_WORDS = re.compile(
    r"\b(personal|discount(?:ed)?|my own|myself|my family|family trip|comp(?:ed|limentary)?|"
    r"ta rate|agent rate|friends? and family|non[- ]?commission(?:able)?|no commission|"
    r"courtesy|free ride|at cost|my trip|for me)\b",
    re.I,
)
STATUS_LABELS = {STATUS_READY: "Payable now", STATUS_PENDING: "Upcoming", STATUS_REVIEW: "In review"}


class CheckRefused(Exception):
    """The booking changed since the page loaded; nothing was written."""


def _letters(text):
    return re.sub(r"[^a-z]", "", (text or "").lower())


def _last_word(text):
    words = re.findall(r"[a-z]+", (text or "").lower())
    return words[-1] if words else ""


@dataclass
class Flagged:
    reservation: object
    reasons: list = field(default_factory=list)
    status: str = ""
    commission: Decimal = Decimal("0")
    list_price: Decimal = None

    @property
    def status_label(self):
        return STATUS_LABELS.get(self.status, "")

    @property
    def strong(self):
        return any(r.startswith("Rider is the agent") for r in self.reasons)


def _open_reservations():
    """Unpaid, not excluded, not cancelled agent bookings, with what the page needs."""
    from reservations.models import Reservation

    return (
        Reservation.objects.filter(travel_agent__isnull=False, commission_paid=False, commission_excluded=False,
                                   base_price__gt=0)
        .exclude(status="cancelled")
        .select_related("travel_agent", "travel_agent__user", "travel_agent__agency", "customer", "rate")
        .prefetch_related("legs")
    )


def _decided(kind):
    from users.models import CommissionCheck

    return set(CommissionCheck.objects.filter(kind=kind).values_list("reservation_id", flat=True))


def personal_reasons(res):
    """Why this booking might be the agent's own trip (empty list = nothing to check)."""
    agent, rider = res.travel_agent, res.customer
    reasons = []
    rider_name = _letters(f"{rider.first_name}{rider.last_name}")
    if len(rider_name) > 5 and rider_name == _letters(agent.agent_name):
        reasons.append(f"Rider is the agent ({rider.first_name} {rider.last_name})".strip())
    elif len(_letters(rider.last_name)) > 2 and _letters(rider.last_name) == _last_word(agent.agent_name):
        reasons.append(f"Rider has the agent's last name ({rider.last_name})")
    words = PERSONAL_WORDS.search(" ".join(filter(None, [res.private_notes, res.special_requests])))
    if words:
        reasons.append(f"Notes say “{words.group(0)}”")
    normal = _normal_price(res)
    if normal and res.base_price < normal * (1 - DISCOUNT_SHARE):
        off = int((1 - res.base_price / normal) * 100)
        reasons.append(f"Priced {off}% under the normal rate (${res.base_price:,.2f} vs ${normal:,.2f})")
    return reasons


def _normal_price(res):
    if not res.rate_id:
        return None
    return res.rate.round_trip_price if res.trip_type == "round_trip" else res.rate.oneway_price


def personal_suspects(*, now=None):
    """Possible personal trips nobody has decided on yet. Payable-now and strongest first."""
    from users.models import CommissionCheck

    decided = _decided(CommissionCheck.PERSONAL)
    out = []
    for res in _open_reservations():
        if res.id in decided:
            continue
        reasons = personal_reasons(res)
        if not reasons:
            continue
        verdict = get_commission_eligibility(res, now=now)
        out.append(Flagged(res, reasons, verdict.status, verdict.commission, _normal_price(res)))
    out.sort(key=lambda f: (f.status != STATUS_READY, not f.strong, -f.commission))
    return out


@dataclass
class DuplicateGroup:
    agent: object
    rider: str
    day: object
    members: list  # of Flagged, oldest booking first

    @property
    def key(self):
        return "-".join(str(f.reservation.id) for f in self.members)

    @property
    def commission(self):
        return sum((f.commission for f in self.members), Decimal("0"))

    @property
    def payable_now(self):
        return sum(1 for f in self.members if f.status == STATUS_READY)


def _first_day(res):
    days = [leg.pickup_date for leg in res.legs.all() if leg.status != "cancelled" and leg.pickup_date]
    return min(days) if days else None


def duplicate_groups(*, now=None):
    """Same agent, same rider, same first-ride day, booked more than once and still unpaid."""
    from users.models import CommissionCheck

    decided = _decided(CommissionCheck.DUPLICATE)
    buckets = defaultdict(list)
    for res in _open_reservations():
        day = _first_day(res)
        rider = _letters(f"{res.customer.first_name}{res.customer.last_name}")
        if day and rider:
            buckets[(res.travel_agent_id, rider, day)].append(res)
    groups = []
    for (_, _, day), rows in buckets.items():
        if len(rows) < 2 or all(r.id in decided for r in rows):
            continue
        rows.sort(key=lambda r: (r.created_at, r.id))
        members = []
        for res in rows:
            verdict = get_commission_eligibility(res, now=now)
            members.append(Flagged(res, status=verdict.status, commission=verdict.commission))
        rider = rows[0].customer
        groups.append(DuplicateGroup(rows[0].travel_agent, f"{rider.first_name} {rider.last_name}".strip(), day, members))
    groups.sort(key=lambda g: (-g.payable_now, g.day))
    return groups


def _record(reservation, kind, decision, user):
    from users.models import CommissionCheck

    CommissionCheck.objects.update_or_create(
        reservation=reservation, kind=kind, defaults={"decision": decision, "decided_by": user},
    )


def _audit(reservation, new_value, user):
    from reservations.models import AuditLog

    AuditLog.objects.create(
        model_name="Reservation", object_id=reservation.id, action="updated", field_name="commission",
        old_value="commission counted", new_value=new_value,
        user=user, username=user.get_username() if user else "system",
    )


def exclude(reservation_id, *, kind, reason, user):
    """Mark one booking not commissionable, the same way the reservation page does."""
    from reservations.models import Reservation
    from django.utils import timezone

    with transaction.atomic():
        res = Reservation.objects.select_for_update().get(pk=reservation_id)
        number = res.display_number
        if res.commission_paid:
            raise CheckRefused(f"#{number}'s commission was already paid, so it was left as it is.")
        if res.commission_excluded:
            _record(res, kind, "excluded", user)
            return res
        res.commission_excluded = True
        res.commission_exclusion_reason = reason[:255]
        res.commission_excluded_at = timezone.now()
        res.commission_excluded_by = user
        res.commission_amount = Decimal("0")
        res.save(update_fields=["commission_excluded", "commission_exclusion_reason",
                                "commission_excluded_at", "commission_excluded_by", "commission_amount"])
        _record(res, kind, "excluded", user)
        _audit(res, f"not commissionable: {reason}", user)
    return res


def mark_fine(reservation_ids, *, kind, user):
    """Someone looked and it's a real, commissionable booking. Never flag it again."""
    from reservations.models import Reservation

    rows = list(Reservation.objects.filter(pk__in=reservation_ids))
    with transaction.atomic():
        for res in rows:
            _record(res, kind, "fine", user)
            _audit(res, "checked: pay as normal" + (" (not a duplicate)" if kind == "duplicate" else ""), user)
    return rows
