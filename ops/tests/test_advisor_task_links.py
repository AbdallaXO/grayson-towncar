"""Recovery Advisor <-> ops task interplay (advisor plan, guard 9 + Stage F).

Run with:  ./manage.py test ops.tests.test_advisor_task_links

What must hold:
  * A card whose leg already has an open DRIVER_CONFLICT task DEEP-LINKS it
    (``task_id`` on the card) and never grows a duplicate "file task" offer —
    the advisor links to the queue, it doesn't compete with it.
  * A detected conflict with NO open task offers ``file_task`` (leg + typed
    task_type + headline); unassigned cards offer ``driver_assign``, every
    other kind ``driver_conflict``.
  * The file-task endpoint creates through ``ops.services.create_task`` and so
    inherits its dedup: re-filing (or racing the 30-min scanner) answers with
    the EXISTING open task's id and creates nothing.
  * Applying a reassign plan auto-closes the linked conflict task through the
    ops/signals path, attributed to the applying user (``leg._reassigned_by``
    set inside ``set_leg_driver`` — the advisor writes no task rows itself).
"""
from datetime import datetime, time, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from dispatching.conflict_advisor import compute_advisor_state
from dispatching.conflict_advisor_actions import apply_advisor_plan
from dispatching.scheduler import preload_timing_cache
from drivers.models import Driver, DriverVehicleAssignment, FleetVehicle
from ops.models import OperationalTask
from ops.services import create_task
from ops.tests.test_tight_turn import TARGET, _TurnFixtureMixin

# apply_advisor_plan refuses past service dates, so the apply-path tests run on
# a future day while the detection-only tests keep the mixin's fixed TARGET.
FUTURE = timezone.localdate() + timedelta(days=7)

NOW = timezone.make_aware(datetime(TARGET.year, TARGET.month, TARGET.day, 6, 0))


