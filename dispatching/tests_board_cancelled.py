"""Schedule board — cancelled jobs stay visible as ghosts, and the board
notices when the day changes underneath it.

Founder, 2026-09-21: the 4:42 PM job was cancelled and the board showed it
looking live. Two causes. The board dropped cancelled legs from its query, so
after a reload the job simply vanished with nothing to say why; and the page
never refreshed its chips after loading, so until a reload the dead job sat
there as if it were real.

What must hold:
  * A cancelled leg on a driver renders in that driver's row as an amber ghost
    with the time struck through, not draggable, and is NOT in the row's job
    count or the header counts.
  * A cancelled leg with no driver renders the same way in the Unassigned row.
  * A leg whose whole reservation was cancelled counts as cancelled too.
  * The board carries a fingerprint; the version endpoint returns the same one
    until something on the day changes, then a different one.
"""
from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from dispatching.scheduler import preload_timing_cache
from drivers.models import Driver, DriverVehicleAssignment, FleetVehicle
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation

DAY = timezone.localdate() + timedelta(days=5)


class _BoardFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        preload_timing_cache()
        cls.vehicle = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
        cls.route = Route.objects.create(
            origin=Location.objects.create(name="MCO"),
            destination=Location.objects.create(name="Disney"),
            inhouse_base_pay=Decimal("50.00"))
        cls.rate = Rate.objects.create(
            vehicle=cls.vehicle, route=cls.route,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"))
        cls.customer = Customer.objects.create(
            first_name="Angeline", last_name="Owens", email="a@example.com", phone_number="5551234567")
        cls.george = Driver.objects.create(
            profile=User.objects.create_user("bc_george", first_name="George"), driver_type="inhouse")
        DriverVehicleAssignment.objects.create(
            driver=cls.george, date=DAY,
            vehicle=FleetVehicle.objects.create(
                vehicle_number="7", vehicle_type=cls.vehicle, year=2024, make="Chevrolet", model="Suburban"))
        cls.staff = User.objects.create_user("bc_staff", password="x", is_staff=True, first_name="Iris")

    def setUp(self):
        self.client.force_login(self.staff)

    def _leg(self, pickup_time=time(9, 0), driver=None, status="confirmed"):
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=Decimal("100.00"), total_price=Decimal("100.00"))
        return Leg.objects.create(
            reservation=res, pickup_date=DAY, pickup_time=pickup_time,
            pickup_location="MCO", dropoff_location="Disney", route=self.route,
            status=status, driver=driver)

    def _board(self):
        return self.client.get(reverse("schedule_board") + f"?date={DAY.isoformat()}")

    def _george_row(self, resp):
        return next(r for r in resp.context["inhouse_timeline"] if r["driver"].id == self.george.id)


class CancelledGhostTests(_BoardFixture):
    def test_a_cancelled_job_stays_in_the_drivers_row_as_a_ghost(self):
        live = self._leg(time(9, 0), driver=self.george)
        dead = self._leg(time(16, 42), driver=self.george)
        dead.status = "cancelled"
        dead._history_user = self.staff   # what the request middleware sets on a real cancel
        dead.save()

        resp = self._board()
        row = self._george_row(resp)

        self.assertEqual(row["total_legs"], 1)
        self.assertEqual([c["leg_id"] for c in row["cancelled_slots"]], [dead.id])
        ghost = row["cancelled_slots"][0]
        self.assertEqual(ghost["pickup_short"], "4:42")
        self.assertEqual(ghost["vehicle_abbr"], "SUV")
        self.assertTrue(ghost["cancelled_label"].startswith("Cancelled "))
        self.assertIn("Iris", ghost["cancelled_label"])
        # The ghost gets its own lane under the real ones, and the row grows for it.
        self.assertGreater(row["row_bar_height"], row["cancelled_lane_top"])
        # Header counts never include it.
        self.assertEqual(resp.context["total_legs"], 1)
        self.assertEqual(resp.context["assigned_count"], 1)
        # And the chip itself is on the page, marked, and not draggable.
        html = resp.content.decode()
        self.assertIn('class="timeline-slot tl-cancelled" data-leg-id="%d"' % dead.id, html)
        self.assertNotIn('data-leg-id="%d"\n                         draggable' % dead.id, html)
        self.assertIn("1 cancelled", html)

    def test_an_unassigned_cancelled_job_ghosts_in_the_unassigned_row(self):
        dead = self._leg(time(16, 42))
        dead.status = "cancelled"
        dead.save()

        resp = self._board()

        self.assertEqual([c["leg_id"] for c in resp.context["unassigned_cancelled_slots"]], [dead.id])
        self.assertEqual(resp.context["unassigned_timeline_slots"], [])
        self.assertEqual(resp.context["unassigned_count"], 0)
        self.assertGreaterEqual(resp.context["unassigned_lane_height"], 36)
        self.assertContains(resp, "cancelled")

    def test_a_cancelled_reservation_counts_as_cancelled(self):
        leg = self._leg(time(10, 0), driver=self.george)
        Reservation.objects.filter(pk=leg.reservation_id).update(status="cancelled")

        resp = self._board()
        row = self._george_row(resp)

        self.assertEqual(row["total_legs"], 0)
        self.assertEqual(len(row["cancelled_slots"]), 1)
        self.assertTrue(row["cancelled_slots"][0]["whole_reservation"])

    def test_a_live_day_has_no_ghosts_and_no_extra_height(self):
        self._leg(time(9, 0), driver=self.george)
        resp = self._board()
        row = self._george_row(resp)
        self.assertEqual(row["cancelled_slots"], [])
        self.assertEqual(resp.context["unassigned_cancelled_slots"], [])
        self.assertNotIn("tl-cancelled\"", resp.content.decode().split("<style")[-1].split("</style>")[-1])


class BoardVersionTests(_BoardFixture):
    def _version(self):
        resp = self.client.get(reverse("schedule_board_version") + f"?date={DAY.isoformat()}")
        self.assertEqual(resp.status_code, 200)
        return resp.json()["version"]

    def test_the_page_and_the_endpoint_agree_and_hold_still(self):
        self._leg(time(9, 0), driver=self.george)
        page = self._board().context["board_version"]
        self.assertEqual(page, self._version())
        self.assertEqual(page, self._version())

    def test_cancelling_a_job_moves_the_version(self):
        leg = self._leg(time(16, 42), driver=self.george)
        before = self._version()
        leg.status = "cancelled"
        leg.save()
        self.assertNotEqual(before, self._version())

    def test_cancelling_the_reservation_moves_the_version(self):
        leg = self._leg(time(16, 42), driver=self.george)
        before = self._version()
        leg.reservation.status = "cancelled"
        leg.reservation.save()
        self.assertNotEqual(before, self._version())

    def test_a_bad_date_falls_back_to_today(self):
        resp = self.client.get(reverse("schedule_board_version") + "?date=nope")
        self.assertEqual(resp.json()["date"], timezone.localdate().isoformat())

    def test_the_page_polls_the_endpoint(self):
        html = self._board().content.decode()
        self.assertIn("BOARD_VERSION_URL", html)
        self.assertIn(reverse("schedule_board_version"), html)
