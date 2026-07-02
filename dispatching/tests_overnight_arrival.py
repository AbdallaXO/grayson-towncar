"""Tests for the overnight-arrival date confirmation flow.

Run with:  ./manage.py test dispatching.tests_overnight_arrival

Covers:
  * The hard gate (12 AM-6 AM window + tracked arrival + flight ident) that
    keeps every other guest from ever seeing the question.
  * derive_arrival_for_takeoff — including refusal of AeroAPI's silent
    next-available-departure fallback (the wrong-night trap).
  * The booking-form AJAX endpoint (overnight_flight_check).
  * The one-tap public confirm page: confirm-as-booked vs move-pickup-a-day,
    idempotency, task auto-close, office notification on a moved date.
  * The backstop sweep: sends once, stamps, creates tracking tasks, honors
    gates, handles guests without email, hands not-found flights to the
    existing verification flow.
  * Booking-form server-side stamping (_stamp_overnight_from_post).
"""
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core import mail
from django.core.cache import cache
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from rates.models import Vehicle, Location, Route, Rate
from reservations.models import Customer, Reservation, Leg, Flight
from ops.models import OperationalTask

from dispatching.overnight_arrival import (
    OVERNIGHT_END_HOUR,
    derive_arrival_for_takeoff,
    is_overnight_pickup_time,
    leg_in_overnight_window,
    leg_needs_overnight_confirmation,
    make_overnight_token,
    overnight_confirm_sweep,
    send_overnight_confirm_email,
    stamp_overnight_confirmed,
)

_EASTERN = ZoneInfo("America/New_York")


def _found(arrival_dt):
    """A derive_arrival_for_takeoff 'found' payload landing at arrival_dt Eastern."""
    aware = arrival_dt.replace(tzinfo=_EASTERN)
    return {
        "status": "found",
        "arrival_local": aware,
        "arrival_date": aware.date(),
        "flight_label": "Delta 123",
        "flight_ident": "DAL123",
        "origin": "JFK",
        "destination": "MCO",
    }


class _OvernightFixtureMixin:
    @classmethod
    def setUpTestData(cls):
        cls.vehicle = Vehicle.objects.create(
            vehicle_type="sedan", capacity=4, luggage_capacity=4
        )
        origin = Location.objects.create(name="MCO")
        dest = Location.objects.create(name="Disney")
        cls.route = Route.objects.create(
            origin=origin, destination=dest, inhouse_base_pay=Decimal("50.00")
        )
        cls.rate = Rate.objects.create(
            vehicle=cls.vehicle, route=cls.route,
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"),
        )
        cls.customer = Customer.objects.create(
            first_name="Jane", last_name="Doe", email="jane@example.com",
            phone_number="5551234567",
        )

    def _res(self, customer=None):
        return Reservation.objects.create(
            trip_type="one-way", customer=customer or self.customer, rate=self.rate,
            vehicle=self.vehicle, base_price=Decimal("100.00"),
            total_price=Decimal("100.00"),
        )

    def _leg(self, res=None, with_flight=True, **kw):
        flight = None
        if with_flight:
            flight = Flight.objects.create(airline="DL", flight_number="123")
        defaults = dict(
            reservation=res or self._res(),
            pickup_date=timezone.localdate() + timedelta(days=3),
            pickup_time=time(0, 20),
            pickup_location="MCO",
            dropoff_location="Disney",
            route=self.route,
            status="confirmed",
            flight_information=flight,
        )
        defaults.update(kw)
        return Leg.objects.create(**defaults)


