"""Out-of-service and permits, wired into the scheduling surfaces.

Run with:  ./manage.py test dispatching.tests_vehicle_status_wiring

The model layer is covered in tests_vehicle_status. This file is about the
promises those fields make to a dispatcher:

  * BLOCKED, NOT HIDDEN: an out-of-service unit stays on the planner where you
    can see WHY, and the server refuses to assign it.
  * OVERRIDABLE: the refusal names the reason and can be overruled by a human.
    This is the whole reason the block was allowed to exist at all — a stale
    flag must never strand a car that came back early.
  * NEVER PROPOSED: Day Setup doesn't suggest a car that's on a lift, and says
    so in its warnings instead of silently coming up a unit short.
  * NOT COPIED FORWARD: yesterday's plan doesn't drag a since-broken car into
    today.
  * PERMITS WARN, NEVER BLOCK: a missing MCO/SFB/Port decal is surfaced by name
    at assignment time and never refuses the assignment.
"""
from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from dispatching.day_setup import suggest_day_setup
from drivers.models import Driver, DriverVehicleAssignment, FleetVehicle
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation

DAY = timezone.localdate() + timedelta(days=4)


class _WiringFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.suv = Vehicle.objects.create(
            vehicle_type="suv", capacity=6, luggage_capacity=6)
        mco = Location.objects.create(name="MCO")
        disney = Location.objects.create(name="Disney")
        cls.route = Route.objects.create(
            origin=mco, destination=disney, inhouse_base_pay=Decimal("50.00"))
        cls.rate = Rate.objects.create(
            vehicle=cls.suv, route=cls.route,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"))
        cls.customer = Customer.objects.create(
            first_name="John", last_name="Doe", email="j@example.com",
            phone_number="5551234567")

        cls.george = Driver.objects.create(
            profile=User.objects.create_user("vw_george", first_name="George"),
            driver_type="inhouse")
        cls.sam = Driver.objects.create(
            profile=User.objects.create_user("vw_sam", first_name="Sam"),
            driver_type="inhouse")

        cls.staff = User.objects.create_user("vw_staff", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)

    def _unit(self, number="7", **kw):
        return FleetVehicle.objects.create(
            vehicle_number=number, vehicle_type=self.suv, year=2024,
            make="Chevrolet", model="Suburban", **kw)

    def _down(self, unit, reason="Transmission, at Bob's", until=None):
        unit.out_of_service_from = DAY - timedelta(days=1)
        unit.out_of_service_until = until
        unit.out_of_service_reason = reason
        unit.save()
        return unit

    def _assign(self, driver, unit, override=False):
        return self.client.post(
            reverse("update_inhouse_vehicle_assignment"),
            {"driver_id": driver.id, "date": DAY.isoformat(),
             "vehicle_id": unit.id, "override_oos": override},
            content_type="application/json")

    def _leg(self, pickup_location="MCO Terminal B", driver=None, day=DAY):
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=self.suv, base_price=Decimal("100.00"),
            total_price=Decimal("100.00"))
        return Leg.objects.create(
            reservation=res, pickup_date=day, pickup_time=time(9, 0),
            pickup_location=pickup_location, dropoff_location="Disney",
            route=self.route, status="confirmed", driver=driver)

    def _feasibility(self, leg, driver):
        return self.client.get(
            reverse("check_driver_feasibility"),
            {"leg_id": leg.id, "driver_id": driver.id},
        ).json()


