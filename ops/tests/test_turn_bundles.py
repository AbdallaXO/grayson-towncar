"""The queue folds a driver's turn checks into one row per day, and parks the
ones for tomorrow and beyond under today's work.

Founder ask 2026-09-21: the scanner files one driver_conflict / tight_turn task
per leg pair, and on a busy next-day board that buries the payment chases and
flight checks a dispatcher actually came to do. Nothing about the tasks
themselves changes here — the auto-closer, the board flags and the advisor all
key on them — only how the queue lays them out.

Pinned:
  * two turn tasks on the same driver and date become ONE bundle row;
  * a driver-day with a single task stays an ordinary row;
  * different drivers on the same date are different bundles;
  * in the working lanes, tomorrow's turn checks leave the priority groups and
    land in the folded "later" section — today's stay put;
  * the Future Blockers lane keeps them in its main list (that lane IS the
    future view);
  * a payment chase on a future trip is NOT folded — this is about turns only.
"""
from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from drivers.models import Driver
from ops.models import OperationalTask
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation


class _QueueFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username="queue_disp", password="x", is_staff=True, first_name="Dana")
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
        cls.george = Driver.objects.create(
            profile=User.objects.create_user(
                username="tb_george", first_name="George", last_name="Hill"),
            driver_type="inhouse")
        cls.maria = Driver.objects.create(
            profile=User.objects.create_user(
                username="tb_maria", first_name="Maria", last_name="Cruz"),
            driver_type="inhouse")

    def setUp(self):
        self.client.force_login(self.staff)
        self.today = timezone.localdate()
        self.tomorrow = self.today + timedelta(days=1)

    def _leg(self, driver, day, pickup_time):
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=Decimal("100.00"),
            total_price=Decimal("100.00"))
        return Leg.objects.create(
            reservation=res, pickup_date=day, pickup_time=pickup_time,
            pickup_location="MCO", dropoff_location="Disney", route=self.route,
            status="confirmed", driver=driver)

    def _turn_task(self, leg, *, red=False, assigned_to=None):
        ttype = (OperationalTask.TaskType.DRIVER_CONFLICT if red
                 else OperationalTask.TaskType.TIGHT_TURN)
        return OperationalTask.objects.create(
            task_type=ttype,
            title=("Driver Conflict — " if red else "Tight turn — ") + str(leg.driver),
            priority=(OperationalTask.Priority.CRITICAL if red
                      else OperationalTask.Priority.MEDIUM),
            due_at=timezone.now(),
            leg=leg, reservation=leg.reservation,
            assigned_to=assigned_to,
            metadata={"driver_id": leg.driver_id, "driver_name": str(leg.driver)},
        )

    def _queue(self, **params):
        return self.client.get(reverse("task_queue"), params)

    @staticmethod
    def _rows(resp):
        return [r for g in resp.context["priority_groups"] for r in g["rows"]]


