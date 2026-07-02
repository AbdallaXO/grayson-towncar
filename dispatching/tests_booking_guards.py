"""Booking sanity guards — wrong-date / AM-PM / flight-schedule checks on the
dispatcher booking wizard's trip-legs step, plus the soft-confirm gate."""
import re
from datetime import date, datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone as django_timezone

from dispatching.booking_guards import (
    build_flight_ident,
    run_leg_sanity_checks,
    warnings_token,
)
from rates.models import Vehicle
from reservations.models import Customer

User = get_user_model()
EASTERN = ZoneInfo("America/New_York")


def _leg(pickup_date, pickup_time, pickup="MCO Airport", dropoff="Disney Pop Century"):
    return {
        "pickup_date": pickup_date.isoformat() if isinstance(pickup_date, date) else pickup_date,
        "pickup_time": pickup_time,
        "pickup_location": pickup,
        "dropoff_location": dropoff,
        "private_notes": "",
    }


def _codes(warnings, severity=None):
    return [w["code"] for w in warnings if severity is None or w["severity"] == severity]


def _aware(d, hh, mm=0):
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=EASTERN)


class BuildFlightIdentTests(TestCase):
    def test_airline_name_plus_number(self):
        self.assertEqual(build_flight_ident("Delta", "1691"), "DL1691")

    def test_flightaware_code_remap(self):
        self.assertEqual(build_flight_ident("JetBlue", "351"), "JBU351")

    def test_prefixed_number_no_airline(self):
        self.assertEqual(build_flight_ident("", "DL567"), "DL567")
        self.assertEqual(build_flight_ident(None, "B6 351"), "JBU351")

    def test_airline_prefix_stripped_from_number(self):
        # Dispatchers often type the prefix into the number field too
        self.assertEqual(build_flight_ident("Delta", "DL567"), "DL567")

    def test_unusable_input(self):
        self.assertIsNone(build_flight_ident("", ""))
        self.assertIsNone(build_flight_ident("Delta", ""))
        self.assertIsNone(build_flight_ident("", "no digits"))


class LegSanityCheckTests(TestCase):
    """Pure plausibility checks — no flight lookups (check_flights=False)."""

    def setUp(self):
        cache.clear()
        self.future = django_timezone.localdate() + timedelta(days=30)

    def test_normal_leg_passes_clean(self):
        legs = [_leg(self.future, "14:30:00")]
        self.assertEqual(run_leg_sanity_checks(legs, [{}], check_flights=False), [])

    def test_early_morning_without_flight_warns(self):
        legs = [_leg(self.future, "02:30:00")]
        out = run_leg_sanity_checks(legs, [{}], check_flights=False)
        self.assertIn("early_morning", _codes(out, "warning"))
        self.assertIn("2:30 PM", out[0]["message"])  # suggests the flipped time

    def test_five_am_is_not_flagged(self):
        legs = [_leg(self.future, "05:00:00")]
        out = run_leg_sanity_checks(legs, [{}], check_flights=False)
        self.assertNotIn("early_morning", _codes(out))

    def test_legs_out_of_order(self):
        legs = [
            _leg(self.future + timedelta(days=7), "10:00:00"),
            _leg(self.future, "10:00:00"),
        ]
        out = run_leg_sanity_checks(legs, [{}, {}], check_flights=False)
        self.assertIn("legs_out_of_order", _codes(out, "warning"))

    def test_legs_same_datetime(self):
        legs = [_leg(self.future, "10:00:00"), _leg(self.future, "10:00:00")]
        out = run_leg_sanity_checks(legs, [{}, {}], check_flights=False)
        self.assertIn("legs_same_time", _codes(out, "warning"))

    def test_far_future_year_typo(self):
        far = django_timezone.localdate() + timedelta(days=400)
        out = run_leg_sanity_checks([_leg(far, "14:00:00")], [{}], check_flights=False)
        self.assertIn("far_future", _codes(out, "warning"))

    def test_today_upcoming_warns(self):
        now = django_timezone.localtime()
        upcoming = now + timedelta(minutes=30)
        if upcoming.date() != now.date():
            self.skipTest("too close to midnight for a stable same-day case")
        legs = [_leg(now.date(), upcoming.strftime("%H:%M:%S"))]
        out = run_leg_sanity_checks(legs, [{}], check_flights=False)
        self.assertIn("today", _codes(out, "warning"))

    def test_today_time_already_passed(self):
        now = django_timezone.localtime()
        passed = now - timedelta(minutes=5)
        if passed.date() != now.date():
            self.skipTest("too close to midnight for a stable same-day case")
        legs = [_leg(now.date(), passed.strftime("%H:%M:%S"))]
        out = run_leg_sanity_checks(legs, [{}], check_flights=False)
        self.assertIn("today_past", _codes(out, "warning"))

    def test_token_is_stable_and_ignores_nonblocking(self):
        legs = [_leg(self.future, "02:30:00")]
        w1 = run_leg_sanity_checks(legs, [{}], check_flights=False)
        w2 = run_leg_sanity_checks(legs, [{}], check_flights=False)
        self.assertEqual(warnings_token(w1), warnings_token(w2))
        # ok/info items don't change the token
        padded = w1 + [{"code": "x", "leg": 1, "severity": "ok", "message": "m"}]
        self.assertEqual(warnings_token(w1), warnings_token(padded))
        # a different warning set does
        other = run_leg_sanity_checks(
            [_leg(self.future, "03:00:00")], [{}], check_flights=False
        )
        self.assertNotEqual(warnings_token(w1), warnings_token(other))