class AssignmentBlockTests(_WiringFixture):
    def test_an_available_unit_assigns_normally(self):
        unit = self._unit()
        self.assertTrue(self._assign(self.george, unit).json()["success"])

    def test_an_out_of_service_unit_is_refused(self):
        unit = self._down(self._unit())
        resp = self._assign(self.george, unit)
        self.assertEqual(resp.status_code, 409)
        body = resp.json()
        self.assertFalse(body["success"])
        self.assertTrue(body["out_of_service"])
        self.assertFalse(DriverVehicleAssignment.objects.filter(date=DAY).exists())

    def test_the_refusal_names_the_unit_and_the_reason(self):
        """A dispatcher must be able to act on the refusal without going hunting."""
        unit = self._down(self._unit("9"), reason="Rear-ended 8/3")
        body = self._assign(self.george, unit).json()
        self.assertIn("#9", body["error"])
        self.assertIn("Rear-ended 8/3", body["error"])
        self.assertEqual(body["vehicle_number"], "9")

    def test_a_human_can_overrule_the_block(self):
        """The override is why blocking was acceptable at all: a forgotten flag
        must never strand a car that came back early."""
        unit = self._down(self._unit())
        self.assertTrue(body_ok(self._assign(self.george, unit, override=True)))
        self.assertTrue(
            DriverVehicleAssignment.objects.filter(
                driver=self.george, date=DAY, vehicle=unit).exists())

    def test_the_block_is_per_date_not_per_vehicle(self):
        """In the shop this week, back next week — next week must still work."""
        unit = self._unit()
        unit.out_of_service_from = DAY
        unit.out_of_service_until = DAY
        unit.out_of_service_reason = "Oil change"
        unit.save()
        blocked = self._assign(self.george, unit)
        self.assertEqual(blocked.status_code, 409)
        later = self.client.post(
            reverse("update_inhouse_vehicle_assignment"),
            {"driver_id": self.george.id,
             "date": (DAY + timedelta(days=1)).isoformat(), "vehicle_id": unit.id},
            content_type="application/json")
        self.assertTrue(later.json()["success"], later.content)

    def test_clearing_a_vehicle_is_never_blocked(self):
        """Taking a car off a driver must always work, even a broken car —
        otherwise a unit that breaks mid-day can't be handed back."""
        unit = self._unit()
        self._assign(self.george, unit)
        self._down(unit)
        resp = self.client.post(
            reverse("update_inhouse_vehicle_assignment"),
            {"driver_id": self.george.id, "date": DAY.isoformat(), "vehicle_id": None},
            content_type="application/json")
        self.assertTrue(resp.json()["success"], resp.content)
        self.assertFalse(DriverVehicleAssignment.objects.filter(date=DAY).exists())


class PlannerPoolTests(_WiringFixture):
    def _planner(self):
        self._leg()  # the VA panel only renders on a day that has legs
        return self.client.get(
            reverse("capacity_planner") + f"?date={DAY.isoformat()}")

    def test_an_out_of_service_unit_stays_in_the_pool_with_its_reason(self):
        """Hiding it would send the dispatcher hunting for a car that isn't there."""
        self._down(self._unit("7"), reason="Transmission, at Bob's")
        resp = self._planner()
        # The card attribute, not the class name — the class also lives in the
        # stylesheet, so it's present on every render.
        self.assertContains(resp, 'data-oos="1"')
        self.assertContains(resp, "Transmission, at Bob&#x27;s")
        self.assertEqual(resp.context["out_of_service_count"], 1)

    def test_an_out_of_service_card_stays_draggable(self):
        """Warn, don't wall it off. Refusing the drag would leave a dispatcher
        who knows the car is back with no way through; the drop asks instead."""
        self._down(self._unit())
        self.assertNotContains(self._planner(), 'draggable="false"')

    def test_a_healthy_pool_reports_none_out_of_service(self):
        self._unit()
        resp = self._planner()
        self.assertEqual(resp.context["out_of_service_count"], 0)
        self.assertFalse(any(v.oos_label for v in resp.context["inhouse_vehicles"]))
        # The pool's "N out of service" badge is inside an {% if %}; its title
        # text appears nowhere else, unlike the class names and the JS literals
        # (which both carry `data-oos="1"` on every render).
        self.assertNotContains(resp, "These units can&#x27;t be assigned")

    def test_available_units_sort_ahead_of_broken_ones(self):
        self._unit("1")
        self._down(self._unit("2"))
        self._unit("3")
        numbers = [v.vehicle_number for v in self._planner().context["inhouse_vehicles"]]
        self.assertEqual(numbers, ["1", "3", "2"])

    def test_permits_held_are_shown_on_the_pool_card(self):
        self._unit(permit_mco=True)
        self.assertContains(self._planner(), "va-permit ok")