class BundleShapeTests(_QueueFixture):
    def test_two_turns_on_one_driver_day_fold_into_one_row(self):
        a = self._turn_task(self._leg(self.george, self.today, time(9, 0)), red=True)
        b = self._turn_task(self._leg(self.george, self.today, time(13, 30)))

        resp = self._queue()
        rows = self._rows(resp)

        self.assertEqual(len(rows), 1)
        bundle = rows[0]
        self.assertEqual(bundle["kind"], "bundle")
        self.assertEqual([t.id for t in bundle["tasks"]], [a.id, b.id])
        self.assertEqual((bundle["red"], bundle["amber"]), (1, 1))
        # The bundle takes the most urgent member's priority and sits in that group.
        self.assertEqual(bundle["priority"], OperationalTask.Priority.CRITICAL)
        self.assertEqual(resp.context["priority_groups"][0]["key"], "critical")
        # The group header still counts tasks, not rows.
        self.assertEqual(resp.context["priority_groups"][0]["count"], 2)
        self.assertContains(resp, "George Hill — 2 turns to check today")
        self.assertContains(resp, "George won&#x27;t make 1 of them; 1 is tight but makeable")

    def test_single_turn_task_stays_a_plain_row(self):
        t = self._turn_task(self._leg(self.george, self.today, time(9, 0)))

        rows = self._rows(self._queue())

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "task")
        self.assertEqual(rows[0]["task"].id, t.id)

    def test_two_drivers_same_day_are_two_bundles(self):
        for _ in range(2):
            self._turn_task(self._leg(self.george, self.today, time(9, 0)))
            self._turn_task(self._leg(self.maria, self.today, time(9, 0)))

        rows = self._rows(self._queue())

        self.assertEqual([r["kind"] for r in rows], ["bundle", "bundle"])
        self.assertEqual({r["driver_name"] for r in rows}, {"George Hill", "Maria Cruz"})

    def test_bundle_carries_claim_all_ids_for_the_unclaimed_members(self):
        mine = self._turn_task(self._leg(self.george, self.today, time(9, 0)),
                               assigned_to=self.staff)
        free = self._turn_task(self._leg(self.george, self.today, time(11, 0)))

        # A task claimed by me lands in the Mine lane; the unclaimed one in
        # Unclaimed. Same driver-day, but lanes are partitioned first, so each
        # lane sees one task and shows it plainly.
        rows = self._rows(self._queue(lane="unclaimed"))
        self.assertEqual([r["task"].id for r in rows], [free.id])
        rows = self._rows(self._queue(lane="mine"))
        self.assertEqual([r["task"].id for r in rows], [mine.id])

    def test_bundle_of_mine_offers_complete_all(self):
        for hour in (9, 11):
            self._turn_task(self._leg(self.george, self.today, time(hour, 0)),
                            assigned_to=self.staff)

        resp = self._queue(lane="mine")
        bundle = self._rows(resp)[0]

        self.assertTrue(bundle["all_mine"])
        self.assertEqual(bundle["mine_count"], 2)
        self.assertContains(resp, "Complete all 2")


class ComingDaysFoldTests(_QueueFixture):
    def test_tomorrows_turns_leave_the_priority_groups(self):
        today_task = self._turn_task(self._leg(self.george, self.today, time(9, 0)))
        for hour in (8, 10, 14):
            self._turn_task(self._leg(self.maria, self.tomorrow, time(hour, 0)))

        resp = self._queue()

        rows = self._rows(resp)
        self.assertEqual([r["task"].id for r in rows], [today_task.id])

        later = resp.context["later_turn_days"]
        self.assertEqual(len(later), 1)
        self.assertEqual(later[0]["date"], self.tomorrow)
        self.assertTrue(later[0]["label"].startswith("Tomorrow"))
        self.assertEqual(later[0]["count"], 3)
        self.assertEqual(later[0]["rows"][0]["kind"], "bundle")
        self.assertEqual(resp.context["later_turn_count"], 3)
        self.assertContains(resp, "Maria Cruz — 3 turns to check tomorrow")
        self.assertContains(resp, "3 turn checks across 1 driver-day")

    def test_a_lone_next_day_turn_is_folded_too(self):
        self._turn_task(self._leg(self.george, self.tomorrow, time(9, 0)))

        resp = self._queue()

        self.assertEqual(self._rows(resp), [])
        self.assertEqual(resp.context["later_turn_count"], 1)
        self.assertEqual(resp.context["later_turn_days"][0]["rows"][0]["kind"], "task")
        self.assertContains(resp, "Today is clear")

    def test_future_blockers_lane_keeps_them_in_the_main_list(self):
        for hour in (8, 10):
            self._turn_task(self._leg(self.maria, self.tomorrow, time(hour, 0)))

        resp = self._queue(lane="future")

        rows = self._rows(resp)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "bundle")
        self.assertEqual(resp.context["later_turn_days"], [])

    def test_a_future_payment_chase_is_not_folded(self):
        leg = self._leg(self.george, self.tomorrow, time(9, 0))
        chase = OperationalTask.objects.create(
            task_type=OperationalTask.TaskType.PAYMENT_CHASE,
            title="Unpaid — Deborah Peters",
            priority=OperationalTask.Priority.MEDIUM,
            due_at=timezone.now(),
            reservation=leg.reservation,
            metadata={"earliest_pickup": self.tomorrow.isoformat()},
        )

        resp = self._queue()

        self.assertEqual([r["task"].id for r in self._rows(resp)], [chase.id])
        self.assertEqual(resp.context["later_turn_count"], 0)

    def test_lane_counts_still_count_tasks(self):
        for hour in (8, 10, 14):
            self._turn_task(self._leg(self.maria, self.tomorrow, time(hour, 0)))

        resp = self._queue()

        self.assertEqual(resp.context["unclaimed_count"], 3)
        self.assertEqual(resp.context["future_blockers_count"], 3)
