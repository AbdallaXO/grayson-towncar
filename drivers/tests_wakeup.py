"""Tests for the early-morning wake-up checks (drivers/wakeup.py).

Run with:  ./manage.py test drivers.tests_wakeup
"""
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from drivers import wakeup
from drivers.models import Driver, DriverWakeupCheck
from drivers.wakeup import run_wakeup_cycle
from rates.models import Vehicle, Location, Route, Rate
from reservations.models import Customer, Reservation, Leg


WAKEUP_SETTINGS = dict(
    WAKEUP_CHECKS_ENABLED=True,
    WAKEUP_EARLY_CUTOFF_HOUR=7,
    WAKEUP_SMS_LEAD_MIN=90,
    WAKEUP_CALL_LEAD_MIN=55,
    WAKEUP_CALL_LEAD_FAR_MIN=90,
    WAKEUP_ESCALATE_LEAD_MIN=50,
    WAKEUP_MIN_STEP_GAP_MIN=10,
    WAKEUP_NOTIFY_PHONES=["+15550000001", "+15550000002"],
)


def _make_driver(username, first="Test", last="Driver", driver_type="inhouse", phone="+14075551234"):
    user = User.objects.create_user(username=username, first_name=first, last_name=last)
    return Driver.objects.create(profile=user, driver_type=driver_type, phone_number=phone)


def _bootstrap_reservation():
    vehicle = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)
    origin = Location.objects.create(name="MCO")
    dest = Location.objects.create(name="Disney")
    route = Route.objects.create(origin=origin, destination=dest)
    Rate.objects.create(
        vehicle=vehicle, route=route,
        oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"),
    )
    customer = Customer.objects.create(
        first_name="Cust", last_name="One", email="c@example.com", phone_number="555",
    )
    return Reservation.objects.create(
        trip_type="one-way", customer=customer,
        rate=Rate.objects.first(), vehicle=vehicle,
        base_price=Decimal("100"), total_price=Decimal("100"),
    )


def _make_leg(reservation, driver, *, pickup_date, pickup_time,
              pickup="123 Maple St, Orlando residence", dropoff="Orlando International Airport MCO",
              status="confirmed"):
    return Leg.objects.create(
        reservation=reservation, driver=driver,
        pickup_date=pickup_date, pickup_time=pickup_time,
        pickup_location=pickup, dropoff_location=dropoff,
        status=status,
    )


def _aware(d, t):
    return timezone.make_aware(datetime.combine(d, t))


