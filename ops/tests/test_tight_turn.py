"""Tests for the early-flight "tight turn" safety net.

Run with:  ./manage.py test ops.tests.test_tight_turn

Covers:
  * classify_turn() tier thresholds — the founder's rule (driver arrival vs the RAW
    flight arrival, no deplaning padding): >=15 min after → red "won't make it",
    0..15 min after → amber "tight", before the flight → no flag.
  * Leg.flight_timing_flag() board signal — amber 'watch' for early arrivals
    (15..19 min), red 'alert' at >= 20 min either direction.
  * _scan_driver_overlaps() raising the right task type, and escalation amber→red
    closing the softer flag.
"""
from datetime import date, datetime, time
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from rates.models import Vehicle, Location, Route, Rate
from reservations.models import Customer, Reservation, Leg, Flight
from ops.models import OperationalTask
from ops.tasks import classify_turn, _scan_driver_overlaps
from ops.services import create_task
from drivers.models import Driver

TARGET = date(2026, 6, 1)


class _TurnFixtureMixin:
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
            first_name="Jane", last_name="Doe", email="jane@example.com",
            phone_number="5559876543",
        )
        user = User.objects.create_user(username="tt_driver", first_name="Tito")
        cls.driver = Driver.objects.create(profile=user, driver_type="inhouse")

    def _res(self):
        return Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=Decimal("100.00"),
            total_price=Decimal("100.00"),
        )

    def _leg(self, pickup, dropoff, pickup_time, **kw):
        defaults = dict(
            reservation=self._res(), pickup_date=TARGET, pickup_time=pickup_time,
            pickup_location=pickup, dropoff_location=dropoff, route=self.route,
            status="confirmed", driver=self.driver,
        )
        defaults.update(kw)
        return Leg.objects.create(**defaults)

    def _arrival_leg_with_flight(self, pickup_time, arrival_dt):
        """An MCO→Disney arrival leg with a flight whose best arrival is arrival_dt."""
        flight = Flight.objects.create(
            airline="DL", flight_number="100",
            scheduled_arrival_local=arrival_dt,
        )
        leg = self._leg("MCO Airport", "Disney Resort", pickup_time)
        leg.flight_information = flight
        leg.save()
        return leg


class ClassifyTurnTierTests(_TurnFixtureMixin, TestCase):
    """The founder's example: a 9:00 Disney→MCO return then a 9:30 MCO→Disney
    arrival, same driver, who is free at the airport at ~9:30. The arrival flight
    creeps earlier."""

    def setUp(self):
        self.prior = self._leg("Disney Resort", "MCO Airport", time(9, 0))
        self.arrival = self._arrival_leg_with_flight(
            time(9, 30), datetime(2026, 6, 1, 9, 30)
        )

    def _tier(self, raw_arrival, driver_free):
        with patch("ops.tasks._get_raw_arrival_dt", return_value=raw_arrival), \
             patch("ops.tasks._estimate_leg_end_time", return_value=driver_free), \
             patch("ops.tasks._reposition_minutes", return_value=0):
            return classify_turn(self.prior, self.arrival, TARGET)

    def test_flight_well_before_driver_is_red(self):
        free = datetime(2026, 6, 1, 9, 30)
        # 9:00 → 30 min after; 9:10 → 20 min after — both "won't make it".
        for raw in (datetime(2026, 6, 1, 9, 0), datetime(2026, 6, 1, 9, 10)):
            risk = self._tier(raw, free)
            self.assertIsNotNone(risk)
            self.assertEqual(risk["tier"], "red")

    def test_fifteen_min_after_is_flagged_red(self):
        # Founder: "even 15 minutes after arrival, still flag that."
        risk = self._tier(datetime(2026, 6, 1, 9, 15), datetime(2026, 6, 1, 9, 30))
        self.assertIsNotNone(risk)
        self.assertEqual(risk["tier"], "red")
        self.assertEqual(risk["late"], 15)

    def test_ten_min_after_is_amber(self):
        # 9:20 flight, driver there 9:30 → 10 min after → "keep an eye".
        risk = self._tier(datetime(2026, 6, 1, 9, 20), datetime(2026, 6, 1, 9, 30))
        self.assertIsNotNone(risk)
        self.assertEqual(risk["tier"], "amber")
        self.assertEqual(risk["late"], 10)

    def test_driver_there_before_flight_is_no_flag(self):
        # Driver free 9:30, flight lands 9:50 → he's there 20 min early.
        self.assertIsNone(self._tier(datetime(2026, 6, 1, 9, 50), datetime(2026, 6, 1, 9, 30)))
        # Exactly on the dot (9:30) is fine too.
        self.assertIsNone(self._tier(datetime(2026, 6, 1, 9, 30), datetime(2026, 6, 1, 9, 30)))