class DaySetupTests(_WiringFixture):
    def test_an_out_of_service_unit_is_never_proposed(self):
        self._unit("1")
        broken = self._down(self._unit("2"))
        proposal = suggest_day_setup(DAY)
        proposed = {r.get("vehicle_id") for r in proposal.get("rows", [])}
        self.assertNotIn(broken.id, proposed)

    def test_day_setup_says_why_the_fleet_is_a_unit_short(self):
        """Coming up short silently is the failure mode worth preventing."""
        self._down(self._unit("2"), reason="Waiting on a part")
        warnings = " ".join(suggest_day_setup(DAY).get("warnings", []))
        self.assertIn("out of service", warnings.lower())
        self.assertIn("Waiting on a part", warnings)

    def test_apply_refuses_a_unit_that_broke_after_the_preview(self):
        unit = self._down(self._unit())
        resp = self.client.post(
            reverse("apply_day_setup"),
            {"date": DAY.isoformat(),
             "pairs": [{"driver_id": self.george.id, "vehicle_id": unit.id}],
             "snapshot": {}},
            content_type="application/json")
        self.assertEqual(resp.status_code, 409)
        self.assertIn("out of service", resp.json()["error"].lower())
        self.assertFalse(DriverVehicleAssignment.objects.filter(date=DAY).exists())

    def test_apply_still_works_for_healthy_units(self):
        unit = self._unit()
        resp = self.client.post(
            reverse("apply_day_setup"),
            {"date": DAY.isoformat(),
             "pairs": [{"driver_id": self.george.id, "vehicle_id": unit.id}],
             "snapshot": {}},
            content_type="application/json")
        self.assertTrue(resp.json().get("success"), resp.content)


class CopyPreviousDayTests(_WiringFixture):
    def test_a_since_broken_unit_is_not_copied_forward(self):
        unit = self._unit("7")
        DriverVehicleAssignment.objects.create(
            driver=self.george, date=DAY - timedelta(days=1), vehicle=unit)
        self._down(unit)
        body = self.client.post(
            reverse("copy_vehicle_assignments"),
            {"date": DAY.isoformat()}, content_type="application/json").json()
        self.assertTrue(body["success"])
        self.assertEqual(body["copied"], 0)
        self.assertEqual(body["skipped_out_of_service"], ["#7"])
        self.assertFalse(DriverVehicleAssignment.objects.filter(date=DAY).exists())

    def test_the_rest_of_the_day_still_copies(self):
        """One broken car must not cost you the whole plan."""
        broken, ok = self._unit("7"), self._unit("8")
        yesterday = DAY - timedelta(days=1)
        DriverVehicleAssignment.objects.create(
            driver=self.george, date=yesterday, vehicle=broken)
        DriverVehicleAssignment.objects.create(
            driver=self.sam, date=yesterday, vehicle=ok)
        self._down(broken)
        body = self.client.post(
            reverse("copy_vehicle_assignments"),
            {"date": DAY.isoformat()}, content_type="application/json").json()
        self.assertEqual(body["copied"], 1)
        self.assertTrue(DriverVehicleAssignment.objects.filter(
            driver=self.sam, date=DAY, vehicle=ok).exists())
        self.assertFalse(DriverVehicleAssignment.objects.filter(
            driver=self.george, date=DAY).exists())


class PermitWarningTests(_WiringFixture):
    """Advisory by decision: warn by name, never refuse."""

    def _with_unit(self, driver, **permit_kw):
        unit = self._unit(**permit_kw)
        DriverVehicleAssignment.objects.create(driver=driver, date=DAY, vehicle=unit)
        return unit

    def test_missing_mco_permit_warns_on_an_mco_pickup(self):
        self._with_unit(self.george)
        body = self._feasibility(self._leg("MCO Terminal B"), self.george)
        self.assertIn("MCO", body["permit_warning"])
        self.assertTrue(any("MCO" in w for w in body["warnings"]))

    def test_the_warning_never_blocks_the_assignment(self):
        self._with_unit(self.george)
        body = self._feasibility(self._leg("MCO Terminal B"), self.george)
        self.assertTrue(body["feasible"], body)

    def test_a_permitted_unit_produces_no_warning(self):
        self._with_unit(self.george, permit_mco=True)
        body = self._feasibility(self._leg("MCO Terminal B"), self.george)
        self.assertEqual(body["permit_warning"], "")

    def test_an_expired_permit_warns_and_names_the_date(self):
        expired_on = DAY - timedelta(days=10)
        self._with_unit(self.george, permit_mco=True, permit_mco_expires_on=expired_on)
        body = self._feasibility(self._leg("MCO Terminal B"), self.george)
        self.assertIn("expired", body["permit_warning"])
        self.assertIn(str(expired_on), body["permit_warning"])

    def test_the_warning_names_the_unit(self):
        self._with_unit(self.george)
        body = self._feasibility(self._leg("MCO Terminal B"), self.george)
        self.assertIn("#7", body["permit_warning"])

    def test_a_pickup_needing_no_permit_is_silent(self):
        self._with_unit(self.george)
        body = self._feasibility(self._leg("Disney's Grand Floridian"), self.george)
        self.assertEqual(body["permit_warning"], "")

    def test_a_driver_with_no_vehicle_reports_no_permit_problem(self):
        """No car assigned is already its own warning — don't stack a second,
        confusing one about a permit for a vehicle that doesn't exist."""
        body = self._feasibility(self._leg("MCO Terminal B"), self.george)
        self.assertEqual(body["permit_warning"], "")


