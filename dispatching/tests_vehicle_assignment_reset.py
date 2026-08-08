"""Vehicle assignments — Reset All (``reset-vehicle-assignments/``).

Run with:  ./manage.py test dispatching.tests_vehicle_assignment_reset

What must hold:
  * PREVIEW FIRST: the endpoint answers "what would this destroy?" without
    destroying it, so the confirm modal can name every driver before you commit.
  * THE WORK SURVIVES: a reset clears the CAR, never the trips. Legs stay on
    their drivers — the preview counts them precisely so the dispatcher is warned
    about the driver who ends up with 4 jobs and no vehicle.
  * SCOPED TO THE DATE: yesterday's and tomorrow's setups are untouched.
  * LOCKED DOWN: staff-only, POST-only, and a bad date destroys nothing.
"""
from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from drivers.models import Driver, DriverVehicleAssignment, FleetVehicle
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation

DAY = timezone.localdate() + timedelta(days=3)


class _ResetFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.vehicle = Vehicle.objects.create(
            vehicle_type="towncar", capacity=4, luggage_capacity=4)
        origin = Location.objects.create(name="MCO")
        dest = Location.objects.create(name="Disney")
        cls.route = Route.objects.create(
            origin=origin, destination=dest, inhouse_base_pay=Decimal("50.00"))
        cls.rate = Rate.objects.create(
            vehicle=cls.vehicle, route=cls.route,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"))
        cls.customer = Customer.objects.create(
            first_name="John", last_name="Doe", email="j@example.com",
            phone_number="5551234567")

        def _driver(username, first_name):
            return Driver.objects.create(
                profile=User.objects.create_user(username, first_name=first_name),
                driver_type="inhouse")

        def _unit(number):
            return FleetVehicle.objects.create(
                vehicle_number=number, vehicle_type=cls.vehicle, year=2024,
                make="Lincoln", model="Continental")

        cls.george = _driver("vr_george", "George")
        cls.sam = _driver("vr_sam", "Sam")
        cls.nora = _driver("vr_nora", "Nora")
        cls.unit7, cls.unit8, cls.unit9 = _unit("7"), _unit("8"), _unit("9")

        cls.staff = User.objects.create_user("vr_staff", password="x", is_staff=True)
        cls.grunt = User.objects.create_user("vr_grunt", password="x")

    def setUp(self):
        self.client.force_login(self.staff)
        self.url = reverse("reset_vehicle_assignments")

    def _assign(self, driver, unit, day=DAY):
        return DriverVehicleAssignment.objects.create(
            driver=driver, date=day, vehicle=unit)

    def _leg(self, driver, pickup_time=time(9, 0), day=DAY, **kw):
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=Decimal("100.00"),
            total_price=Decimal("100.00"), **kw.pop("reservation_kw", {}))
        return Leg.objects.create(
            reservation=res, pickup_date=day, pickup_time=pickup_time,
            pickup_location="MCO", dropoff_location="Disney", route=self.route,
            status=kw.pop("status", "confirmed"), driver=driver)

    def _post(self, **payload):
        payload.setdefault("date", DAY.isoformat())
        return self.client.post(self.url, payload, content_type="application/json")

    def _preview(self, **payload):
        return self._post(preview=True, **payload).json()

    def _setup_day(self):
        self._assign(self.george, self.unit7)
        self._assign(self.sam, self.unit8)
        self._assign(self.nora, self.unit9)


class ResetPreviewTests(_ResetFixture):
    def test_preview_lists_everyone_losing_a_vehicle(self):
        self._setup_day()
        body = self._preview()
        self.assertTrue(body["success"])
        self.assertEqual(body["total"], 3)
        self.assertEqual({d["driver_name"] for d in body["drivers"]},
                         {"George", "Sam", "Nora"})
        self.assertEqual({d["vehicle_number"] for d in body["drivers"]},
                         {"7", "8", "9"})

    def test_preview_destroys_nothing(self):
        """The whole point of the two-step: looking must be free."""
        self._setup_day()
        self._preview()
        self.assertEqual(DriverVehicleAssignment.objects.filter(date=DAY).count(), 3)

    def test_preview_counts_the_jobs_each_driver_keeps(self):
        self._setup_day()
        self._leg(self.george, time(8, 0))
        self._leg(self.george, time(14, 0))
        self._leg(self.sam, time(10, 0))
        body = self._preview()
        counts = {d["driver_name"]: d["leg_count"] for d in body["drivers"]}
        self.assertEqual(counts, {"George": 2, "Sam": 1, "Nora": 0})
        self.assertEqual(body["with_jobs"], 2)

    def test_cancelled_legs_do_not_raise_the_alarm(self):
        """A cancelled trip is not work anyone has to cover."""
        self._setup_day()
        self._leg(self.george, status="cancelled")
        body = self._preview()
        counts = {d["driver_name"]: d["leg_count"] for d in body["drivers"]}
        self.assertEqual(counts["George"], 0)
        self.assertEqual(body["with_jobs"], 0)

    def test_cancelled_reservation_does_not_raise_the_alarm(self):
        self._setup_day()
        self._leg(self.george, reservation_kw={"status": "cancelled"})
        self.assertEqual(self._preview()["with_jobs"], 0)

    def test_another_days_jobs_are_not_counted(self):
        self._setup_day()
        self._leg(self.george, day=DAY + timedelta(days=1))
        self.assertEqual(self._preview()["with_jobs"], 0)

    def test_preview_on_an_empty_day_reports_nothing_to_do(self):
        body = self._preview()
        self.assertTrue(body["success"])
        self.assertEqual(body["total"], 0)
        self.assertEqual(body["drivers"], [])


