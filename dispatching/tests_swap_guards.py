"""Phase-3 H1: guards enforced on the swap path.

The window test exercises the gate `find_swaps`/`_search` use to accept or reject EVERY
candidate swap — `check_feasibility` with `driver_window`. A candidate that violates
clear-by (Guard C) is rejected as infeasible, so no swap that would create such a
placement can be produced. The last tests cover `execute_swap`'s atomic
abort-and-write-nothing on re-validation failure. (Guard A / capacity was removed.)
"""
import json
from datetime import time as dt_time
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth.models import User

from dispatching.scheduler import check_feasibility, DriverDaySchedule, preload_timing_cache


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
