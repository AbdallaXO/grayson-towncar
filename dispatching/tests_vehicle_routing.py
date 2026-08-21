"""Tests for the right-click "where is the car" menu row.

The menu shows one thing: Google Maps directions from the assigned car's live
coordinates to the leg's NEXT stop — the pickup, or the drop-off once the guest
is aboard. Covers the pure link/label rules and the two endpoints that serve
them.

Both endpoints are DB-only — the position comes from the columns the 3-minute
poller maintains — so there is nothing to mock and no network in any test here.

Run with:  ./manage.py test dispatching.tests_vehicle_routing
"""
from datetime import date, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from dispatching import vehicle_routing as vr
from drivers.models import Driver, DriverVehicleAssignment, FleetVehicle
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation

TD = date(2026, 6, 1)
LAT, LNG = Decimal("28.431200"), Decimal("-81.308100")


class ShortPlaceTests(TestCase):
    def test_drops_state_and_zip(self):
        self.assertEqual(
            vr.short_place("5744 Crowntree Lane, Orlando, FL, 32829"),
            "5744 Crowntree Lane, Orlando",
        )

    def test_keeps_a_bare_city(self):
        self.assertEqual(vr.short_place("Bay Lake, FL, 32830"), "Bay Lake")

    def test_venue_names_survive(self):
        self.assertEqual(
            vr.short_place("Express Pick-Up, Orlando, FL, 32862"),
            "Express Pick-Up, Orlando",
        )

    def test_blank_is_blank(self):
        self.assertEqual(vr.short_place(""), "")
        self.assertEqual(vr.short_place(None), "")


class LiveLinkTests(TestCase):
    def test_no_position_means_no_link(self):
        self.assertEqual(vr.live_link(None, None), (None, ""))
        self.assertEqual(vr.live_link(28.4, None), (None, ""))
        self.assertEqual(vr.live_link(None, -81.3), (None, ""))

    def test_a_destination_produces_directions_from_the_car(self):
        """The question behind the right-click: how far out is he?"""
        url, label = vr.live_link(28.4312, -81.3081, "Disney's Grand Floridian")
        self.assertIn("maps/dir", url)
        self.assertIn("origin=28.431200%2C-81.308100", url)
        self.assertIn("Grand%20Floridian", url)
        self.assertEqual(label, "Disney's Grand Floridian")

    def test_the_coordinate_is_never_region_hinted(self):
        """map_query passes a bare lat,lng through; ", FL" would move the pin."""
        url, _ = vr.live_link(28.4312, -81.3081, "MCO")
        self.assertIn("origin=28.431200%2C-81.308100&", url)

    def test_no_destination_falls_back_to_a_pin_on_the_coordinates(self):
        url, label = vr.live_link(28.4312, -81.3081)
        self.assertIn("maps/search", url)
        self.assertIn("28.431200%2C-81.308100", url)
        self.assertEqual(label, "")

    def test_a_blank_destination_falls_back_to_the_pin(self):
        """
        maps_directions_url refuses a one-ended route, because Google silently
        resolves it to "directions from your current location" — a lie about
        where the car is.
        """
        url, label = vr.live_link(28.4312, -81.3081, "   ")
        self.assertIn("maps/search", url)
        self.assertEqual(label, "")

    def test_the_label_is_trimmed_but_the_link_is_not(self):
        """A 300px menu row can't carry a full postal address; Google wants one."""
        full = "Disney's Grand Floridian Resort & Spa, Floridian Way, Lake Buena Vista, FL, USA"
        url, label = vr.live_link(28.4312, -81.3081, full)
        self.assertEqual(label, "Disney's Grand Floridian Resort & Spa")
        self.assertIn("Lake%20Buena%20Vista", url)

    def test_decimal_coordinates_are_accepted(self):
        """The model stores DecimalField, so the view hands these straight in."""
        url, _ = vr.live_link(LAT, LNG, "MCO")
        self.assertIn("origin=28.431200%2C-81.308100", url)


