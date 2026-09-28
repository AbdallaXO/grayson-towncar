"""Tasks are for working, not looking at.

Founder ask 2026-09-21, after an audit of six months of task history
(docs/scheduling-redesign/analysis/29_task_queue_audit.py): the scanners filed
about 200 tasks a day, dispatchers clicked on tasks about 120 times a day, and
most of those clicks changed nothing — the task was going to close on its own,
or it came back two hours later because closing it did not touch the cause.

Pinned here, one rule per class:

  * a same-day turn has to be seen on two consecutive scans before it files;
    a red turn with the pickup inside two hours files at once;
  * a person's close STICKS while the facts are unchanged, and lifts the
    moment a time moves — for turns and for flight drifts alike;
  * a tight-but-makeable turn on a FUTURE board is never filed (0 of 321
    ended in a move); a future red conflict still is;
  * a same-day flight drift also waits one tick (72% self-resolved in 19 min);
  * an after-hours fee task cannot be completed blank — charge, collected or
    waive are the only ways to finish it;
  * the queue shows how many hand-closes came back, so the rule can be watched.
"""
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from drivers.models import Driver
from ops.models import OperationalTask
from ops.services import close_task
from ops.tasks import (
    _handle_future_driver_conflict,
    _handle_future_mismatch,
    _scan_driver_overlaps,
)
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation

TARGET = date(2026, 6, 1)

# Leg saves spawn background threads (notifications, route matching) that race
# SQLite in tests. Same neutralisation as ops/tests/test_tight_turn.py.
_NOOP = lambda *a, **k: None  # noqa: E731
_bg_targets = [
    "reservations.utils._run_in_background",
    "drivers.signals._run_in_background",
    "dispatching.views._run_in_background",
]
_bg_patchers = []


def setUpModule():
    for target in _bg_targets:
        p = patch(target, _NOOP)
        p.start()
        _bg_patchers.append(p)


def tearDownModule():
    for p in _bg_patchers:
        p.stop()
    _bg_patchers.clear()


class _BoardFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username="noise_disp", password="x", is_staff=True, first_name="Dana")
        cls.vehicle = Vehicle.objects.create(
            vehicle_type="van", capacity=11, luggage_capacity=10)
        cls.route = Route.objects.create(
            origin=Location.objects.create(name="MCO"),
            destination=Location.objects.create(name="Disney"),
            inhouse_base_pay=Decimal("50.00"))
        cls.rate = Rate.objects.create(
            vehicle=cls.vehicle, route=cls.route,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"))
        cls.customer = Customer.objects.create(
            first_name="Deborah", last_name="Peters", email="d@example.com",
            phone_number="5550001111")
        cls.driver = Driver.objects.create(
            profile=User.objects.create_user(
                username="noise_george", first_name="George", last_name="Hill"),
            driver_type="inhouse")

    def setUp(self):
        cache.clear()

    def _leg(self, pickup_time, day=TARGET, pickup="Disney Resort", dropoff="MCO Airport"):
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=Decimal("100.00"),
            total_price=Decimal("100.00"))
        return Leg.objects.create(
            reservation=res, pickup_date=day, pickup_time=pickup_time,
            pickup_location=pickup, dropoff_location=dropoff, route=self.route,
            status="confirmed", driver=self.driver)

    @staticmethod
    def _aware(h, m=0, day=TARGET):
        return timezone.make_aware(datetime.combine(day, time(h, m)))

    def _scan(self, now, driver_free):
        with patch("ops.tasks.timezone.now", return_value=now), \
             patch("ops.tasks.timezone.localdate", return_value=TARGET), \
             patch("ops.tasks._reposition_minutes", return_value=0), \
             patch("ops.tasks._estimate_leg_end_time", return_value=driver_free):
            return _scan_driver_overlaps()

    @staticmethod
    def _open(task_type, leg=None):
        qs = OperationalTask.objects.filter(
            task_type=task_type, status__in=list(OperationalTask.OPEN_STATUSES))
        return qs.filter(leg=leg) if leg is not None else qs


