"""Tests for users.eligibility -- the single source of truth for travel agent
commission payability.

Covers the 10 business scenarios discussed before implementation:
  1. Future reservation                                         -> Pending
  2. Past + all legs completed + paid + no refund               -> Ready
  3. Past + one leg not marked completed + paid + no refund     -> Ready (auto)
  4. Past + fully refunded                                      -> Excluded
  5. Past + cancelled                                           -> Excluded
  6. Past + customer never paid (is_paid=False)                 -> Excluded
  7. Past + partial refund                                      -> Needs Review
  8. Reservation already paid out in a CommissionPayout         -> Paid
  9. Reservation in an open/draft statement                     -> N/A in this system
                                                                   (no draft state -- payouts are atomic;
                                                                   see commission_paid guard test)
 10. Agent missing payment info                                 -> handled by separate
                                                                   "missing" tab; eligibility still Ready

Run with: python manage.py test users.tests_eligibility
"""
from datetime import date, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation
from users.eligibility import (
    STATUS_EXCLUDED,
    STATUS_PAID,
    STATUS_PENDING,
    STATUS_READY,
    STATUS_REVIEW,
    REASON_ALREADY_PAID,
    REASON_AUTO_READY_STALE,
    REASON_CANCELLED,
    REASON_COMPLETED_FAST_PATH,
    REASON_FULLY_REFUNDED,
    REASON_FUTURE_TRIP,
    REASON_PARTIAL_REFUND,
    REASON_WITHIN_GRACE,
    bucket_agent_reservations,
    get_commission_eligibility,
    sum_ready,
)
from users.models import TravelAgent


def _bootstrap():
    """Minimal fixture so each test class can build reservations cheaply."""
    vehicle = Vehicle.objects.create(vehicle_type="sedan", capacity=4, luggage_capacity=4)
    origin = Location.objects.create(name="MCO")
    dest = Location.objects.create(name="Disney")
    route = Route.objects.create(origin=origin, destination=dest)
    rate = Rate.objects.create(
        vehicle=vehicle, route=route,
        oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"),
    )
    customer = Customer.objects.create(
        first_name="Test", last_name="Guest", email="g@example.com", phone_number="555",
    )
    user = User.objects.create_user(username="agent1", email="agent1@example.com")
    agent = TravelAgent.objects.create(
        user=user, agent_name="Agent One", phone="555-0100",
        commission_rate=Decimal("10.00"), payment_method="venmo", payment_info="@agent",
    )
    # The model defines float defaults (0.00) on several Decimal fields, which
    # leaves the in-memory instance with float values until first DB refresh.
    # A pre-existing post_save signal then does Decimal + float -> TypeError.
    # Refreshing here makes the test path mirror long-lived production agents.
    agent.refresh_from_db()
    return vehicle, rate, customer, agent


def _make_reservation(
    rate, customer, agent, *,
    status="confirmed", base_price=Decimal("100"),
    is_paid=True, paid_amount=Decimal("100"), total_refunded=Decimal("0"),
    commission_paid=False,
):
    """Build one reservation with sane defaults for the eligibility scenarios."""
    res = Reservation.objects.create(
        trip_type="one-way", customer=customer, rate=rate,
        base_price=base_price, total_price=base_price,
        status=status, travel_agent=agent,
        is_paid=is_paid, paid_amount=paid_amount, total_refunded=total_refunded,
        commission_paid=commission_paid,
    )
    return res


def _add_leg(res, *, pickup_date, pickup_time=time(10, 0), status="confirmed"):
    """Attach one leg to a reservation. Leg.save() requires a route; we mirror
    the reservation's rate.route so the auto-route-assign hook is satisfied."""
    return Leg.objects.create(
        reservation=res, route=res.rate.route, vehicle=res.rate.vehicle,
        pickup_date=pickup_date, pickup_time=pickup_time, status=status,
    )