class LegDestinationTests(TestCase):
    """Which end of the trip the car is still heading for."""

    PICKUP = "Disney's Grand Floridian, Lake Buena Vista, FL"
    DROPOFF = "MCO Terminal B, Orlando, FL"

    def _dest(self, status):
        return vr.leg_destination(status, self.PICKUP, self.DROPOFF)

    def test_before_the_guest_is_aboard_it_is_the_pickup(self):
        for status in ("in-progress", "confirmed", "on-the-way"):
            with self.subTest(status=status):
                self.assertEqual(self._dest(status), (self.PICKUP, "pickup"))

    def test_picked_up_flips_to_the_dropoff(self):
        """The reason this exists: 'how far out is he' becomes 'how far to done'."""
        self.assertEqual(self._dest("picked-up"), (self.DROPOFF, "dropoff"))

    def test_on_location_counts_as_aboard(self):
        """He is standing at the pickup — routing him to it is a link to himself."""
        self.assertEqual(self._dest("on-location"), (self.DROPOFF, "dropoff"))

    def test_a_finished_or_cancelled_trip_has_no_next_stop(self):
        for status in ("completed", "cancelled"):
            with self.subTest(status=status):
                self.assertEqual(self._dest(status), ("", ""))

    def test_a_missing_status_is_treated_as_not_started(self):
        self.assertEqual(self._dest(None), (self.PICKUP, "pickup"))
        self.assertEqual(self._dest(""), (self.PICKUP, "pickup"))
        self.assertEqual(self._dest("  picked-up  "), (self.DROPOFF, "dropoff"))

    def test_an_unknown_status_never_loses_the_route(self):
        """A status we don't recognise falls back to the pickup, not to nothing."""
        self.assertEqual(self._dest("waiting-on-guest"), (self.PICKUP, "pickup"))

    def test_the_missing_end_comes_back_blank_but_still_labelled(self):
        """The caller needs the kind to say WHICH address is missing."""
        self.assertEqual(vr.leg_destination("picked-up", self.PICKUP, ""), ("", "dropoff"))
        self.assertEqual(vr.leg_destination("confirmed", None, self.DROPOFF), ("", "pickup"))

    def test_it_matches_the_line_the_eta_sweep_draws(self):
        """
        One definition of "the guest is aboard", or the board badge reads
        "18 min to drop-off" over a menu still offering a route to the pickup.
        """
        from dispatching import samsara_risk
        for status in samsara_risk._ON_TRIP:
            with self.subTest(status=status):
                self.assertEqual(self._dest(status)[1], "dropoff")