class ConfirmBeforeFilingTests(_BoardFixture):
    def setUp(self):
        super().setUp()
        self.prior = self._leg(time(15, 30))
        self.next = self._leg(time(16, 30), pickup="Loews Royal Pacific", dropoff="MCO Airport")
        self.morning = self._aware(6, 0)

    def test_a_tight_turn_never_becomes_a_task(self):
        # 6 min behind → amber: he makes the meet deadline. Retired 2026-09-22;
        # no number of sightings turns it into a task.
        free = datetime(2026, 6, 1, 16, 36)
        for tick in range(4):
            self.assertEqual(self._scan(self.morning + timedelta(minutes=30 * tick), free), 0)
        self.assertFalse(self._open(OperationalTask.TaskType.TIGHT_TURN).exists())
        self.assertEqual(OperationalTask.objects.count(), 0)

    def test_a_red_conflict_hours_out_also_waits_one_tick(self):
        free = datetime(2026, 6, 1, 16, 50)  # 20 min late → red, pickup 10h away
        self.assertEqual(self._scan(self.morning, free), 0)
        self.assertEqual(self._scan(self.morning + timedelta(minutes=30), free), 1)
        self.assertTrue(self._open(OperationalTask.TaskType.DRIVER_CONFLICT, self.next).exists())

    def test_a_red_conflict_inside_two_hours_files_at_once(self):
        # 15:45 now, prior job 15:50, next pickup 16:30 and the driver clears 16:50.
        Leg.objects.filter(pk=self.prior.pk).update(pickup_time=time(15, 50))
        now = self._aware(15, 45)
        self.assertEqual(self._scan(now, datetime(2026, 6, 1, 16, 50)), 1)
        self.assertTrue(self._open(OperationalTask.TaskType.DRIVER_CONFLICT, self.next).exists())

    def test_a_wobble_that_is_gone_by_the_next_tick_never_becomes_a_task(self):
        self.assertEqual(self._scan(self.morning, datetime(2026, 6, 1, 16, 36)), 0)
        # Next tick the estimate has settled: the driver clears in time.
        self.assertEqual(self._scan(self.morning + timedelta(minutes=30),
                                    datetime(2026, 6, 1, 16, 25)), 0)
        self.assertEqual(OperationalTask.objects.count(), 0)

    def test_a_sighting_is_forgotten_after_ninety_minutes(self):
        free = datetime(2026, 6, 1, 16, 50)  # 20 min late → red, pickup hours out
        self.assertEqual(self._scan(self.morning, free), 0)
        cache.clear()  # what TURN_CONFIRM_TTL_SECONDS does to a stale sighting
        self.assertEqual(self._scan(self.morning + timedelta(hours=3), free), 0)
        self.assertFalse(self._open(OperationalTask.TaskType.DRIVER_CONFLICT).exists())

    def test_the_task_carries_a_fingerprint_of_the_facts(self):
        free = datetime(2026, 6, 1, 16, 50)
        self._scan(self.morning, free)
        self._scan(self.morning + timedelta(minutes=30), free)
        task = self._open(OperationalTask.TaskType.DRIVER_CONFLICT, self.next).get()
        fp = task.metadata["fingerprint"]
        self.assertIn(f"{self.prior.id}@15:30:00", fp)
        self.assertIn(f"{self.next.id}@16:30:00", fp)
        self.assertIn("|red|2", fp)  # 20 minutes late → bucket 2