class EligibilityScenariosTest(TestCase):
    """One test per scenario in the spec."""

    @classmethod
    def setUpTestData(cls):
        cls.vehicle, cls.rate, cls.customer, cls.agent = _bootstrap()
        cls.today = timezone.localtime(timezone.now()).date()
        cls.future = cls.today + timedelta(days=10)
        cls.recent_past = cls.today - timedelta(days=5)

    # ---- Scenario 1: Future reservation ----
    def test_future_trip_is_pending(self):
        res = _make_reservation(self.rate, self.customer, self.agent, status="confirmed")
        _add_leg(res, pickup_date=self.future)
        result = get_commission_eligibility(res)
        self.assertEqual(result.status, STATUS_PENDING)
        self.assertEqual(result.reason_code, REASON_FUTURE_TRIP)

    def test_in_grace_window_is_pending(self):
        # Trip happened 12 hours ago (still inside the 24h grace window).
        res = _make_reservation(self.rate, self.customer, self.agent, status="confirmed")
        twelve_hr_ago = timezone.now() - timedelta(hours=12)
        _add_leg(res, pickup_date=twelve_hr_ago.date(), pickup_time=twelve_hr_ago.time())
        result = get_commission_eligibility(res)
        self.assertEqual(result.status, STATUS_PENDING)
        self.assertEqual(result.reason_code, REASON_WITHIN_GRACE)

    # ---- Scenario 2: Past + all legs completed + paid + no refund ----
    def test_past_completed_paid_is_ready(self):
        res = _make_reservation(self.rate, self.customer, self.agent, status="completed")
        _add_leg(res, pickup_date=self.recent_past, status="completed")
        result = get_commission_eligibility(res)
        self.assertEqual(result.status, STATUS_READY)
        self.assertEqual(result.reason_code, REASON_COMPLETED_FAST_PATH)
        self.assertEqual(result.commission, Decimal("10.00"))

    # ---- Scenario 3: Past + one leg not marked completed (the bug fix) ----
    def test_past_uncompleted_legs_auto_ready(self):
        """The crucial bug-fix test: status='confirmed' (because a leg was never
        clicked completed) but the trip clearly happened weeks ago and was paid
        with no refund -- must NOT stay stuck pending forever."""
        res = _make_reservation(self.rate, self.customer, self.agent, status="confirmed")
        _add_leg(res, pickup_date=self.recent_past, status="confirmed")  # not completed
        result = get_commission_eligibility(res)
        self.assertEqual(result.status, STATUS_READY)
        self.assertEqual(result.reason_code, REASON_AUTO_READY_STALE)
        self.assertEqual(result.commission, Decimal("10.00"))

    def test_multi_leg_uses_latest_leg_for_anchor(self):
        """Round trip: arrival May 20, return May 23. On May 24 the trip is
        complete; on May 22 the return hasn't happened yet so trip is pending."""
        res = _make_reservation(self.rate, self.customer, self.agent, status="confirmed")
        _add_leg(res, pickup_date=self.today - timedelta(days=3), status="completed")
        # Return leg still 5 days in the future
        _add_leg(res, pickup_date=self.today + timedelta(days=5), status="confirmed")
        result = get_commission_eligibility(res)
        self.assertEqual(result.status, STATUS_PENDING)
        self.assertEqual(result.reason_code, REASON_FUTURE_TRIP)

    # ---- Scenario 4: Past + fully refunded ----
    def test_fully_refunded_is_excluded(self):
        res = _make_reservation(
            self.rate, self.customer, self.agent, status="completed",
            paid_amount=Decimal("0"), total_refunded=Decimal("100"),
        )
        _add_leg(res, pickup_date=self.recent_past, status="completed")
        result = get_commission_eligibility(res)
        self.assertEqual(result.status, STATUS_EXCLUDED)
        self.assertEqual(result.reason_code, REASON_FULLY_REFUNDED)

    # ---- Scenario 5: Past + cancelled ----
    def test_cancelled_is_excluded(self):
        res = _make_reservation(self.rate, self.customer, self.agent, status="cancelled")
        _add_leg(res, pickup_date=self.recent_past, status="cancelled")
        result = get_commission_eligibility(res)
        self.assertEqual(result.status, STATUS_EXCLUDED)
        self.assertEqual(result.reason_code, REASON_CANCELLED)

    # ---- Scenario 6: Past + no Stripe payment (off-platform settle) ----
    # Travel-agent trips are typically invoiced/cash-settled, so is_paid=False
    # is the NORMAL state. We do NOT exclude on is_paid alone -- dispatcher
    # completion + no refund + not cancelled is enough.
    def test_off_platform_settle_still_ready(self):
        res = _make_reservation(
            self.rate, self.customer, self.agent, status="completed",
            is_paid=False, paid_amount=Decimal("0"),
        )
        _add_leg(res, pickup_date=self.recent_past, status="completed")
        result = get_commission_eligibility(res)
        self.assertEqual(result.status, STATUS_READY)

    # ---- Scenario 7: Past + partial refund ----
    def test_partial_refund_needs_review(self):
        res = _make_reservation(
            self.rate, self.customer, self.agent, status="completed",
            paid_amount=Decimal("60"), total_refunded=Decimal("40"),
        )
        _add_leg(res, pickup_date=self.recent_past, status="completed")
        result = get_commission_eligibility(res)
        self.assertEqual(result.status, STATUS_REVIEW)
        self.assertEqual(result.reason_code, REASON_PARTIAL_REFUND)

    # ---- Scenario 8: Reservation already paid out ----
    def test_already_paid_is_paid(self):
        res = _make_reservation(
            self.rate, self.customer, self.agent, status="completed",
            commission_paid=True,
        )
        _add_leg(res, pickup_date=self.recent_past, status="completed")
        result = get_commission_eligibility(res)
        self.assertEqual(result.status, STATUS_PAID)
        self.assertEqual(result.reason_code, REASON_ALREADY_PAID)
        self.assertEqual(result.commission, Decimal("0"))  # already counted in total_paid

    # ---- Scenario 10: Missing payment info still Ready ----
    # (Per the operator: keep the existing "missing" tab behavior --
    # eligibility itself doesn't gate on agent payment_method/info.)
    def test_missing_agent_payment_info_does_not_block_eligibility(self):
        self.agent.payment_method = ""
        self.agent.payment_info = ""
        self.agent.save()
        res = _make_reservation(self.rate, self.customer, self.agent, status="completed")
        _add_leg(res, pickup_date=self.recent_past, status="completed")
        result = get_commission_eligibility(res)
        self.assertEqual(result.status, STATUS_READY)


