"""
"Flight not found" must be raised once, and must not close itself.

A wrong flight number produced 1,048 tasks across 307 legs — one leg collected
36. The loop was airtight:

  1. a refresh can't find the flight, so it wipes the arrival times and raises
     a task (dispatching/views.py did this with a bare
     ``OperationalTask.objects.create``, skipping the dedup and the two-hour
     cooldown that ``create_task`` applies);
  2. half an hour later ``_auto_close_resolved_tasks`` asks
     ``has_flight_time_mismatch()``, which returns False because there are no
     arrival times left to compare — the ones step 1 wiped — and closes the
     task as "flight mismatch resolved";
  3. the next refresh still can't find the flight, so back to step 1.

Nothing about it ever resolved. A wrong flight number stays wrong until a human
corrects it, which is exactly why it must not be auto-closed — and why raising
it more than once tells a dispatcher nothing they didn't already know.
"""

from datetime import date, time, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from ops.models import OperationalTask
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Flight, Leg, Reservation


class _Fixture:
    @classmethod
    def setUpTestData(cls):
        cls.vehicle = Vehicle.objects.create(
            vehicle_type="towncar", capacity=3, luggage_capacity=3
        )
        origin = Location.objects.create(name="MCO")
        dest = Location.objects.create(name="Disney")
        cls.route = Route.objects.create(
            origin=origin, destination=dest, inhouse_base_pay=Decimal("50.00")
        )
        cls.rate = Rate.objects.create(
            vehicle=cls.vehicle, route=cls.route,
            oneway_price=Decimal("105.00"), round_trip_price=Decimal("195.00"),
        )
        cls.customer = Customer.objects.create(
            first_name="Pat", last_name="Doe", email="pat@example.com",
            phone_number="5551234567",
        )

    def _leg(self, *, flight=None, **kw):
        res = Reservation.objects.create(
            trip_type="one_way", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=Decimal("105.00"),
            total_price=Decimal("105.00"),
        )
        defaults = dict(
            reservation=res,
            pickup_date=timezone.localdate() + timedelta(days=3),
            pickup_time=time(14, 0),
            pickup_location="MCO", dropoff_location="Disney", route=self.route,
            status="confirmed", flight_information=flight,
        )
        defaults.update(kw)
        return Leg.objects.create(**defaults)

    def _not_found_flight(self):
        """A flight the tracker could not find — every arrival time wiped."""
        return Flight.objects.create(
            airline="Eurowings Discover", flight_number="4Y068",
            scheduled_arrival_local=None, estimated_arrival_local=None,
            actual_arrival_local=None, scheduled_gate_arrival_local=None,
            estimated_gate_arrival_local=None, actual_gate_arrival_local=None,
        )


class RaisedOnlyOnce(_Fixture, TestCase):
    def _open(self, leg):
        return OperationalTask.objects.filter(
            leg=leg,
            task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
            status__in=list(OperationalTask.OPEN_STATUSES),
        )

    def test_a_missing_flight_raises_one_task(self):
        from ops.tasks import flag_flight_not_found

        leg = self._leg(flight=self._not_found_flight())

        flag_flight_not_found(leg)

        self.assertEqual(self._open(leg).count(), 1)

    def test_a_second_refresh_does_not_raise_another(self):
        from ops.tasks import flag_flight_not_found

        leg = self._leg(flight=self._not_found_flight())
        flag_flight_not_found(leg)

        flag_flight_not_found(leg)
        flag_flight_not_found(leg)

        self.assertEqual(self._open(leg).count(), 1)

    def test_the_task_records_why_it_exists(self):
        """The reason is what stops the auto-closer touching it."""
        from ops.tasks import flag_flight_not_found

        leg = self._leg(flight=self._not_found_flight())

        task = flag_flight_not_found(leg)

        self.assertEqual((task.metadata or {}).get("reason"), "flight_not_found")

    def test_a_leg_with_no_flight_raises_nothing(self):
        from ops.tasks import flag_flight_not_found

        leg = self._leg(flight=None)

        self.assertIsNone(flag_flight_not_found(leg))


class NotAutoClosedWhileTheFlightIsStillMissing(_Fixture, TestCase):
    def _open(self, leg):
        return OperationalTask.objects.filter(
            leg=leg,
            task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
            status__in=list(OperationalTask.OPEN_STATUSES),
        )

    def test_the_auto_closer_leaves_a_missing_flight_alone(self):
        """The bug: no arrival times reads as 'no mismatch', so it closed."""
        from ops.tasks import _auto_close_resolved_tasks, flag_flight_not_found

        leg = self._leg(flight=self._not_found_flight())
        flag_flight_not_found(leg)

        _auto_close_resolved_tasks()

        self.assertEqual(self._open(leg).count(), 1)

    def test_it_closes_once_the_flight_is_found_again(self):
        """A corrected flight number resolves it — that part must still work."""
        from ops.tasks import _auto_close_resolved_tasks, flag_flight_not_found

        flight = self._not_found_flight()
        leg = self._leg(flight=flight)
        flag_flight_not_found(leg)

        arrival = timezone.make_aware(
            timezone.datetime.combine(leg.pickup_date, time(14, 0)),
            timezone.get_current_timezone(),
        )
        flight.scheduled_arrival_local = arrival
        flight.save()

        _auto_close_resolved_tasks()

        self.assertEqual(self._open(leg).count(), 0)

    def test_an_ordinary_mismatch_task_still_auto_closes(self):
        """Only the not-found reason is protected; normal ones behave as before."""
        from ops.services import create_task
        from ops.tasks import _auto_close_resolved_tasks

        flight = self._not_found_flight()
        leg = self._leg(flight=flight)
        # A real, matching arrival — nothing is mismatched.
        arrival = timezone.make_aware(
            timezone.datetime.combine(leg.pickup_date, time(14, 0)),
            timezone.get_current_timezone(),
        )
        flight.scheduled_arrival_local = arrival
        flight.save()
        create_task(
            task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
            title="Flight mismatch: Pat Doe",
            leg=leg,
            reservation=leg.reservation,
        )

        _auto_close_resolved_tasks()

        self.assertEqual(self._open(leg).count(), 0)
