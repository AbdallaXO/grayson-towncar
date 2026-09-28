"""
Duplicate-booking detection for the Duplicate Reservations page.

A "duplicate group" is every live (non-cancelled) reservation that shares the
same last name, the same phone number (last ten digits) and the same first
pickup date, where at least one is paid (or has a card on file) and at least
one is not. That key alone is NOT proof of a duplicate — over six months of
history, 36 groups on this key had two or more PAID live bookings (a second
vehicle, a separately booked return, spouses sharing a phone). What makes the
unpaid one deletable is the shape of the pair, so every unpaid row in a group
gets a verdict:

  SAFE    identical trip to the paid one in every way that matters — same
          pickup dates, same route and vehicle, booked from the website before
          the paid one, older than a day, nobody has touched it since.
  REVIEW  the only difference is the route or vehicle class (the price differs),
          which is almost always a re-book with a tweak — but a person decides.
  HOLD    something says it may be a real second booking: it has a pickup date
          the paid one doesn't cover, it was started after the paid one, staff
          or the agent portal created it, staff already reached out about it,
          a driver is on it, a checkout was started against it, it is under a
          day old, or the first name doesn't match across customer rows.

The tiers were calibrated against the 2026-09-21 snapshot: 158 unpaid rows
split 99 safe / 25 review / 34 hold. See docs/release-notes/
2026-09-22-duplicates-sorted-safe-review-hold.md.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from django.db.models import Prefetch
from django.utils import timezone

SAFE = "safe"
REVIEW = "review"
HOLD = "hold"

TIER_ORDER = {SAFE: 0, REVIEW: 1, HOLD: 2}

# How far back the page looks for groups. Older trips have been driven; a
# leftover unpaid twin there is a reporting wart, not an operational one.
SCAN_BACK_DAYS = 90

# An unpaid booking younger than this may still be mid-checkout. The booking
# form debounces double-clicks for 30s and cleans abandoned twins for 10 min;
# beyond that a guest can still be reading the payment email.
MIN_AGE_HOURS = 24

PAID_STATES = ("paid", "card_saved")


def phone_last10(phone) -> str:
    return "".join(ch for ch in (phone or "") if ch.isdigit())[-10:]


def name_key(customer) -> str:
    """Last name, falling back to first name when last is blank."""
    return (customer.last_name or customer.first_name or "").strip().lower()


def group_key(customer, first_leg):
    """(last_name_lower, phone_last10, pickup_date) or None when unkeyable."""
    if customer is None or first_leg is None:
        return None
    phone = phone_last10(customer.phone_number)
    name = name_key(customer)
    if not phone or not name or not first_leg.pickup_date:
        return None
    return (name, phone, first_leg.pickup_date)


def _norm_first(customer) -> str:
    return re.sub(r"[^a-z0-9]", "", (customer.first_name or "").lower())


def _first_names_match(a, b) -> bool:
    """Same customer row, or first names that agree on their first three letters
    (Jeff/Jeffery, Kate/Katie, Rob/Robert all pass; Kathleen/Jerry does not)."""
    if a.pk == b.pk:
        return True
    na, nb = _norm_first(a), _norm_first(b)
    if not na or not nb:
        return True  # nothing to disagree on
    return na[:3] == nb[:3]


def is_paid(reservation) -> bool:
    return reservation.payment_status in PAID_STATES


@dataclass
class Verdict:
    tier: str
    reasons: list[str] = field(default_factory=list)

    @property
    def is_safe(self):
        return self.tier == SAFE

    @property
    def label(self):
        return {SAFE: "Safe to delete", REVIEW: "Check first", HOLD: "Hold"}[self.tier]


@dataclass
class DuplicateGroup:
    customer: object
    pickup_date: object
    paid: list
    unpaid: list  # each carries .dupe_verdict

    @property
    def safe_count(self):
        return sum(1 for r in self.unpaid if r.dupe_verdict.is_safe)


def classify(unpaid, paid, *, now=None, contacted_ids=frozenset()) -> Verdict:
    """
    Decide whether `unpaid` is safe to delete given the paid reservations in
    its group. `contacted_ids` is the set of reservation ids that have at least
    one staff CommunicationAttempt logged against them.

    Uses prefetched `legs` and `payments` when present so the page renders in
    a fixed number of queries.
    """
    now = now or timezone.now()
    hold: list[str] = []
    review: list[str] = []

    u_legs = list(unpaid.legs.all())
    paid_dates = {leg.pickup_date for p in paid for leg in p.legs.all()}
    extra_dates = sorted({leg.pickup_date for leg in u_legs} - paid_dates)
    if extra_dates:
        days = ", ".join(d.strftime("%b %d") for d in extra_dates)
        hold.append(f"Has a pickup on {days} that the paid booking doesn't cover")

    newest_paid = max(paid, key=lambda p: p.created_at)
    if unpaid.created_at > newest_paid.created_at:
        hold.append(
            f"Started after the paid booking #{newest_paid.id}, may be a second trip"
        )

    if unpaid.created_by_id:
        who = getattr(unpaid.created_by, "username", None) or "staff"
        if unpaid.travel_agent_id:
            hold.append(f"Booked through the agent portal by {who}")
        else:
            hold.append(f"Created by {who}, not the website")

    if any(leg.driver_id for leg in u_legs):
        hold.append("A driver is already assigned to it")

    if unpaid.id in contacted_ids:
        hold.append("Staff already reached out about this booking")

    if list(unpaid.payments.all()):
        hold.append("A checkout or refund was recorded against it")

    if unpaid.created_at > now - timedelta(hours=MIN_AGE_HOURS):
        hold.append("Booked less than 24 hours ago, the guest may still be paying")

    if not any(_first_names_match(unpaid.customer, p.customer) for p in paid):
        other = newest_paid.customer.first_name or "?"
        hold.append(
            f"Different first name ({unpaid.customer.first_name or '?'} vs {other}), "
            "may be a family member sharing the phone"
        )

    if unpaid.travel_agent_id and unpaid.travel_agent_id not in {
        p.travel_agent_id for p in paid
    }:
        hold.append("Booked under a different travel agent than the paid one")

    if unpaid.rate_id not in {p.rate_id for p in paid}:
        review.append(
            f"Different route or vehicle than the paid booking "
            f"(${unpaid.total_price} vs ${newest_paid.total_price})"
        )

    if hold:
        return Verdict(HOLD, hold + review)
    if review:
        return Verdict(REVIEW, review)
    return Verdict(SAFE)


def _base_queryset(cutoff):
    from payment.models import Payment
    from reservations.models import Leg, Reservation

    return (
        Reservation.objects.filter(legs__pickup_date__gte=cutoff)
        .exclude(status="cancelled")
        .select_related("customer", "vehicle", "created_by")
        .prefetch_related(
            Prefetch("payments", queryset=Payment.objects.all()),
            Prefetch(
                "legs",
                queryset=Leg.objects.select_related(
                    "flight_information", "cruise_information"
                ).order_by("pickup_date", "pickup_time"),
            ),
        )
        .distinct()
    )


def _contacted_ids(reservation_ids):
    from ops.models import CommunicationAttempt

    if not reservation_ids:
        return frozenset()
    return frozenset(
        CommunicationAttempt.objects.filter(
            task__reservation_id__in=reservation_ids
        ).values_list("task__reservation_id", flat=True)
    )


def build_groups(*, now=None, scan_back_days=SCAN_BACK_DAYS) -> list[DuplicateGroup]:
    """
    Every duplicate group in the window, upcoming dates first. Each unpaid
    reservation in a group carries `.dupe_verdict`.
    """
    now = now or timezone.now()
    today = timezone.localdate(now)
    cutoff = today - timedelta(days=scan_back_days)

    buckets = defaultdict(dict)  # key -> {pk: reservation}
    for res in _base_queryset(cutoff):
        first_leg = next(iter(res.legs.all()), None)
        key = group_key(res.customer, first_leg)
        if key is None:
            continue
        buckets[key][res.pk] = res

    candidates = []
    unpaid_ids = []
    for (_name, _phone, pickup_date), by_pk in buckets.items():
        members = list(by_pk.values())
        if len(members) < 2:
            continue
        paid = [r for r in members if is_paid(r)]
        unpaid = [r for r in members if not is_paid(r)]
        if not paid or not unpaid:
            continue
        candidates.append((pickup_date, paid, unpaid))
        unpaid_ids.extend(r.id for r in unpaid)

    contacted = _contacted_ids(unpaid_ids)

    groups = []
    for pickup_date, paid, unpaid in candidates:
        for res in unpaid:
            res.dupe_verdict = classify(res, paid, now=now, contacted_ids=contacted)
        unpaid.sort(key=lambda r: (TIER_ORDER[r.dupe_verdict.tier], r.created_at))
        groups.append(
            DuplicateGroup(
                customer=paid[0].customer,
                pickup_date=pickup_date,
                paid=paid,
                unpaid=unpaid,
            )
        )

    groups.sort(key=lambda g: (0 if g.pickup_date >= today else 1, g.pickup_date))
    return groups


def verdict_for(reservation, *, now=None):
    """
    Re-derive one unpaid reservation's verdict from live data, for the delete
    endpoint. Returns None when the reservation is not in a duplicate group
    any more (paid twin gone, or it is paid itself).
    """
    from reservations.models import Reservation

    if is_paid(reservation):
        return None
    first_leg = reservation.legs.order_by("pickup_date", "pickup_time").first()
    key = group_key(reservation.customer, first_leg)
    if key is None:
        return None
    name, phone, pickup_date = key

    siblings = (
        _base_queryset(pickup_date)
        .filter(legs__pickup_date=pickup_date)
        .exclude(pk=reservation.pk)
    )
    paid = []
    for sib in siblings:
        sib_first = next(iter(sib.legs.all()), None)
        if group_key(sib.customer, sib_first) == key and is_paid(sib):
            paid.append(sib)
    if not paid:
        return None

    fresh = _base_queryset(pickup_date).get(pk=reservation.pk)
    return classify(
        fresh, paid, now=now, contacted_ids=_contacted_ids([reservation.id])
    )