@override_settings(**WAKEUP_SETTINGS)
class WakeupCycleTests(TestCase):
    """Drive run_wakeup_cycle with an injected clock; all Twilio traffic is
    captured via the _send_sms/_place_call patch points."""

    def setUp(self):
        self.driver = _make_driver("early_bird")
        self.reservation = _bootstrap_reservation()
        # Tomorrow keeps every relative time unambiguously in the future.
        self.date = timezone.localdate() + timedelta(days=1)
        self.leg = _make_leg(
            self.reservation, self.driver,
            pickup_date=self.date, pickup_time=time(5, 30),
        )
        self.T = _aware(self.date, time(5, 30))

        self.sms = mock.patch.object(
            wakeup, "_send_sms", return_value=(True, None)
        ).start()
        self.call = mock.patch.object(
            wakeup, "_place_call", return_value=(True, None)
        ).start()
        self.addCleanup(mock.patch.stopall)

    def _cycle(self, minutes_before_pickup):
        return run_wakeup_cycle(now=self.T - timedelta(minutes=minutes_before_pickup))

    def _check(self):
        return DriverWakeupCheck.objects.get(driver=self.driver, date=self.date)

    # ── eligibility ──────────────────────────────────────────────────

    def test_no_check_outside_lead_window(self):
        self._cycle(120)
        self.assertFalse(DriverWakeupCheck.objects.exists())

    def test_late_first_pickup_gets_no_check(self):
        self.leg.pickup_time = time(9, 0)
        self.leg.save()
        self._cycle(90)
        self.assertFalse(DriverWakeupCheck.objects.exists())

    def test_affiliate_driver_gets_no_check(self):
        affiliate = _make_driver("affiliate_guy", driver_type="affiliate")
        self.leg.driver = affiliate
        self.leg.save()
        self._cycle(90)
        self.assertFalse(DriverWakeupCheck.objects.exists())

    def test_cancelled_leg_ignored(self):
        self.leg.status = "cancelled"
        self.leg.save()
        self._cycle(90)
        self.assertFalse(DriverWakeupCheck.objects.exists())

    def test_first_leg_defines_t_even_with_later_legs(self):
        _make_leg(self.reservation, self.driver,
                  pickup_date=self.date, pickup_time=time(11, 0))
        self._cycle(90)
        self.assertEqual(self._check().first_pickup_at, self.T)

    # ── the ladder ───────────────────────────────────────────────────

    def test_sms_fires_at_t_minus_90_once(self):
        self._cycle(90)
        check = self._check()
        self.assertIsNotNone(check.sms_sent_at)
        self.assertEqual(self.sms.call_count, 1)
        to, body = self.sms.call_args[0]
        self.assertEqual(to, self.driver.phone_number)
        self.assertIn("5:30 AM", body)
        self.assertIn(check.token, body)
        # Re-running the same minute must not double-send.
        self._cycle(90)
        self.assertEqual(self.sms.call_count, 1)
        self.assertEqual(self.call.call_count, 0)

    def test_call_fires_at_t_minus_55(self):
        self._cycle(90)
        self._cycle(56)
        self.assertEqual(self.call.call_count, 0)
        self._cycle(55)
        check = self._check()
        self.assertIsNotNone(check.call_started_at)
        self.assertEqual(self.call.call_count, 1)
        to, twiml = self.call.call_args[0]
        self.assertEqual(to, self.driver.phone_number)
        self.assertIn("Gather", twiml)
        self.assertIn(check.token, twiml)
        # Founder script: first name, A.I. assistant intro, live countdown,
        # say-yes-or-press-a-key, no back-to-back repeat (pause + short nudge).
        self.assertIn("Good morning Test", twiml)
        self.assertIn("A.I. assistant", twiml)
        self.assertIn("in about 55 minutes", twiml)
        self.assertIn("say yes", twiml)
        self.assertIn("dtmf speech", twiml)
        self.assertIn("<Pause", twiml)
        self.assertNotIn("loop=", twiml)

    def test_far_job_call_fires_at_t_minus_90_and_sms_earlier(self):
        self.leg.dropoff_location = "Port Canaveral Cruise Terminal 6"
        self.leg.save()
        # SMS floor moves to call lead + gap = T-100.
        self._cycle(100)
        self.assertEqual(self.sms.call_count, 1)
        self.assertEqual(self.call.call_count, 0)
        self._cycle(90)
        self.assertEqual(self.call.call_count, 1)

    def test_escalation_calls_and_texts_every_owner(self):
        self._cycle(90)
        self._cycle(55)
        self._cycle(50)
        check = self._check()
        self.assertEqual(check.status, DriverWakeupCheck.STATUS_ESCALATED)
        self.assertIsNotNone(check.escalated_at)
        # 1 driver SMS + 2 owner SMS; 1 driver call + 2 owner calls.
        self.assertEqual(self.sms.call_count, 3)
        self.assertEqual(self.call.call_count, 3)
        owner_sms_targets = [c[0][0] for c in self.sms.call_args_list[1:]]
        self.assertEqual(owner_sms_targets, ["+15550000001", "+15550000002"])
        self.assertIn("has NOT confirmed", self.sms.call_args_list[1][0][1])

    def test_one_step_per_cycle_with_min_gap_when_created_late(self):
        # Leg assigned at T-52: inside ALL three deadlines at once. Rungs ramp
        # gap-spaced (10 min, capped at the ladder's own call→owners 5 min).
        self._cycle(52)
        self.assertEqual(self.sms.call_count, 1)
        self.assertEqual(self.call.call_count, 0)
        self._cycle(45)  # only 7 min after the SMS — gap is 10
        self.assertEqual(self.call.call_count, 0)
        self._cycle(42)
        self.assertEqual(self.call.call_count, 1)
        check = self._check()
        self.assertIsNone(check.escalated_at)
        self._cycle(38)  # 4 min after the call — escalate gap is 5
        self.assertIsNone(self._check().escalated_at)
        self._cycle(37)
        self.assertEqual(self._check().status, DriverWakeupCheck.STATUS_ESCALATED)

    def test_nothing_fires_after_pickup_passed(self):
        self._cycle(90)
        run_wakeup_cycle(now=self.T + timedelta(minutes=30))
        self.assertEqual(self.call.call_count, 0)
        self.assertIsNone(self._check().escalated_at)

    # ── acks stop the ladder ─────────────────────────────────────────

    def test_ack_stops_call_and_escalation(self):
        self._cycle(90)
        wakeup.ack_check(self._check(), source="link")
        self._cycle(55)
        self._cycle(50)
        check = self._check()
        self.assertEqual(check.status, DriverWakeupCheck.STATUS_ACKED)
        self.assertEqual(self.call.call_count, 0)
        self.assertEqual(self.sms.call_count, 1)  # just the original wake-up text

    def test_ack_after_escalation_texts_all_clear(self):
        self._cycle(90)
        self._cycle(55)
        self._cycle(50)
        self.sms.reset_mock()
        wakeup.ack_check(self._check(), source="call")
        self.assertEqual(self.sms.call_count, 2)  # both owners
        self.assertIn("All clear", self.sms.call_args_list[0][0][1])

    def test_ack_is_idempotent(self):
        self._cycle(90)
        check = self._check()
        wakeup.ack_check(check, source="link")
        first = self._check().acked_at
        wakeup.ack_check(self._check(), source="call")
        self.assertEqual(self._check().acked_at, first)
        self.assertEqual(self._check().ack_source, "link")

    # ── board churn ──────────────────────────────────────────────────

    def test_retimed_out_of_early_window_cancels_check(self):
        self._cycle(90)
        self.leg.pickup_time = time(10, 0)
        self.leg.save()
        self._cycle(85)
        check = self._check()
        self.assertEqual(check.status, DriverWakeupCheck.STATUS_CANCELLED)
        # ...and the ladder stays dead.
        self._cycle(55)
        self.assertEqual(self.call.call_count, 0)

    def test_retime_within_window_moves_deadlines(self):
        self._cycle(90)
        self.leg.pickup_time = time(6, 0)  # 30 min later
        self.leg.save()
        new_t = _aware(self.date, time(6, 0))
        run_wakeup_cycle(now=new_t - timedelta(minutes=56))
        self.assertEqual(self._check().first_pickup_at, new_t)
        self.assertEqual(self.call.call_count, 0)
        run_wakeup_cycle(now=new_t - timedelta(minutes=55))
        self.assertEqual(self.call.call_count, 1)

    def test_driver_without_phone_still_escalates(self):
        self.driver.phone_number = ""
        self.driver.save()
        self._cycle(90)
        self._cycle(55)
        self._cycle(50)
        check = self._check()
        self.assertEqual(check.status, DriverWakeupCheck.STATUS_ESCALATED)
        self.assertIn("no phone number on file", check.log)
        # Only the 2 owner texts/calls went out.
        self.assertEqual(self.sms.call_count, 2)
        self.assertEqual(self.call.call_count, 2)

    def test_disabled_flag_does_nothing(self):
        with override_settings(WAKEUP_CHECKS_ENABLED=False):
            result = self._cycle(90)
        self.assertEqual(result, {"status": "disabled"})
        self.assertFalse(DriverWakeupCheck.objects.exists())


