"""Outbound automation is production's job. Nothing else sends.

2026-09-21: a `manage.py runserver` on the founder's laptop, with the real mail
and GoHighLevel credentials in .env and a week-old copy of the database, ran the
scheduler every 30 minutes and sent 48 payment reminders and 151 follow-up
texts to real guests — 14 of the emails to people who had already paid, one of
whom asked why. Nothing in the code asked "am I production?".

Now `settings.OUTBOUND_AUTOMATION_ENABLED` is on in production (Railway) and in
tests, off everywhere else, and it is checked in five places so no single
bypass can send: the app hook that starts the scheduler, the scheduler thread,
the batch it runs, the reminder engine, and the two senders themselves (every
text goes through GoHighLevelService.send_sms; every automated reminder email
through send_payment_reminder with automated=True).
"""
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from ghl_integration.services import GoHighLevelService
from ops.unpaid_reminders import STAGE_FIRST, UnpaidReminderEngine
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation
from users.emails import send_payment_reminder


def _aware(dt):
    return timezone.make_aware(dt, timezone.get_current_timezone())


class _Fixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.vehicle = Vehicle.objects.create(vehicle_type="towncar", capacity=4, luggage_capacity=4)
        cls.rate = Rate.objects.create(
            vehicle=cls.vehicle,
            route=Route.objects.create(
                origin=Location.objects.create(name="A"), destination=Location.objects.create(name="B")),
            oneway_price=Decimal("100.00"), round_trip_price=Decimal("180.00"))
        cls.customer = Customer.objects.create(
            first_name="Mary", last_name="Tomasso", email="mary@example.com", phone_number="7327403095")

    def _due_reservation(self, now):
        res = Reservation.objects.create(
            customer=self.customer, rate=self.rate, vehicle=self.vehicle, trip_type="one_way",
            base_price=Decimal("500.00"), total_price=Decimal("500.00"), status="confirmed")
        Reservation.objects.filter(pk=res.pk).update(created_at=now - timedelta(hours=3))
        res.refresh_from_db()
        pickup = now + timedelta(days=7)
        Leg.objects.create(reservation=res, pickup_date=pickup.date(), pickup_time=pickup.time(),
                           pickup_location="A", dropoff_location="B", status="confirmed")
        return res


class ReminderEngineGuardTests(_Fixture):
    def test_the_engine_sends_when_automation_is_on(self):
        now = _aware(datetime(2026, 6, 2, 12, 0))
        res = self._due_reservation(now)
        with patch("ops.unpaid_reminders.send_payment_reminder") as send:
            self.assertEqual(UnpaidReminderEngine(now=now).process_one(res), f"sent:{STAGE_FIRST}")
        send.assert_called_once()

    @override_settings(OUTBOUND_AUTOMATION_ENABLED=False)
    def test_the_engine_is_idle_off_production(self):
        now = _aware(datetime(2026, 6, 2, 12, 0))
        res = self._due_reservation(now)
        with patch("ops.unpaid_reminders.send_payment_reminder") as send:
            self.assertEqual(UnpaidReminderEngine(now=now).process_one(res), "skipped:automation_disabled")
            result = UnpaidReminderEngine(now=now).process()
        send.assert_not_called()
        self.assertEqual(sum(result.sent.values()), 0)
        res.refresh_from_db()
        self.assertIsNone(res.unpaid_first_reminder_sent_at)

    @override_settings(OUTBOUND_AUTOMATION_ENABLED=False)
    def test_a_dry_run_still_reports_off_production(self):
        """The audit command (--dry-run) reads and writes nothing, so it may run anywhere."""
        now = _aware(datetime(2026, 6, 2, 12, 0))
        res = self._due_reservation(now)
        self.assertEqual(UnpaidReminderEngine(now=now, dry_run=True).process_one(res), f"sent:{STAGE_FIRST}")


class SenderGuardTests(_Fixture):
    @override_settings(OUTBOUND_AUTOMATION_ENABLED=False)
    def test_an_automated_reminder_email_refuses_and_raises(self):
        res = self._due_reservation(timezone.now())
        with patch("users.emails.EmailMultiAlternatives") as mail:
            with self.assertRaises(RuntimeError):
                send_payment_reminder(res, "https://example.com/pay", stage="first", automated=True)
        mail.assert_not_called()

    @override_settings(OUTBOUND_AUTOMATION_ENABLED=False)
    def test_a_dispatcher_sending_a_reminder_by_hand_still_works(self):
        res = self._due_reservation(timezone.now())
        with patch("users.emails.EmailMultiAlternatives") as mail:
            send_payment_reminder(res, "https://example.com/pay", stage="manual", automated=False)
        self.assertTrue(mail.return_value.send.called)  # the guest copy, plus the office copy

    @override_settings(OUTBOUND_AUTOMATION_ENABLED=False, GHL_API_KEY="k", GHL_LOCATION_ID="l")
    def test_no_text_leaves_a_non_production_machine(self):
        with patch("ghl_integration.services.requests") as http:
            self.assertFalse(GoHighLevelService().send_sms("contact-1", "Hey Mary"))
        http.post.assert_not_called()
        http.get.assert_not_called()


class SchedulerGuardTests(TestCase):
    @override_settings(OUTBOUND_AUTOMATION_ENABLED=False)
    def test_the_scheduler_thread_is_not_started(self):
        import ghl_integration.scheduler as sched
        sched._scheduler_started = False
        with patch("ghl_integration.scheduler.threading.Thread") as thread:
            sched.start_scheduler()
        thread.assert_not_called()
        self.assertFalse(sched._scheduler_started)

    @override_settings(OUTBOUND_AUTOMATION_ENABLED=False)
    def test_the_batch_does_nothing(self):
        from ghl_integration.scheduler import _run_batch_tasks
        with patch("ghl_integration.tasks.batch_send_unsent_leads") as batch, \
             patch("ops.tasks.generate_ops_tasks") as ops:
            _run_batch_tasks()
        batch.assert_not_called()
        ops.assert_not_called()

    @override_settings(OUTBOUND_AUTOMATION_ENABLED=False)
    def test_the_dedicated_scheduler_command_refuses(self):
        from io import StringIO
        from django.core.management import call_command
        err = StringIO()
        with patch("ghl_integration.scheduler.start_scheduler") as start:
            call_command("run_schedulers", stderr=err)
        start.assert_not_called()
        self.assertIn("not production", err.getvalue())
