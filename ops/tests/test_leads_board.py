"""
Tests for the date-anchored leads board (ops/leads_board.py).

Hermetic: GHL creds blanked so the Lead post_save sync can't hit the network;
send_sms / send-window patched where the offer action is exercised.
"""

import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ghl_integration.models import FollowUpTask
from ghl_integration.pre_pickup import NUDGE_STEP
from ops.models import OperationalTask
from reservations.models import Lead

SEND_SMS = "ghl_integration.services.GoHighLevelService.send_sms"
IN_WINDOW = "ghl_integration.timing.is_within_send_window"


@override_settings(GHL_API_KEY="", GHL_LOCATION_ID="")
class LeadsBoardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = get_user_model().objects.create_user(
            username="boardstaff", password="x", is_staff=True
        )

    def setUp(self):
        self.client.force_login(self.staff)
        self.date = timezone.localdate() + timedelta(days=5)
        # Neutralize the Lead-create GHL sync background thread so tests stay
        # hermetic and deterministic (it otherwise races SQLite + can outlive
        # the override_settings context).
        p = patch("ghl_integration.runner.run_in_background", lambda fn, *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    def _lead(self, **kw):
        d = dict(
            first_name="A", last_name="B", phone="407-555-0100",
            pickup_location="MCO", dropoff_location="Disney",
            pickup_date=self.date, estimated_price=Decimal("120"),
            status=Lead.StatusChoices.NEW,
        )
        d.update(kw)
        return Lead.objects.create(**d)

    def test_requires_staff(self):
        self.client.logout()
        resp = self.client.get(reverse("leads_board"))
        self.assertNotEqual(resp.status_code, 200)  # redirected to login

    def test_board_renders_and_buckets(self):
        self._lead(phone="407-555-0001")  # safe
        self._lead(phone="407-555-0002", status=Lead.StatusChoices.CONVERTED, converted=True)  # booked
        self._lead(phone="407-555-0003", last_reply_at=timezone.now())  # active
        self._lead(phone="407-555-0004", sms_opt_out=True)  # opted out

        resp = self.client.get(reverse("leads_board"), {"date": self.date.isoformat()})
        self.assertEqual(resp.status_code, 200)
        c = resp.context["counts"]
        self.assertEqual(resp.context["total"], 4)
        self.assertEqual(c["safe"], 1)
        self.assertEqual(c["booked"], 1)
        self.assertEqual(c["active"], 1)
        self.assertEqual(c["opted_out"], 1)

    def test_other_dates_excluded(self):
        self._lead(phone="407-555-0005")
        resp = self.client.get(reverse("leads_board"),
                               {"date": (self.date + timedelta(days=1)).isoformat()})
        self.assertEqual(resp.context["total"], 0)

    def test_send_offer_sends_and_marks_nudged(self):
        lead = self._lead(phone="407-555-0006", ghl_contact_id="C9")
        with patch(SEND_SMS, return_value=True), patch(IN_WINDOW, return_value=True):
            resp = self.client.post(
                reverse("leads_board_send_nudge"),
                data=json.dumps({"lead_id": lead.id}),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])
        self.assertTrue(
            FollowUpTask.objects.filter(
                lead=lead, step_number=NUDGE_STEP,
                status=FollowUpTask.StatusChoices.SENT,
            ).exists()
        )

    def test_send_offer_blocks_opted_out(self):
        lead = self._lead(phone="407-555-0007", ghl_contact_id="C10", sms_opt_out=True)
        with patch(SEND_SMS, return_value=True) as mock_send, patch(IN_WINDOW, return_value=True):
            resp = self.client.post(
                reverse("leads_board_send_nudge"),
                data=json.dumps({"lead_id": lead.id}),
                content_type="application/json",
            )
        self.assertFalse(resp.json()["success"])
        self.assertIn("sms_opted_out", resp.json()["result"])
        mock_send.assert_not_called()

    def test_create_task(self):
        lead = self._lead(phone="407-555-0008")
        resp = self.client.post(
            reverse("leads_board_create_task"),
            data=json.dumps({"lead_id": lead.id, "note": "call them"}),
            content_type="application/json",
        )
        self.assertTrue(resp.json()["success"])
        self.assertTrue(
            OperationalTask.objects.filter(
                lead=lead, task_type=OperationalTask.TaskType.MANUAL
            ).exists()
        )

    def test_mark_lost(self):
        lead = self._lead(phone="407-555-0009")
        resp = self.client.post(
            reverse("leads_board_mark_lost"),
            data=json.dumps({"lead_id": lead.id}),
            content_type="application/json",
        )
        self.assertTrue(resp.json()["success"])
        lead.refresh_from_db()
        self.assertEqual(lead.status, Lead.StatusChoices.LOST)

    def test_offer_preview_renders(self):
        lead = self._lead(phone="407-555-0013", first_name="Zeke")
        resp = self.client.get(reverse("leads_board_offer_preview"), {"lead_id": lead.id})
        self.assertEqual(resp.status_code, 200)
        d = resp.json()
        self.assertTrue(d["sendable"])
        self.assertNotIn("{first_name}", d["message"])  # placeholders filled
        self.assertIn("Zeke", d["message"])

    def test_offer_preview_discount_not_sendable(self):
        lead = self._lead(phone="407-555-0014", pre_pickup_discount=Decimal("25"))
        resp = self.client.get(reverse("leads_board_offer_preview"), {"lead_id": lead.id})
        d = resp.json()
        self.assertFalse(d["sendable"])
        self.assertEqual(d["variant"], "discount")

    def test_send_with_edited_message(self):
        lead = self._lead(phone="407-555-0012", ghl_contact_id="C11")
        custom = "Custom hand-written offer just for you!"
        with patch(SEND_SMS, return_value=True) as mock_send, patch(IN_WINDOW, return_value=True):
            resp = self.client.post(
                reverse("leads_board_send_nudge"),
                data=json.dumps({"lead_id": lead.id, "message": custom}),
                content_type="application/json",
            )
        self.assertTrue(resp.json()["success"])
        self.assertEqual(mock_send.call_args[0][1], custom)  # the edited text was sent
        task = FollowUpTask.objects.get(lead=lead, step_number=NUDGE_STEP)
        self.assertEqual(task.message_body, custom)

    def test_detail_timeline(self):
        from ghl_integration.models import LeadActivity
        lead = self._lead(phone="407-555-0011")
        FollowUpTask.objects.create(
            lead=lead, step_number=3, segment="general",
            status=FollowUpTask.StatusChoices.SENT,
            scheduled_at=timezone.now(), sent_at=timezone.now(),
            message_body="our outbound text",
        )
        LeadActivity.objects.create(
            lead=lead, activity_type=LeadActivity.ActivityType.REPLY_RECEIVED,
            description="reply", metadata={"message_body": "their reply text"},
        )
        resp = self.client.get(reverse("leads_board_detail"), {"lead_id": lead.id})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["name"], "A B")
        texts = [e["text"] for e in data["timeline"]]
        dirs = {e["dir"] for e in data["timeline"]}
        self.assertIn("our outbound text", texts)
        self.assertIn("their reply text", texts)
        self.assertEqual({"in", "out"}, dirs & {"in", "out"})