class HandCloseSticksTests(_BoardFixture):
    def setUp(self):
        super().setUp()
        self.prior = self._leg(time(15, 30))
        self.next = self._leg(time(16, 30), pickup="Loews Royal Pacific", dropoff="MCO Airport")
        self.morning = self._aware(6, 0)
        self.free = datetime(2026, 6, 1, 16, 50)  # red, 20 min

    def _file(self, now):
        self._scan(now, self.free)
        self._scan(now + timedelta(minutes=30), self.free)
        return self._open(OperationalTask.TaskType.DRIVER_CONFLICT, self.next).get()

    def _close(self, task, **kw):
        """Close at 06:40 on the board's day. create_task also refuses to re-file
        anything for a flat two hours after ANY close, so the re-scans below run
        at 09:00 — outside that cooldown — and only the dismissal rule is in play."""
        close_task(task, resolved_by=self.staff, **kw)
        OperationalTask.objects.filter(pk=task.pk).update(
            resolved_at=self.morning + timedelta(minutes=40))
        cache.clear()
        return self.morning + timedelta(hours=3)

    def test_a_person_closing_it_keeps_it_closed_while_nothing_changed(self):
        task = self._file(self.morning)
        later = self._close(task, resolution_notes="")
        # Two more ticks, same numbers: the scanner respects the dismissal.
        self._scan(later, self.free)
        self._scan(later + timedelta(minutes=30), self.free)
        self.assertFalse(self._open(OperationalTask.TaskType.DRIVER_CONFLICT, self.next).exists())

    def test_a_moved_time_is_a_new_fact_and_files_again(self):
        task = self._file(self.morning)
        later = self._close(task, resolution_notes="")
        Leg.objects.filter(pk=self.prior.pk).update(pickup_time=time(15, 45))
        self._scan(later, self.free)
        self._scan(later + timedelta(minutes=30), self.free)
        self.assertTrue(self._open(OperationalTask.TaskType.DRIVER_CONFLICT, self.next).exists())

    def test_an_auto_close_is_not_a_dismissal(self):
        task = self._file(self.morning)
        # A reassignment signal closes with resolved_by set but an Auto-closed note.
        later = self._close(
            task, resolution_notes="Auto-closed: driver reassigned, original conflict resolved")
        self._scan(later, self.free)
        self._scan(later + timedelta(minutes=30), self.free)
        self.assertTrue(self._open(OperationalTask.TaskType.DRIVER_CONFLICT, self.next).exists())

    def test_a_dismissal_wears_off_after_three_days(self):
        task = self._file(self.morning)
        close_task(task, resolved_by=self.staff, resolution_notes="")
        OperationalTask.objects.filter(pk=task.pk).update(
            resolved_at=self.morning - timedelta(days=4))
        cache.clear()
        self._scan(self.morning, self.free)
        self._scan(self.morning + timedelta(minutes=30), self.free)
        self.assertTrue(self._open(OperationalTask.TaskType.DRIVER_CONFLICT, self.next).exists())


class FutureBoardTests(_BoardFixture):
    def _conflict(self, tier, minutes, other):
        return [{
            "conflicting_leg": other,
            "driver_clears_at": self._aware(16, 50, day=other.pickup_date),
            "effective_ready": self._aware(16, 30, day=other.pickup_date),
            "conflict_minutes": minutes,
            "tier": tier,
            "direction": "this_delays_other",
        }]

    def test_a_future_tight_turn_is_not_filed(self):
        tomorrow = timezone.localdate() + timedelta(days=1)
        leg = self._leg(time(14, 0), day=tomorrow)
        other = self._leg(time(16, 30), day=tomorrow)
        with patch("ops.tasks.detect_driver_conflicts",
                   return_value=self._conflict("amber", 6, other)):
            n = _handle_future_driver_conflict(
                leg, {"direction": "later", "minutes": 20, "label": "20 min late"},
                "UA 2396", days_until=1, now=timezone.now())
        self.assertEqual(n, 0)
        self.assertEqual(OperationalTask.objects.count(), 0)

    def test_a_future_red_conflict_still_files_with_a_fingerprint(self):
        tomorrow = timezone.localdate() + timedelta(days=1)
        leg = self._leg(time(14, 0), day=tomorrow)
        other = self._leg(time(16, 30), day=tomorrow)
        with patch("ops.tasks.detect_driver_conflicts",
                   return_value=self._conflict("red", 20, other)):
            n = _handle_future_driver_conflict(
                leg, {"direction": "later", "minutes": 20, "label": "20 min late"},
                "UA 2396", days_until=1, now=timezone.now())
        self.assertEqual(n, 1)
        task = OperationalTask.objects.get()
        self.assertEqual(task.task_type, OperationalTask.TaskType.DRIVER_CONFLICT)
        self.assertIn("fingerprint", task.metadata)


