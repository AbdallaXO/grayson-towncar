"""The Day-Builder (scheduling redesign, Build 3b — Tickets A/B/D/E, rescoped).

The heavy acceptance proof is the Gate-4 replay harness
(docs/scheduling-redesign/analysis/17_build3_gate.py) — ten criteria over ten
real replayed dates. These tests cover what a replay cannot:

  * the REFUSALS (Ticket E): a held (sandbox) date, an empty roster, an empty
    day — each refused with a reason a dispatcher can act on, before any work;
  * PROPOSE-ONLY: a full build writes NO Leg.driver and NO
    DriverVehicleAssignment row — the one non-negotiable posture;
  * the STRUCTURAL guarantees of the A1 comparator: driver-days never exceed
    the seed's and farm-outs never exceed seed + epsilon (criterion 9 / 2 by
    construction, not by luck);
  * the JOB LADDER (Ticket D): the row-level claim makes a double-click a
    no-op, a crashed 'running' row is reclaimable after the stale window, and
    results round-trip through the status endpoint with the stale flag;
  * the ENDPOINT gates: staff-only, opt_enabled off => the feature does not
    respond, held date => 409 naming the draft;
  * the ADDITIVE payload keys on suggest_day_setup (opt_enabled / epsilon).

Run with:  ./manage.py test dispatching.tests_day_planner
"""
import json
from datetime import date, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from dispatching.day_planner import (
    PlanRefused, build_day_plan, start_day_plan_job, _run_plan_job,
)
from dispatching.models import DayPlan, SchedulerSettings
from drivers.models import Driver, DriverVehicleAssignment, FleetVehicle
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation, ScheduleDraft

TD = date(2026, 6, 2)
MCO = "Orlando International Airport (MCO), Jeff Fuqua Blvd, Orlando, FL"
DISNEY = "Disney's Grand Floridian Resort, Lake Buena Vista, FL"


class PlannerBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        p = patch("users.emails.send_internal_confirmation", lambda *a, **k: None)
        p.start()
        cls.addClassCleanup(p.stop)

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("disp3b", password="x", is_staff=True)
        cls.civilian = User.objects.create_user("guest3b", password="x")
        cls.vtype = Vehicle.objects.create(
            vehicle_type="suv", capacity=6, luggage_capacity=4)
        origin = Location.objects.create(name="MCO-3b")
        dest = Location.objects.create(name="Disney-3b")
        cls.route = Route.objects.create(
            origin=origin, destination=dest, inhouse_base_pay=Decimal("50.00"))
        cls.rate = Rate.objects.create(
            route=cls.route, vehicle=cls.vtype,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"))
        cls.d1 = Driver.objects.create(
            profile=User.objects.create_user(username="cal", first_name="Cal"),
            driver_type="inhouse")
        cls.d2 = Driver.objects.create(
            profile=User.objects.create_user(username="dee", first_name="Dee"),
            driver_type="inhouse")
        cls.unit1 = FleetVehicle.objects.create(
            vehicle_number="911", year=2023, make="Chevrolet", model="Suburban",
            vehicle_type=cls.vtype)
        cls.unit2 = FleetVehicle.objects.create(
            vehicle_number="912", year=2023, make="Chevrolet", model="Suburban",
            vehicle_type=cls.vtype)
        cls.customer = Customer.objects.create(
            first_name="Pat", last_name="Guest", email="pat3b@example.com",
            phone_number="5550003333")
        cls.reservation = Reservation.objects.create(
            trip_type="one-way", customer=cls.customer, vehicle=cls.vtype,
            rate=cls.rate, base_price=Decimal("100.00"),
            total_price=Decimal("100.00"))

    def setUp(self):
        SchedulerSettings.clear_cache()
        self.addCleanup(SchedulerSettings.clear_cache)

    # -- helpers ---------------------------------------------------------
    def _leg(self, hh, mm, day=TD, pickup=DISNEY, dropoff=MCO, driver=None):
        return Leg.objects.create(
            reservation=self.reservation, pickup_date=day,
            pickup_time=time(hh, mm), pickup_location=pickup,
            dropoff_location=dropoff, driver=driver, route=self.route,
            status="confirmed")

    def _roster(self, driver, unit, day=TD):
        return DriverVehicleAssignment.objects.create(
            driver=driver, vehicle=unit, date=day)

    def _enable(self, **overrides):
        cfg = SchedulerSettings.get_settings()
        cfg.opt_enabled = True
        for k, v in overrides.items():
            setattr(cfg, k, v)
        cfg.save()
        SchedulerSettings.clear_cache()


class RefusalTests(PlannerBase):
    """Ticket E: refusals happen early, loudly, and name the reason."""

    def test_held_date_is_refused_and_names_the_draft(self):
        self._roster(self.d1, self.unit1)
        self._leg(9, 0)
        draft = ScheduleDraft.objects.create(schedule_date=TD)
        with self.assertRaises(PlanRefused) as ctx:
            build_day_plan(TD)
        self.assertIn(f"draft #{draft.pk}", ctx.exception.reason)

    def test_no_roster_is_refused_with_day_setup_instruction(self):
        self._leg(9, 0)
        with self.assertRaises(PlanRefused) as ctx:
            build_day_plan(TD)
        self.assertIn("Day Setup", ctx.exception.reason)

    def test_empty_day_is_refused(self):
        self._roster(self.d1, self.unit1)
        with self.assertRaises(PlanRefused) as ctx:
            build_day_plan(TD)
        self.assertIn("No trips", ctx.exception.reason)


class ProposeOnlyTests(PlannerBase):
    """The one non-negotiable posture: a full build writes NOTHING."""

    def test_build_writes_no_leg_and_no_dva_row(self):
        self._roster(self.d1, self.unit1)
        self._roster(self.d2, self.unit2)
        legs = [self._leg(8, 0), self._leg(12, 0), self._leg(16, 0)]
        dva_before = sorted(DriverVehicleAssignment.objects
                            .values_list("id", "driver_id", "vehicle_id", "date"))
        plan = build_day_plan(TD, epsilon=0)
        for lg in legs:
            lg.refresh_from_db()
            self.assertIsNone(lg.driver_id, "propose-only: Leg.driver was written")
        dva_after = sorted(DriverVehicleAssignment.objects
                           .values_list("id", "driver_id", "vehicle_id", "date"))
        self.assertEqual(dva_before, dva_after,
                         "propose-only: a DVA row changed during the build")
        self.assertTrue(plan.assignments, "the plan should place the legs")

    def test_existing_assignments_are_respected_not_replanned(self):
        self._roster(self.d1, self.unit1)
        self._roster(self.d2, self.unit2)
        pinned = self._leg(8, 0, driver=self.d1)
        self._leg(12, 0)
        plan = build_day_plan(TD, epsilon=0)
        self.assertNotIn(pinned.id, plan.assignments)
        self.assertEqual(plan.assigned_existing, 1)


class ComparatorTests(PlannerBase):
    """A1's structural guarantees: never more drivers, never worse coverage."""

    def test_plan_never_uses_more_driver_days_or_farm_outs_than_seed(self):
        self._roster(self.d1, self.unit1)
        self._roster(self.d2, self.unit2)
        for hh in (7, 9, 11, 13, 15, 17):
            self._leg(hh, 0)
        plan = build_day_plan(TD, epsilon=0)
        self.assertLessEqual(plan.score["driver_days"],
                             plan.baseline["driver_days"])
        self.assertLessEqual(plan.score["farm_outs"],
                             plan.baseline["farm_outs"])

    def test_result_contract_fields_for_the_gate_harness(self):
        self._roster(self.d1, self.unit1)
        self._leg(9, 0)
        plan = build_day_plan(TD, epsilon=0)
        for attr in ("assignments", "dva_rows", "exceptions", "shares",
                     "budget_exhausted", "evaluations", "wall_clock_s",
                     "roster_driver_ids"):
            self.assertTrue(hasattr(plan, attr), attr)
        json.dumps(plan.to_payload())   # JSON-safe, or the job row can't store it


class JobLadderTests(PlannerBase):
    """Ticket D: the claim row, the double-click, the stale takeover."""

    def test_double_click_is_a_no_op_while_running(self):
        with patch("reservations.utils._run_in_background", lambda *a, **k: None):
            started, row = start_day_plan_job(TD, self.staff, 0)
            self.assertTrue(started)
            self.assertEqual(row.status, "running")
            started2, _row2 = start_day_plan_job(TD, self.staff, 0)
            self.assertFalse(started2)

    def test_crashed_running_row_is_reclaimable_after_stale_window(self):
        with patch("reservations.utils._run_in_background", lambda *a, **k: None):
            started, row = start_day_plan_job(TD, self.staff, 0)
            self.assertTrue(started)
            DayPlan.objects.filter(pk=row.pk).update(
                requested_at=timezone.now() - timedelta(minutes=30))
            started2, _ = start_day_plan_job(TD, self.staff, 0)
            self.assertTrue(started2, "a stale 'running' claim must be takeable")

    def test_job_body_stores_refusal_with_reason(self):
        ScheduleDraft.objects.create(schedule_date=TD)
        row = DayPlan.objects.create(date=TD, status="running")
        _run_plan_job(TD, 0, row.pk)
        row.refresh_from_db()
        self.assertEqual(row.status, "refused")
        self.assertIn("held for review", row.error)

    def test_job_body_stores_a_done_plan(self):
        self._roster(self.d1, self.unit1)
        self._leg(9, 0)
        row = DayPlan.objects.create(date=TD, status="running")
        _run_plan_job(TD, 0, row.pk)
        row.refresh_from_db()
        self.assertEqual(row.status, "done", row.error)
        stored = json.loads(row.result_json)
        self.assertEqual(stored["date"], TD.isoformat())


class EndpointTests(PlannerBase):
    """The two endpoints: gates, refusals, and the status round-trip."""

    def _post_build(self, user=None, day=TD, epsilon=0):
        self.client.force_login(user or self.staff)
        return self.client.post(
            reverse("build_day_plan"),
            data=json.dumps({"date": day.isoformat(), "epsilon": epsilon}),
            content_type="application/json")

    def test_switched_off_means_no_feature(self):
        r = self._post_build()
        self.assertEqual(r.status_code, 400)
        self.assertIn("switched off", r.json()["error"])

    def test_staff_only(self):
        self._enable()
        r = self._post_build(user=self.civilian)
        self.assertEqual(r.status_code, 403)

    def test_held_date_409_names_the_draft(self):
        self._enable()
        draft = ScheduleDraft.objects.create(schedule_date=TD)
        r = self._post_build()
        self.assertEqual(r.status_code, 409)
        self.assertIn(f"draft #{draft.pk}", r.json()["error"])
        self.assertFalse(DayPlan.objects.filter(date=TD, status="running").exists())

    def test_build_claims_and_reports_running(self):
        self._enable()
        with patch("reservations.utils._run_in_background", lambda *a, **k: None):
            r = self._post_build()
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["started"])
        self.assertEqual(body["status"], "running")

    def test_status_none_then_done_with_stale_flag(self):
        self._enable(opt_stale_after_min=120)
        self.client.force_login(self.staff)
        url = reverse("day_plan_status") + f"?date={TD.isoformat()}"
        self.assertEqual(self.client.get(url).json()["status"], "none")
        DayPlan.objects.create(
            date=TD, status="done",
            computed_at=timezone.now() - timedelta(minutes=240),
            result_json=json.dumps({"date": TD.isoformat()}))
        body = self.client.get(url).json()
        self.assertEqual(body["status"], "done")
        self.assertTrue(body["stale"])
        self.assertEqual(body["result"]["date"], TD.isoformat())


