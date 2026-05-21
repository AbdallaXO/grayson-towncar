"""
Tests for the automated unpaid-reservation reminder engine.

We never hit SMTP — `users.emails.send_payment_reminder` is patched so the
engine sees a successful call without touching email infrastructure.
Time-sensitive cases pass an explicit ``now=`` into the engine instead of
freezing the global clock; that's simpler and stays local to each test.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from ops.models import CommunicationAttempt, EmailLog, OperationalTask
from ops.unpaid_reminders import (
    EXCLUDE_TRAVEL_AGENT,
    STAGE_FIELD,
    STAGE_FIRST,
    STAGE_SECOND,
    STAGE_THREE_DAY,
    STAGE_FINAL,
    STAGE_AUTO_CANCEL_FLAG,
    UnpaidReminderEngine,
)
from payment.models import Payment
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Customer, Leg, Reservation


SEND_PATH = "ops.unpaid_reminders.send_payment_reminder"


def _aware(dt):
    """Localize a naive datetime to the project tz."""
    if timezone.is_naive(dt):
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


class _ReminderFixtureMixin:
    """Build the minimum object graph needed to exercise the engine."""

    @classmethod
    def setUpTestData(cls):
        cls.vehicle = Vehicle.objects.create(
            vehicle_type="towncar", capacity=4, luggage_capacity=4,
        )
        cls.origin = Location.objects.create(name="Test Pickup")
        cls.dest = Location.objects.create(name="Test Dropoff")
        cls.route = Route.objects.create(origin=cls.origin, destination=cls.dest)
        cls.rate = Rate.objects.create(
            vehicle=cls.vehicle,
            route=cls.route,
            oneway_price=Decimal("100.00"),
            round_trip_price=Decimal("180.00"),
        )

    def _customer(self, **overrides):
        defaults = {
            "first_name": "Alice",
            "last_name": "Tester",
            "email": "alice@example.com",
            "phone_number": "555-123-4567",
            "zipcode": "32801",
        }
        defaults.update(overrides)
        return Customer.objects.create(**defaults)

    def _reservation(
        self,
        customer=None,
        *,
        created_at,
        pickup_dt,
        status="confirmed",
        travel_agent=None,
        total_price=Decimal("100.00"),
        with_leg=True,
        leg_status="confirmed",
    ):
        customer = customer or self._customer()
        res = Reservation.objects.create(
            customer=customer,
            rate=self.rate,
            vehicle=self.vehicle,
            trip_type="one_way",
            base_price=total_price,
            total_price=total_price,
            status=status,
            travel_agent=travel_agent,
        )
        # auto_now_add forces created_at to now() — override afterward.
        Reservation.objects.filter(pk=res.pk).update(created_at=created_at)
        res.refresh_from_db()

        if with_leg:
            Leg.objects.create(
                reservation=res,
                pickup_date=pickup_dt.date(),
                pickup_time=pickup_dt.time(),
                pickup_location="Test Pickup",
                dropoff_location="Test Dropoff",
                status=leg_status,
            )
        return res


# ── Stage timing ────────────────────────────────────────────────────────────


class StageTimingTests(_ReminderFixtureMixin, TestCase):
    """Each numbered case in the reminder schedule fires at its window."""

    def test_first_reminder_fires_2h_after_booking(self):
        now = _aware(datetime(2026, 6, 1, 12, 0))
        booking = now - timedelta(hours=2, minutes=5)
        pickup = now + timedelta(days=7)
        res = self._reservation(created_at=booking, pickup_dt=pickup)

        with patch(SEND_PATH) as mock_send:
            action = UnpaidReminderEngine(now=now).process_one(res)

        self.assertEqual(action, f"sent:{STAGE_FIRST}")
        mock_send.assert_called_once()
        kwargs = mock_send.call_args.kwargs
        self.assertEqual(kwargs["stage"], STAGE_FIRST)
        self.assertEqual(kwargs["reservation"], res)
        self.assertTrue(kwargs["automated"])

        res.refresh_from_db()
        self.assertIsNotNone(res.unpaid_first_reminder_sent_at)
        self.assertIsNone(res.unpaid_second_reminder_sent_at)

    def test_first_reminder_does_not_fire_before_2h(self):
        now = _aware(datetime(2026, 6, 1, 12, 0))
        booking = now - timedelta(minutes=30)
        pickup = now + timedelta(days=7)
        res = self._reservation(created_at=booking, pickup_dt=pickup)

        with patch(SEND_PATH) as mock_send:
            action = UnpaidReminderEngine(now=now).process_one(res)

        self.assertEqual(action, "skipped:no_stage_window")
        mock_send.assert_not_called()

    def test_second_reminder_requires_first_already_sent(self):
        now = _aware(datetime(2026, 6, 2, 12, 0))
        booking = now - timedelta(hours=25)
        pickup = now + timedelta(days=7)
        res = self._reservation(created_at=booking, pickup_dt=pickup)
        # First reminder NOT sent → stage 2 must not fire
        with patch(SEND_PATH) as mock_send:
            action = UnpaidReminderEngine(now=now).process_one(res)
        self.assertEqual(action, f"sent:{STAGE_FIRST}")
        # Engine sends FIRST instead because second is gated.
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args.kwargs["stage"], STAGE_FIRST)

    def test_second_reminder_fires_after_first(self):
        now = _aware(datetime(2026, 6, 2, 12, 0))
        booking = now - timedelta(hours=25)
        pickup = now + timedelta(days=7)
        res = self._reservation(created_at=booking, pickup_dt=pickup)
        Reservation.objects.filter(pk=res.pk).update(
            unpaid_first_reminder_sent_at=now - timedelta(hours=23)
        )
        res.refresh_from_db()

        with patch(SEND_PATH) as mock_send:
            action = UnpaidReminderEngine(now=now).process_one(res)

        self.assertEqual(action, f"sent:{STAGE_SECOND}")
        mock_send.assert_called_once_with(
            reservation=res,
            checkout_url=mock_send.call_args.kwargs["checkout_url"],
            stage=STAGE_SECOND,
            sent_by=None,
            automated=True,
        )
        res.refresh_from_db()
        self.assertIsNotNone(res.unpaid_second_reminder_sent_at)

    def test_three_day_warning_fires_in_window(self):
        now = _aware(datetime(2026, 6, 1, 12, 0))
        booking = now - timedelta(days=10)
        pickup = now + timedelta(days=2)  # inside (24h, 3d]
        res = self._reservation(created_at=booking, pickup_dt=pickup)
        # Earlier reminders already sent — irrelevant to stage 3 window.
        Reservation.objects.filter(pk=res.pk).update(
            unpaid_first_reminder_sent_at=booking + timedelta(hours=3),
            unpaid_second_reminder_sent_at=booking + timedelta(days=1),
        )
        res.refresh_from_db()

        with patch(SEND_PATH) as mock_send:
            action = UnpaidReminderEngine(now=now).process_one(res)

        self.assertEqual(action, f"sent:{STAGE_THREE_DAY}")
        self.assertEqual(mock_send.call_args.kwargs["stage"], STAGE_THREE_DAY)

    def test_final_warning_fires_in_window(self):
        now = _aware(datetime(2026, 6, 1, 12, 0))
        booking = now - timedelta(days=10)
        pickup = now + timedelta(hours=10)  # inside (2h, 24h]
        res = self._reservation(created_at=booking, pickup_dt=pickup)

        with patch(SEND_PATH) as mock_send:
            action = UnpaidReminderEngine(now=now).process_one(res)

        self.assertEqual(action, f"sent:{STAGE_FINAL}")
        self.assertEqual(mock_send.call_args.kwargs["stage"], STAGE_FINAL)
        res.refresh_from_db()
        self.assertIsNotNone(res.unpaid_final_warning_sent_at)

    def test_auto_cancel_flag_fires_within_2h_of_pickup(self):
        now = _aware(datetime(2026, 6, 1, 12, 0))
        booking = now - timedelta(days=10)
        pickup = now + timedelta(minutes=90)  # <= 2h
        res = self._reservation(created_at=booking, pickup_dt=pickup)

        with patch(SEND_PATH) as mock_send:
            action = UnpaidReminderEngine(now=now).process_one(res)

        self.assertEqual(action, "flagged")
        mock_send.assert_not_called()
        res.refresh_from_db()
        self.assertIsNotNone(res.unpaid_auto_cancel_eligible_at)

        # PAYMENT_CHASE task was created with CRITICAL priority and URGENT title.
        task = OperationalTask.objects.get(reservation=res)
        self.assertEqual(task.priority, OperationalTask.Priority.CRITICAL)
        self.assertIn("URGENT", task.title)


# ── Exclusion guards ────────────────────────────────────────────────────────


class ExclusionTests(_ReminderFixtureMixin, TestCase):

    def _booking_far_from_pickup(self):
        now = _aware(datetime(2026, 6, 1, 12, 0))
        booking = now - timedelta(hours=3)
        pickup = now + timedelta(days=5)
        return now, booking, pickup

    def test_paid_reservation_excluded(self):
        now, booking, pickup = self._booking_far_from_pickup()
        res = self._reservation(created_at=booking, pickup_dt=pickup)
        Payment.objects.create(
            reservation=res,
            customer=res.customer,
            amount=res.total_price,
            status="paid",
            payment_type="pay_now",
        )
        res.refresh_from_db()

        with patch(SEND_PATH) as mock_send:
            action = UnpaidReminderEngine(now=now).process_one(res)

        self.assertTrue(action.startswith("skipped:"))
        mock_send.assert_not_called()

    def test_cancelled_reservation_excluded(self):
        now, booking, pickup = self._booking_far_from_pickup()
        res = self._reservation(
            created_at=booking, pickup_dt=pickup, status="cancelled",
        )
        with patch(SEND_PATH) as mock_send:
            action = UnpaidReminderEngine(now=now).process_one(res)
        self.assertEqual(action, "skipped:cancelled")
        mock_send.assert_not_called()

    def test_card_saved_excluded(self):
        now, booking, pickup = self._booking_far_from_pickup()
        res = self._reservation(created_at=booking, pickup_dt=pickup)
        Payment.objects.create(
            reservation=res,
            customer=res.customer,
            amount=res.total_price,
            status="card_saved",
            payment_type="pay_later",
        )
        res.refresh_from_db()

        with patch(SEND_PATH) as mock_send:
            action = UnpaidReminderEngine(now=now).process_one(res)

        self.assertEqual(action, "skipped:payment_status=card_saved")
        mock_send.assert_not_called()

    def test_missing_email_handled_safely(self):
        now, booking, pickup = self._booking_far_from_pickup()
        customer = self._customer(email="")
        res = self._reservation(
            customer=customer, created_at=booking, pickup_dt=pickup,
        )
        with patch(SEND_PATH) as mock_send:
            action = UnpaidReminderEngine(now=now).process_one(res)
        self.assertEqual(action, "skipped:no_email")
        mock_send.assert_not_called()

    def test_manual_hold_excluded(self):
        now, booking, pickup = self._booking_far_from_pickup()
        res = self._reservation(created_at=booking, pickup_dt=pickup)
        Reservation.objects.filter(pk=res.pk).update(
            unpaid_auto_reminder_hold=True,
            unpaid_auto_reminder_hold_reason="testing",
        )
        res.refresh_from_db()
        with patch(SEND_PATH) as mock_send:
            action = UnpaidReminderEngine(now=now).process_one(res)
        self.assertEqual(action, "skipped:manual_hold")
        mock_send.assert_not_called()

    def test_travel_agent_excluded_by_default(self):
        from users.models import TravelAgent
        from django.contrib.auth import get_user_model

        User = get_user_model()
        agent_user = User.objects.create_user(username="agent1", password="x")
        agent = TravelAgent.objects.create(
            user=agent_user,
            agent_name="Agent One",
            commission_rate=Decimal("10.00"),
        )
        now, booking, pickup = self._booking_far_from_pickup()
        res = self._reservation(
            created_at=booking, pickup_dt=pickup, travel_agent=agent,
        )
        with patch(SEND_PATH) as mock_send:
            action = UnpaidReminderEngine(now=now).process_one(res)
        self.assertEqual(action, "skipped:travel_agent")
        mock_send.assert_not_called()

    def test_travel_agent_included_when_toggle_disabled(self):
        from users.models import TravelAgent
        from django.contrib.auth import get_user_model

        User = get_user_model()
        agent_user = User.objects.create_user(username="agent2", password="x")
        agent = TravelAgent.objects.create(
            user=agent_user,
            agent_name="Agent Two",
            commission_rate=Decimal("10.00"),
        )
        now, booking, pickup = self._booking_far_from_pickup()
        res = self._reservation(
            created_at=booking, pickup_dt=pickup, travel_agent=agent,
        )
        with patch("ops.unpaid_reminders.EXCLUDE_TRAVEL_AGENT", False), \
             patch(SEND_PATH) as mock_send:
            action = UnpaidReminderEngine(now=now).process_one(res)
        self.assertEqual(action, f"sent:{STAGE_FIRST}")
        mock_send.assert_called_once()

    def test_recent_staff_contact_defers_cycle(self):
        from django.contrib.auth import get_user_model
        from ops.services import create_task

        User = get_user_model()
        staff = User.objects.create_user(
            username="dispatch", password="x", is_staff=True,
        )

        now, booking, pickup = self._booking_far_from_pickup()
        res = self._reservation(created_at=booking, pickup_dt=pickup)

        task = create_task(
            task_type=OperationalTask.TaskType.PAYMENT_CHASE,
            title="Unpaid chase",
            due_at=now,
            reservation=res,
        )
        attempt = CommunicationAttempt.objects.create(
            task=task,
            channel="email",
            outcome="sent",
            staff_user=staff,
            contact_value=res.customer.email,
        )
        # auto_now_add sets created_at to real now() — pin it to 1h before the
        # engine's `now` so the 6h suppression window catches it.
        CommunicationAttempt.objects.filter(pk=attempt.pk).update(
            created_at=now - timedelta(hours=1)
        )

        with patch(SEND_PATH) as mock_send:
            action = UnpaidReminderEngine(now=now).process_one(res)
        self.assertEqual(action, "skipped:recent_staff_contact")
        mock_send.assert_not_called()


# ── Duplicate handling ──────────────────────────────────────────────────────


class DuplicateTests(_ReminderFixtureMixin, TestCase):

    def test_duplicate_blocked_and_flagged(self):
        now = _aware(datetime(2026, 6, 1, 12, 0))
        booking = now - timedelta(hours=3)
        pickup_dt = _aware(datetime(2026, 6, 8, 9, 30))

        cust1 = self._customer(email="alice@example.com")
        cust2 = self._customer(
            email="alice+dup@example.com",
            first_name="Alice", last_name="Tester", phone_number="555-123-4567",
        )

        res1 = self._reservation(customer=cust1, created_at=booking, pickup_dt=pickup_dt)
        res2 = self._reservation(customer=cust2, created_at=booking, pickup_dt=pickup_dt)

        with patch(SEND_PATH) as mock_send:
            action = UnpaidReminderEngine(now=now).process_one(res1)

        self.assertEqual(action, "dup_blocked")
        mock_send.assert_not_called()
        res1.refresh_from_db()
        self.assertTrue(res1.unpaid_duplicate_suspected)
        self.assertTrue(
            OperationalTask.objects.filter(
                reservation=res1,
                task_type=OperationalTask.TaskType.PAYMENT_CHASE,
            ).exists()
        )


# ── Idempotency and dry-run ─────────────────────────────────────────────────


class IdempotencyAndDryRunTests(_ReminderFixtureMixin, TestCase):

    def test_idempotency_no_double_send(self):
        now = _aware(datetime(2026, 6, 1, 12, 0))
        booking = now - timedelta(hours=3)
        pickup = now + timedelta(days=5)
        res = self._reservation(created_at=booking, pickup_dt=pickup)

        with patch(SEND_PATH) as mock_send:
            UnpaidReminderEngine(now=now).process_one(res)
            UnpaidReminderEngine(now=now).process_one(res)

        self.assertEqual(mock_send.call_count, 1)
        res.refresh_from_db()
        self.assertIsNotNone(res.unpaid_first_reminder_sent_at)

    def test_last_minute_booking_skips_booking_relative_stages(self):
        # Pickup 5h away, booked 3h ago. Stage 1 (booking+2h) would normally
        # apply but pickup-relative check blocks it (must be >24h to pickup).
        # Stage 4 (final 24h) should fire instead.
        now = _aware(datetime(2026, 6, 1, 12, 0))
        booking = now - timedelta(hours=3)
        pickup = now + timedelta(hours=5)
        res = self._reservation(created_at=booking, pickup_dt=pickup)

        with patch(SEND_PATH) as mock_send:
            action = UnpaidReminderEngine(now=now).process_one(res)

        self.assertEqual(action, f"sent:{STAGE_FINAL}")
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args.kwargs["stage"], STAGE_FINAL)

    def test_dry_run_no_email_no_writes(self):
        now = _aware(datetime(2026, 6, 1, 12, 0))
        booking = now - timedelta(hours=3)
        pickup = now + timedelta(days=5)
        res = self._reservation(created_at=booking, pickup_dt=pickup)

        before_email = EmailLog.objects.count()
        before_tasks = OperationalTask.objects.count()

        with patch(SEND_PATH) as mock_send:
            engine = UnpaidReminderEngine(now=now, dry_run=True)
            action = engine.process_one(res)

        self.assertEqual(action, f"sent:{STAGE_FIRST}")  # classification still records intent
        mock_send.assert_not_called()  # but no send
        self.assertEqual(engine.result.sent[STAGE_FIRST], 1)
        res.refresh_from_db()
        self.assertIsNone(res.unpaid_first_reminder_sent_at)
        self.assertEqual(EmailLog.objects.count(), before_email)
        self.assertEqual(OperationalTask.objects.count(), before_tasks)
