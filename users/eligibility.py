"""
Centralized commission-payment eligibility.

Single source of truth for: which travel-agent commissions are safe to pay,
which need human review, which are excluded, which are still pending, and
which are already paid. Used by:

  - users.models.TravelAgent.calculate_unpaid_commissions / calculate_pending_commissions
  - users.models.TravelAgent.process_commission_payment
  - users.services.preview_agent_payout / preview_agency_payout
  - dispatching.views.affiliate_payments and per-agent/agency detail views

Business rules (confirmed with the operator on 2026-05-23):
  - Anchor "trip happened" date = latest non-cancelled leg's pickup_date + pickup_time
  - Grace period = 24h after the anchor
  - Reservation.status == "completed" is a fast path: skip the grace wait
  - Reservation.status == "cancelled" is always Excluded
  - total_refunded > 0 and paid_amount <= 0 -> Excluded "Fully refunded"
  - total_refunded > 0 and paid_amount > 0  -> Needs Review "Partial refund"
  - commission_paid=True -> Paid (no clawback on later refunds; flagged separately)
  - Commissionable amount = base_price * commission_rate / 100 (no gratuity/extras)

NOTE on is_paid: this field is set by payment.signals when a Stripe Payment hits
status=paid. Travel-agent bookings at Grayson are typically settled outside Stripe
(invoiced to agency, cash, check), so is_paid stays False on them by design --
checking it here would silently drop the entire agent queue into Excluded. We
therefore DO NOT require is_paid=True; the Stripe-refund checks above still catch
the only "customer paid then got money back" case that matters in practice.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List, Optional

from django.utils import timezone


# Status codes (string-based so they serialize cleanly into templates/JSON).
STATUS_PENDING = "pending"
STATUS_READY = "ready"
STATUS_REVIEW = "review"
STATUS_EXCLUDED = "excluded"
STATUS_PAID = "paid"

ALL_STATUSES = (STATUS_PENDING, STATUS_READY, STATUS_REVIEW, STATUS_EXCLUDED, STATUS_PAID)

# Reason codes -- machine-readable so views/templates can group/filter without
# string-matching humanized labels.
REASON_NO_AGENT = "no_agent"
REASON_NO_COMMISSIONABLE_AMOUNT = "no_commissionable_amount"
REASON_ALREADY_PAID = "already_paid"
REASON_CANCELLED = "cancelled"
REASON_FULLY_REFUNDED = "fully_refunded"
REASON_PARTIAL_REFUND = "partial_refund"
REASON_COMPLETED_FAST_PATH = "completed_fast_path"
REASON_FUTURE_TRIP = "future_trip"
REASON_WITHIN_GRACE = "within_grace"
REASON_AUTO_READY_STALE = "auto_ready_stale_completion"
REASON_NO_LEG_DATE = "no_leg_date"

# Default grace period: 24h after the final non-cancelled leg's pickup datetime.
DEFAULT_GRACE_HOURS = 24


@dataclass
class EligibilityResult:
    """The verdict for one reservation's commission."""

    status: str                       # one of ALL_STATUSES
    reason_code: str                  # machine code -- see REASON_*
    reason: str                       # human-readable label shown in the UI
    commission: Decimal = Decimal("0")  # current eligible commission amount (0 if not ready)
    last_leg_at: Optional[datetime] = None  # anchor datetime used (for display/debug)
    blockers: List[str] = field(default_factory=list)  # extra issues worth surfacing

    @property
    def safe_to_pay(self) -> bool:
        """True only when this reservation can be safely included in a payout."""
        return self.status == STATUS_READY

    @property
    def needs_review(self) -> bool:
        return self.status == STATUS_REVIEW


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _last_leg_datetime(reservation):
    """Latest non-cancelled leg's pickup_date+pickup_time as a tz-aware datetime.

    Returns None if the reservation has no usable leg date (no legs, or all legs
    cancelled). For multi-leg trips this picks the last leg actually scheduled
    to run -- e.g. for a round trip with arrival May 20 and return May 23, the
    anchor is May 23 (so the trip is not considered "complete" until then).
    """
    # Use prefetched .legs.all() if caller already loaded them, but fall back to
    # a direct ordered query so this stays correct even when used in isolation.
    legs = list(reservation.legs.all())
    if not legs:
        return None

    candidates = []
    for leg in legs:
        if leg.status == "cancelled":
            continue
        if not leg.pickup_date or not leg.pickup_time:
            continue
        naive = datetime.combine(leg.pickup_date, leg.pickup_time)
        candidates.append(timezone.make_aware(naive, timezone.get_current_timezone()))

    if not candidates:
        return None
    return max(candidates)