class CatchTheRestTests(PlannerBase):
    """D16 (founder, 2026-08-25): when trips farm out while certified drivers
    sit available and cars sit free, the plan PROPOSES ticking them by name —
    each suggestion must verifiably capture >=1 otherwise-farmed trip, pass
    rest/availability/certification, and remain propose-only."""

    def _bench_driver(self, username):
        return Driver.objects.create(
            profile=User.objects.create_user(username=username,
                                             first_name=username.title()),
            driver_type="inhouse")

    def _overloaded_day(self):
        """One rostered driver, two same-time trips: one MUST farm."""
        self._roster(self.d1, self.unit1)
        a = self._leg(9, 0)
        b = self._leg(9, 5)
        return a, b

    def test_bench_driver_on_free_car_is_proposed_and_captures_the_trip(self):
        self._overloaded_day()          # unit2 stays free; d2 has no DVA row
        plan = build_day_plan(TD, epsilon=0)
        self.assertEqual(plan.baseline["farm_outs"], 1)
        self.assertEqual(len(plan.additions), 1)
        add = plan.additions[0]
        self.assertEqual(add["driver_id"], self.d2.id)
        self.assertEqual(add["vehicle_id"], self.unit2.id)
        self.assertEqual(len(add["captured_leg_ids"]), 1)
        self.assertEqual(plan.with_additions["farm_outs"], 0)
        self.assertEqual(plan.with_additions["coverage_pct"], 100.0)
        # propose-only: the addition wrote nothing
        self.assertFalse(DriverVehicleAssignment.objects
                         .filter(driver=self.d2, date=TD).exists())
        # and the fixed-headcount plan is untouched by the suggestion
        self.assertNotIn(self.d2.id, set(plan.assignments.values()))

    def test_no_free_car_means_no_addition(self):
        self._overloaded_day()
        DriverVehicleAssignment.objects.create(   # unit2 held by someone off-roster
            driver=self._bench_driver("holder"), date=TD, vehicle=self.unit2)
        plan = build_day_plan(TD, epsilon=0)
        self.assertEqual(plan.additions, [])

    def test_uncertified_driver_never_offered_a_certified_unit(self):
        vt_van14 = Vehicle.objects.create(
            vehicle_type="Van(14 Pax)", capacity=14, luggage_capacity=14,
            requires_certification=True)
        self.unit2.vehicle_type = vt_van14
        self.unit2.save()
        self._overloaded_day()          # only free unit now needs certification
        plan = build_day_plan(TD, epsilon=0)
        self.assertEqual(plan.additions, [],
                         "an uncertified bench driver was offered a "
                         "certification-restricted unit")

    def test_rest_blocked_bench_driver_is_not_proposed(self):
        self._overloaded_day()
        # d2 worked late yesterday; with a 12h floor his 9:00 pickup today is
        # an unambiguous breach regardless of the estimated trip duration.
        self._leg(23, 30, day=TD - timedelta(days=1), driver=self.d2)
        cfg = SchedulerSettings.get_settings()
        cfg.rest_min_gap_minutes = 720
        cfg.save()
        SchedulerSettings.clear_cache()
        plan = build_day_plan(TD, epsilon=0)
        self.assertEqual(plan.additions, [],
                         "a rest-blocked bench driver was proposed")

    def test_nothing_farmed_means_no_additions(self):
        self._roster(self.d1, self.unit1)
        self._leg(9, 0)
        plan = build_day_plan(TD, epsilon=0)
        self.assertEqual(plan.baseline["farm_outs"], 0)
        self.assertEqual(plan.additions, [])