class ResetApplyTests(_ResetFixture):
    def test_reset_clears_every_assignment_for_the_date(self):
        self._setup_day()
        body = self._post().json()
        self.assertTrue(body["success"])
        self.assertEqual(body["cleared"], 3)
        self.assertFalse(DriverVehicleAssignment.objects.filter(date=DAY).exists())

    def test_reset_leaves_the_work_alone(self):
        """Clearing the car must never cancel the trip."""
        self._setup_day()
        leg = self._leg(self.george)
        self._post()
        leg.refresh_from_db()
        self.assertEqual(leg.driver_id, self.george.id)
        self.assertEqual(leg.status, "confirmed")

    def test_other_dates_are_untouched(self):
        self._setup_day()
        yesterday = self._assign(self.george, self.unit7, day=DAY - timedelta(days=1))
        tomorrow = self._assign(self.sam, self.unit8, day=DAY + timedelta(days=1))
        self._post()
        self.assertTrue(DriverVehicleAssignment.objects.filter(id=yesterday.id).exists())
        self.assertTrue(DriverVehicleAssignment.objects.filter(id=tomorrow.id).exists())

    def test_reset_takes_planned_share_windows_with_it(self):
        """An AM/PM split describes an assignment that no longer exists."""
        DriverVehicleAssignment.objects.create(
            driver=self.george, date=DAY, vehicle=self.unit7,
            planned_start_hour=4, planned_end_hour=15)
        DriverVehicleAssignment.objects.create(
            driver=self.sam, date=DAY, vehicle=self.unit7,
            planned_start_hour=15, planned_end_hour=23)
        self.assertEqual(self._post().json()["cleared"], 2)
        self.assertFalse(DriverVehicleAssignment.objects.filter(date=DAY).exists())

    def test_reset_frees_the_units_for_reassignment(self):
        self._setup_day()
        self._post()
        resp = self.client.post(
            reverse("update_inhouse_vehicle_assignment"),
            {"driver_id": self.sam.id, "date": DAY.isoformat(),
             "vehicle_id": self.unit7.id},
            content_type="application/json")
        self.assertTrue(resp.json()["success"], resp.content)

    def test_reset_on_an_empty_day_is_a_harmless_no_op(self):
        body = self._post().json()
        self.assertTrue(body["success"])
        self.assertEqual(body["cleared"], 0)

    def test_reset_is_idempotent(self):
        self._setup_day()
        self.assertEqual(self._post().json()["cleared"], 3)
        self.assertEqual(self._post().json()["cleared"], 0)

    def test_a_row_with_no_vehicle_still_counts_as_cleared(self):
        """Day Setup can leave a driver row holding a null vehicle. It is still a
        row on the date and a reset must not leave it behind."""
        DriverVehicleAssignment.objects.create(driver=self.nora, date=DAY, vehicle=None)
        self.assertEqual(self._post().json()["cleared"], 1)
        self.assertFalse(DriverVehicleAssignment.objects.filter(date=DAY).exists())

    def test_preview_survives_a_null_vehicle_row(self):
        DriverVehicleAssignment.objects.create(driver=self.nora, date=DAY, vehicle=None)
        row = self._preview()["drivers"][0]
        self.assertEqual(row["vehicle_number"], "")
        self.assertEqual(row["vehicle_type"], "")


class ResetGuardTests(_ResetFixture):
    def test_non_staff_cannot_reset(self):
        self._setup_day()
        self.client.force_login(self.grunt)
        self.assertEqual(self._post().status_code, 403)
        self.assertEqual(DriverVehicleAssignment.objects.filter(date=DAY).count(), 3)

    def test_anonymous_is_redirected_and_destroys_nothing(self):
        self._setup_day()
        self.client.logout()
        self.assertEqual(self._post().status_code, 302)
        self.assertEqual(DriverVehicleAssignment.objects.filter(date=DAY).count(), 3)

    def test_get_is_rejected(self):
        self._setup_day()
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assertEqual(DriverVehicleAssignment.objects.filter(date=DAY).count(), 3)

    def test_missing_date_destroys_nothing(self):
        self._setup_day()
        resp = self.client.post(self.url, {}, content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(DriverVehicleAssignment.objects.filter(date=DAY).count(), 3)

    def test_malformed_date_destroys_nothing(self):
        self._setup_day()
        resp = self.client.post(self.url, {"date": "not-a-date"},
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(DriverVehicleAssignment.objects.filter(date=DAY).count(), 3)

    def test_invalid_json_destroys_nothing(self):
        self._setup_day()
        resp = self.client.post(self.url, "{not json",
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(DriverVehicleAssignment.objects.filter(date=DAY).count(), 3)


class ResetButtonTests(_ResetFixture):
    """The planner is where the day is built, so that's where the reset lives —
    alongside the two buttons that build a plan."""

    def _planner(self):
        # The whole Vehicle Assignments panel is gated on the day having legs,
        # so a bare date renders the "No jobs scheduled" empty state instead.
        self._leg(self.george)
        return self.client.get(
            reverse("capacity_planner") + f"?date={DAY.isoformat()}")

    def test_planner_renders_the_reset_button(self):
        resp = self._planner()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="vaResetAll"')
        self.assertContains(resp, "Reset All")

    def test_reset_sits_with_the_other_setup_actions(self):
        html = self._planner().content.decode()
        self.assertLess(html.index('id="vaCopyPrev"'), html.index('id="vaResetAll"'),
                        "Reset must come last — it destroys the plan the others build")
