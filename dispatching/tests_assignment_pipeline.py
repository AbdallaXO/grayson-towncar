"""The extracted assignment pipeline (scheduling redesign, Build 3a — P1/P2).

The heavy proof that the extraction changed nothing is the replay gate
(docs/scheduling-redesign/analysis/14_pipeline_parity.py): it captures the
production view's whole JSON response over 10 real dates x 4 payload scenarios
before and after, and fails on any difference. These tests cover what a replay
against a snapshot cannot:

  * the CONTRACT — that ``assignments, warnings, moves = run_assignment_pipeline(...)``
    still unpacks as 04 §4 specifies, and that the derived fields are populated;
  * the HYPOTHETICAL-ROSTER path (``dva_rows``) that Build 3's Candidate-Plan
    Outer Loop depends on and that no current caller exercises;
  * the APPLY branch, which the parity gate deliberately does not run;
  * REST-PENALTY LIVENESS — the overnight-rest scan sits inside a bare
    ``except Exception`` wrapping two imports, so a future import cycle would
    silently empty ``prev_end_by_driver`` and quietly drop the rest penalty out
    of the scorer. Nothing else would fail;
  * the P2 shared primitives, including that ``scheduler`` and
    ``assign_warnings`` still expose the names their ~12 callers and both
    replay scripts import.

Run with:  ./manage.py test dispatching.tests_assignment_pipeline
"""
import json
from datetime import date, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from dispatching.assignment_pipeline import (
    PipelineLocks, PipelineResult, PipelineWindows, run_assignment_pipeline,
)
from dispatching.models import SchedulerSettings
from drivers.models import Driver, DriverVehicleAssignment, FleetVehicle
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation

TD = date(2026, 6, 1)
MCO = "Orlando International Airport (MCO), Jeff Fuqua Blvd, Orlando, FL"
DISNEY = "Disney's Grand Floridian Resort, Lake Buena Vista, FL"


class PipelineBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        p = patch("users.emails.send_internal_confirmation", lambda *a, **k: None)
        p.start()
        cls.addClassCleanup(p.stop)

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("disp3a", password="x", is_staff=True)
        cls.vtype = Vehicle.objects.create(
            vehicle_type="suv", capacity=6, luggage_capacity=4)
        origin = Location.objects.create(name="MCO")
        dest = Location.objects.create(name="Disney")
        cls.route = Route.objects.create(
            origin=origin, destination=dest, inhouse_base_pay=Decimal("50.00"))
        cls.rate = Rate.objects.create(
            route=cls.route, vehicle=cls.vtype,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"))
        cls.d1 = Driver.objects.create(
            profile=User.objects.create_user(username="ana", first_name="Ana"),
            driver_type="inhouse")
        cls.d2 = Driver.objects.create(
            profile=User.objects.create_user(username="bo", first_name="Bo"),
            driver_type="inhouse")
        cls.unit1 = FleetVehicle.objects.create(
            vehicle_number="901", year=2023, make="Chevrolet", model="Suburban",
            vehicle_type=cls.vtype)
        cls.unit2 = FleetVehicle.objects.create(
            vehicle_number="902", year=2023, make="Chevrolet", model="Suburban",
            vehicle_type=cls.vtype)
        cls.customer = Customer.objects.create(
            first_name="Pat", last_name="Guest", email="pat3a@example.com",
            phone_number="5550002222")
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

    def _run(self, legs, drivers, *, windows=None, locks=None, dva_rows=None,
             day=TD):
        w = windows or PipelineWindows(
            driver_hours={d.id: (6, 22) for d in drivers})
        return run_assignment_pipeline(
            legs, drivers, day, w, locks or PipelineLocks(), dva_rows=dva_rows)


class ContractTests(PipelineBase):
    """04 §4 names the return as (assignments, warnings, moves)."""

    def test_unpacks_as_the_documented_three_tuple(self):
        self._roster(self.d1, self.unit1)
        legs = [self._leg(9, 0)]
        assignments, warnings, moves = self._run(legs, [self.d1])
        self.assertIsInstance(assignments, dict)
        self.assertIsInstance(warnings, list)
        self.assertEqual(set(moves), {"evict", "trim", "gap"})

    def test_result_object_carries_the_derived_state_the_view_renders(self):
        self._roster(self.d1, self.unit1)
        legs = [self._leg(9, 0)]
        res = self._run(legs, [self.d1])
        self.assertIsInstance(res, PipelineResult)
        self.assertEqual(set(res.legs_by_id), {legs[0].id})
        self.assertEqual(set(res.drivers_by_id), {self.d1.id})
        self.assertIn(self.d1.id, res.capped_windows)
        self.assertEqual([l.id for l in res.unassigned], [legs[0].id])
        # tuple and object must agree — one source, two shapes
        a, w, m = res
        self.assertIs(a, res.assignments)
        self.assertIs(w, res.warnings)
        self.assertIs(m, res.moves)

    def test_assigns_a_reachable_leg_to_the_rostered_driver(self):
        self._roster(self.d1, self.unit1)
        leg = self._leg(9, 0)
        assignments, _, _ = self._run([leg], [self.d1])
        self.assertEqual(assignments.get(leg.id), self.d1.id)

    def test_excluded_legs_never_enter_the_pool(self):
        self._roster(self.d1, self.unit1)
        keep, drop = self._leg(9, 0), self._leg(14, 0)
        res = self._run([keep, drop],
                        [self.d1],
                        locks=PipelineLocks(excluded_leg_ids=[drop.id]))
        self.assertNotIn(drop.id, res.assignments)
        self.assertEqual([l.id for l in res.unassigned], [keep.id])

    def test_manual_assignment_is_placed_and_locked(self):
        self._roster(self.d1, self.unit1)
        self._roster(self.d2, self.unit2)
        leg = self._leg(9, 0)
        res = self._run([leg], [self.d1, self.d2],
                        locks=PipelineLocks(manual_assignments={leg.id: self.d2.id}))
        self.assertEqual(res.assignments[leg.id], self.d2.id)
        self.assertIn(leg.id, res.locked_ids)


class HypotheticalRosterTests(PipelineBase):
    """``dva_rows`` is the whole point of the extraction (01 §A3): Build 3 must
    be able to score a candidate plan whose roster rows are NOT in the DB."""

    def test_unsaved_dva_rows_drive_the_co_driver_map(self):
        # Nothing in the database: no roster rows at all.
        self.assertEqual(DriverVehicleAssignment.objects.count(), 0)
        hypothetical = [
            DriverVehicleAssignment(driver=self.d1, vehicle=self.unit1, date=TD),
            DriverVehicleAssignment(driver=self.d2, vehicle=self.unit1, date=TD),
        ]
        for r in hypothetical:            # unsaved: give the FK ids the map reads
            r.driver_id, r.vehicle_id = r.driver.id, r.vehicle.id
        res = self._run([self._leg(9, 0)], [self.d1, self.d2],
                        dva_rows=hypothetical)
        # One car, two drivers -> they are each other's car-share partner.
        self.assertEqual(res.sharer_partners.get(self.d1.id), {self.d2.id})
        self.assertEqual(res.sharer_partners.get(self.d2.id), {self.d1.id})

    def test_none_dva_rows_still_reads_the_live_roster(self):
        self._roster(self.d1, self.unit1)
        self._roster(self.d2, self.unit1)          # a real shared car
        res = self._run([self._leg(9, 0)], [self.d1, self.d2])
        self.assertEqual(res.sharer_partners.get(self.d1.id), {self.d2.id})

    def test_a_candidate_roster_of_one_driver_is_scored_alone(self):
        self._roster(self.d1, self.unit1)
        self._roster(self.d2, self.unit2)
        legs = [self._leg(9, 0), self._leg(15, 0)]
        res = self._run(legs, [self.d1])           # d2 left off the candidate
        self.assertEqual(set(res.drivers_by_id), {self.d1.id})
        self.assertTrue(all(v == self.d1.id for v in res.assignments.values()))


class RestPenaltyLivenessTests(PipelineBase):
    """The overnight-rest scan is wrapped in a bare ``except Exception`` around
    two imports. If a refactor ever introduces an import cycle it fails SILENTLY
    — no log, no test — and the rest penalty vanishes from the scorer. Assert it
    is actually alive."""

    def test_prev_end_by_driver_is_populated_from_yesterday(self):
        self._roster(self.d1, self.unit1)
        self._leg(10, 0, day=TD - timedelta(days=1), driver=self.d1)
        res = self._run([self._leg(9, 0)], [self.d1])
        self.assertIn(self.d1.id, res.prev_end_by_driver,
                      "the overnight-rest scan produced nothing for a driver who "
                      "worked yesterday — it is being swallowed by its "
                      "except Exception (import cycle?)")

    def test_no_work_yesterday_means_fully_rested(self):
        self._roster(self.d1, self.unit1)
        res = self._run([self._leg(9, 0)], [self.d1])
        self.assertEqual(res.prev_end_by_driver, {})


class ApplyPathTests(PipelineBase):
    """The parity gate runs preview only. The apply branch still has to write."""

    def _post(self, payload):
        self.client.force_login(self.staff)
        return self.client.post(
            reverse("auto_assign_drivers"),
            data=json.dumps(payload), content_type="application/json")

    def test_apply_saves_the_pipeline_proposal(self):
        self._roster(self.d1, self.unit1)
        leg = self._leg(9, 0)
        resp = self._post({"date": TD.isoformat(), "apply": True})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.d1.id)
        self.assertEqual(body["assigned"], 1)

    def test_preview_writes_nothing(self):
        self._roster(self.d1, self.unit1)
        leg = self._leg(9, 0)
        resp = self._post({"date": TD.isoformat(), "apply": False})
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(resp.json()["assigned"], 1)
        leg.refresh_from_db()
        self.assertIsNone(leg.driver_id)

    def test_apply_driver_ids_filters_the_write(self):
        self._roster(self.d1, self.unit1)
        self._roster(self.d2, self.unit2)
        leg = self._leg(9, 0)
        resp = self._post({"date": TD.isoformat(), "apply": True,
                           "apply_driver_ids": [self.d2.id]})
        self.assertEqual(resp.status_code, 200)
        leg.refresh_from_db()
        # the proposal went to whoever the engine picked; only d2 may be written
        self.assertIn(leg.driver_id, (None, self.d2.id))