@patch("dispatching.aeroapi_service.AeroAPIService")
class FlightCrossCheckTests(TestCase):
    """Flight-vs-pickup comparison with a mocked AeroAPI."""

    def setUp(self):
        cache.clear()
        self.d = django_timezone.localdate() + timedelta(days=45)
        self.flight = [{"airline": "Delta", "flight_number": "1691", "flight_type": "arrival"}]

    def _arrival_result(self, lands_at):
        return {
            "status": "success",
            "flight_iata": "DL1691",
            "origin": "ATL",
            "destination": "MCO",
            "scheduled_gate_arrival_local": lands_at,
            "scheduled_arrival_local": lands_at,
            "scheduled_departure_local": lands_at - timedelta(hours=2),
        }

    def test_arrival_verified(self, MockSvc):
        MockSvc.return_value.get_flight_data.return_value = self._arrival_result(_aware(self.d, 17, 5))
        out = run_leg_sanity_checks([_leg(self.d, "17:30:00")], self.flight)
        self.assertEqual(_codes(out, "ok"), ["flight_verified"])
        self.assertEqual(_codes(out, "warning"), [])

    def test_arrival_ampm_flip_caught(self, MockSvc):
        # Flight lands 5:05 PM; dispatcher typed 5:05 AM
        MockSvc.return_value.get_flight_data.return_value = self._arrival_result(_aware(self.d, 17, 5))
        out = run_leg_sanity_checks([_leg(self.d, "05:05:00")], self.flight)
        self.assertIn("flight_time_mismatch", _codes(out, "warning"))

    def test_arrival_early_morning_suppressed_when_flight_vouches(self, MockSvc):
        # Red-eye landing 2:05 AM, pickup 2:30 AM — legit, no AM/PM nag
        MockSvc.return_value.get_flight_data.return_value = self._arrival_result(_aware(self.d, 2, 5))
        out = run_leg_sanity_checks([_leg(self.d, "02:30:00")], self.flight)
        self.assertIn("flight_verified", _codes(out, "ok"))
        self.assertNotIn("early_morning", _codes(out))

    def test_arrival_wrong_date_caught(self, MockSvc):
        # Flight lands the day AFTER the typed pickup date (both lookups agree)
        MockSvc.return_value.get_flight_data.return_value = self._arrival_result(
            _aware(self.d + timedelta(days=1), 10, 30)
        )
        out = run_leg_sanity_checks([_leg(self.d, "10:30:00")], self.flight)
        self.assertIn("flight_date_mismatch", _codes(out, "warning"))

    def test_after_midnight_landing_validated_via_previous_day(self, MockSvc):
        # Pickup date D at 4:45 AM; schedule lookup keyed on departure date finds
        # the D-departing flight landing D+1 — the D-1 retry finds the one that
        # lands ON D. Must validate, and must not nag about early morning.
        def by_date(ident, flight_date=None, trip_type=None):
            asked = date.fromisoformat(flight_date)
            return self._arrival_result(_aware(asked + timedelta(days=1), 4, 30))

        MockSvc.return_value.get_flight_data.side_effect = by_date
        out = run_leg_sanity_checks([_leg(self.d, "04:45:00")], self.flight)
        # Founder rule 2026-07-02: a validated red-eye still surfaces ONE
        # acknowledgeable warning spelling out the departs-the-day-before rule —
        # verification proves the flight exists, not which night the guest is
        # on (the same flight number lands every night). No early-morning nag.
        self.assertEqual(_codes(out, "warning"), ["overnight_arrival"])
        self.assertIn("flight_verified", _codes(out, "ok"))
        self.assertNotIn("early_morning", _codes(out))

    def test_flight_not_found(self, MockSvc):
        MockSvc.return_value.get_flight_data.return_value = {"status": "not_found", "error": "x"}
        out = run_leg_sanity_checks([_leg(self.d, "17:30:00")], self.flight)
        self.assertIn("flight_not_found", _codes(out, "warning"))

    def test_api_error_is_informational_only(self, MockSvc):
        MockSvc.return_value.get_flight_data.return_value = {"status": "error", "error": "down"}
        out = run_leg_sanity_checks([_leg(self.d, "17:30:00")], self.flight)
        self.assertEqual(_codes(out, "warning"), [])
        self.assertIn("flight_unverified", _codes(out, "info"))

    def test_departure_pickup_after_takeoff(self, MockSvc):
        MockSvc.return_value.get_flight_data.return_value = {
            "status": "success", "flight_iata": "DL1690",
            "origin": "MCO", "destination": "ATL",
            "scheduled_departure_local": _aware(self.d, 10, 0),
        }
        flights = [{"airline": "Delta", "flight_number": "1690", "flight_type": "departure"}]
        out = run_leg_sanity_checks([_leg(self.d, "11:00:00")], flights)
        self.assertIn("flight_time_mismatch", _codes(out, "warning"))

    def test_departure_sane_lead_verified(self, MockSvc):
        MockSvc.return_value.get_flight_data.return_value = {
            "status": "success", "flight_iata": "DL1690",
            "origin": "MCO", "destination": "ATL",
            "scheduled_departure_local": _aware(self.d, 10, 0),
        }
        flights = [{"airline": "Delta", "flight_number": "1690", "flight_type": "departure"}]
        out = run_leg_sanity_checks([_leg(self.d, "07:00:00")], flights)
        self.assertEqual(_codes(out, "warning"), [])
        self.assertIn("flight_verified", _codes(out, "ok"))


