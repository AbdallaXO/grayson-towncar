"""Schedule board — filter by driver (``?driver=<id>``).

Run with:  ./manage.py test dispatching.tests_board_driver_filter

What must hold:
  * FOCUS: ``?driver=<id>`` leaves exactly that driver's lane on the board.
  * THE DAY IS STILL THE DAY: the Unassigned lane, the header counts and the
    timeline axis are untouched by the filter. A filtered board must never read
    as a quiet day, and the backlog must stay droppable — handing the focused
    driver a job off it is the main reason to filter in the first place.
  * HONEST OPTIONS: the dropdown lists exactly the drivers who have a lane on
    this board, this date — never an off driver, never the other board's roster.
  * GRACEFUL FALLBACK: a stale/hand-typed/off-today driver falls back to the
    whole board and says so, instead of rendering an empty one.
  * The filter survives date navigation and is dropped by the board switch.
"""
from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from dispatching.scheduler import preload_timing_cache
from drivers.models import (AffiliateProfile, Driver, DriverDateOverride,
                            DriverVehicleAssignment, FleetVehicle)
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation

DAY = timezone.localdate() + timedelta(days=5)


class _BoardFilterFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        preload_timing_cache()
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
            first_name="John", last_name="Doe", email="john@example.com",
            phone_number="5551234567")

        def _inhouse(username, first_name, vehicle_number=None):
            d = Driver.objects.create(
                profile=User.objects.create_user(username, first_name=first_name),
                driver_type="inhouse")
            if vehicle_number:
                DriverVehicleAssignment.objects.create(
                    driver=d, date=DAY,
                    vehicle=FleetVehicle.objects.create(
                        vehicle_number=vehicle_number, vehicle_type=cls.vehicle,
                        year=2024, make="Lincoln", model="Continental"))
            return d

        cls.george = _inhouse("bf_george", "George", "7")
        cls.sam = _inhouse("bf_sam", "Sam", "8")
        cls.nora = _inhouse("bf_nora", "Nora")  # working, no vehicle yet

        cls.waleed = Driver.objects.create(
            profile=User.objects.create_user("bf_waleed", first_name="Waleed"),
            driver_type="affiliate")
        AffiliateProfile.objects.create(driver=cls.waleed, capacity_mode="single_chain")

        cls.staff = User.objects.create_user("bf_staff", password="x", is_staff=True)

    def setUp(self):
        self.client.force_login(self.staff)

    def _leg(self, pickup_time=time(9, 0), driver=None, day=DAY):
        res = Reservation.objects.create(
            trip_type="one-way", customer=self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=Decimal("100.00"),
            total_price=Decimal("100.00"))
        return Leg.objects.create(
            reservation=res, pickup_date=day, pickup_time=pickup_time,
            pickup_location="MCO", dropoff_location="Disney", route=self.route,
            status="confirmed", driver=driver)

    def _board(self, driver=None, view=None, day=DAY):
        url = reverse("schedule_board") + f"?date={day.isoformat()}"
        if view:
            url += f"&view={view}"
        if driver is not None:
            url += f"&driver={driver}"
        return self.client.get(url)

    @staticmethod
    def _row_driver_ids(resp):
        return {r["driver"].id for r in resp.context["inhouse_timeline"]}

    @staticmethod
    def _option_ids(resp):
        return {o["id"] for o in resp.context["board_driver_options"]}