class LegVehicleRouteTests(TestCase):
    @classmethod
    def setUpClass(cls):
        # Saving a Reservation fires a post_save that emails on a REAL daemon
        # thread (reservations/signals.py -> users/emails.py); there is no
        # TESTING gate on it. Against the shared-cache in-memory SQLite test DB
        # those racing writes trip SQLITE_LOCKED, and enough of them make
        # UNRELATED modules fail intermittently. Patched for the whole class,
        # and the fixtures are built ONCE rather than per test, so this module
        # contributes one reservation save to the run instead of a dozen.
        super().setUpClass()
        patcher = patch("users.emails.send_internal_confirmation", lambda *a, **k: None)
        patcher.start()
        cls.addClassCleanup(patcher.stop)

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("disp", password="x", is_staff=True)
        cls.plain = User.objects.create_user("guest", password="x")
        cls.vtype = Vehicle.objects.create(
            vehicle_type="suv", capacity=6, luggage_capacity=4)
        origin = Location.objects.create(name="MCO")
        dest = Location.objects.create(name="Disney")
        cls.route = Route.objects.create(
            origin=origin, destination=dest, inhouse_base_pay=Decimal("50.00"))
        cls.rate = Rate.objects.create(
            route=cls.route, vehicle=cls.vtype,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"))
        cls.car = FleetVehicle.objects.create(
            vehicle_number="001", year=2023, make="Chevrolet", model="Suburban",
            vehicle_type=cls.vtype, samsara_vehicle_id="sam-1",
            samsara_last_latitude=LAT, samsara_last_longitude=LNG,
            samsara_last_location_label="Beachline Expressway, Orlando, FL, 32812",
            samsara_movement_status="driving",
        )
        driver_user = User.objects.create_user(username="alex", first_name="Alex")
        cls.driver = Driver.objects.create(profile=driver_user, driver_type="inhouse")
        cls.customer = Customer.objects.create(
            first_name="Pat", last_name="Guest", email="pat@example.com",
            phone_number="5550001111")
        cls.reservation = Reservation.objects.create(
            trip_type="one-way", customer=cls.customer, vehicle=cls.vtype,
            rate=cls.rate, base_price=Decimal("100.00"), total_price=Decimal("100.00"),
        )
        cls.leg = Leg.objects.create(
            reservation=cls.reservation, pickup_date=TD, pickup_time=time(9, 0),
            pickup_location="Disney's Grand Floridian Resort, Lake Buena Vista, FL",
            dropoff_location="MCO", driver=cls.driver,
            route=cls.route, status="confirmed",
        )
        DriverVehicleAssignment.objects.create(
            driver=cls.driver, date=TD, vehicle=cls.car,
        )

    def setUp(self):
        # Freshness is relative to now, so it has to be stamped per test rather
        # than baked into setUpTestData.
        self.car.samsara_last_seen_at = timezone.now()
        self.car.save()

    def _url(self):
        return reverse("leg_vehicle_route", args=[self.leg.id])

    def _get(self):
        self.client.force_login(self.staff)
        return self.client.get(self._url()).json()

    # --- access ---
    def test_anonymous_is_redirected(self):
        self.assertEqual(self.client.get(self._url()).status_code, 302)

    def test_non_staff_is_refused(self):
        self.client.force_login(self.plain)
        self.assertEqual(self.client.get(self._url()).status_code, 403)

    def test_post_is_not_allowed(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(self._url()).status_code, 405)

    # --- the happy path ---
    def test_the_link_routes_the_car_to_this_legs_pickup(self):
        data = self._get()
        self.assertIn("maps/dir", data["live"]["url"])
        self.assertIn("origin=28.431200%2C-81.308100", data["live"]["url"])
        self.assertIn("Grand%20Floridian", data["live"]["url"])
        self.assertEqual(data["live"]["destination"],
                         "Disney's Grand Floridian Resort")
        self.assertEqual(data["note"], "")

    def test_a_picked_up_leg_routes_to_the_dropoff_instead(self):
        """
        The whole change: he already has the guest, so the dispatcher's question
        is how far he is from DROPPING OFF, not from a pickup he has made.
        """
        self.leg.status = "picked-up"
        self.leg.save()
        data = self._get()
        self.assertIn("maps/dir", data["live"]["url"])
        self.assertIn("origin=28.431200%2C-81.308100", data["live"]["url"])
        self.assertIn("MCO", data["live"]["url"])
        self.assertNotIn("Grand%20Floridian", data["live"]["url"])
        self.assertEqual(data["live"]["destination"], "MCO")
        self.assertEqual(data["live"]["destination_kind"], "dropoff")
        self.assertEqual(data["note"], "")

    def test_an_unstarted_leg_still_routes_to_the_pickup(self):
        self.assertEqual(self._get()["live"]["destination_kind"], "pickup")

    def test_a_finished_leg_drops_to_a_pin_rather_than_a_stale_route(self):
        """Nothing is next on a completed job; "Route to pickup" would be a lie."""
        self.leg.status = "completed"
        self.leg.save()
        data = self._get()
        self.assertIn("maps/search", data["live"]["url"])
        self.assertEqual(data["live"]["destination"], "")
        self.assertEqual(data["live"]["destination_kind"], "")
        self.assertIn("completed", data["note"])

    def test_a_picked_up_leg_with_no_dropoff_address_names_the_missing_end(self):
        self.leg.status = "picked-up"
        self.leg.dropoff_location = ""
        self.leg.save()
        data = self._get()
        self.assertIn("maps/search", data["live"]["url"])
        self.assertIn("No drop-off address", data["note"])

    def test_the_bare_unit_number_is_sent(self):
        """The row shows "#001 · <place>"; make and model only took up width."""
        self.assertEqual(self._get()["vehicle_number"], "001")

    def test_the_location_label_is_trimmed(self):
        self.assertEqual(self._get()["live"]["place"], "Beachline Expressway, Orlando")

    def test_a_fresh_sample_reports_motion(self):
        data = self._get()
        self.assertTrue(data["live"]["fresh"])
        self.assertTrue(data["live"]["moving"])

    def test_a_stale_sample_never_claims_the_car_is_moving(self):
        """
        A gateway that goes quiet mid-drive leaves "driving" in the column
        forever. "Moving · 38h ago" is a straight contradiction — the position
        is still worth opening, the motion is not.
        """
        self.car.samsara_last_seen_at = timezone.now() - timedelta(hours=38)
        self.car.save()
        data = self._get()
        self.assertFalse(data["live"]["fresh"])
        self.assertFalse(data["live"]["moving"])
        self.assertIsNotNone(data["live"]["url"])

    def test_it_never_calls_samsara(self):
        """DB-only: no outbound call can sit between a dispatcher and this menu."""
        with patch("dispatching.samsara_service.SamsaraService.get_vehicle_stats") as m:
            self._get()
        m.assert_not_called()

    # --- honest notes ---
    def test_a_leg_with_no_driver_says_so(self):
        self.leg.driver = None
        self.leg.save()
        data = self._get()
        self.assertIn("No driver", data["note"])
        self.assertIsNone(data["live"])

    def test_a_driver_with_no_car_that_day_says_so(self):
        DriverVehicleAssignment.objects.all().delete()
        data = self._get()
        self.assertIn("No car assigned", data["note"])
        self.assertIsNone(data["live"])

    def test_an_unmapped_car_says_so(self):
        self.car.samsara_vehicle_id = ""
        self.car.save()
        self.assertIn("isn't mapped to Samsara", self._get()["note"])

    def test_a_car_that_never_reported_says_so(self):
        self.car.samsara_last_latitude = None
        self.car.samsara_last_longitude = None
        self.car.save()
        data = self._get()
        self.assertIn("hasn't reported a position", data["note"])
        self.assertIsNone(data["live"])

    def test_a_leg_with_no_pickup_address_still_gives_the_position(self):
        self.leg.pickup_location = ""
        self.leg.save()
        data = self._get()
        self.assertIn("maps/search", data["live"]["url"])
        self.assertIn("No pickup address", data["note"])


class FleetVehicleRouteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("disp2", password="x", is_staff=True)
        cls.car = FleetVehicle.objects.create(
            vehicle_number="007", year=2023, make="Chevrolet", model="Suburban",
            samsara_vehicle_id="sam-7",
            samsara_last_latitude=LAT, samsara_last_longitude=LNG,
        )

    def _url(self, pk=None):
        return reverse("fleet_vehicle_route", args=[pk or self.car.pk])

    def test_non_staff_is_refused(self):
        self.client.force_login(User.objects.create_user("guest2", password="x"))
        self.assertEqual(self.client.get(self._url()).status_code, 403)

    def test_with_no_job_in_view_the_link_is_a_plain_pin(self):
        self.client.force_login(self.staff)
        data = self.client.get(self._url()).json()
        self.assertIn("maps/search", data["live"]["url"])
        self.assertEqual(data["live"]["destination"], "")
        self.assertEqual(data["vehicle_number"], "007")

    def test_an_unmapped_vehicle_says_so(self):
        unmapped = FleetVehicle.objects.create(
            vehicle_number="004", year=2021, make="Mercedes", model="Sprinter")
        self.client.force_login(self.staff)
        data = self.client.get(self._url(unmapped.pk)).json()
        self.assertIn("isn't mapped to Samsara", data["note"])
        self.assertIsNone(data["live"])
