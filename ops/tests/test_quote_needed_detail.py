"""The "QUOTE NEEDED" task page shows the whole request, not a text dump.

A guest asks the website for a price on a route the rate card cannot quote and
a manual task is filed for a person to send one. The page used to show five
lines of description and a name. Pinned here: the page renders the route, the
date and how soon, the vehicle they chose with what it holds, the quotes on
file, what automation already sent, and a calculator link prefilled with the
route — and a manual task with no lead behind it is untouched.
"""
import json
from datetime import timedelta
from decimal import Decimal

from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ghl_integration.models import LeadActivity
from ops.models import OperationalTask
from rates.models import Vehicle
from reservations.models import Lead, Quote


@override_settings(GHL_API_KEY="", GHL_LOCATION_ID="")
class QuoteNeededDetailTests(TestCase):
    """Saving a Lead kicks off a GoHighLevel sync in a background thread, which
    races SQLite here. Same neutralisation as ops/tests/test_leads_board.py."""

    def setUp(self):
        p = patch("reservations.utils._run_in_background", lambda fn, *a, **k: None)
        p.start()
        self.addCleanup(p.stop)
        self.client.force_login(self.staff)

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username="qn_disp", password="x", is_staff=True, first_name="Dana")
        cls.suv = Vehicle.objects.create(vehicle_type="suv", capacity=6, luggage_capacity=6)

    def _lead(self, **kw):
        defaults = dict(
            first_name="Angeline", last_name="Owens",
            email="angeline@example.com", phone="17734167497",
            pickup_location="Sanford Int'l Airport",
            dropoff_location="Orlando International Airport",
            pickup_date=timezone.localdate() + timedelta(days=9),
            trip_type="oneway", vehicle=self.suv, utm_source="google",
        )
        defaults.update(kw)
        return Lead.objects.create(**defaults)

    def _task(self, lead):
        return OperationalTask.objects.create(
            task_type=OperationalTask.TaskType.MANUAL,
            title="QUOTE NEEDED — Angeline Owens: Sanford Int'l Airport → Orlando International Airport",
            description="Custom route with no online rate — send this guest a price.",
            priority=OperationalTask.Priority.HIGH, due_at=timezone.now(),
            lead=lead, metadata={"source": "quote_form_no_rate"},
        )

    def test_the_page_shows_the_request_on_one_screen(self):
        lead = self._lead()
        Quote.objects.create(
            lead=lead, pickup_location=lead.pickup_location,
            dropoff_location=lead.dropoff_location, pickup_date=lead.pickup_date,
            trip_type="oneway", vehicle=self.suv, status="pending", is_current=True)
        LeadActivity.objects.create(
            lead=lead, activity_type="sms_sent",
            description="Hi Angeline, thanks for asking about a ride...")
        task = self._task(lead)

        resp = self.client.get(reverse("task_detail", args=[task.id]))

        self.assertEqual(resp.status_code, 200)
        ctx = resp.context
        self.assertTrue(ctx["is_quote_needed"])
        self.assertEqual(ctx["qn_vehicle_label"], "SUV")
        self.assertEqual(ctx["qn_when_relative"], "in 9 days")
        self.assertEqual(ctx["qn_when_tone"], "amber")
        self.assertEqual(ctx["qn_source"], "Google Ads")
        self.assertEqual(len(ctx["qn_quotes"]), 1)
        self.assertEqual(len(ctx["qn_touches"]), 1)
        self.assertContains(resp, "Text Angeline")
        self.assertContains(resp, "Open in the calculator")
        self.assertContains(resp, "Working out the price")
        self.assertContains(resp, "up to 6")
        self.assertContains(resp, "no online rate")
        self.assertContains(resp, "SMS Sent")
        # Where the lead stands, in sentences.
        texts = [line["text"] for line in ctx["qn_standing"]]
        self.assertTrue(any(t.startswith("Texted 1 time by the automatic follow-ups") for t in texts), texts)
        self.assertIn("No email has gone out.", texts)
        self.assertIn("No reply from them yet.", texts)
        self.assertTrue(any(t.startswith("Nobody from the team has contacted them yet") for t in texts), texts)
        # The raw description no longer heads the page.
        self.assertNotContains(resp, "Custom route with no online rate — send this guest a price.")

    def test_the_calculator_link_carries_the_route_car_and_trip(self):
        task = self._task(self._lead(trip_type="roundtrip"))
        url = self.client.get(reverse("task_detail", args=[task.id])).context["qn_calculator_url"]
        self.assertTrue(url.startswith(reverse("quote_calculator") + "?"))
        self.assertIn("pickup=Sanford+Int%27l+Airport", url)
        self.assertIn("dropoff=Orlando+International+Airport", url)
        self.assertIn("vehicle=suv", url)
        self.assertIn("trip=roundtrip", url)

    def test_the_text_is_written_with_the_price_left_to_fill(self):
        task = self._task(self._lead())
        ctx = self.client.get(reverse("task_detail", args=[task.id])).context
        compose = ctx["qn_compose"]
        self.assertEqual(compose["phone"], "17734167497")
        self.assertEqual(
            compose["sms_with_price"],
            "Hi Angeline, this is Grayson Towncar — thank you for your quote request. "
            "Your one-way SUV from Sanford Int'l Airport to Orlando International Airport "
            f"on {ctx['qn_lead'].pickup_date.strftime('%A, %B ')}{ctx['qn_lead'].pickup_date.day} "
            "comes to $PRICE. Reply to this text or call us and we'll hold the car for you.",
        )
        self.assertIn("will text you the price shortly", compose["sms_holding"])
        self.assertIn("$PRICE", compose["email_body"])
        self.assertFalse(compose["opted_out"])
        self.assertEqual(ctx["qn_phone_pretty"], "(773) 416-7497")

    def test_the_page_carries_the_request_that_prices_the_trip(self):
        task = self._task(self._lead(trip_type="roundtrip"))
        req = self.client.get(reverse("task_detail", args=[task.id])).context["qn_compose"]["price_request"]
        self.assertEqual(req, {
            "pickup": "Sanford Int'l Airport", "dropoff": "Orlando International Airport",
            "vehicle": "suv", "trip_type": "roundtrip", "cache": True,
        })

    def test_no_route_means_no_price_request(self):
        task = self._task(self._lead(dropoff_location=""))
        ctx = self.client.get(reverse("task_detail", args=[task.id])).context
        self.assertIsNone(ctx["qn_compose"]["price_request"])

    def test_a_ten_digit_phone_gets_the_country_code(self):
        task = self._task(self._lead(phone="(773) 416-7497"))
        ctx = self.client.get(reverse("task_detail", args=[task.id])).context
        self.assertEqual(ctx["qn_compose"]["phone"], "17734167497")

    def test_opted_out_lead_shows_the_warning_first(self):
        task = self._task(self._lead(sms_opt_out=True))
        resp = self.client.get(reverse("task_detail", args=[task.id]))
        self.assertTrue(resp.context["qn_compose"]["opted_out"])
        self.assertEqual(resp.context["qn_standing"][0]["tone"], "bad")
        self.assertContains(resp, "opted out of texts. Call or email instead.")

    def test_soon_and_far_dates_get_the_right_urgency(self):
        soon = self._task(self._lead(pickup_date=timezone.localdate() + timedelta(days=1)))
        far = self._task(self._lead(pickup_date=timezone.localdate() + timedelta(days=40)))
        none = self._task(self._lead(pickup_date=None, vehicle=None))
        self.assertEqual(self.client.get(reverse("task_detail", args=[soon.id])).context["qn_when_tone"], "red")
        self.assertEqual(self.client.get(reverse("task_detail", args=[far.id])).context["qn_when_tone"], "ok")
        resp = self.client.get(reverse("task_detail", args=[none.id]))
        self.assertEqual(resp.context["qn_when_relative"], "no date given")
        self.assertContains(resp, "None picked")

    def test_a_manual_task_without_a_lead_is_untouched(self):
        task = OperationalTask.objects.create(
            task_type=OperationalTask.TaskType.MANUAL, title="Call the mechanic",
            description="Ask about the van's brakes.", due_at=timezone.now())
        resp = self.client.get(reverse("task_detail", args=[task.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("is_quote_needed", resp.context)
        self.assertContains(resp, "Ask about the van")

    def test_the_calculator_page_still_renders(self):
        resp = self.client.get(reverse("quote_calculator") + "?pickup=A&dropoff=B&vehicle=suv&trip=oneway")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "prefillFromQuery")


@override_settings(GHL_API_KEY="", GHL_LOCATION_ID="")
class LeadContactedTests(TestCase):
    """Texting, calling or emailing from the task records the contact on the
    lead, on the task, and hands the task to the person who did it."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username="qn_contact", password="x", is_staff=True, first_name="Dana", last_name="Lee")

    def setUp(self):
        p = patch("reservations.utils._run_in_background", lambda fn, *a, **k: None)
        p.start()
        self.addCleanup(p.stop)
        self.client.force_login(self.staff)
        self.lead = Lead.objects.create(
            first_name="Angeline", last_name="Owens", phone="17734167497",
            email="angeline@example.com", pickup_location="Sanford", dropoff_location="MCO")
        self.task = OperationalTask.objects.create(
            task_type=OperationalTask.TaskType.MANUAL, title="QUOTE NEEDED — Angeline Owens",
            due_at=timezone.now(), lead=self.lead, metadata={"source": "quote_form_no_rate"})

    def _post(self, channel, task=None):
        import json
        return self.client.post(
            reverse("task_lead_contacted"),
            json.dumps({"task_id": (task or self.task).id, "channel": channel}),
            content_type="application/json")

    def test_texting_marks_the_lead_contacted_and_logs_it(self):
        resp = self._post("sms")
        body = resp.json()
        self.assertTrue(body["success"], body)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, "contacted")
        self.assertEqual(self.lead.contact_attempts, 1)
        self.assertIsNotNone(self.lead.last_contact_date)
        activity = LeadActivity.objects.get(lead=self.lead)
        self.assertIn("Dana Lee texted Angeline", activity.description)
        self.task.refresh_from_db()
        self.assertEqual(self.task.attempts, 1)
        self.assertEqual(self.task.comm_attempts.get().channel, "sms")
        self.assertEqual(self.task.comm_attempts.get().outcome, "sent")
        # It is now Dana's task.
        self.assertEqual(self.task.assigned_to_id, self.staff.id)
        self.assertEqual(self.task.status, OperationalTask.Status.IN_PROGRESS)
        self.assertTrue(body["claimed"])

    def test_a_call_marks_contacted_but_leaves_the_outcome_to_the_person(self):
        self._post("call")
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, "contacted")
        self.assertEqual(self.task.comm_attempts.count(), 0)

    def test_a_lead_already_interested_keeps_its_status(self):
        Lead.objects.filter(pk=self.lead.pk).update(status="interested")
        self._post("email")
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, "interested")
        self.assertEqual(self.lead.contact_attempts, 1)

    def test_texting_an_opted_out_lead_is_refused(self):
        Lead.objects.filter(pk=self.lead.pk).update(sms_opt_out=True)
        resp = self._post("sms")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("opted out", resp.json()["error"])
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.contact_attempts, 0)

    def test_a_task_without_a_lead_is_refused(self):
        plain = OperationalTask.objects.create(
            task_type=OperationalTask.TaskType.MANUAL, title="Call the mechanic", due_at=timezone.now())
        self.assertEqual(self._post("call", task=plain).status_code, 400)

    def test_a_claimed_task_stays_with_its_owner(self):
        other = User.objects.create_user(username="qn_other", password="x", is_staff=True)
        OperationalTask.objects.filter(pk=self.task.pk).update(assigned_to=other)
        body = self._post("sms").json()
        self.task.refresh_from_db()
        self.assertEqual(self.task.assigned_to_id, other.id)
        self.assertFalse(body["claimed"])


@override_settings(GHL_API_KEY="", GHL_LOCATION_ID="")
class AutomationContextTests(TestCase):
    """The page shows every automatic message that reached the guest, with its
    full text, and every one still scheduled — rendered as it would go out —
    so a dispatcher never repeats what the robot already said. And the
    automatic texts can be stopped from the task once a person takes over."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username="qn_auto", password="x", is_staff=True, first_name="Dana", last_name="Lee")

    def setUp(self):
        p = patch("reservations.utils._run_in_background", lambda fn, *a, **k: None)
        p.start()
        self.addCleanup(p.stop)
        self.client.force_login(self.staff)
        from ghl_integration.models import FollowUpSequence, FollowUpTask, GHLSyncLog
        self.lead = Lead.objects.create(
            first_name="Angeline", last_name="Owens", phone="17734167497",
            email="angeline@example.com", pickup_location="Sanford Int'l Airport",
            dropoff_location="Orlando International Airport",
            pickup_date=timezone.localdate() + timedelta(days=9),
            trip_type="oneway", segment="general", sequence_active=True,
            initial_sms_sent=True, initial_sms_sent_at=timezone.now() - timedelta(hours=5),
            ghl_contact_id="c123", status="contacted")
        self.task = OperationalTask.objects.create(
            task_type=OperationalTask.TaskType.MANUAL, title="QUOTE NEEDED — Angeline Owens",
            due_at=timezone.now(), lead=self.lead, metadata={"source": "quote_form_no_rate"})
        # The first text lives only in the sync log.
        log = GHLSyncLog.objects.create(
            lead=self.lead, action="send_sms", status="success",
            request_payload={"message": "Hey Angeline, this is Grayson Towncar. Do you still need transportation from Sanford Int'l Airport to Orlando International Airport on November 29? ", "contact_id": "c123"})
        # created_at is auto-set; in production it is the moment the text went.
        GHLSyncLog.objects.filter(pk=log.pk).update(created_at=timezone.now() - timedelta(hours=5))
        FollowUpTask.objects.create(lead=self.lead, step_number=1, segment="general", status="sent",
                                    scheduled_at=timezone.now() - timedelta(hours=5), sent_at=timezone.now() - timedelta(hours=5),
                                    message_body="(initial SMS — sent via sync_lead_to_ghl_and_send_sms)")
        FollowUpTask.objects.create(lead=self.lead, step_number=2, segment="general", status="sent",
                                    scheduled_at=timezone.now() - timedelta(hours=1), sent_at=timezone.now() - timedelta(hours=1),
                                    message_body="Hey Angeline! Wanted to make sure my earlier message came through.")
        self.step3 = FollowUpTask.objects.create(lead=self.lead, step_number=3, segment="general", status="pending",
                                                 scheduled_at=timezone.now() + timedelta(hours=15))
        self.step4 = FollowUpTask.objects.create(lead=self.lead, step_number=4, segment="general", status="pending",
                                                 scheduled_at=timezone.now() + timedelta(hours=43))
        FollowUpSequence.objects.update_or_create(step_number=3, segment="general", defaults={
            "delay_hours": 20, "is_active": True,
            "message_template": "Hey {first_name} — still looking for a ride on {pickup_date}?"})
        FollowUpSequence.objects.update_or_create(step_number=4, segment="general", defaults={
            "delay_hours": 48, "is_active": True,
            "message_template": "Hey {first_name}, your quoted rate of {estimated_price} for the {pickup_date} trip is still available."})
        LeadActivity.objects.create(
            lead=self.lead, activity_type="reply_received",
            description="SMS reply received: Yes please, how much?",
            metadata={"message_body": "Yes please, how much?"})

    def _ctx(self):
        return self.client.get(reverse("task_detail", args=[self.task.id])).context

    def test_every_message_that_reached_them_is_shown_in_full(self):
        sent = self._ctx()["qn_sent"]
        self.assertEqual([m["who"] for m in sent], ["Automatic text 1", "Automatic text 2", "Angeline replied"])
        self.assertTrue(sent[0]["body"].startswith("Hey Angeline, this is Grayson Towncar. Do you still need"))
        self.assertEqual(sent[2]["body"], "Yes please, how much?")
        self.assertEqual(sent[2]["kind"], "reply")

    def test_scheduled_texts_are_rendered_and_the_price_one_is_flagged(self):
        ctx = self._ctx()
        up = ctx["qn_upcoming"]
        self.assertEqual([u["step"] for u in up], [3, 4])
        self.assertEqual(up[0]["body"], f"Hey Angeline — still looking for a ride on {self.lead.pickup_date.strftime('%B %-d')}?")
        self.assertFalse(up[0]["skipped"])
        self.assertTrue(up[1]["skipped"])
        self.assertIn("quotes a website price and there is none", up[1]["why"])
        self.assertEqual(ctx["qn_next_auto"], self.step3.scheduled_at)
        self.assertTrue(ctx["qn_sequence_active"])
        self.assertIn("skipped", ctx["qn_nudge_note"])

    def test_the_page_offers_to_stop_the_texts(self):
        resp = self.client.get(reverse("task_detail", args=[self.task.id]))
        self.assertContains(resp, "Stop the automatic texts")
        self.assertContains(resp, "What has reached them so far")
        self.assertContains(resp, "Yes please, how much?")

    def test_stopping_cancels_the_pending_texts_and_records_who(self):
        resp = self.client.post(
            reverse("task_lead_stop_sequence"), json.dumps({"task_id": self.task.id}),
            content_type="application/json")
        body = resp.json()
        self.assertTrue(body["success"], body)
        self.assertEqual(body["cancelled"], 2)
        self.step3.refresh_from_db(); self.step4.refresh_from_db()
        self.assertEqual(self.step3.status, "cancelled")
        self.assertEqual(self.step3.cancel_reason, "manual")
        self.lead.refresh_from_db()
        self.assertFalse(self.lead.sequence_active)
        self.assertTrue(LeadActivity.objects.filter(
            lead=self.lead, activity_type="sequence_stopped",
            description__startswith="Dana Lee stopped").exists())
        self.assertEqual(self._ctx()["qn_upcoming"], [])