class WindowGateTests(_OvernightFixtureMixin, TestCase):
    """The founder rule: only 12 AM-6 AM arrivals with flight info are asked."""

    def test_time_window_boundaries(self):
        self.assertTrue(is_overnight_pickup_time(time(0, 0)))
        self.assertTrue(is_overnight_pickup_time(time(0, 20)))
        self.assertTrue(is_overnight_pickup_time(time(5, 59)))
        self.assertFalse(is_overnight_pickup_time(time(6, 0)))
        self.assertFalse(is_overnight_pickup_time(time(12, 15)))
        self.assertFalse(is_overnight_pickup_time(time(23, 59)))
        self.assertFalse(is_overnight_pickup_time(None))
        self.assertEqual(OVERNIGHT_END_HOUR, 6)

    def test_overnight_arrival_with_flight_is_gated_in(self):
        leg = self._leg()
        self.assertTrue(leg_in_overnight_window(leg))
        self.assertTrue(leg_needs_overnight_confirmation(leg))

    def test_daytime_arrival_is_excluded(self):
        leg = self._leg(pickup_time=time(14, 0))
        self.assertFalse(leg_in_overnight_window(leg))

    def test_overnight_pickup_without_flight_is_excluded(self):
        leg = self._leg(with_flight=False)
        self.assertFalse(leg_in_overnight_window(leg))

    def test_overnight_departure_run_is_excluded(self):
        # 4 AM hotel -> airport run: same window, but a RETURN — no ambiguity.
        leg = self._leg(
            pickup_time=time(4, 0),
            pickup_location="Disney All Star", dropoff_location="MCO Airport",
        )
        self.assertFalse(leg_in_overnight_window(leg))

    def test_confirmed_leg_no_longer_needs_confirmation(self):
        leg = self._leg()
        stamp_overnight_confirmed(leg, leg.pickup_date - timedelta(days=1), "staff")
        leg.refresh_from_db()
        self.assertFalse(leg_needs_overnight_confirmation(leg))
        self.assertTrue(leg_in_overnight_window(leg))  # still in window, just answered
        self.assertEqual(leg.overnight_confirmed_source, "staff")
        self.assertEqual(
            leg.flight_information.departure_date, leg.pickup_date - timedelta(days=1)
        )

    def test_overnight_date_status_property(self):
        leg = self._leg()
        self.assertEqual(leg.overnight_date_status, "unconfirmed")
        stamp_overnight_confirmed(leg, leg.pickup_date - timedelta(days=1), "one_tap")
        leg.refresh_from_db()
        self.assertEqual(leg.overnight_date_status, "confirmed")
        day_leg = self._leg(pickup_time=time(14, 0))
        self.assertIsNone(day_leg.overnight_date_status)


class DeriveArrivalTests(TestCase):
    """derive_arrival_for_takeoff wraps get_scheduled_flight with the
    departs-on-the-stated-date guard."""

    def _sched(self, dep_dt, arr_dt):
        return {
            "status": "success",
            "scheduled_departure_local": dep_dt.replace(tzinfo=_EASTERN),
            "scheduled_gate_arrival_local": arr_dt.replace(tzinfo=_EASTERN),
            "scheduled_arrival_local": arr_dt.replace(tzinfo=_EASTERN),
            "origin": "JFK",
            "destination": "MCO",
        }

    @patch("dispatching.aeroapi_service.AeroAPIService.get_scheduled_flight")
    def test_found_red_eye(self, mock_sched):
        mock_sched.return_value = self._sched(
            datetime(2026, 6, 2, 21, 30), datetime(2026, 6, 3, 0, 20)
        )
        out = derive_arrival_for_takeoff("Delta", "123", date(2026, 6, 2))
        self.assertEqual(out["status"], "found")
        self.assertEqual(out["arrival_date"], date(2026, 6, 3))
        mock_sched.assert_called_once()
        self.assertEqual(mock_sched.call_args[0][1], "2026-06-02")

    @patch("dispatching.aeroapi_service.AeroAPIService.get_scheduled_flight")
    def test_fallback_to_other_day_is_refused(self, mock_sched):
        # AeroAPI silently returns the NEXT day's instance when the stated
        # date has none — that is exactly the wrong-night trap; refuse it.
        mock_sched.return_value = self._sched(
            datetime(2026, 6, 3, 21, 30), datetime(2026, 6, 4, 0, 20)
        )
        out = derive_arrival_for_takeoff("Delta", "123", date(2026, 6, 2))
        self.assertEqual(out["status"], "not_found_on_date")

    @patch("dispatching.aeroapi_service.AeroAPIService.get_scheduled_flight")
    def test_not_found_passthrough(self, mock_sched):
        mock_sched.return_value = {"status": "not_found", "error": "nope"}
        out = derive_arrival_for_takeoff("Delta", "123", date(2026, 6, 2))
        self.assertEqual(out["status"], "not_found")

    def test_unknown_airline_is_rejected_before_api(self):
        out = derive_arrival_for_takeoff("Some Fake Airline", "123", date(2026, 6, 2))
        self.assertEqual(out["status"], "bad_airline")