def _commission_for(reservation) -> Decimal:
    """Commission amount based on current rate -- recomputed, never trusted from
    stored commission_amount (rate may have changed)."""
    agent = reservation.travel_agent
    if agent is None or reservation.base_price is None:
        return Decimal("0")
    rate = (agent.commission_rate or Decimal("0")) / Decimal("100")
    return (reservation.base_price * rate).quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_commission_eligibility(reservation, *, now=None, grace_hours=DEFAULT_GRACE_HOURS) -> EligibilityResult:
    """Decide where this reservation's commission belongs in the payment queue.

    Decision order matters -- the first matching rule wins. The order is built
    so that "no commission to pay" exits come first, then exclusions for known
    bad states, then the happy path, then the time-based bucketing.

      1. No travel agent / no commissionable amount  -> Excluded
      2. Commission already paid                     -> Paid
      3. Reservation cancelled                       -> Excluded
      4. Fully refunded                              -> Excluded
      5. Partial refund                              -> Needs Review
      6. status == "completed" (fast path)           -> Ready
      7. No usable leg date                          -> Needs Review
      8. Last leg date + grace still in the future   -> Pending
      9. Past the grace window                       -> Ready (auto, fixes the
                                                        "forgot to click completed" bug)
    """
    now = now or timezone.now()

    if reservation.travel_agent_id is None:
        return EligibilityResult(STATUS_EXCLUDED, REASON_NO_AGENT, "No travel agent on reservation")

    if reservation.base_price is None or reservation.base_price <= 0:
        return EligibilityResult(
            STATUS_EXCLUDED, REASON_NO_COMMISSIONABLE_AMOUNT,
            "No commissionable base price",
        )

    commission = _commission_for(reservation)

    if reservation.commission_paid:
        return EligibilityResult(
            STATUS_PAID, REASON_ALREADY_PAID,
            "Commission already paid",
            commission=Decimal("0"),  # already counted in total_paid_commission, not owed
        )

    if reservation.status == "cancelled":
        return EligibilityResult(STATUS_EXCLUDED, REASON_CANCELLED, "Reservation cancelled")

    total_refunded = reservation.total_refunded or Decimal("0")
    paid_amount = reservation.paid_amount or Decimal("0")
    if total_refunded > 0 and paid_amount <= 0:
        return EligibilityResult(STATUS_EXCLUDED, REASON_FULLY_REFUNDED, "Fully refunded")
    if total_refunded > 0:
        # Partial -- operator decides per-case whether to pay (and how much).
        return EligibilityResult(
            STATUS_REVIEW, REASON_PARTIAL_REFUND,
            "Partial refund - review commission",
            commission=commission,
        )

    # Fast path: dispatcher clicked every leg completed and the reservation
    # status auto-rolled to "completed". No grace needed.
    if reservation.status == "completed":
        return EligibilityResult(
            STATUS_READY, REASON_COMPLETED_FAST_PATH,
            "Trip completed and customer paid",
            commission=commission,
        )

    # status is "confirmed" at this point (or some other live state). The
    # leg-date-based fallback is what fixes the "stuck pending forever" bug.
    last_leg_at = _last_leg_datetime(reservation)
    if last_leg_at is None:
        return EligibilityResult(
            STATUS_REVIEW, REASON_NO_LEG_DATE,
            "Missing leg date - cannot verify trip occurred",
            commission=commission,
        )

    grace_threshold = last_leg_at + timedelta(hours=grace_hours)
    if now < last_leg_at:
        return EligibilityResult(
            STATUS_PENDING, REASON_FUTURE_TRIP,
            "Future trip",
            commission=commission,
            last_leg_at=last_leg_at,
        )
    if now < grace_threshold:
        return EligibilityResult(
            STATUS_PENDING, REASON_WITHIN_GRACE,
            f"Within {grace_hours}h grace window after final leg",
            commission=commission,
            last_leg_at=last_leg_at,
        )

    return EligibilityResult(
        STATUS_READY, REASON_AUTO_READY_STALE,
        "Final leg date passed - auto-eligible",
        commission=commission,
        last_leg_at=last_leg_at,
    )


