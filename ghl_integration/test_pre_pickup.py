"""
Tests for the date-anchored pre-pickup nudge engine (ghl_integration/pre_pickup.py).

We never hit GHL or SMTP: ``GoHighLevelService.send_sms`` is patched, the email
fallback's ``send_lead_quote_email`` is patched where exercised, and GHL creds
are blanked via override_settings so the Lead post_save sync signal can't make
real API calls. Time is controlled by passing an explicit ``now=`` into the
engine (no global clock freezing), mirroring ops/tests/test_unpaid_reminders.py.
"""

import json
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from ghl_integration.models import FollowUpSequence, FollowUpTask, LeadActivity
from ghl_integration.pre_pickup import (
    NUDGE_STEP,
    VARIANT_CRUISE,
    VARIANT_DISCOUNT,
    VARIANT_URGENCY,
    PrePickupNudgeEngine,
    resolve_pre_pickup_offer,
)
from ghl_integration.services import GoHighLevelService
from ghl_integration.views import ghl_webhook
from ops.models import OperationalTask
from reservations.models import Lead

ET = ZoneInfo("America/New_York")
SEND_SMS = "ghl_integration.services.GoHighLevelService.send_sms"
EMAIL_FALLBACK = "users.emails.send_lead_quote_email"


def _et(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET)


# Blank GHL creds so the Lead post_save sync signal short-circuits (no network,
# no risk of touching the real CRM during tests).
@override_settings(GHL_API_KEY="", GHL_LOCATION_ID="")
class _NudgeTestCase(TestCase):
    """Base: builds the editable nudge templates + a lead factory."""

    @classmethod
    def setUpTestData(cls):
        FollowUpSequence.objects.update_or_create(
            step_number=NUDGE_STEP, segment=VARIANT_URGENCY,
            defaults={"delay_hours": 0, "is_active": True,
                      "message_template": "Hi {first_name}! {pickup_date} trip — {estimated_price}. Book: {booking_link}"},
        )
        FollowUpSequence.objects.update_or_create(
            step_number=NUDGE_STEP, segment=VARIANT_CRUISE,
            defaults={"delay_hours": 0, "is_active": True,
                      "message_template": "Hi {first_name}! Your sailing on {pickup_date}. Book: {booking_link}"},
        )

    def _lead(self, now, *, days_out=3, **overrides):
        today = timezone.localdate(now)
        defaults = dict(
            first_name="Pat",
            last_name="Traveler",
            phone="407-555-0100",
            email="pat@example.com",
            pickup_location="MCO Airport",
            dropoff_location="Disney World",
            pickup_date=today + timedelta(days=days_out),
            estimated_price=Decimal("125.00"),
            status=Lead.StatusChoices.INTERESTED,
            segment="general",
            ghl_contact_id="ghl_test_contact",
        )
        defaults.update(overrides)
        return Lead.objects.create(**defaults)


# ── Resolver ──────────────────────────────────────────────────────────────────


class ResolverTests(TestCase):
    """discount > cruise_urgency > urgency, first match wins."""

    def test_discount_wins(self):
        lead = Lead(first_name="A", segment="cruise_transfer",
                    pre_pickup_discount=Decimal("25.00"))
        variant, ctx = resolve_pre_pickup_offer(lead, "http://book")
        self.assertEqual(variant, VARIANT_DISCOUNT)
        self.assertEqual(ctx["discount"], Decimal("25.00"))

    def test_cruise_when_no_discount(self):
        lead = Lead(first_name="A", segment="cruise_transfer",
                    pre_pickup_discount=Decimal("0"))
        variant, ctx = resolve_pre_pickup_offer(lead, "http://book")
        self.assertEqual(variant, VARIANT_CRUISE)
        self.assertEqual(ctx["booking_link"], "http://book")

    def test_urgency_default(self):
        lead = Lead(first_name="A", segment="airport_transfer",
                    pre_pickup_discount=Decimal("0"))
        variant, _ = resolve_pre_pickup_offer(lead, "http://book")
        self.assertEqual(variant, VARIANT_URGENCY)


# ── Eligibility window ──────────────────────────────────────────────────────────