class FlightTimingFlagTests(_TurnFixtureMixin, TestCase):
    """Board badge: amber 'watch' for early 15..19, red 'alert' at >= 20 either way."""

    def _flag(self, minutes_delta):
        """minutes_delta < 0 = flight earlier than pickup."""
        pickup = time(9, 30)
        arr = datetime(2026, 6, 1, 9, 30) + timezone.timedelta(minutes=minutes_delta)
        leg = self._arrival_leg_with_flight(pickup, arr)
        return leg.flight_timing_flag()

    def test_early_15_is_watch(self):
        flag = self._flag(-15)
        self.assertIsNotNone(flag)
        self.assertEqual(flag["level"], "watch")
        self.assertEqual(flag["direction"], "early")

    def test_early_22_is_alert(self):
        flag = self._flag(-22)
        self.assertEqual(flag["level"], "alert")

    def test_late_25_is_alert(self):
        flag = self._flag(25)
        self.assertEqual(flag["level"], "alert")
        self.assertEqual(flag["direction"], "late")

    def test_early_4_is_none(self):
        self.assertIsNone(self._flag(-4))

    def test_late_10_is_none(self):
        # Late but under the 20-min alert and there's no late 'watch' tier.
        self.assertIsNone(self._flag(10))


class DriverOverlapScanTests(_TurnFixtureMixin, TestCase):
    """The scan creates the right task type and escalation closes the soft flag."""

    def setUp(self):
        self.prior = self._leg("Disney Resort", "MCO Airport", time(9, 0))
        self.arrival = self._arrival_leg_with_flight(
            time(9, 30), datetime(2026, 6, 1, 9, 30)
        )
        self.fixed_now = timezone.make_aware(datetime(2026, 6, 1, 6, 0))

    def _run_scan(self, raw_arrival, driver_free=datetime(2026, 6, 1, 9, 30)):
        with patch("ops.tasks.timezone.now", return_value=self.fixed_now), \
             patch("ops.tasks.timezone.localdate", return_value=TARGET), \
             patch("ops.tasks._reposition_minutes", return_value=0), \
             patch("ops.tasks._estimate_leg_end_time", return_value=driver_free), \
             patch("ops.tasks._get_raw_arrival_dt", return_value=raw_arrival):
            return _scan_driver_overlaps()

    def _open(self, task_type):
        return OperationalTask.objects.filter(
            task_type=task_type,
            leg=self.arrival,
            status__in=list(OperationalTask.OPEN_STATUSES),
        )

    def test_amber_creates_tight_turn_task(self):
        self._run_scan(datetime(2026, 6, 1, 9, 20))  # 10 min after → amber
        self.assertTrue(self._open(OperationalTask.TaskType.TIGHT_TURN).exists())
        self.assertFalse(self._open(OperationalTask.TaskType.DRIVER_CONFLICT).exists())

    def test_red_creates_driver_conflict_task(self):
        self._run_scan(datetime(2026, 6, 1, 9, 0))  # 30 min after → red
        self.assertTrue(self._open(OperationalTask.TaskType.DRIVER_CONFLICT).exists())
        self.assertFalse(self._open(OperationalTask.TaskType.TIGHT_TURN).exists())

    def test_escalation_closes_open_tight_turn(self):
        # Pre-existing amber flag for the leg…
        create_task(
            task_type=OperationalTask.TaskType.TIGHT_TURN,
            title="Tight turn — Tito",
            leg=self.arrival,
            reservation=self.arrival.reservation,
        )
        self.assertTrue(self._open(OperationalTask.TaskType.TIGHT_TURN).exists())
        # …a now-earlier flight escalates it to red.
        self._run_scan(datetime(2026, 6, 1, 9, 0))
        self.assertFalse(self._open(OperationalTask.TaskType.TIGHT_TURN).exists())
        self.assertTrue(self._open(OperationalTask.TaskType.DRIVER_CONFLICT).exists())