class _AdvisorTaskFixture(_TurnFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        preload_timing_cache()
        # The mixin's "sedan" isn't a VEHICLE_TIER_ORDER class, so the apply
        # path's vehicle gate can't reason about it — the legs these tests
        # build ride a real towncar booking instead.
        from decimal import Decimal
        from rates.models import Rate, Vehicle
        from reservations.models import Reservation
        cls._Reservation = Reservation
        cls.towncar = Vehicle.objects.create(
            vehicle_type="towncar", capacity=4, luggage_capacity=4)
        cls.towncar_rate = Rate.objects.create(
            vehicle=cls.towncar, route=cls.route,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"))
        cls._base_price = Decimal("100.00")
        # A second in-house driver (the reassign receiver) + towncars on both
        # test days: load_all_driver_vtypes only sees DVA holders.
        cls.marco = Driver.objects.create(
            profile=User.objects.create_user("tl_marco", first_name="Marco"),
            driver_type="inhouse")
        for i, drv in enumerate((cls.driver, cls.marco)):
            fleet = FleetVehicle.objects.create(
                vehicle_number=f"TL-{i + 1}", vehicle_type=cls.towncar,
                year=2024, make="Lincoln", model="Continental")
            for day in (TARGET, FUTURE):
                DriverVehicleAssignment.objects.create(
                    driver=drv, date=day, vehicle=fleet)
        # Superuser-only during the owner trial (advisor_visible_to).
        cls.staff = User.objects.create_user(
            "tl_staff", password="x", is_staff=True, is_superuser=True)

    def _towncar_res(self):
        return self._Reservation.objects.create(
            trip_type="one-way", customer=self.customer,
            rate=self.towncar_rate, vehicle=self.towncar,
            base_price=self._base_price, total_price=self._base_price)

    def _overlap_pair(self, day=TARGET):
        """Two legs 5 minutes apart on the same driver — a critical overlap."""
        a = self._leg("Disney Resort", "MCO Airport", time(9, 0),
                      pickup_date=day, reservation=self._towncar_res())
        b = self._leg("Disney Grand Floridian", "Disney Boardwalk", time(9, 5),
                      pickup_date=day, reservation=self._towncar_res())
        return a, b

    def _conflict_task(self, leg, conflicting_leg):
        return create_task(
            task_type=OperationalTask.TaskType.DRIVER_CONFLICT,
            title="Driver conflict — Tito",
            leg=leg, reservation=leg.reservation,
            metadata={"driver_id": leg.driver_id,
                      "conflicting_leg_id": conflicting_leg.id},
        )

    @staticmethod
    def _overlap_card(state, a, b):
        return next(c for c in state["disruptions"]
                    if c["id"] == f"overlap:{a.id}:{b.id}")


class CardTaskLinkTests(_AdvisorTaskFixture):
    """Deep-link when a task exists; offer filing one only when none does."""

    def test_card_deep_links_open_conflict_task_no_file_offer(self):
        a, b = self._overlap_pair()
        task = self._conflict_task(b, a)
        state = compute_advisor_state(TARGET, now=NOW)
        card = self._overlap_card(state, a, b)
        self.assertEqual(card["task_id"], task.id)
        # The card links to the existing task — offering to file another
        # would race create_task's own dedup for no reason.
        self.assertNotIn("file_task", card)
        # The one-click payloads carry the same task id for the apply path.
        for plan in card["plans"]:
            if "apply" in plan:
                self.assertEqual(plan["apply"]["task_id"], task.id)

    def test_card_without_task_offers_file_task(self):
        a, b = self._overlap_pair()
        state = compute_advisor_state(TARGET, now=NOW)
        card = self._overlap_card(state, a, b)
        self.assertIsNone(card["task_id"])
        self.assertEqual(card["file_task"], {
            "leg_id": b.id,                     # the anchor (broken) pickup
            "task_type": "driver_conflict",
            "title": card["headline"],
            # Carried so the advisor ledger can tie the filed task back to the
            # card that offered it — leg_id alone cannot, since two kinds can
            # raise cards on the same leg at the same minute.
            "disruption_id": card["id"],
        })

    def test_unassigned_card_offers_driver_assign_type(self):
        leg = self._leg("Disney Resort", "MCO Airport", time(7, 0),
                        driver=None)
        state = compute_advisor_state(TARGET, now=NOW)
        card = next(c for c in state["disruptions"]
                    if c["id"] == f"unassigned:{leg.id}")
        self.assertIsNone(card["task_id"])
        self.assertEqual(card["file_task"]["task_type"], "driver_assign")
        self.assertEqual(card["file_task"]["leg_id"], leg.id)


class FileTaskEndpointTests(_AdvisorTaskFixture):
    """The offer's write side inherits create_task's dedup wholesale."""

    def setUp(self):
        self.client.force_login(self.staff)

    def _file(self, leg, task_type="driver_conflict", **extra):
        payload = {"date": leg.pickup_date.isoformat(), "leg_id": leg.id,
                   "task_type": task_type, "title": "Untangle Tito's 9:05 turn"}
        payload.update(extra)
        return self.client.post(reverse("recovery_advisor_file_task"), payload,
                                content_type="application/json")

    def test_files_open_task_linked_to_leg(self):
        _a, b = self._overlap_pair()
        resp = self._file(b)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["created"])
        task = OperationalTask.objects.get(id=body["task_id"])
        self.assertEqual(task.task_type, OperationalTask.TaskType.DRIVER_CONFLICT)
        self.assertEqual(task.leg_id, b.id)
        self.assertEqual(task.created_by_id, self.staff.id)
        self.assertTrue(task.is_open)
        # And the next advisor pass deep-links it instead of re-offering.
        state = compute_advisor_state(TARGET, now=NOW)
        card = self._overlap_card(state, _a, b)
        self.assertEqual(card["task_id"], task.id)
        self.assertNotIn("file_task", card)

    def test_refile_dedups_to_existing_open_task(self):
        _a, b = self._overlap_pair()
        first = self._file(b).json()
        second = self._file(b).json()
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(second["task_id"], first["task_id"])
        self.assertEqual(OperationalTask.objects.filter(
            leg=b, task_type=OperationalTask.TaskType.DRIVER_CONFLICT).count(), 1)

    def test_scanner_created_task_also_dedups(self):
        # The 30-min scanner got there first — filing resolves to ITS task.
        a, b = self._overlap_pair()
        existing = self._conflict_task(b, a)
        body = self._file(b).json()
        self.assertFalse(body["created"])
        self.assertEqual(body["task_id"], existing.id)

    def test_bad_task_type_and_missing_leg_rejected(self):
        _a, b = self._overlap_pair()
        self.assertEqual(self._file(b, task_type="payment_chase").status_code, 400)
        resp = self.client.post(
            reverse("recovery_advisor_file_task"),
            {"date": TARGET.isoformat(), "leg_id": 999999,
             "task_type": "driver_conflict", "title": "x"},
            content_type="application/json")
        self.assertEqual(resp.status_code, 404)

    def test_non_staff_is_403(self):
        plain = User.objects.create_user("tl_plain", password="x")
        self.client.force_login(plain)
        _a, b = self._overlap_pair()
        self.assertEqual(self._file(b).status_code, 403)
        self.assertFalse(OperationalTask.objects.exists())

    def test_staff_dispatcher_is_403_while_superuser_only(self):
        dispatcher = User.objects.create_user("tl_disp", password="x", is_staff=True)
        self.client.force_login(dispatcher)
        _a, b = self._overlap_pair()
        self.assertEqual(self._file(b).status_code, 403)
        self.assertFalse(OperationalTask.objects.exists())


class ApplyAutoCloseTests(_AdvisorTaskFixture):
    """Applying a reassign closes the conflict task via ops/signals, attributed."""

    def test_apply_reassign_auto_closes_task_attributed_to_applier(self):
        a, b = self._overlap_pair(day=FUTURE)
        task = self._conflict_task(b, a)
        status, body = apply_advisor_plan({
            "schema": 1, "date": FUTURE.isoformat(),
            "disruption_id": f"overlap:{a.id}:{b.id}",
            "plan_id": f"overlap:{a.id}:{b.id}#p1",
            "task_id": task.id,
            "actions": [{"op": "reassign", "leg_id": b.id,
                         "to_driver_id": self.marco.id}],
            "expected": {str(b.id): self.driver.id},
            "expected_times": {},
        }, self.staff)
        self.assertEqual(status, 200, body)
        b.refresh_from_db()
        self.assertEqual(b.driver_id, self.marco.id)
        task.refresh_from_db()
        # ops/signals closed it on the driver change (the advisor never touches
        # task rows for assignment plans) — attributed via leg._reassigned_by.
        self.assertEqual(task.status, OperationalTask.Status.COMPLETED)
        self.assertEqual(task.resolved_by_id, self.staff.id)
        self.assertIn("reassigned", task.resolution_notes)

    def test_created_file_task_auto_closes_on_apply_too(self):
        # End-to-end loop: card offers -> dispatcher files -> plan applied ->
        # the filed task closes itself, same attribution path.
        self.client.force_login(self.staff)
        a, b = self._overlap_pair(day=FUTURE)
        resp = self.client.post(
            reverse("recovery_advisor_file_task"),
            {"date": FUTURE.isoformat(), "leg_id": b.id,
             "task_type": "driver_conflict", "title": "Untangle the 9:05 turn"},
            content_type="application/json")
        task_id = resp.json()["task_id"]
        status, body = apply_advisor_plan({
            "schema": 1, "date": FUTURE.isoformat(),
            "disruption_id": f"overlap:{a.id}:{b.id}",
            "plan_id": f"overlap:{a.id}:{b.id}#p1",
            "task_id": task_id,
            "actions": [{"op": "reassign", "leg_id": b.id,
                         "to_driver_id": self.marco.id}],
            "expected": {str(b.id): self.driver.id},
            "expected_times": {},
        }, self.staff)
        self.assertEqual(status, 200, body)
        task = OperationalTask.objects.get(id=task_id)
        self.assertEqual(task.status, OperationalTask.Status.COMPLETED)
        self.assertEqual(task.resolved_by_id, self.staff.id)