class EligibilityWindowTests(_NudgeTestCase):
    def setUp(self):
        self.now = _et(2026, 6, 15, 10)  # 10 AM ET — inside send window

    def _run_once(self):
        with patch(SEND_SMS, return_value=True):
            return PrePickupNudgeEngine(now=self.now).process()

    def test_included_at_2_and_3_days(self):
        self._lead(self.now, days_out=2, phone="407-555-0001")
        self._lead(self.now, days_out=3, phone="407-555-0002")
        result = self._run_once()
        self.assertEqual(result.sent, 2)

    def test_excluded_at_1_and_4_days(self):
        self._lead(self.now, days_out=1)
        self._lead(self.now, days_out=4)
        result = self._run_once()
        self.assertEqual(result.sent, 0)

    def test_excluded_when_converted_or_lost(self):
        self._lead(self.now, status=Lead.StatusChoices.CONVERTED, converted=True)
        self._lead(self.now, status=Lead.StatusChoices.LOST)
        result = self._run_once()
        self.assertEqual(result.sent, 0)

    def test_replied_unbooked_is_included(self):
        # The core target: replied weeks ago, flagged for human, never booked.
        self._lead(self.now, has_replied=True, needs_human_follow_up=True)
        result = self._run_once()
        self.assertEqual(result.sent, 1)


# ── Dedup / idempotency ─────────────────────────────────────────────────────────


class DedupTests(_NudgeTestCase):
    def test_runs_twice_sends_once(self):
        now = _et(2026, 6, 15, 10)
        lead = self._lead(now)
        with patch(SEND_SMS, return_value=True) as mock_send:
            PrePickupNudgeEngine(now=now).process()
            PrePickupNudgeEngine(now=now).process()
        self.assertEqual(mock_send.call_count, 1)
        self.assertEqual(
            FollowUpTask.objects.filter(lead=lead, step_number=NUDGE_STEP).count(), 1
        )
        task = FollowUpTask.objects.get(lead=lead, step_number=NUDGE_STEP)
        self.assertEqual(task.status, FollowUpTask.StatusChoices.SENT)
        self.assertEqual(task.segment, VARIANT_URGENCY)

    def test_same_phone_nudged_once(self):
        # Two leads (round-trip) sharing a phone get a single nudge.
        now = _et(2026, 6, 15, 10)
        self._lead(now, days_out=2, phone="407-555-0100")
        self._lead(now, days_out=3, phone="(407) 555-0100")
        with patch(SEND_SMS, return_value=True) as mock_send:
            result = PrePickupNudgeEngine(now=now).process()
        self.assertEqual(mock_send.call_count, 1)
        self.assertEqual(result.sent, 1)
        self.assertEqual(result.skipped["phone_already_nudged"], 1)


# ── Throttle ────────────────────────────────────────────────────────────────────


class ThrottleTests(_NudgeTestCase):
    def setUp(self):
        self.now = _et(2026, 6, 15, 10)

    def test_recent_outbound_skips(self):
        self._lead(self.now, last_contact_date=self.now - timedelta(hours=10))
        with patch(SEND_SMS, return_value=True) as mock_send:
            result = PrePickupNudgeEngine(now=self.now).process()
        self.assertEqual(result.sent, 0)
        self.assertEqual(result.skipped["recent_outbound"], 1)
        mock_send.assert_not_called()

    def test_old_outbound_sends(self):
        self._lead(self.now, last_contact_date=self.now - timedelta(hours=60))
        with patch(SEND_SMS, return_value=True):
            result = PrePickupNudgeEngine(now=self.now).process()
        self.assertEqual(result.sent, 1)


# ── Send window ─────────────────────────────────────────────────────────────────


class SendWindowTests(_NudgeTestCase):
    def test_outside_window_defers_without_writing_a_row(self):
        now = _et(2026, 6, 15, 2)  # 2 AM ET
        lead = self._lead(now)
        with patch(SEND_SMS, return_value=True) as mock_send:
            result = PrePickupNudgeEngine(now=now).process_one(lead)
        self.assertEqual(result, "skipped:outside_send_window")
        mock_send.assert_not_called()
        self.assertFalse(
            FollowUpTask.objects.filter(lead=lead, step_number=NUDGE_STEP).exists()
        )


# ── Discount → human ────────────────────────────────────────────────────────────


