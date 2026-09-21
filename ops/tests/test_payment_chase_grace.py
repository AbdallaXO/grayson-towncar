"""
Tests for the booking grace period on payment_chase task generation.

A reservation booked today and still unpaid is not a chase-worthy problem —
customers routinely pay within hours of checkout. The scan waits
UNPAID_TASK_BOOKING_GRACE from created_at before raising a task; the pickup date
never shortens that wait.

Times are built relative to the real clock (the scanner calls timezone.now()
internally) using offsets large enough that wall-clock drift can't flip a case.
"""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from ops.models import OperationalTask
from ops.tasks import (
    PAYMENT_CHASE_DAYS_AHEAD, UNPAID_TASK_BOOKING_GRACE, _scan_unpaid_reservations,
)
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation


class PaymentChaseGraceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.vehicle = Vehicle.objects.create(
            vehicle_type="towncar", capacity=4, luggage_capacity=4,
        )
        cls.route = Route.objects.create(
            origin=Location.objects.create(name="Test Pickup"),
            destination=Location.objects.create(name="Test Dropoff"),
        )
        cls.rate = Rate.objects.create(
            vehicle=cls.vehicle,
            route=cls.route,
            oneway_price=Decimal("100.00"),
            round_trip_price=Decimal("180.00"),
        )

    def _unpaid_reservation(self, *, booked_ago, pickup_in):
        """Confirmed, zero payments, one leg. `booked_ago`/`pickup_in` are deltas."""
        now = timezone.now()
        customer = Customer.objects.create(
            first_name="Alice",
            last_name="Tester",
            email="alice@example.com",
            phone_number="555-123-4567",
            zipcode="32801",
        )
        res = Reservation.objects.create(
            customer=customer,
            rate=self.rate,
            vehicle=self.vehicle,
            trip_type="one_way",
            base_price=Decimal("100.00"),
            total_price=Decimal("100.00"),
            status="confirmed",
        )
        # created_at is auto_now_add — override after insert.
        Reservation.objects.filter(pk=res.pk).update(created_at=now - booked_ago)

        pickup = timezone.localtime(now + pickup_in)
        Leg.objects.create(
            reservation=res,
            pickup_date=pickup.date(),
            pickup_time=pickup.time(),
            pickup_location="Test Pickup",
            dropoff_location="Test Dropoff",
            status="confirmed",
        )
        res.refresh_from_db()
        return res

    def _chase_tasks(self, res):
        return OperationalTask.objects.filter(
            task_type=OperationalTask.TaskType.PAYMENT_CHASE, reservation=res,
        )

    def test_no_task_while_the_booking_is_fresh(self):
        """Booked 2h ago, trip 5 days out — leave it alone for now."""
        res = self._unpaid_reservation(
            booked_ago=timedelta(hours=2), pickup_in=timedelta(days=5),
        )
        _scan_unpaid_reservations()
        self.assertFalse(self._chase_tasks(res).exists())

    def test_task_created_once_grace_expires(self):
        """Same reservation past the grace window, trip inside the chase window."""
        res = self._unpaid_reservation(
            booked_ago=UNPAID_TASK_BOOKING_GRACE + timedelta(hours=1),
            pickup_in=timedelta(days=2),
        )
        _scan_unpaid_reservations()
        self.assertTrue(self._chase_tasks(res).exists())

    def test_no_task_while_the_trip_is_still_far_out(self):
        """Past the booking grace but the trip is a week away: nothing to chase yet.
        Untouched unpaid bookings pay themselves in a median 10 hours, and the
        reminder engine is already emailing the guest. The task comes back on its
        own PAYMENT_CHASE_DAYS_AHEAD days before the trip."""
        res = self._unpaid_reservation(
            booked_ago=UNPAID_TASK_BOOKING_GRACE + timedelta(hours=1),
            pickup_in=timedelta(days=PAYMENT_CHASE_DAYS_AHEAD + 2),
        )
        _scan_unpaid_reservations()
        self.assertFalse(self._chase_tasks(res).exists())

    def test_an_open_chase_that_is_now_too_early_closes_itself(self):
        """A task filed under the old rule for a trip a week out closes on the next
        scan, with a note that says when it will be back."""
        res = self._unpaid_reservation(
            booked_ago=UNPAID_TASK_BOOKING_GRACE + timedelta(hours=1),
            pickup_in=timedelta(days=PAYMENT_CHASE_DAYS_AHEAD + 3),
        )
        early = OperationalTask.objects.create(
            task_type=OperationalTask.TaskType.PAYMENT_CHASE,
            title="Unpaid $100: Alice Tester", due_at=timezone.now(),
            reservation=res, metadata={},
        )
        _scan_unpaid_reservations()
        early.refresh_from_db()
        self.assertEqual(early.status, OperationalTask.Status.COMPLETED)
        self.assertIn("too early", early.resolution_notes)
        self.assertEqual(self._chase_tasks(res).filter(status="pending").count(), 0)

    def test_grace_holds_even_for_an_imminent_pickup(self):
        """Booked an hour ago, rolling in 6h — the pickup date doesn't shorten
        the grace. Fresh same-day bookings are left to the email reminder engine."""
        res = self._unpaid_reservation(
            booked_ago=timedelta(hours=1), pickup_in=timedelta(hours=6),
        )
        _scan_unpaid_reservations()
        self.assertFalse(self._chase_tasks(res).exists())

    def test_grace_holds_for_a_far_out_trip(self):
        """The example case: booked today, trip 5 days out — still no task."""
        res = self._unpaid_reservation(
            booked_ago=timedelta(hours=6), pickup_in=timedelta(days=5),
        )
        _scan_unpaid_reservations()
        self.assertFalse(self._chase_tasks(res).exists())

    def test_paid_reservation_never_chased(self):
        """Grace change must not disturb the existing paid/card_saved skip."""
        from payment.models import Payment

        res = self._unpaid_reservation(
            booked_ago=UNPAID_TASK_BOOKING_GRACE + timedelta(hours=1),
            pickup_in=timedelta(days=2),
        )
        Payment.objects.create(
            reservation=res, amount=Decimal("100.00"), status="paid",
        )
        _scan_unpaid_reservations()
        self.assertFalse(self._chase_tasks(res).exists())