class SumReadyTest(TestCase):
    """Verifies sum_ready/bucket helpers compose correctly."""

    @classmethod
    def setUpTestData(cls):
        cls.vehicle, cls.rate, cls.customer, cls.agent = _bootstrap()
        cls.today = timezone.localtime(timezone.now()).date()

    def test_sum_ready_only_includes_ready_bucket(self):
        # Ready: completed + paid -> commission $10
        r1 = _make_reservation(self.rate, self.customer, self.agent, status="completed")
        _add_leg(r1, pickup_date=self.today - timedelta(days=3))
        # Review: partial refund -> commission $10 but in review bucket, NOT summed
        r2 = _make_reservation(
            self.rate, self.customer, self.agent, status="completed",
            paid_amount=Decimal("60"), total_refunded=Decimal("40"),
        )
        _add_leg(r2, pickup_date=self.today - timedelta(days=3))
        # Excluded: cancelled -> not summed
        r3 = _make_reservation(self.rate, self.customer, self.agent, status="cancelled")
        _add_leg(r3, pickup_date=self.today - timedelta(days=3))
        # Pending: future trip -> not summed
        r4 = _make_reservation(self.rate, self.customer, self.agent, status="confirmed")
        _add_leg(r4, pickup_date=self.today + timedelta(days=10))
        # Already paid: not summed
        r5 = _make_reservation(
            self.rate, self.customer, self.agent, status="completed",
            commission_paid=True,
        )
        _add_leg(r5, pickup_date=self.today - timedelta(days=3))

        self.assertEqual(sum_ready(self.agent), Decimal("10.00"))

    def test_bucket_agent_reservations_partitions_correctly(self):
        # One reservation per bucket.
        ready = _make_reservation(self.rate, self.customer, self.agent, status="completed")
        _add_leg(ready, pickup_date=self.today - timedelta(days=3))
        review = _make_reservation(
            self.rate, self.customer, self.agent, status="completed",
            paid_amount=Decimal("60"), total_refunded=Decimal("40"),
        )
        _add_leg(review, pickup_date=self.today - timedelta(days=3))
        pending = _make_reservation(self.rate, self.customer, self.agent, status="confirmed")
        _add_leg(pending, pickup_date=self.today + timedelta(days=10))
        excluded = _make_reservation(self.rate, self.customer, self.agent, status="cancelled")
        _add_leg(excluded, pickup_date=self.today - timedelta(days=3))

        buckets = bucket_agent_reservations(self.agent)
        self.assertEqual(len(buckets[STATUS_READY]), 1)
        self.assertEqual(len(buckets[STATUS_REVIEW]), 1)
        self.assertEqual(len(buckets[STATUS_PENDING]), 1)
        self.assertEqual(len(buckets[STATUS_EXCLUDED]), 1)
        self.assertEqual(buckets[STATUS_READY][0][0].id, ready.id)
        self.assertEqual(buckets[STATUS_REVIEW][0][0].id, review.id)
        self.assertEqual(buckets[STATUS_PENDING][0][0].id, pending.id)
        self.assertEqual(buckets[STATUS_EXCLUDED][0][0].id, excluded.id)


