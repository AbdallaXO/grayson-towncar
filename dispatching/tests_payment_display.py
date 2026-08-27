"""Card on file is not the same thing as unpaid.

A trip with a card on file is one click from collected; a trip with no card
means someone has to ring the guest. The board used to draw them identically —
same amber ring, same dollar sign — and the reservations list's "Saved Cards"
filter returned every trip ever *booked* with a card, including the ones long
since paid.

Run with:
  ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching.tests_payment_display
"""
from datetime import time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from payment.models import Payment
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation
from dispatching.payment_display import board_pay_state

FUTURE = timezone.localdate() + timedelta(days=7)

# Creating a Reservation spawns real background email threads, which race the
# test's own SQLite writes. Same neutralisation dispatching/tests_keoi.py uses.
_NOOP = lambda *a, **k: None
_bg_targets = [
    "reservations.utils._run_in_background",
    "drivers.signals._run_in_background",
    "dispatching.views._run_in_background",
]
_bg_patchers = []


def setUpModule():
    for target in _bg_targets:
        p = mock.patch(target, _NOOP)
        p.start()
        _bg_patchers.append(p)


def tearDownModule():
    for p in _bg_patchers:
        p.stop()
    _bg_patchers.clear()


class _PayFixtureMixin:
    @classmethod
    def setUpTestData(cls):
        cls.vehicle = Vehicle.objects.create(
            vehicle_type="sedan", capacity=4, luggage_capacity=4
        )
        origin = Location.objects.create(name="MCO")
        dest = Location.objects.create(name="Disney")
        cls.route = Route.objects.create(
            origin=origin, destination=dest, inhouse_base_pay=Decimal("50.00")
        )
        cls.rate = Rate.objects.create(
            vehicle=cls.vehicle, route=cls.route,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"),
        )
        cls.customer = Customer.objects.create(
            first_name="Ada", last_name="Rowe", email="ada@example.com",
            phone_number="5551230000",
        )
        cls.admin = User.objects.create_superuser("pay_admin", password="x")

    def _res(self, price="100.00"):
        return Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=Decimal(price),
            total_price=Decimal(price), status="confirmed",
        )

    def _pay(self, res, status, amount="100.00"):
        return Payment.objects.create(
            reservation=res, customer=self.customer,
            amount=Decimal(amount), status=status,
        )

    def _leg(self, res, **kw):
        defaults = dict(
            reservation=res, pickup_date=FUTURE, pickup_time=time(9, 0),
            pickup_location="MCO", dropoff_location="Disney",
            route=self.route, status="confirmed",
        )
        defaults.update(kw)
        return Leg.objects.create(**defaults)


class BoardPayStateTests(_PayFixtureMixin, TestCase):
    """The helper every board surface delegates to."""

    def test_three_way_split(self):
        paid = self._res()
        self._pay(paid, "paid")
        card = self._res()
        self._pay(card, "card_saved")
        nothing = self._res()

        self.assertEqual(board_pay_state(paid), "paid")
        self.assertEqual(board_pay_state(card), "card_saved")
        self.assertEqual(board_pay_state(nothing), "unpaid")

    def test_orphan_leg_reads_as_paid(self):
        """Matches the long-standing `if leg.reservation else True` default —
        a leg with no reservation must not sprout a payment warning."""
        self.assertEqual(board_pay_state(None), "paid")

    def test_pending_and_failed_collapse_to_unpaid(self):
        """Keeps today's rendering for the tail states — they still owe money."""
        pending = self._res()
        self._pay(pending, "pending")
        self.assertEqual(board_pay_state(pending), "unpaid")

    def test_a_later_payment_beats_the_booking_time_card_row(self):
        """The precedence bug this must never resurrect: a card_saved row written
        at booking time must not mask the real payment that followed it."""
        res = self._res()
        self._pay(res, "card_saved")
        self._pay(res, "paid")
        self.assertEqual(board_pay_state(res), "paid")