@override_settings(**WAKEUP_SETTINGS)
class WakeupEndpointTests(TestCase):
    def setUp(self):
        self.driver = _make_driver("tap_driver")
        self.reservation = _bootstrap_reservation()
        self.date = timezone.localdate() + timedelta(days=1)
        self.leg = _make_leg(
            self.reservation, self.driver,
            pickup_date=self.date, pickup_time=time(5, 0),
        )
        self.check = DriverWakeupCheck.objects.create(
            driver=self.driver, date=self.date, leg=self.leg,
            first_pickup_at=_aware(self.date, time(5, 0)),
        )

    def test_get_renders_without_acking(self):
        resp = self.client.get(f"/drivers/wakeup/{self.check.token}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "I'M UP")
        self.check.refresh_from_db()
        self.assertIsNone(self.check.acked_at)  # link previews must not confirm

    def test_post_acks(self):
        resp = self.client.post(f"/drivers/wakeup/{self.check.token}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "confirmed")
        self.check.refresh_from_db()
        self.assertEqual(self.check.status, DriverWakeupCheck.STATUS_ACKED)
        self.assertEqual(self.check.ack_source, "link")

    def test_gather_with_digits_acks(self):
        resp = self.client.post(
            f"/drivers/wakeup/{self.check.token}/gather/", {"Digits": "1"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"confirmed", resp.content)
        self.check.refresh_from_db()
        self.assertEqual(self.check.ack_source, "call")

    def test_gather_with_speech_acks(self):
        resp = self.client.post(
            f"/drivers/wakeup/{self.check.token}/gather/", {"SpeechResult": "yes"}
        )
        self.assertEqual(resp.status_code, 200)
        self.check.refresh_from_db()
        self.assertEqual(self.check.ack_source, "call")

    def test_gather_without_digits_does_not_ack(self):
        resp = self.client.post(f"/drivers/wakeup/{self.check.token}/gather/")
        self.assertEqual(resp.status_code, 200)
        self.check.refresh_from_db()
        self.assertIsNone(self.check.acked_at)

    def test_bad_token_404s(self):
        self.assertEqual(self.client.get("/drivers/wakeup/nope/").status_code, 404)


class FarJobClassifierTests(TestCase):
    def setUp(self):
        self.driver = _make_driver("far_driver")
        self.reservation = _bootstrap_reservation()
        self.date = timezone.localdate() + timedelta(days=1)

    def _leg(self, pickup, dropoff):
        return _make_leg(self.reservation, self.driver,
                         pickup_date=self.date, pickup_time=time(4, 0),
                         pickup=pickup, dropoff=dropoff)

    def test_port_canaveral_is_far(self):
        leg = self._leg("123 Maple St, Orlando residence", "Port Canaveral Terminal 6")
        self.assertTrue(wakeup.is_far_job(leg))

    def test_sanford_terminal_is_far(self):
        leg = self._leg("Sanford International Airport", "123 Maple St, Orlando residence")
        self.assertTrue(wakeup.is_far_job(leg))

    def test_local_residence_to_mco_is_not_far(self):
        leg = self._leg(
            "Hilton Orlando Lake Buena Vista hotel",
            "Orlando International Airport MCO Terminal A",
        )
        self.assertFalse(wakeup.is_far_job(leg))