class DriverFilterTests(_BoardFilterFixture):
    def test_no_filter_shows_every_driver(self):
        resp = self._board()
        self.assertEqual(resp.context["driver_filter"], "")
        self.assertEqual(
            self._row_driver_ids(resp), {self.george.id, self.sam.id, self.nora.id})

    def test_filter_leaves_only_that_drivers_lane(self):
        self._leg(driver=self.george)
        self._leg(pickup_time=time(11, 0), driver=self.sam)
        resp = self._board(driver=self.george.id)
        self.assertEqual(self._row_driver_ids(resp), {self.george.id})
        self.assertEqual(resp.context["driver_filter"], str(self.george.id))
        self.assertEqual(resp.context["filtered_driver_name"], str(self.george))
        self.assertEqual(resp.context["filtered_driver_legs"], 1)

    def test_filtered_driver_keeps_all_of_his_own_jobs(self):
        for h in (7, 12, 18):
            self._leg(pickup_time=time(h, 0), driver=self.george)
        resp = self._board(driver=self.george.id)
        row = resp.context["inhouse_timeline"][0]
        self.assertEqual(row["total_legs"], 3)
        self.assertEqual(len(row["schedule"].slots), 3)

    def test_unassigned_lane_survives_the_filter(self):
        """Focusing on a driver is usually a prelude to giving him a backlog job,
        so hiding the backlog would defeat the feature."""
        self._leg(driver=self.george)
        self._leg(pickup_time=time(10, 0))   # unassigned
        self._leg(pickup_time=time(13, 0))   # unassigned
        resp = self._board(driver=self.george.id)
        self.assertEqual(len(resp.context["unassigned_timeline_slots"]), 2)

    def test_header_counts_stay_whole_day(self):
        """A filtered board must not read as a quiet day."""
        self._leg(driver=self.george)
        self._leg(pickup_time=time(11, 0), driver=self.sam)
        self._leg(pickup_time=time(13, 0))
        resp = self._board(driver=self.george.id)
        self.assertEqual(resp.context["total_legs"], 3)
        self.assertEqual(resp.context["assigned_count"], 2)
        self.assertEqual(resp.context["unassigned_count"], 1)

    def test_axis_is_unchanged_by_the_filter(self):
        """Same axis filtered or not, so an unassigned chip still lines up with
        the gap in the focused driver's lane."""
        self._leg(pickup_time=time(6, 0), driver=self.sam)
        self._leg(pickup_time=time(21, 0), driver=self.sam)
        self._leg(pickup_time=time(12, 0), driver=self.george)
        full = self._board()
        focused = self._board(driver=self.george.id)
        self.assertEqual(focused.context["board_display_start"],
                         full.context["board_display_start"])
        self.assertEqual(focused.context["board_total_minutes"],
                         full.context["board_total_minutes"])

    def test_affiliate_board_filters_too(self):
        self._leg(driver=self.waleed)
        resp = self._board(driver=self.waleed.id, view="affiliate")
        self.assertEqual(self._row_driver_ids(resp), {self.waleed.id})
        self.assertEqual(resp.context["board_view"], "affiliate")

    def test_affiliate_roster_counts_ignore_the_filter(self):
        """The 'N/M working' badge describes the bench, not the current view."""
        resp = self._board(driver=self.waleed.id, view="affiliate")
        self.assertEqual(resp.context["affiliate_roster_count"], 1)

    def test_no_vehicle_divider_is_dropped_when_focused(self):
        """The divider is a header over a group. Filtering to the first no-vehicle
        driver would otherwise leave it captioning a single row."""
        unfiltered = self._board()
        self.assertTrue(any(r.get("starts_no_vehicle_group")
                            for r in unfiltered.context["inhouse_timeline"]))
        resp = self._board(driver=self.nora.id)
        self.assertFalse(resp.context["inhouse_timeline"][0].get("starts_no_vehicle_group"))
        self.assertNotContains(resp, "Available — no vehicle assigned")


class DriverFilterOptionTests(_BoardFilterFixture):
    def test_options_are_the_drivers_who_have_a_lane(self):
        resp = self._board()
        self.assertEqual(
            self._option_ids(resp), {self.george.id, self.sam.id, self.nora.id})

    def test_options_carry_the_days_job_count(self):
        self._leg(driver=self.george)
        self._leg(pickup_time=time(11, 0), driver=self.george)
        resp = self._board()
        opts = {o["id"]: o for o in resp.context["board_driver_options"]}
        self.assertEqual(opts[self.george.id]["total_legs"], 2)
        self.assertEqual(opts[self.sam.id]["total_legs"], 0)
        self.assertEqual(opts[self.george.id]["vehicle_number"], "7")

    def test_options_are_alphabetical(self):
        labels = [o["label"] for o in self._board().context["board_driver_options"]]
        self.assertEqual(labels, sorted(labels, key=str.lower))

    def test_options_stay_populated_while_filtered(self):
        """You must be able to switch straight from one driver to another."""
        resp = self._board(driver=self.george.id)
        self.assertEqual(
            self._option_ids(resp), {self.george.id, self.sam.id, self.nora.id})

    def test_options_never_cross_the_two_boards(self):
        inhouse = self._option_ids(self._board())
        affiliate = self._option_ids(self._board(view="affiliate"))
        self.assertNotIn(self.waleed.id, inhouse)
        self.assertEqual(affiliate, {self.waleed.id})

    def test_off_driver_with_no_jobs_is_not_offered(self):
        """He has no lane on the board, so focusing on him would show nothing."""
        DriverDateOverride.objects.create(
            driver=self.sam, date=DAY, exception_type="off", status="approved")
        resp = self._board()
        self.assertNotIn(self.sam.id, self._option_ids(resp))
        self.assertNotIn(self.sam.id, self._row_driver_ids(resp))

    def test_off_driver_still_holding_jobs_is_offered(self):
        DriverDateOverride.objects.create(
            driver=self.sam, date=DAY, exception_type="off", status="approved")
        self._leg(driver=self.sam)
        resp = self._board()
        self.assertIn(self.sam.id, self._option_ids(resp))
        self.assertEqual(self._row_driver_ids(self._board(driver=self.sam.id)),
                         {self.sam.id})

    def test_selector_renders_with_the_current_choice_marked(self):
        resp = self._board(driver=self.george.id)
        self.assertContains(resp, 'id="boardDriverFilter"')
        self.assertContains(resp, f'<option value="{self.george.id}" selected>')