class DiscountRouteTests(_NudgeTestCase):
    def test_discount_routes_to_human_no_sms(self):
        now = _et(2026, 6, 15, 10)
        lead = self._lead(now, pre_pickup_discount=Decimal("25.00"))
        with patch(SEND_SMS, return_value=True) as mock_send:
            result = PrePickupNudgeEngine(now=now).process_one(lead)

        self.assertEqual(result, "routed_to_human:discount")
        mock_send.assert_not_called()

        lead.refresh_from_db()
        self.assertTrue(lead.needs_human_follow_up)

        self.assertTrue(
            OperationalTask.objects.filter(
                lead=lead, task_type=OperationalTask.TaskType.MANUAL
            ).exists()
        )
        task = FollowUpTask.objects.get(lead=lead, step_number=NUDGE_STEP)
        self.assertEqual(task.status, FollowUpTask.StatusChoices.SKIPPED)
        self.assertEqual(task.segment, VARIANT_DISCOUNT)


# ── SMS failure → email fallback ────────────────────────────────────────────────


class EmailFallbackTests(_NudgeTestCase):
    def test_sms_failure_falls_back_to_email_and_flags_human(self):
        now = _et(2026, 6, 15, 10)
        lead = self._lead(now)
        with patch(SEND_SMS, return_value=False), patch(EMAIL_FALLBACK, return_value=True) as mock_email:
            result = PrePickupNudgeEngine(now=now).process_one(lead)

        self.assertEqual(result, f"email_fallback:{VARIANT_URGENCY}")
        mock_email.assert_called_once()
        lead.refresh_from_db()
        self.assertTrue(lead.needs_human_follow_up)
        task = FollowUpTask.objects.get(lead=lead, step_number=NUDGE_STEP)
        self.assertEqual(task.status, FollowUpTask.StatusChoices.SENT)

    def test_no_phone_uses_email_only_path(self):
        now = _et(2026, 6, 15, 10)
        lead = self._lead(now, phone="")
        with patch(SEND_SMS, return_value=True) as mock_send, patch(EMAIL_FALLBACK, return_value=True) as mock_email:
            result = PrePickupNudgeEngine(now=now).process_one(lead)

        self.assertEqual(result, f"email_fallback:{VARIANT_URGENCY}")
        mock_send.assert_not_called()
        mock_email.assert_called_once()
        # Still writes the dedup row so the lead isn't reprocessed.
        self.assertTrue(
            FollowUpTask.objects.filter(lead=lead, step_number=NUDGE_STEP).exists()
        )


# ── Opt-out: nudge engine early guard ───────────────────────────────────────────


class NudgeOptOutTests(_NudgeTestCase):
    def test_nudge_skips_opted_out_lead(self):
        now = _et(2026, 6, 15, 10)
        lead = self._lead(now, sms_opt_out=True)
        with patch(SEND_SMS, return_value=True) as mock_send:
            result = PrePickupNudgeEngine(now=now).process_one(lead)
        self.assertEqual(result, "skipped:sms_opted_out")
        mock_send.assert_not_called()
        self.assertFalse(
            FollowUpTask.objects.filter(lead=lead, step_number=NUDGE_STEP).exists()
        )


# ── Opt-out: shared send_sms guard ──────────────────────────────────────────────