class FleetEditEndpointTests(_WiringFixture):
    def _save(self, unit, **payload):
        return self.client.post(
            reverse("fleet_update_details", args=[unit.pk]),
            payload, content_type="application/json")

    def test_setting_an_out_of_service_window(self):
        unit = self._unit()
        resp = self._save(unit, out_of_service_from=DAY.isoformat(),
                          out_of_service_until=(DAY + timedelta(days=2)).isoformat(),
                          out_of_service_reason="Transmission")
        self.assertTrue(resp.json()["success"], resp.content)
        unit.refresh_from_db()
        self.assertTrue(unit.is_out_of_service_on(DAY))
        self.assertEqual(unit.out_of_service_reason, "Transmission")

    def test_clearing_the_start_date_puts_the_unit_back_in_service(self):
        unit = self._down(self._unit())
        self._save(unit, out_of_service_from="", out_of_service_until="")
        unit.refresh_from_db()
        self.assertFalse(unit.is_out_of_service_on(DAY))

    def test_a_backwards_window_is_refused(self):
        """It would match no date at all — the form would look set and the unit
        would stay bookable everywhere else."""
        unit = self._unit()
        resp = self._save(unit, out_of_service_from=DAY.isoformat(),
                          out_of_service_until=(DAY - timedelta(days=3)).isoformat())
        self.assertEqual(resp.status_code, 400)
        unit.refresh_from_db()
        self.assertIsNone(unit.out_of_service_from)

    def test_an_end_date_with_no_start_is_refused(self):
        unit = self._unit()
        resp = self._save(unit, out_of_service_until=DAY.isoformat())
        self.assertEqual(resp.status_code, 400)

    def test_saving_permits_and_expiries(self):
        unit = self._unit()
        expiry = DAY + timedelta(days=200)
        resp = self._save(unit, permit_mco=True,
                          permit_mco_expires_on=expiry.isoformat(),
                          permit_port_canaveral=True)
        self.assertTrue(resp.json()["success"], resp.content)
        unit.refresh_from_db()
        self.assertTrue(unit.permit_mco)
        self.assertEqual(unit.permit_mco_expires_on, expiry)
        self.assertTrue(unit.permit_port_canaveral)
        self.assertFalse(unit.permit_sanford)

    def test_unticking_a_permit_clears_its_expiry(self):
        """A permit that comes back must not inherit the old date."""
        unit = self._unit(permit_mco=True,
                          permit_mco_expires_on=DAY + timedelta(days=30))
        self._save(unit, permit_mco=False)
        unit.refresh_from_db()
        self.assertFalse(unit.permit_mco)
        self.assertIsNone(unit.permit_mco_expires_on)

    def test_a_bad_expiry_date_is_refused(self):
        unit = self._unit()
        resp = self._save(unit, permit_mco=True, permit_mco_expires_on="whenever")
        self.assertEqual(resp.status_code, 400)

    def test_non_staff_cannot_edit(self):
        unit = self._unit()
        self.client.force_login(
            User.objects.create_user("vw_grunt", password="x"))
        resp = self._save(unit, out_of_service_from=DAY.isoformat())
        self.assertIn(resp.status_code, (302, 403))
        unit.refresh_from_db()
        self.assertIsNone(unit.out_of_service_from)


def body_ok(response):
    return response.json().get("success") is True


