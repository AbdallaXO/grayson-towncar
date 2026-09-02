"""The trip step's live flight lookup, and the pickup time it recommends.

The recommendation is the house rule in code:

  * ARRIVAL   — the pickup IS the landing time. The driver is due at the
                in-terminal meet point ten minutes later (pickup_policy).
  * DEPARTURE — the guest should be standing in the terminal two hours before
                takeoff, and the drive is budgeted at thirty minutes, so the
                pickup is two and a half hours before the flight.

Nothing here ever writes a time on its own: the endpoint only says what "Match"
would fill in, and the dispatcher presses it.
"""

from datetime import date, datetime, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

User = get_user_model()


def _schedule(*, origin, destination, departure=None, arrival=None, ident="WN980"):
    """The shape _fetch_flight hands back on a successful lookup."""
    return {
        "status": "success",
        "flight_iata": ident,
        "origin": origin,
        "destination": destination,
        "flight_status": "Scheduled",
        "data_source": "schedules",
        "scheduled_departure_local": departure,
        "scheduled_gate_arrival_local": arrival,
        "scheduled_arrival_local": arrival,
        "cancelled": False,
        "diverted": False,
    }


class FlightLookupTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("dispatcher_fl", password="x", is_staff=True)
        cls.other = User.objects.create_user("driver_fl", password="x", is_staff=False)

    def setUp(self):
        cache.clear()
        self.client.force_login(self.staff)
        self.day = date.today() + timedelta(days=3)

    def _get(self, **params):
        params.setdefault("airline", "WN")
        params.setdefault("flight_number", "980")
        params.setdefault("date", self.day.isoformat())
        return self.client.get(reverse("booking_flight_lookup"), params)

    def test_only_staff_may_look_a_flight_up(self):
        self.client.force_login(self.other)
        self.assertEqual(self._get().status_code, 403)

    def test_arrival_recommends_the_landing_time(self):
        landing = datetime.combine(self.day, datetime.min.time()).replace(hour=9)
        with mock.patch(
            "dispatching.booking_guards._fetch_flight",
            return_value=_schedule(origin="BWI", destination="MCO", arrival=landing),
        ):
            data = self._get().json()

        self.assertTrue(data["ok"])
        self.assertEqual(data["verb"], "lands")
        self.assertEqual(data["time"], "9:00 AM")
        self.assertEqual(data["recommended_pickup"]["time"], "9:00 AM")
        self.assertEqual(data["recommended_pickup"]["day_offset"], 0)
        self.assertEqual(data["recommended_pickup"]["why"], "the landing time")

    def test_departure_recommends_two_and_a_half_hours_before_takeoff(self):
        """A 3:00 PM flight means a 12:30 PM pickup and the guest there by 1."""
        takeoff = datetime.combine(self.day, datetime.min.time()).replace(hour=15)
        with mock.patch(
            "dispatching.booking_guards._fetch_flight",
            return_value=_schedule(origin="MCO", destination="BWI", departure=takeoff),
        ):
            data = self._get(direction="departure").json()

        self.assertTrue(data["ok"])
        self.assertEqual(data["verb"], "departs")
        self.assertEqual(data["time"], "3:00 PM")
        self.assertEqual(data["recommended_pickup"]["time"], "12:30 PM")
        self.assertEqual(data["recommended_pickup"]["day_offset"], 0)
        self.assertEqual(data["recommended_lead_min"], 150)
        self.assertEqual(data["guest_at_airport_min"], 120)

    def test_the_suggested_departure_time_rounds_down_to_the_quarter_hour(self):
        """Nobody says "we'll collect you at 6:25". 8:55 PM less the 2h30 lead
        is 6:25, which becomes 6:15 — down, never up."""
        takeoff = datetime.combine(self.day, datetime.min.time()).replace(hour=20, minute=55)
        with mock.patch(
            "dispatching.booking_guards._fetch_flight",
            return_value=_schedule(origin="MCO", destination="BWI", departure=takeoff),
        ):
            data = self._get(direction="departure").json()

        self.assertEqual(data["recommended_pickup"]["time"], "6:15 PM")
        # And it describes the lead it really gives, not the rule's 2h30.
        self.assertEqual(data["recommended_pickup"]["why"], "2h 40m before takeoff")

    def test_the_founders_rounding_example(self):
        """8:35 PM takeoff lands the raw suggestion on 6:05 PM, which rounds to 6."""
        takeoff = datetime.combine(self.day, datetime.min.time()).replace(hour=20, minute=35)
        with mock.patch(
            "dispatching.booking_guards._fetch_flight",
            return_value=_schedule(origin="MCO", destination="BWI", departure=takeoff),
        ):
            data = self._get(direction="departure").json()

        self.assertEqual(data["recommended_pickup"]["time"], "6:00 PM")

    def test_an_arrival_time_is_never_rounded(self):
        """The pickup IS the landing time. Rounding it down would put the driver
        at the meet point before the plane is on the ground."""
        landing = datetime.combine(self.day, datetime.min.time()).replace(hour=9, minute=7)
        with mock.patch(
            "dispatching.booking_guards._fetch_flight",
            return_value=_schedule(origin="BWI", destination="MCO", arrival=landing),
        ):
            data = self._get().json()

        self.assertEqual(data["recommended_pickup"]["time"], "9:07 AM")

    def test_an_early_hours_departure_recommends_the_evening_before(self):
        """The lead crosses midnight, and the answer says so instead of quietly
        putting a 10:30 PM pickup on the wrong day."""
        takeoff = datetime.combine(self.day, datetime.min.time()).replace(hour=1)
        with mock.patch(
            "dispatching.booking_guards._fetch_flight",
            return_value=_schedule(origin="MCO", destination="BWI", departure=takeoff),
        ):
            data = self._get(direction="departure").json()

        self.assertEqual(data["recommended_pickup"]["time"], "10:30 PM")
        self.assertEqual(data["recommended_pickup"]["day_offset"], -1)

    def test_the_route_settles_the_direction_when_no_address_has_been_typed(self):
        """The flight fields sit above the address, so the flight's own route —
        not the dispatcher — says which end is Orlando."""
        landing = datetime.combine(self.day, datetime.min.time()).replace(hour=9)
        with mock.patch(
            "dispatching.booking_guards._fetch_flight",
            return_value=_schedule(origin="BWI", destination="MCO", arrival=landing),
        ):
            arriving = self._get().json()

        takeoff = datetime.combine(self.day, datetime.min.time()).replace(hour=15)
        with mock.patch(
            "dispatching.booking_guards._fetch_flight",
            return_value=_schedule(origin="MCO", destination="BWI", departure=takeoff),
        ):
            departing = self._get().json()

        self.assertEqual(arriving["direction"], "arrival")
        self.assertEqual(departing["direction"], "departure")
        self.assertEqual(departing["recommended_pickup"]["time"], "12:30 PM")

    def test_a_cruise_port_departure_is_timed_off_the_ship_not_the_flight(self):
        """The 2h30 rule is a resort rule. A guest cannot leave before they
        disembark, and Port Canaveral is nowhere near a thirty-minute drive."""
        takeoff = datetime.combine(self.day, datetime.min.time()).replace(hour=15)
        with mock.patch(
            "dispatching.booking_guards._fetch_flight",
            return_value=_schedule(origin="MCO", destination="BWI", departure=takeoff),
        ):
            data = self._get(
                direction="departure", pickup="Port Canaveral Cruise Terminal 10"
            ).json()

        self.assertTrue(data["ok"])
        self.assertTrue(data["from_cruise_port"])
        self.assertIsNone(data["recommended_pickup"])
        self.assertIn("disembarkation", data["recommendation_note"])

    def test_a_resort_departure_still_gets_the_house_recommendation(self):
        takeoff = datetime.combine(self.day, datetime.min.time()).replace(hour=15)
        with mock.patch(
            "dispatching.booking_guards._fetch_flight",
            return_value=_schedule(origin="MCO", destination="BWI", departure=takeoff),
        ):
            data = self._get(
                direction="departure", pickup="Disney's Grand Floridian Resort & Spa"
            ).json()

        self.assertFalse(data["from_cruise_port"])
        self.assertEqual(data["recommended_pickup"]["time"], "12:30 PM")
        self.assertEqual(data["recommendation_note"], "")

    def test_an_arrival_to_the_port_still_matches_the_landing_time(self):
        """Only the DEPARTURE side is timed off the ship — meeting an inbound
        flight is still the landing time, wherever the guest is going."""
        landing = datetime.combine(self.day, datetime.min.time()).replace(hour=9)
        with mock.patch(
            "dispatching.booking_guards._fetch_flight",
            return_value=_schedule(origin="BWI", destination="MCO", arrival=landing),
        ):
            data = self._get(pickup="Orlando International Airport (MCO)").json()

        self.assertEqual(data["recommended_pickup"]["time"], "9:00 AM")

    def test_an_unknown_flight_says_so_rather_than_guessing(self):
        with mock.patch(
            "dispatching.booking_guards._fetch_flight",
            return_value={"status": "not_found"},
        ):
            data = self._get().json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["reason"], "not_found")

    def test_a_flight_number_that_cannot_be_parsed_never_calls_the_api(self):
        with mock.patch("dispatching.booking_guards._fetch_flight") as fetch:
            data = self._get(airline="", flight_number="").json()
        fetch.assert_not_called()
        self.assertEqual(data["reason"], "no_ident")

    def test_a_missing_date_never_calls_the_api(self):
        with mock.patch("dispatching.booking_guards._fetch_flight") as fetch:
            data = self._get(date="").json()
        fetch.assert_not_called()
        self.assertEqual(data["reason"], "no_date")