class ProcessPaymentGuardTest(TestCase):
    """Verifies process_commission_payment respects the eligibility boundaries."""

    @classmethod
    def setUpTestData(cls):
        cls.vehicle, cls.rate, cls.customer, cls.agent = _bootstrap()
        cls.today = timezone.localtime(timezone.now()).date()

    def test_process_payment_skips_review_and_excluded(self):
        """The actual pay action must never include Review/Excluded reservations
        even if they were technically commissionable."""
        ready = _make_reservation(self.rate, self.customer, self.agent, status="completed")
        _add_leg(ready, pickup_date=self.today - timedelta(days=3))

        partial_refund = _make_reservation(
            self.rate, self.customer, self.agent, status="completed",
            paid_amount=Decimal("60"), total_refunded=Decimal("40"),
        )
        _add_leg(partial_refund, pickup_date=self.today - timedelta(days=3))

        cancelled = _make_reservation(
            self.rate, self.customer, self.agent, status="cancelled",
        )
        _add_leg(cancelled, pickup_date=self.today - timedelta(days=3))

        payout, amount, _ = self.agent.process_commission_payment()
        self.assertIsNotNone(payout)
        # Only the Ready reservation got paid -- $10 commission on $100 base.
        self.assertEqual(amount, Decimal("10.00"))
        paid_ids = set(payout.reservations.values_list("id", flat=True))
        self.assertEqual(paid_ids, {ready.id})

        # Review and Excluded reservations remain unpaid.
        partial_refund.refresh_from_db()
        cancelled.refresh_from_db()
        self.assertFalse(partial_refund.commission_paid)
        self.assertFalse(cancelled.commission_paid)

    def test_already_paid_not_double_billed(self):
        """A reservation in a finalized CommissionPayout cannot be paid again --
        the commission_paid flag guards re-entry."""
        ready = _make_reservation(self.rate, self.customer, self.agent, status="completed")
        _add_leg(ready, pickup_date=self.today - timedelta(days=3))

        # First payout
        payout1, amount1, _ = self.agent.process_commission_payment()
        self.assertEqual(amount1, Decimal("10.00"))
        self.assertEqual(payout1.reservations.count(), 1)

        # Second call -- nothing should be Ready.
        payout2, amount2, _ = self.agent.process_commission_payment()
        self.assertIsNone(payout2)
        self.assertEqual(amount2, 0)