class CardOnFileFilterTests(_PayFixtureMixin, TestCase):
    """The reservations list: "Card on File" means still owing, not ever-carded."""

    def setUp(self):
        self.client.force_login(self.admin)

    def _get(self, **params):
        return self.client.get(reverse("reservations_list"), {"time": "all", **params})

    def test_paid_reservation_with_a_saved_card_is_excluded(self):
        """The founder's complaint: the filter was returning already-collected
        trips because charging a saved card INSERTs a second payment row rather
        than mutating the booking-time card_saved one."""
        collected = self._res()
        self._pay(collected, "card_saved")
        self._pay(collected, "paid")

        rows = self._get(status="card_saved").context["object_list"]
        self.assertNotIn(collected, rows)

    def test_still_owing_reservation_with_a_saved_card_is_included(self):
        owing = self._res()
        self._pay(owing, "card_saved")

        rows = self._get(status="card_saved").context["object_list"]
        self.assertIn(owing, rows)

    def test_headline_count_equals_the_rows_shown(self):
        """The count and the list must never disagree — that mismatch is how the
        wrong predicate hid for so long."""
        owing = self._res()
        self._pay(owing, "card_saved")
        collected = self._res()
        self._pay(collected, "card_saved")
        self._pay(collected, "paid")

        resp = self._get(status="card_saved")
        self.assertEqual(resp.context["card_saved_count"], len(resp.context["object_list"]))
        self.assertEqual(resp.context["card_saved_count"], 1)

    def test_card_total_counts_each_reservation_once(self):
        """Two card_saved rows on one reservation must not double its price."""
        res = self._res(price="250.00")
        self._pay(res, "card_saved")
        self._pay(res, "card_saved")

        resp = self._get(status="card_saved")
        self.assertEqual(Decimal(resp.context["card_saved_total"]), Decimal("250.00"))

    def test_revenue_counts_each_reservation_once(self):
        """A reservation paid in two instalments was adding its FULL price twice
        to the revenue headline — the join fanned out and Sum() has no distinct."""
        res = self._res(price="300.00")
        self._pay(res, "paid", amount="150.00")
        self._pay(res, "paid", amount="150.00")

        resp = self._get()
        self.assertEqual(Decimal(resp.context["total_revenue"]), Decimal("300.00"))

    def test_total_count_counts_each_reservation_once(self):
        res = self._res()
        self._pay(res, "paid")
        self._pay(res, "paid")

        resp = self._get()
        self.assertEqual(resp.context["total_reservations"], 1)


class BoardMarkerTests(_PayFixtureMixin, TestCase):
    """What the dispatcher actually sees. Asserted on rendered HTML, because a
    producer fixed without its template is exactly the half-landed fix."""

    # The bare class names also appear in the page's <style> block and in the
    # legend, so assert on the chip's own markup: the title text is unique to a
    # rendered slot.
    CARD_CHIP = 'title="Card on file — not charged yet"'
    UNPAID_CHIP = 'title="Unpaid — no card on file"'

    def setUp(self):
        self.client.force_login(self.admin)

    def _board_html(self):
        resp = self.client.get(reverse("schedule_board"), {"date": FUTURE.isoformat()})
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_card_on_file_and_unpaid_render_differently(self):
        card = self._res()
        self._pay(card, "card_saved")
        self._leg(card, pickup_time=time(9, 0))

        broke = self._res()
        self._leg(broke, pickup_time=time(11, 0))

        html = self._board_html()
        self.assertIn(self.CARD_CHIP, html)
        self.assertIn(self.UNPAID_CHIP, html)

    def test_a_paid_trip_carries_neither_marker(self):
        paid = self._res()
        self._pay(paid, "paid")
        self._leg(paid, pickup_time=time(9, 0))

        html = self._board_html()
        self.assertNotIn(self.CARD_CHIP, html)
        self.assertNotIn(self.UNPAID_CHIP, html)

    def test_card_on_file_does_not_get_the_unpaid_alarm_ring(self):
        """The ring is the loud half of the signal — the 8px chip is not what
        reads at board scale. A card-on-file trip keeps the calm treatment, so
        the amber ring stays meaningful for a trip nobody can collect on."""
        card = self._res()
        self._pay(card, "card_saved")
        self._leg(card, pickup_time=time(9, 0))

        html = self._board_html()
        self.assertIn(self.CARD_CHIP, html)
        self.assertNotIn(self.UNPAID_CHIP, html)
        # No rendered slot may carry the ring class on a board holding only a
        # card-on-file trip.
        self.assertNotIn("timeline-slot tl-chip arrival unpaid", html)
        self.assertNotIn("timeline-slot arrival unpaid", html)

    def test_a_collected_trip_that_was_booked_with_a_card_shows_no_marker(self):
        """The board mirror of the filter fix: paid beats a leftover card_saved
        row, so the trip must read as done, not as money outstanding."""
        res = self._res()
        self._pay(res, "card_saved")
        self._pay(res, "paid")
        self._leg(res, pickup_time=time(9, 0))

        html = self._board_html()
        self.assertNotIn(self.CARD_CHIP, html)
        self.assertNotIn(self.UNPAID_CHIP, html)