class BillingWallTests(PlannerBase):
    """Ticket D: a hypothetical board must never spend money — probe mode makes
    route-distance lookups read-only, and a full build enqueues NOTHING."""

    def test_probe_mode_reads_but_never_enqueues(self):
        from dispatching.route_distance import cached_drive_minutes, probe_mode
        from reservations.models import RouteDistanceCache
        with probe_mode():
            self.assertIsNone(cached_drive_minutes("123 Probe St", "456 Probe Ave"))
        self.assertEqual(RouteDistanceCache.objects.count(), 0,
                         "a probe-mode miss enqueued a pending (billable) row")
        cached_drive_minutes("123 Probe St", "456 Probe Ave")
        self.assertEqual(RouteDistanceCache.objects.count(), 1,
                         "probe mode must not suppress a later REAL enqueue")

    def test_full_build_enqueues_no_route_rows(self):
        from reservations.models import RouteDistanceCache
        self._roster(self.d1, self.unit1)
        self._roster(self.d2, self.unit2)
        for hh in (8, 11, 14):
            self._leg(hh, 0)
        before = RouteDistanceCache.objects.count()
        build_day_plan(TD, epsilon=0)
        self.assertEqual(RouteDistanceCache.objects.count(), before,
                         "the Day-Builder enqueued route-distance rows — a new "
                         "billing surface (Ticket D wall)")


class SuggestPayloadTests(PlannerBase):
    """Build 3b's ADDITIVE keys on the Day Setup suggest payload."""

    def test_payload_carries_opt_flags(self):
        from dispatching.day_setup import suggest_day_setup
        out = suggest_day_setup(TD)
        self.assertIn("opt_enabled", out)
        self.assertIn("opt_epsilon_farmouts", out)
        self.assertFalse(out["opt_enabled"])   # ships OFF