class FlightCheckEndpointTests(_OvernightFixtureMixin, TestCase):
    """The booking-form AJAX endpoint."""

    def setUp(self):
        cache.clear()  # reset the per-IP throttle between tests

    def _post(self, payload):
        return self.client.post(
            reverse("overnight_flight_check"),
            data=json.dumps(payload),
            content_type="application/json",
        )

    @patch("dispatching.overnight_views.derive_arrival_for_takeoff")
    def test_match_confirms_booked_date(self, mock_derive):
        pickup = timezone.localdate() + timedelta(days=5)
        takeoff = pickup - timedelta(days=1)
        mock_derive.return_value = _found(
            datetime(pickup.year, pickup.month, pickup.day, 0, 20)
        )
        resp = self._post({
            "airline": "Delta", "flight_number": "123",
            "takeoff_date": takeoff.isoformat(), "pickup_date": pickup.isoformat(),
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["found"])
        self.assertTrue(data["matches_booked"])
        self.assertEqual(data["arrival_date"], pickup.isoformat())

    @patch("dispatching.overnight_views.derive_arrival_for_takeoff")
    def test_mismatch_reports_real_landing_date(self, mock_derive):
        pickup = timezone.localdate() + timedelta(days=5)
        lands = pickup + timedelta(days=1)
        mock_derive.return_value = _found(
            datetime(lands.year, lands.month, lands.day, 0, 20)
        )
        resp = self._post({
            "airline": "Delta", "flight_number": "123",
            "takeoff_date": pickup.isoformat(), "pickup_date": pickup.isoformat(),
        })
        data = resp.json()
        self.assertTrue(data["found"])
        self.assertFalse(data["matches_booked"])
        self.assertEqual(data["arrival_date"], lands.isoformat())

    @patch("dispatching.overnight_views.derive_arrival_for_takeoff")
    def test_not_found_returns_soft_response(self, mock_derive):
        mock_derive.return_value = {"status": "not_found", "error": "nope"}
        pickup = timezone.localdate() + timedelta(days=5)
        resp = self._post({
            "airline": "Delta", "flight_number": "123",
            "takeoff_date": pickup.isoformat(), "pickup_date": pickup.isoformat(),
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertFalse(data["found"])

    def test_invalid_takeoff_date_400(self):
        resp = self._post({
            "airline": "Delta", "flight_number": "123",
            "takeoff_date": "not-a-date", "pickup_date": "2026-06-03",
        })
        self.assertEqual(resp.status_code, 400)

    @patch("dispatching.overnight_views.derive_arrival_for_takeoff")
    def test_rate_limit_kicks_in(self, mock_derive):
        mock_derive.return_value = {"status": "not_found", "error": "x"}
        pickup = timezone.localdate() + timedelta(days=5)
        payload = {
            "airline": "Delta", "flight_number": "123",
            "takeoff_date": pickup.isoformat(), "pickup_date": pickup.isoformat(),
        }
        for _ in range(10):
            self.assertEqual(self._post(payload).status_code, 200)
        self.assertEqual(self._post(payload).status_code, 429)


class OneTapConfirmViewTests(_OvernightFixtureMixin, TestCase):
    """The public confirm page reached from the one-tap email."""

    def _url(self, leg):
        return reverse("overnight_confirm_public", args=[make_overnight_token(leg.id)])

    def test_get_renders_both_choices(self):
        leg = self._leg()
        resp = self.client.get(self._url(leg) + "?choice=prev")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Which night do you land")
        self.assertContains(resp, 'value="prev"')
        self.assertContains(resp, 'value="same"')

    def test_bad_token_400(self):
        url = reverse("overnight_confirm_public", args=["garbage:token"])
        self.assertEqual(self.client.get(url).status_code, 400)

    def test_confirm_prev_keeps_date_and_stamps(self):
        leg = self._leg()
        booked = leg.pickup_date
        task = OperationalTask.objects.create(
            task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
            title="pending", leg=leg, reservation=leg.reservation,
            due_at=timezone.now(),
        )
        resp = self.client.post(self._url(leg), {"choice": "prev"})
        self.assertEqual(resp.status_code, 200)
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_date, booked)
        self.assertIsNotNone(leg.overnight_confirmed_at)
        self.assertEqual(leg.overnight_confirmed_source, "one_tap")
        self.assertEqual(
            leg.flight_information.departure_date, booked - timedelta(days=1)
        )
        task.refresh_from_db()
        self.assertEqual(task.status, OperationalTask.Status.COMPLETED)
        # No office alarm for a booking that was correct all along.
        self.assertEqual(len(mail.outbox), 0)

    def test_confirm_same_moves_pickup_and_notifies_office(self):
        leg = self._leg()
        booked = leg.pickup_date
        resp = self.client.post(self._url(leg), {"choice": "same"})
        self.assertEqual(resp.status_code, 200)
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_date, booked + timedelta(days=1))
        self.assertEqual(leg.flight_information.departure_date, booked)
        self.assertEqual(leg.overnight_confirmed_source, "one_tap")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("PICKUP DATE MOVED", mail.outbox[0].subject)

    def test_repeat_post_is_idempotent(self):
        leg = self._leg()
        booked = leg.pickup_date
        self.client.post(self._url(leg), {"choice": "same"})
        self.client.post(self._url(leg), {"choice": "same"})
        leg.refresh_from_db()
        # Moved exactly once.
        self.assertEqual(leg.pickup_date, booked + timedelta(days=1))
        self.assertEqual(len(mail.outbox), 1)

    def test_missing_choice_400(self):
        leg = self._leg()
        self.assertEqual(
            self.client.post(self._url(leg), {}).status_code, 400
        )


class StaffConfirmTests(_OvernightFixtureMixin, TestCase):
    """The dispatcher board buttons: record a texted/called guest's answer,
    or confirm right after a backend booking."""

    def setUp(self):
        from django.contrib.auth.models import User
        self.staff = User.objects.create_user(username="dispatcher", is_staff=True)
        self.client.force_login(self.staff)

    def _post(self, payload):
        return self.client.post(
            reverse("overnight_staff_confirm"),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_requires_staff(self):
        self.client.logout()
        leg = self._leg()
        resp = self._post({"leg_id": leg.id, "choice": "prev"})
        self.assertEqual(resp.status_code, 403)
        leg.refresh_from_db()
        self.assertIsNone(leg.overnight_confirmed_at)

    def test_prev_stamps_without_moving_or_emailing(self):
        leg = self._leg()
        booked = leg.pickup_date
        task = OperationalTask.objects.create(
            task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
            title="pending", leg=leg, reservation=leg.reservation,
            due_at=timezone.now(),
        )
        resp = self._post({"leg_id": leg.id, "choice": "prev"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["moved"])
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_date, booked)
        self.assertEqual(leg.overnight_confirmed_source, "staff")
        self.assertEqual(
            leg.flight_information.departure_date, booked - timedelta(days=1)
        )
        task.refresh_from_db()
        self.assertEqual(task.status, OperationalTask.Status.COMPLETED)
        self.assertEqual(task.resolved_by, self.staff)
        # Dispatcher did it themselves — no office heads-up email.
        self.assertEqual(len(mail.outbox), 0)

    def test_same_moves_pickup_without_office_email(self):
        leg = self._leg()
        booked = leg.pickup_date
        resp = self._post({"leg_id": leg.id, "choice": "same"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["moved"])
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_date, booked + timedelta(days=1))
        self.assertEqual(leg.flight_information.departure_date, booked)
        self.assertEqual(len(mail.outbox), 0)

    def test_rejects_daytime_leg(self):
        leg = self._leg(pickup_time=time(14, 0))
        resp = self._post({"leg_id": leg.id, "choice": "prev"})
        self.assertEqual(resp.status_code, 400)

    def test_already_confirmed_is_noop(self):
        leg = self._leg()
        booked = leg.pickup_date
        self._post({"leg_id": leg.id, "choice": "same"})
        resp = self._post({"leg_id": leg.id, "choice": "same"})
        self.assertTrue(resp.json().get("already_confirmed"))
        leg.refresh_from_db()
        self.assertEqual(leg.pickup_date, booked + timedelta(days=1))  # moved once

    def test_bad_choice_400(self):
        leg = self._leg()
        self.assertEqual(self._post({"leg_id": leg.id, "choice": "x"}).status_code, 400)


class SweepTests(_OvernightFixtureMixin, TestCase):
    """The backstop: every unconfirmed overnight leg gets asked exactly once."""

    def test_sends_one_tap_email_and_opens_tracking_task(self):
        leg = self._leg()
        arrival = datetime(
            leg.pickup_date.year, leg.pickup_date.month, leg.pickup_date.day, 0, 20
        )
        with patch(
            "dispatching.overnight_arrival.derive_arrival_for_takeoff",
            return_value=_found(arrival),
        ):
            result = overnight_confirm_sweep()
        self.assertEqual(result["sent"], 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Which night do you land", mail.outbox[0].subject)
        leg.refresh_from_db()
        self.assertIsNotNone(leg.overnight_confirm_sent_at)
        task = OperationalTask.objects.get(leg=leg)
        self.assertEqual(task.priority, OperationalTask.Priority.LOW)
        self.assertIn("Overnight date confirmation pending", task.title)

    def test_never_asks_twice(self):
        leg = self._leg()
        arrival = datetime(
            leg.pickup_date.year, leg.pickup_date.month, leg.pickup_date.day, 0, 20
        )
        with patch(
            "dispatching.overnight_arrival.derive_arrival_for_takeoff",
            return_value=_found(arrival),
        ):
            overnight_confirm_sweep()
            second = overnight_confirm_sweep()
        self.assertEqual(second["sent"], 0)
        self.assertEqual(len(mail.outbox), 1)

    def test_skips_daytime_confirmed_and_flightless_legs(self):
        self._leg(pickup_time=time(14, 0))            # daytime
        self._leg(with_flight=False)                   # no flight
        confirmed = self._leg()
        stamp_overnight_confirmed(
            confirmed, confirmed.pickup_date - timedelta(days=1), "staff"
        )
        with patch(
            "dispatching.overnight_arrival.derive_arrival_for_takeoff"
        ) as mock_derive:
            result = overnight_confirm_sweep()
        mock_derive.assert_not_called()
        self.assertEqual(result["sent"], 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_likely_wrong_date_gets_high_priority(self):
        leg = self._leg()
        next_day = leg.pickup_date + timedelta(days=1)
        arrival_next = datetime(next_day.year, next_day.month, next_day.day, 0, 20)

        def fake_derive(airline, number, takeoff):
            if takeoff == leg.pickup_date - timedelta(days=1):
                return {"status": "not_found_on_date", "error": "no such takeoff"}
            return _found(arrival_next)

        with patch(
            "dispatching.overnight_arrival.derive_arrival_for_takeoff",
            side_effect=fake_derive,
        ):
            result = overnight_confirm_sweep()
        self.assertEqual(result["sent"], 1)
        task = OperationalTask.objects.get(leg=leg)
        self.assertEqual(task.priority, OperationalTask.Priority.HIGH)

    def test_guest_without_email_becomes_call_task(self):
        quiet = Customer.objects.create(
            first_name="No", last_name="Email", email="", phone_number="5559999999"
        )
        leg = self._leg(res=self._res(customer=quiet))
        arrival = datetime(
            leg.pickup_date.year, leg.pickup_date.month, leg.pickup_date.day, 0, 20
        )
        with patch(
            "dispatching.overnight_arrival.derive_arrival_for_takeoff",
            return_value=_found(arrival),
        ):
            result = overnight_confirm_sweep()
        self.assertEqual(result["sent"], 0)
        self.assertEqual(len(mail.outbox), 0)
        task = OperationalTask.objects.get(leg=leg)
        self.assertIn("Call to confirm overnight date", task.title)
        leg.refresh_from_db()
        self.assertIsNotNone(leg.overnight_confirm_sent_at)  # asked (via task) once

    def test_unfindable_flight_hands_off_to_verification_flow(self):
        leg = self._leg()
        with patch(
            "dispatching.overnight_arrival.derive_arrival_for_takeoff",
            return_value={"status": "not_found", "error": "nope"},
        ), patch(
            "dispatching.flight_verify_email.send_flight_verification_email",
            return_value={"success": True},
        ) as mock_verify:
            result = overnight_confirm_sweep()
        self.assertEqual(result["sent"], 0)
        mock_verify.assert_called_once()
        task = OperationalTask.objects.get(leg=leg)
        self.assertIn("flight not found", task.title)
        self.assertEqual(task.priority, OperationalTask.Priority.HIGH)


class WizardOvernightWordingTests(TestCase):
    """The backend booking wizard's sanity panel must state the overnight rule
    explicitly (founder 2026-07-02): the flight DEPARTS the day before and
    lands after midnight, with this booking's real dates."""

    def setUp(self):
        cache.clear()  # booking_guards caches AeroAPI results for 15 min

    def _run(self, legs, flights, check_flights=False):
        from dispatching.booking_guards import run_leg_sanity_checks
        return run_leg_sanity_checks(legs, flights, check_flights=check_flights)

    def test_after_midnight_arrival_with_flight_states_departs_day_before(self):
        pickup = timezone.localdate() + timedelta(days=5)
        prev = pickup - timedelta(days=1)
        out = self._run(
            [{"pickup_date": pickup.isoformat(), "pickup_time": "00:30"}],
            [{"airline": "DL", "flight_number": "123", "flight_type": "arrival"}],
        )
        hits = [w for w in out if w["code"] == "early_morning"]
        self.assertEqual(len(hits), 1)
        msg = hits[0]["message"]
        self.assertIn("OVERNIGHT arrival", msg)
        self.assertIn("take off the day BEFORE", msg)
        self.assertIn(f"depart on {prev.strftime('%A, %b')} {prev.day}", msg)
        self.assertIn("departs MIA 10:30 PM", msg)
        self.assertIn("a day early", msg)
        self.assertIn("AM/PM mix-up", msg)  # their render test pins this phrase
        # Structured payload drives the wizard's flight-path card.
        visual = hits[0]["visual"]
        self.assertEqual(visual["kind"], "overnight")
        self.assertFalse(visual["verified"])
        self.assertEqual(visual["takeoff_date"], f"{prev.strftime('%a, %b')} {prev.day}")
        self.assertEqual(visual["land_date"], f"{pickup.strftime('%a, %b')} {pickup.day}")
        self.assertEqual(visual["land_time"], "12:30 AM")
        self.assertIn("AM/PM mix-up", visual["footnote"])

    def test_after_midnight_without_flight_keeps_plain_ampm_wording(self):
        pickup = timezone.localdate() + timedelta(days=5)
        out = self._run(
            [{"pickup_date": pickup.isoformat(), "pickup_time": "00:30"}],
            [{}],
        )
        msgs = [w["message"] for w in out if w["code"] == "early_morning"]
        self.assertEqual(len(msgs), 1)
        self.assertNotIn("OVERNIGHT arrival", msgs[0])
        self.assertIn("AM/PM mix-up", msgs[0])

    def test_verified_red_eye_adds_overnight_acknowledgment_with_real_schedule(self):
        pickup = timezone.localdate() + timedelta(days=5)
        prev = pickup - timedelta(days=1)
        land = datetime(pickup.year, pickup.month, pickup.day, 0, 20, tzinfo=_EASTERN)
        dep = datetime(prev.year, prev.month, prev.day, 21, 30, tzinfo=_EASTERN)
        fetched = {
            "status": "success",
            "flight_iata": "DL123",
            "origin": "MIA",
            "destination": "MCO",
            "scheduled_gate_arrival_local": land,
            "scheduled_arrival_local": land,
            "scheduled_departure_local": dep,
        }
        with patch("dispatching.booking_guards._fetch_flight", return_value=fetched):
            out = self._run(
                [{"pickup_date": pickup.isoformat(), "pickup_time": "00:30"}],
                [{"airline": "DL", "flight_number": "123", "flight_type": "arrival"}],
                check_flights=True,
            )
        codes = [(w["code"], w["severity"]) for w in out]
        self.assertIn(("flight_verified", "ok"), codes)
        self.assertIn(("overnight_arrival", "warning"), codes)
        # No generic early-morning nag when the flight vouches for the time.
        self.assertNotIn("early_morning", [w["code"] for w in out])
        hit = next(w for w in out if w["code"] == "overnight_arrival")
        msg = hit["message"]
        self.assertIn("departs MIA at 9:30 PM", msg)
        self.assertIn("the day BEFORE", msg)
        self.assertIn("lands MCO at 12:20 AM", msg)
        self.assertIn("a day early", msg)
        visual = hit["visual"]
        self.assertEqual(visual["kind"], "overnight")
        self.assertTrue(visual["verified"])
        self.assertEqual(visual["origin"], "MIA")
        self.assertEqual(visual["takeoff_time"], "9:30 PM")
        self.assertEqual(visual["land_time"], "12:20 AM")


class WizardEarlyDepartureTests(TestCase):
    """Routine 3–6 AM departure runs get a light 'AM, not PM' check — never
    the 'double-check with the customer' treatment (founder 2026-07-02)."""

    def setUp(self):
        cache.clear()

    def _run(self, legs, flights):
        from dispatching.booking_guards import run_leg_sanity_checks
        return run_leg_sanity_checks(legs, flights, check_flights=False)

    def _leg(self, hhmm, pickup="Disney Pop Century Resort", dropoff="Orlando International Airport (MCO)"):
        pickup_date = timezone.localdate() + timedelta(days=5)
        return {
            "pickup_date": pickup_date.isoformat(),
            "pickup_time": hhmm,
            "pickup_location": pickup,
            "dropoff_location": dropoff,
        }

    def test_routine_early_departure_gets_light_check(self):
        out = self._run(
            [self._leg("03:30")],
            [{"airline": "DL", "flight_number": "456", "flight_type": "departure"}],
        )
        codes = [w["code"] for w in out]
        self.assertIn("early_morning_departure", codes)
        self.assertNotIn("early_morning", codes)
        msg = next(w["message"] for w in out if w["code"] == "early_morning_departure")
        self.assertIn("normal for an early flight out", msg)
        self.assertIn("3:30 PM", msg)
        self.assertNotIn("customer", msg.lower())

    def test_no_flight_departure_inferred_from_locations(self):
        out = self._run([self._leg("04:00")], [{}])
        self.assertIn("early_morning_departure", [w["code"] for w in out])

    def test_predawn_departure_keeps_strong_warning(self):
        # 2 AM hotel -> airport: nothing takes off that needs this — stay loud.
        out = self._run(
            [self._leg("02:00")],
            [{"airline": "DL", "flight_number": "456", "flight_type": "departure"}],
        )
        codes = [w["code"] for w in out]
        self.assertIn("early_morning", codes)
        self.assertNotIn("early_morning_departure", codes)

    def test_arrival_shape_is_not_treated_as_departure(self):
        out = self._run(
            [self._leg("03:30", pickup="MCO Airport", dropoff="Disney Pop Century Resort")],
            [{"airline": "DL", "flight_number": "456", "flight_type": "arrival"}],
        )
        codes = [w["code"] for w in out]
        self.assertNotIn("early_morning_departure", codes)
        self.assertIn("early_morning", codes)  # the overnight-arrival variant


class BookingStampTests(_OvernightFixtureMixin, TestCase):
    """Server-side persistence of the booking-form popup's answer."""

    def _request_with(self, takeoff):
        rf = RequestFactory()
        return rf.post("/fake/", {"overnight_takeoff_date": takeoff})

    def test_stamps_valid_takeoff(self):
        from reservations.views import _stamp_overnight_from_post
        leg = self._leg()
        takeoff = leg.pickup_date - timedelta(days=1)
        _stamp_overnight_from_post(self._request_with(takeoff.isoformat()), leg)
        leg.refresh_from_db()
        self.assertIsNotNone(leg.overnight_confirmed_at)
        self.assertEqual(leg.overnight_confirmed_source, "booking_form")
        self.assertEqual(leg.flight_information.departure_date, takeoff)

    def test_rejects_out_of_range_takeoff(self):
        from reservations.views import _stamp_overnight_from_post
        leg = self._leg()
        bogus = leg.pickup_date + timedelta(days=5)
        _stamp_overnight_from_post(self._request_with(bogus.isoformat()), leg)
        leg.refresh_from_db()
        self.assertIsNone(leg.overnight_confirmed_at)

    def test_ignores_daytime_leg_even_with_field(self):
        from reservations.views import _stamp_overnight_from_post
        leg = self._leg(pickup_time=time(14, 0))
        takeoff = leg.pickup_date - timedelta(days=1)
        _stamp_overnight_from_post(self._request_with(takeoff.isoformat()), leg)
        leg.refresh_from_db()
        self.assertIsNone(leg.overnight_confirmed_at)

    def test_no_field_no_stamp(self):
        from reservations.views import _stamp_overnight_from_post
        leg = self._leg()
        rf = RequestFactory()
        _stamp_overnight_from_post(rf.post("/fake/", {}), leg)
        leg.refresh_from_db()
        self.assertIsNone(leg.overnight_confirmed_at)


class RefreshAnchorTests(_OvernightFixtureMixin, TestCase):
    """auto_refresh_flights anchors the AeroAPI lookup on the confirmed
    takeoff date when one exists (red-eyes otherwise pull the NEXT night's
    instance)."""

    def test_lookup_uses_departure_date_when_set(self):
        leg = self._leg(pickup_date=timezone.localdate())
        takeoff = leg.pickup_date - timedelta(days=1)
        flight = leg.flight_information
        flight.departure_date = takeoff
        flight.save(update_fields=["departure_date"])

        from ops.tasks import auto_refresh_flights
        with patch("ops.tasks._get_refresh_date_ranges", return_value=[leg.pickup_date]), \
             patch("dispatching.aeroapi_service.AeroAPIService.get_flight_data") as mock_get, \
             patch("dispatching.aeroapi_service.AeroAPIService.__init__", return_value=None):
            # AeroAPIService() with mocked __init__ has no api_key attr; give it one.
            from dispatching.aeroapi_service import AeroAPIService
            AeroAPIService.api_key = "test-key"
            try:
                mock_get.return_value = {"status": "not_found", "error": "x"}
                auto_refresh_flights()
            finally:
                del AeroAPIService.api_key
        self.assertTrue(mock_get.called)
        self.assertEqual(mock_get.call_args[1]["flight_date"], takeoff.isoformat())