def _agent_reservations(agent):
    """Reservations relevant to this agent's commission queue display.

    Includes cancelled / refunded / unpaid-by-customer ones so they can show
    up in the Excluded bucket with a reason ("Cancelled", "Fully refunded",
    "Unpaid by customer"). The Paid bucket (commission_paid=True) is filtered
    out for performance -- those are already counted in the agent's
    total_paid_commission and visible in the payout history.
    """
    from reservations.models import Reservation

    return (
        Reservation.objects.filter(travel_agent=agent)
        .exclude(commission_paid=True)
        .select_related("travel_agent")
        .prefetch_related("legs")
    )


def bucket_agent_reservations(agent, *, now=None, grace_hours=DEFAULT_GRACE_HOURS):
    """Run eligibility for every open reservation belonging to one agent.

    Returns a dict keyed by status code (pending/ready/review/excluded/paid),
    each value a list of (reservation, EligibilityResult) tuples. Excluded
    includes "fully refunded" and "unpaid by customer" -- the visible bad-state
    list. Paid is empty by design (we filter commission_paid=True at the SQL
    layer for performance).
    """
    buckets = {s: [] for s in ALL_STATUSES}
    for res in _agent_reservations(agent):
        result = get_commission_eligibility(res, now=now, grace_hours=grace_hours)
        buckets[result.status].append((res, result))
    return buckets


def sum_ready(agent, *, now=None, grace_hours=DEFAULT_GRACE_HOURS) -> Decimal:
    """Sum of commission amounts in the Ready bucket -- what the agent is actually owed today."""
    total = Decimal("0")
    for res in _agent_reservations(agent):
        result = get_commission_eligibility(res, now=now, grace_hours=grace_hours)
        if result.status == STATUS_READY:
            total += result.commission
    return total.quantize(Decimal("0.01"))


def bulk_ready_totals(agent_ids, *, now=None, grace_hours=DEFAULT_GRACE_HOURS):
    """Ready-bucket totals for many agents in one query pair.

    Semantically identical to calling sum_ready(agent) for each id, but issues
    one Reservation query + one legs prefetch for the whole set instead of 2*N.
    Used by the affiliate-payments page to avoid an N+1 across the visible page.

    Returns {agent_id: Decimal} for every id passed in (zero when no ready
    reservations exist), so callers can index without KeyError.
    """
    from collections import defaultdict
    from reservations.models import Reservation

    agent_ids = list(agent_ids)
    if not agent_ids:
        return {}
    totals = defaultdict(lambda: Decimal("0"))
    qs = (
        Reservation.objects.filter(travel_agent_id__in=agent_ids)
        .exclude(commission_paid=True)
        .select_related("travel_agent")
        .prefetch_related("legs")
    )
    for res in qs:
        result = get_commission_eligibility(res, now=now, grace_hours=grace_hours)
        if result.status == STATUS_READY:
            totals[res.travel_agent_id] += result.commission
    return {aid: totals.get(aid, Decimal("0")).quantize(Decimal("0.01")) for aid in agent_ids}


def sum_pending(agent, *, now=None, grace_hours=DEFAULT_GRACE_HOURS) -> Decimal:
    """Sum of commission amounts in the Pending bucket -- future trips and grace-window holds."""
    total = Decimal("0")
    for res in _agent_reservations(agent):
        result = get_commission_eligibility(res, now=now, grace_hours=grace_hours)
        if result.status == STATUS_PENDING:
            total += result.commission
    return total.quantize(Decimal("0.01"))


def ready_reservations(agent, *, now=None, grace_hours=DEFAULT_GRACE_HOURS):
    """Iterate (reservation, EligibilityResult) for every Ready reservation.

    This is what `process_commission_payment` walks when actually paying --
    using the SAME helper as the queue display guarantees you can never pay
    something the queue called Excluded/Review.
    """
    for res in _agent_reservations(agent):
        result = get_commission_eligibility(res, now=now, grace_hours=grace_hours)
        if result.status == STATUS_READY:
            yield res, result