class DriverFilterFallbackTests(_BoardFilterFixture):
    """A filter that cannot be honoured must show the whole board and say why —
    never an empty one, and never a silent reset."""

    def test_unknown_id_falls_back_to_the_whole_board(self):
        resp = self._board(driver=999999)
        self.assertEqual(resp.context["driver_filter"], "")
        self.assertEqual(
            self._row_driver_ids(resp), {self.george.id, self.sam.id, self.nora.id})
        self.assertEqual(resp.context["driver_filter_dropped"], "That driver")

    def test_non_numeric_id_is_ignored(self):
        resp = self._board(driver="'; DROP TABLE--")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["driver_filter"], "")
        self.assertEqual(len(resp.context["inhouse_timeline"]), 3)

    def test_empty_driver_param_is_treated_as_no_filter(self):
        resp = self._board(driver="")
        self.assertEqual(resp.context["driver_filter"], "")
        self.assertEqual(resp.context["driver_filter_dropped"], "")
        self.assertEqual(len(resp.context["inhouse_timeline"]), 3)

    def test_driver_off_on_this_date_is_named_in_the_fallback(self):
        """Stepping a focused board onto the driver's day off — say so by name."""
        DriverDateOverride.objects.create(
            driver=self.sam, date=DAY, exception_type="off", status="approved")
        resp = self._board(driver=self.sam.id)
        self.assertEqual(resp.context["driver_filter"], "")
        self.assertEqual(resp.context["driver_filter_dropped"], str(self.sam))
        self.assertContains(resp, "has no lane on")

    def test_wrong_board_falls_back_instead_of_emptying(self):
        """An in-house driver id on the affiliate board has no lane there."""
        resp = self._board(driver=self.george.id, view="affiliate")
        self.assertEqual(resp.context["driver_filter"], "")
        self.assertEqual(self._row_driver_ids(resp), {self.waleed.id})


class DriverFilterNavigationTests(_BoardFilterFixture):
    def test_date_arrows_keep_the_filter(self):
        resp = self._board(driver=self.george.id)
        nxt = (DAY + timedelta(days=1)).isoformat()
        prev = (DAY - timedelta(days=1)).isoformat()
        self.assertContains(resp, f'href="?date={nxt}&view=inhouse&driver={self.george.id}"')
        self.assertContains(resp, f'href="?date={prev}&view=inhouse&driver={self.george.id}"')

    def test_board_switch_drops_the_filter(self):
        """The two boards hold different rosters — carrying the id across would
        only ever land on the fallback."""
        resp = self._board(driver=self.george.id)
        self.assertContains(resp, f'href="?date={DAY.isoformat()}&view=affiliate"')
        self.assertNotContains(resp, f"view=affiliate&driver={self.george.id}")

    def test_clear_link_returns_to_the_whole_board(self):
        resp = self._board(driver=self.george.id)
        self.assertContains(resp, "Show all drivers")
        self.assertContains(resp, f'href="?date={DAY.isoformat()}&view=inhouse"')

    def test_focus_note_names_the_driver_and_the_job_count(self):
        self._leg(driver=self.george)
        self._leg(pickup_time=time(15, 0), driver=self.george)
        resp = self._board(driver=self.george.id)
        self.assertContains(resp, f"Showing <strong>{self.george}</strong> only — 2 jobs")

    def test_no_focus_note_on_an_unfiltered_board(self):
        # The class name also appears in the stylesheet, so assert on the copy.
        self.assertNotContains(self._board(), "Showing <strong>")