class EverySurfaceShowsItTests(_WiringFixture):
    """The bug this class exists to prevent: the planner greyed #001 out while
    the legs dashboard drew it as a perfectly normal card. A car marked down has
    to look the same wherever it appears, or nobody trusts either page.

    All three pools go through views._annotate_vehicle_status for exactly that
    reason.
    """

    def _legs_dashboard(self):
        return self.client.get(reverse("dashboard") + f"?date={DAY.isoformat()}")

    def _planner(self):
        self._leg()  # the VA panel only renders on a day that has legs
        return self.client.get(
            reverse("capacity_planner") + f"?date={DAY.isoformat()}")

    def _board(self):
        return self.client.get(
            reverse("schedule_board") + f"?date={DAY.isoformat()}")

    # NOTE on assertions: `data-oos="1"` and the va-oos class appear as literals
    # in every render (the drop handler builds a chip with them, the stylesheet
    # defines them), so presence/absence is asserted on the server-rendered
    # context or on the reason text, never on those markers.

    def test_legs_dashboard_pool_marks_it(self):
        """The surface in the bug report."""
        self._down(self._unit("1"), reason="Transmission")
        resp = self._legs_dashboard()
        self.assertTrue(any(v.oos_label for v in resp.context["inhouse_vehicles"]))
        self.assertContains(resp, "Transmission")

    def test_planner_pool_marks_it(self):
        self._down(self._unit("1"), reason="Transmission")
        resp = self._planner()
        self.assertTrue(any(v.oos_label for v in resp.context["inhouse_vehicles"]))
        self.assertContains(resp, "Transmission")

    def test_both_pools_agree(self):
        """Same unit, same date, same verdict on both pages."""
        self._down(self._unit("1"), reason="Transmission")
        self._unit("2")
        planner = {v.vehicle_number: bool(v.oos_label)
                   for v in self._planner().context["inhouse_vehicles"]}
        dash = {v.vehicle_number: bool(v.oos_label)
                for v in self._legs_dashboard().context["inhouse_vehicles"]}
        self.assertEqual(planner, dash)
        self.assertEqual(planner, {"1": True, "2": False})

    def test_an_out_of_service_card_is_still_draggable(self):
        """Warn, don't wall it off — a dispatcher who knows the car is back must
        have a way through."""
        self._down(self._unit("1"))
        for resp in (self._planner(), self._legs_dashboard()):
            self.assertNotContains(resp, 'draggable="false"')

    def test_the_hover_popup_carries_the_reason(self):
        self._down(self._unit("1"), reason="Rear-ended 8/3")
        for resp in (self._planner(), self._legs_dashboard()):
            self.assertContains(resp, 'data-oos-label="Rear-ended 8/3 — no return date"')

    def test_an_already_assigned_car_going_down_marks_the_driver_chip(self):
        """A car can be marked down AFTER it's assigned. The chip on the driver's
        card has to change too, not just the pool."""
        unit = self._unit("1")
        DriverVehicleAssignment.objects.create(
            driver=self.george, date=DAY, vehicle=unit)
        self._down(unit, reason="Transmission")

        planner_row = next(r for r in self._planner().context["vehicle_assign_rows"]
                           if r["driver"].id == self.george.id)
        self.assertIn("Transmission", planner_row["vehicle_oos_label"])

        dash_row = next(r for r in self._legs_dashboard().context["inhouse_driver_rows"]
                        if r["driver"].id == self.george.id)
        self.assertIn("Transmission", dash_row["vehicle_oos_label"])

    def test_the_schedule_board_flags_a_driver_holding_a_down_car(self):
        unit = self._unit("1")
        DriverVehicleAssignment.objects.create(
            driver=self.george, date=DAY, vehicle=unit)
        self._leg(driver=self.george)
        self._down(unit, reason="Transmission")
        resp = self._board()
        row = next(r for r in resp.context["inhouse_timeline"]
                   if r["driver"].id == self.george.id)
        self.assertIn("Transmission", row["vehicle_oos_label"])
        self.assertContains(resp, "Car out of service")

    def test_a_healthy_car_is_flagged_nowhere(self):
        unit = self._unit("1")
        DriverVehicleAssignment.objects.create(
            driver=self.george, date=DAY, vehicle=unit)
        self._leg(driver=self.george)
        for resp in (self._planner(), self._legs_dashboard()):
            self.assertFalse(any(v.oos_label for v in resp.context["inhouse_vehicles"]))
        planner_row = next(r for r in self._planner().context["vehicle_assign_rows"]
                           if r["driver"].id == self.george.id)
        self.assertEqual(planner_row["vehicle_oos_label"], "")
        board = self._board()
        board_row = next(r for r in board.context["inhouse_timeline"]
                         if r["driver"].id == self.george.id)
        self.assertEqual(board_row["vehicle_oos_label"], "")
        self.assertNotContains(board, "Car out of service")

    def test_the_flag_is_per_date_on_every_surface(self):
        """In the shop today, fine next week — every page must agree on that."""
        unit = self._unit("1")
        unit.out_of_service_from = DAY
        unit.out_of_service_until = DAY
        unit.out_of_service_reason = "Oil change"
        unit.save()
        later = (DAY + timedelta(days=7)).isoformat()
        for url in (reverse("dashboard"), reverse("capacity_planner")):
            resp = self.client.get(f"{url}?date={later}")
            flagged = [v for v in resp.context["inhouse_vehicles"] if v.oos_label]
            self.assertEqual(flagged, [], f"{url} still flags a unit that is back")
