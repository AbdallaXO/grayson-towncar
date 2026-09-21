"""A text that quotes the website price never goes to a lead who never got one.

Found 2026-09-21 while laying out the QUOTE NEEDED task page: step 4 of the
follow-up sequence reads "your quoted rate of {estimated_price} …", and for a
custom-route lead (no online rate — the very leads that raise a quote task)
the renderer filled the blank with nothing. Sixty texts went out between
August and September saying "your quoted rate of  for the October 27 trip".
The pre-pickup nudge has the same placeholder. Both senders now skip the step
for a lead with no price, with the reason on the row.
"""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from ghl_integration.models import FollowUpSequence, FollowUpTask
from ghl_integration.tasks import process_follow_up_batch
from ghl_integration.templates_engine import template_needs_price
from reservations.models import Lead


@override_settings(GHL_API_KEY="", GHL_LOCATION_ID="")
class PriceGuardTests(TestCase):
    def setUp(self):
        p = patch("reservations.utils._run_in_background", lambda fn, *a, **k: None)
        p.start()
        self.addCleanup(p.stop)
        self.lead = Lead.objects.create(
            first_name="Rachel", last_name="Kim", phone="14075550101",
            pickup_location="Kissimmee", dropoff_location="Port Canaveral",
            pickup_date=timezone.localdate() + timedelta(days=20),
            trip_type="oneway", segment="general", sequence_active=True,
            initial_sms_sent=True, ghl_contact_id="c9", status="contacted")
        FollowUpSequence.objects.update_or_create(step_number=3, segment="general", defaults={
            "delay_hours": 20, "is_active": True,
            "message_template": "Hey {first_name} — still looking for a ride on {pickup_date}?"})
        FollowUpSequence.objects.update_or_create(step_number=4, segment="general", defaults={
            "delay_hours": 48, "is_active": True,
            "message_template": "Hey {first_name}, your quoted rate of {estimated_price} for the {pickup_date} trip is still available."})

    def _due(self, step):
        return FollowUpTask.objects.create(
            lead=self.lead, step_number=step, segment="general", status="pending",
            scheduled_at=timezone.now() - timedelta(minutes=5))

    def _run(self):
        with patch("ghl_integration.timing.is_within_send_window", return_value=True), \
             patch("ghl_integration.services.GoHighLevelService.contact_has_replied", return_value=False), \
             patch("ghl_integration.services.GoHighLevelService.send_sms", return_value=True) as send:
            process_follow_up_batch()
        return send

    def test_template_needs_price_reads_the_placeholder(self):
        self.assertTrue(template_needs_price("your quoted rate of {estimated_price}"))
        self.assertFalse(template_needs_price("still looking for a ride on {pickup_date}?"))
        self.assertFalse(template_needs_price(None))

    def test_the_price_step_is_skipped_for_a_lead_with_no_price(self):
        task = self._due(4)
        send = self._run()
        send.assert_not_called()
        task.refresh_from_db()
        self.assertEqual(task.status, "cancelled")
        self.assertEqual(task.cancel_reason, "no_price")

    def test_a_step_without_a_price_still_goes_out(self):
        task = self._due(3)
        send = self._run()
        send.assert_called_once()
        self.assertIn("Hey Rachel — still looking", send.call_args.args[1])
        task.refresh_from_db()
        self.assertEqual(task.status, "sent")

    def test_the_price_step_goes_out_when_the_lead_has_a_price(self):
        Lead.objects.filter(pk=self.lead.pk).update(estimated_price="125.00")
        task = self._due(4)
        send = self._run()
        send.assert_called_once()
        self.assertIn("your quoted rate of $125 for", send.call_args.args[1])
        task.refresh_from_db()
        self.assertEqual(task.status, "sent")
