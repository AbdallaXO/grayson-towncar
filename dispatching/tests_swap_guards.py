"""Phase-3 H1: guards enforced on the swap path.

The window test exercises the gate `find_swaps`/`_search` use to accept or reject EVERY
candidate swap — `check_feasibility` with `driver_window`. A candidate that violates
clear-by (Guard C) is rejected as infeasible, so no swap that would create such a
placement can be produced. The last tests cover `execute_swap`'s atomic
abort-and-write-nothing on re-validation failure. (Guard A / capacity was removed.)
"""
import json
from datetime import datetime, date as dt_date, time as dt_time
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth.models import User

from dispatching.scheduler import (
    check_feasibility, DriverDaySchedule, ScheduleSlot, preload_timing_cache,
)


def fake_leg(leg_id=1, pickup=dt_time(13, 0), trip="return", pax=2, lug=2,
             need=False, rf=0, ff=0, bo=0, pickup_loc="Disney", dropoff_loc="Disney"):
    return SimpleNamespace(
        id=leg_id, pickup_time=pickup,
        pickup_location=pickup_loc, dropoff_location=dropoff_loc,
        get_trip_type=lambda: trip, reservation=None, flight_information=None,
        effective_passenger_count=pax, effective_luggage_count=lug,
        effective_need_carseats=need, effective_rf_carseats=rf,
        effective_ff_carseats=ff, effective_booster_seats=bo,
    )


class SwapCandidateGuardTests(TestCase):
    """The accept/reject gate find_swaps uses for every candidate placement."""

    @classmethod
    def setUpTestData(cls):
        preload_timing_cache()  # empty in test DB -> get_drive_time falls back, no per-call DB

    def empty_sched(self):
        return DriverDaySchedule(driver_id=10, driver_name="d", driver_type="inhouse", slots=[])

    def test_swap_blocked_by_clearby_window(self):
        # A 22:00 pickup clears after a 17:00 clear-by — rejected (Guard C clear-by).
        leg = fake_leg(pickup=dt_time(22, 0))
        window = {"start": 0, "end": 17, "max_hours": None, "flexible": False}
        feas = check_feasibility(self.empty_sched(), leg, self.date(), driver_window=window)
        self.assertFalse(feas.feasible)
        self.assertIn("driver window", feas.reason.lower())

    def test_swap_allowed_within_window(self):
        leg = fake_leg(pickup=dt_time(10, 0))
        window = {"start": 0, "end": 17, "max_hours": None, "flexible": False}
        feas = check_feasibility(self.empty_sched(), leg, self.date(), driver_window=window)
        self.assertTrue(feas.feasible)

    @staticmethod
    def date():
        from datetime import date
        return date(2026, 5, 1)


class SharedCarSwapGuardTests(TestCase):
    """A driver who SHARES one physical car with a partner can't be handed a leg that
    overlaps the partner's jobs, even though his OWN calendar is free at that moment.

    Reproduces the founder's report: David (006) and Angel (006) split the car; Angel
    works 09:00–14:30; David's first job is 17:00. A 09:40 leg must NOT be offered to
    David — the car is physically with Angel — but a per-driver-only feasibility check
    (David is idle at 09:30) would wrongly accept it."""

    DAY = dt_date(2026, 5, 1)
    DAVID, ANGEL = 10, 20

    @classmethod
    def setUpTestData(cls):
        preload_timing_cache()

    def _angel_busy_board(self):
        # Angel holds a 09:00 → 14:30 job; David's day is empty (first real job at 17:00
        # is irrelevant to the 09:40 placement attempt, so we leave him idle here).
        angel_slot = ScheduleSlot(
            leg_id=999, pickup_time=dt_time(9, 0),
            pickup_location="Disney", pickup_category="resort",
            dropoff_location="MCO", dropoff_category="airport",
            trip_type="departure",
            estimated_end_time=datetime.combine(self.DAY, dt_time(14, 30)),
            reservation_id=0, customer_name="", status="in-progress", has_flight=False,
        )
        return {
            self.DAVID: DriverDaySchedule(driver_id=self.DAVID, driver_name="David",
                                          driver_type="inhouse", slots=[]),
            self.ANGEL: DriverDaySchedule(driver_id=self.ANGEL, driver_name="Angel",
                                          driver_type="inhouse",
                                          slots=[angel_slot]),
        }

    def _run(self, sharer_partners):
        from dispatching.swap_optimizer import find_swaps
        target = fake_leg(leg_id=1, pickup=dt_time(9, 40), trip="return",
                          pickup_loc="Disney", dropoff_loc="Disney")
        target.driver_id = None
        target.revenue_share = 0
        flex = {"start": 0, "end": 24, "max_hours": None, "flexible": True}
        return find_swaps(
            target_leg=target,
            inhouse_schedules=self._angel_busy_board(),
            all_legs_by_id={},
            driver_vtypes={self.DAVID: "towncar", self.ANGEL: "towncar"},
            target_date=self.DAY,
            driver_windows={self.DAVID: flex, self.ANGEL: flex},
            sharer_partners=sharer_partners,
        )

    def test_without_sharer_map_david_is_wrongly_offered(self):
        # Baseline (the OLD behavior): with no shared-car map, David's empty calendar makes
        # the 09:40 leg look placeable on him. This is exactly the bug.
        res = self._run(sharer_partners=None)
        david_targets = [s for s in res.solutions if s.target_driver_id == self.DAVID]
        self.assertTrue(david_targets, "expected the un-gated search to (wrongly) offer David")

    def test_shared_car_blocks_offering_david_the_overlapping_leg(self):
        # With the shared-car map, David and Angel are one physical unit. The 09:40 leg
        # overlaps Angel's 09:00–14:30 job, so it must NOT be offered to David — and Angel
        # herself can't take it (her own calendar conflicts), so there is NO solution.
        res = self._run(sharer_partners={self.DAVID: {self.ANGEL}, self.ANGEL: {self.DAVID}})
        self.assertEqual(
            [s for s in res.solutions if s.target_driver_id == self.DAVID], [],
            "shared-car gate must not offer David a leg overlapping Angel's car time")
        self.assertEqual(res.solutions, [],
                         "neither sharer can take the 09:40 leg — expected no solution")


class ExecuteSwapAbortTests(TestCase):
    """execute_swap must re-validate and persist NOTHING on an infeasible result."""

    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create_user("dispatcher", password="x", is_staff=True)
        self.client.force_login(self.staff)

    def _post(self):
        return self.client.post(
            reverse("execute_swap"),
            data=json.dumps({"date": "2026-05-01", "moves": [{"leg_id": 1, "to_driver_id": 1}]}),
            content_type="application/json",
        )

    def test_execute_swap_aborts_on_infeasible(self):
        # Re-validation reports infeasible -> the transaction raises _SwapInfeasible BEFORE
        # the save loop, so it returns 409 and writes nothing.
        with patch("dispatching.views._revalidate_swap_feasibility", return_value=(False, "7 pax > 6 seats")):
            resp = self._post()
        self.assertEqual(resp.status_code, 409)
        body = resp.json()
        self.assertFalse(body["success"])
        self.assertIn("infeasible", body["error"].lower())

    def test_execute_swap_proceeds_past_revalidation_when_feasible(self):
        # When re-validation passes, control proceeds to the save loop (which then 404s
        # on the non-existent dummy leg) — proving the gate is re-validation, not a no-op.
        with patch("dispatching.views._revalidate_swap_feasibility", return_value=(True, "")):
            resp = self._post()
        self.assertEqual(resp.status_code, 404)  # reached save loop; dummy leg 1 doesn't exist