class FlightDriftTests(_BoardFixture):
    def setUp(self):
        super().setUp()
        self.leg = self._leg(time(14, 0), day=timezone.localdate(),
                             pickup="MCO Airport", dropoff="Disney Resort")
        self.mismatch = {"direction": "later", "minutes": 45, "label": "45 min late"}

    def _file(self, days_until, mismatch=None):
        return _handle_future_mismatch(
            self.leg, mismatch or self.mismatch, "Deborah Peters", "DL 100",
            days_until=days_until, now=timezone.now())

    def test_a_same_day_drift_waits_for_the_next_tick(self):
        self.assertEqual(self._file(days_until=0), 0)
        self.assertEqual(self._file(days_until=0), 1)

    def test_a_drift_days_out_files_at_once(self):
        self.assertEqual(self._file(days_until=2), 1)

    def test_keeping_the_pickup_time_is_a_decision_that_sticks(self):
        self._file(days_until=2)
        task = OperationalTask.objects.get()
        close_task(task, resolved_by=self.staff, resolution_notes="")
        # Step past create_task's flat two-hour cooldown so only the fingerprint decides.
        OperationalTask.objects.filter(pk=task.pk).update(
            resolved_at=timezone.now() - timedelta(hours=3))
        self.assertEqual(self._file(days_until=2), 0)
        # A wobble of a few minutes is the same fact...
        self.assertEqual(self._file(days_until=2, mismatch={
            "direction": "later", "minutes": 52, "label": "52 min late"}), 0)
        # ...another half hour of drift is not.
        self.assertEqual(self._file(days_until=2, mismatch={
            "direction": "later", "minutes": 80, "label": "80 min late"}), 1)


class FeeTaskCannotBeClosedBlankTests(_BoardFixture):
    def test_complete_is_refused_and_points_at_the_fee_question(self):
        leg = self._leg(time(23, 30), day=timezone.localdate())
        task = OperationalTask.objects.create(
            task_type=OperationalTask.TaskType.AFTERHOURS_FEE,
            title="After-hours fee not collected", due_at=timezone.now(),
            leg=leg, reservation=leg.reservation, metadata={})
        self.client.force_login(self.staff)
        resp = self.client.post(
            reverse("task_complete"),
            json.dumps({"task_id": task.id, "notes": ""}),
            content_type="application/json")
        body = resp.json()
        self.assertFalse(body["success"])
        self.assertTrue(body["needs_afterhours_decision"])
        self.assertEqual(body["leg_id"], leg.id)
        task.refresh_from_db()
        self.assertTrue(task.is_open)

    def test_other_task_types_still_complete_normally(self):
        leg = self._leg(time(9, 0), day=timezone.localdate())
        task = OperationalTask.objects.create(
            task_type=OperationalTask.TaskType.TIGHT_TURN,
            title="Tight turn — George Hill", due_at=timezone.now(),
            leg=leg, reservation=leg.reservation, metadata={})
        self.client.force_login(self.staff)
        resp = self.client.post(
            reverse("task_complete"),
            json.dumps({"task_id": task.id, "notes": "board is right"}),
            content_type="application/json")
        self.assertTrue(resp.json()["success"])
        task.refresh_from_db()
        self.assertEqual(task.status, OperationalTask.Status.COMPLETED)


class CameBackGaugeTests(_BoardFixture):
    def _task(self, leg, **kw):
        defaults = dict(
            task_type=OperationalTask.TaskType.TIGHT_TURN,
            title="Tight turn — George Hill", due_at=timezone.now(),
            leg=leg, reservation=leg.reservation, metadata={})
        defaults.update(kw)
        return OperationalTask.objects.create(**defaults)

    def test_counts_a_hand_close_the_scanner_refiled_within_a_day(self):
        leg = self._leg(time(9, 0), day=timezone.localdate())
        first = self._task(leg)
        close_task(first, resolved_by=self.staff, resolution_notes="")
        OperationalTask.objects.filter(pk=first.pk).update(
            created_at=timezone.now() - timedelta(hours=4),
            resolved_at=timezone.now() - timedelta(hours=3))
        self._task(leg)  # came back
        # An auto-close that came back is the scanner's business, not a wasted look.
        other = self._leg(time(11, 0), day=timezone.localdate())
        auto = self._task(other)
        close_task(auto, resolution_notes="Auto-closed: turn no longer tight")
        self._task(other)

        self.client.force_login(self.staff)
        resp = self.client.get(reverse("task_queue"))
        self.assertEqual(resp.context["came_back_count"], 1)
        self.assertContains(resp, "Came back")