class SoftConfirmViewTests(TestCase):
    """The legs step blocks once on warnings, then honors the acknowledgment."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("dispatcher1", password="x", is_staff=True)
        cls.customer = Customer.objects.create(
            first_name="Test", last_name="Guest",
            email="guest@example.com", phone_number="4070000000",
        )
        cls.vehicle = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)

    def setUp(self):
        cache.clear()
        self.client.force_login(self.staff)
        self.future = django_timezone.localdate() + timedelta(days=30)

    def _seed_session(self, num_legs=1):
        session = self.client.session
        session["dispatcher_booking"] = {
            "customer_id": self.customer.id,
            "num_legs": num_legs,
            "trip_type": "one_way" if num_legs == 1 else "round_trip",
            "reservation_data": {
                "manual_vehicle": str(self.vehicle.id),
                "passenger_count": "2",
                "luggage_count": "2",
            },
            "step": 3,
        }
        session.save()

    def _post_data(self, legs, flights=None):
        n = len(legs)
        flights = flights or [{} for _ in range(n)]
        data = {
            "legs-TOTAL_FORMS": str(n), "legs-INITIAL_FORMS": "0",
            "legs-MIN_NUM_FORMS": "1", "legs-MAX_NUM_FORMS": "5",
            "flights-TOTAL_FORMS": str(n), "flights-INITIAL_FORMS": "0",
            "flights-MIN_NUM_FORMS": "0", "flights-MAX_NUM_FORMS": "5",
        }
        for i, leg in enumerate(legs):
            for k, v in leg.items():
                data[f"legs-{i}-{k}"] = v
        for i, fl in enumerate(flights):
            for k, v in fl.items():
                data[f"flights-{i}-{k}"] = v
        return data

    def test_clean_legs_pass_without_ack(self):
        self._seed_session()
        resp = self.client.post(
            reverse("dispatcher_booking_legs"),
            self._post_data([_leg(self.future, "14:30")]),
        )
        self.assertRedirects(
            resp, reverse("dispatcher_booking_pricing"),
            fetch_redirect_response=False,
        )
        saved = self.client.session["dispatcher_booking"]
        self.assertEqual(len(saved["legs_data"]), 1)
        self.assertFalse(saved["sanity_acknowledged"])

    def test_warning_blocks_then_ack_passes(self):
        self._seed_session()
        payload = self._post_data([_leg(self.future, "02:30")])

        resp = self.client.post(reverse("dispatcher_booking_legs"), payload)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("sanityPanel", html)
        self.assertIn("AM/PM mix-up", html)
        # Nothing stored yet
        self.assertNotIn("legs_data", self.client.session["dispatcher_booking"])

        token = re.search(r'name="sanity_ack_token" value="([0-9a-f]+)"', html).group(1)
        payload.update({"sanity_ack": "1", "sanity_ack_token": token})
        resp2 = self.client.post(reverse("dispatcher_booking_legs"), payload)
        self.assertRedirects(
            resp2, reverse("dispatcher_booking_pricing"),
            fetch_redirect_response=False,
        )
        saved = self.client.session["dispatcher_booking"]
        self.assertTrue(saved["sanity_acknowledged"])
        self.assertTrue(any(w["code"] == "early_morning" for w in saved["sanity_results"]))

    def test_stale_token_reblocks(self):
        self._seed_session()
        payload = self._post_data([_leg(self.future, "02:30")])
        payload.update({"sanity_ack": "1", "sanity_ack_token": "deadbeefdeadbeef"})
        resp = self.client.post(reverse("dispatcher_booking_legs"), payload)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("sanityPanel", resp.content.decode())

    def test_ack_without_checkbox_reblocks(self):
        self._seed_session()
        payload = self._post_data([_leg(self.future, "02:30")])
        resp = self.client.post(reverse("dispatcher_booking_legs"), payload)
        token = re.search(
            r'name="sanity_ack_token" value="([0-9a-f]+)"', resp.content.decode()
        ).group(1)
        payload["sanity_ack_token"] = token  # token but no sanity_ack checkbox
        resp2 = self.client.post(reverse("dispatcher_booking_legs"), payload)
        self.assertEqual(resp2.status_code, 200)
        self.assertIn("sanityPanel", resp2.content.decode())

    @patch("dispatching.booking_guards.FLIGHT_CHECK_ENABLED", False)
    def test_flight_stays_with_its_leg(self):
        # Leg 1 has no flight, leg 2 does: the old two-loop collection shifted
        # leg 2's flight onto leg 1. Alignment must survive blank forms.
        self._seed_session(num_legs=2)
        legs = [
            _leg(self.future, "10:00"),
            _leg(self.future + timedelta(days=7), "17:30"),
        ]
        flights = [
            {},
            {"airline": "Delta", "flight_number": "1691", "flight_type": "arrival"},
        ]
        resp = self.client.post(
            reverse("dispatcher_booking_legs"), self._post_data(legs, flights)
        )
        self.assertRedirects(
            resp, reverse("dispatcher_booking_pricing"),
            fetch_redirect_response=False,
        )
        saved_flights = self.client.session["dispatcher_booking"]["flights_data"]
        self.assertEqual(len(saved_flights), 2)
        self.assertFalse(saved_flights[0])
        self.assertEqual(saved_flights[1].get("flight_number"), "1691")