@override_settings(GHL_API_KEY="", GHL_LOCATION_ID="")
class SendSmsOptOutGuardTests(TestCase):
    """The shared send_sms choke point protects every sender, keyed by phone."""

    def _lead(self, **kw):
        defaults = dict(first_name="A", phone="407-555-0100",
                        status=Lead.StatusChoices.INTERESTED)
        defaults.update(kw)
        return Lead.objects.create(**defaults)

    def test_is_opted_out_direct(self):
        self._lead(ghl_contact_id="C1", sms_opt_out=True)
        self._lead(ghl_contact_id="C2", phone="407-555-0200")
        svc = GoHighLevelService()
        self.assertTrue(svc._is_opted_out("C1"))
        self.assertFalse(svc._is_opted_out("C2"))
        self.assertFalse(svc._is_opted_out("UNKNOWN"))

    def test_is_opted_out_propagates_by_phone(self):
        # Two leads, same phone, different contact ids; only one flagged.
        self._lead(ghl_contact_id="C1", phone="407-555-0100", sms_opt_out=True)
        self._lead(ghl_contact_id="C2", phone="(407) 555-0100", sms_opt_out=False)
        # The un-flagged contact still resolves opted-out via the shared phone.
        self.assertTrue(GoHighLevelService()._is_opted_out("C2"))

    def test_send_sms_blocks_opted_out_before_api(self):
        self._lead(ghl_contact_id="C1", sms_opt_out=True)
        self._lead(ghl_contact_id="C2", phone="407-555-0200")
        with override_settings(GHL_API_KEY="k", GHL_LOCATION_ID="l"):
            with patch("ghl_integration.services.requests.post") as mock_post:
                mock_post.return_value.status_code = 201
                svc = GoHighLevelService()
                self.assertFalse(svc.send_sms("C1", "hi"))   # opted out → blocked
                mock_post.assert_not_called()
                self.assertTrue(svc.send_sms("C2", "hi"))     # clean → sends
                mock_post.assert_called_once()


# ── Opt-out: webhook STOP detection + full-body persistence ──────────────────────


@override_settings(GHL_API_KEY="", GHL_LOCATION_ID="")
class WebhookOptOutTests(TestCase):
    def _post(self, payload):
        req = RequestFactory().post(
            "/ghl/webhook/", data=json.dumps(payload), content_type="application/json"
        )
        with patch("ghl_integration.views.send_ntfy_notification"), \
             patch("ghl_integration.runner.run_in_background"):
            return ghl_webhook(req)

    def _lead(self, **kw):
        defaults = dict(first_name="A", phone="407-555-0100",
                        status=Lead.StatusChoices.INTERESTED, ghl_contact_id="C1")
        defaults.update(kw)
        return Lead.objects.create(**defaults)

    def test_stop_opts_out_all_leads_on_phone(self):
        a = self._lead()
        b = self._lead(phone="(407) 555-0100", ghl_contact_id="C2")  # round-trip sibling
        resp = self._post({"type": "InboundMessage", "contactId": "C1",
                           "phone": "407-555-0100", "body": "Stop."})
        self.assertEqual(resp.status_code, 200)
        a.refresh_from_db(); b.refresh_from_db()
        self.assertTrue(a.sms_opt_out)
        self.assertTrue(b.sms_opt_out)   # propagated to the sibling on the same phone

    def test_non_stop_reply_does_not_opt_out(self):
        a = self._lead()
        resp = self._post({"type": "InboundMessage", "contactId": "C1",
                           "phone": "407-555-0100", "body": "yes please book me"})
        self.assertEqual(resp.status_code, 200)
        a.refresh_from_db()
        self.assertFalse(a.sms_opt_out)
        self.assertTrue(a.has_replied)

    def test_full_inbound_body_persisted(self):
        self._lead()
        long_body = "I have a question — " + ("x" * 300)
        resp = self._post({"type": "InboundMessage", "contactId": "C1",
                           "phone": "407-555-0100", "body": long_body})
        self.assertEqual(resp.status_code, 200)
        act = LeadActivity.objects.filter(
            activity_type=LeadActivity.ActivityType.REPLY_RECEIVED
        ).latest("created_at")
        self.assertEqual(act.metadata["message_body"], long_body)   # full, untruncated
        self.assertEqual(len(act.metadata["message_preview"]), 200)  # legacy preview kept


# ── #5 Discount ops-task SLA (due date) ─────────────────────────────────────────


class DiscountTaskDueDateTests(_NudgeTestCase):
    def test_discount_task_due_on_pickup_day(self):
        now = _et(2026, 6, 15, 10)
        lead = self._lead(now, pre_pickup_discount=Decimal("25.00"))  # pickup = today+3
        PrePickupNudgeEngine(now=now).process_one(lead)
        task = OperationalTask.objects.get(
            lead=lead, task_type=OperationalTask.TaskType.MANUAL
        )
        # due_at lands on the pickup date (ET morning); escalate_at is a day earlier.
        self.assertEqual(timezone.localdate(task.due_at), lead.pickup_date)
        self.assertEqual(task.escalate_at, task.due_at - timedelta(days=1))
