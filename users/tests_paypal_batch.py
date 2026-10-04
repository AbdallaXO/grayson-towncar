"""Tests for the PayPal / Venmo bulk payout file.

Run with:  ./manage.py test users.tests_paypal_batch

Agents type their own payout handle into one free-text box, so the file has to
read "Venmo: @Jane-Doe", a venmo.com link, or a bare email -- and refuse to
guess when it can't tell, because a wrong guess sends money to a stranger.
The mark-paid step must only record what PayPal actually sent.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from reservations.models import Reservation
from users.models import Agency, CommissionPayout, TravelAgent
from users.paypal_batch import build_batch, csv_line, read_recipient
from users.services import process_bulk_payouts
from users.tests_eligibility import _add_leg, _bootstrap, _make_reservation


class ReadPayPalRecipientTests(SimpleTestCase):
    def test_plain_email(self):
        self.assertEqual(read_recipient("paypal", "jane.doe@gmail.com"), ("jane.doe@gmail.com", ""))

    def test_labelled_email_with_extra_text(self):
        info = "PayPal email: jane@gmail.com (paypal.me/JaneDoe)"
        self.assertEqual(read_recipient("paypal", info), ("jane@gmail.com", ""))

    def test_email_next_to_a_venmo_handle(self):
        info = "PayPal: jane@gmail.com - Venmo: @Jane-Doe"
        self.assertEqual(read_recipient("paypal", info), ("jane@gmail.com", ""))

    def test_two_different_emails_is_refused(self):
        recipient, problem = read_recipient("paypal", "jane@gmail.com or jdoe@work.com")
        self.assertEqual(recipient, "")
        self.assertTrue(problem)

    def test_same_email_twice_is_fine(self):
        info = "jane@gmail.com (jane@gmail.com)"
        self.assertEqual(read_recipient("paypal", info), ("jane@gmail.com", ""))

    def test_no_email_is_refused(self):
        for info in ("@JaneDoe2024", "Jane", "999", ""):
            recipient, problem = read_recipient("paypal", info)
            self.assertEqual(recipient, "", info)
            self.assertTrue(problem, info)


class ReadVenmoRecipientTests(SimpleTestCase):
    def test_handle(self):
        self.assertEqual(read_recipient("venmo", "@Jane-Doe-7"), ("@Jane-Doe-7", ""))

    def test_labelled_handle(self):
        self.assertEqual(read_recipient("venmo", "Venmo: @Jane-Doe"), ("@Jane-Doe", ""))

    def test_handle_with_trailing_note(self):
        info = "@jdoe (last 4 of my phone are 1234)"
        self.assertEqual(read_recipient("venmo", info), ("@jdoe", ""))

    def test_name_then_handle_on_next_line(self):
        self.assertEqual(read_recipient("venmo", "Jane Doe\n @jdoe1234"), ("@jdoe1234", ""))

    def test_profile_link(self):
        info = "https://venmo.com/u/JaneDoe"
        self.assertEqual(read_recipient("venmo", info), ("@JaneDoe", ""))

    def test_bare_handle_with_hyphen(self):
        self.assertEqual(read_recipient("venmo", "Jane-Doe"), ("@Jane-Doe", ""))

    def test_bare_single_word_is_refused(self):
        # "Joseph" could be a first name; @Joseph is somebody else's account.
        recipient, problem = read_recipient("venmo", "Joseph")
        self.assertEqual(recipient, "")
        self.assertTrue(problem)

    def test_email(self):
        self.assertEqual(read_recipient("venmo", "jane@gmail.com"), ("jane@gmail.com", ""))

    def test_phone(self):
        self.assertEqual(read_recipient("venmo", "407-555-0123"), ("4075550123", ""))
        self.assertEqual(read_recipient("venmo", "+1 (407) 555 0123"), ("4075550123", ""))

    def test_two_different_handles_is_refused(self):
        recipient, problem = read_recipient("venmo", "@JaneDoe or @JDoe-77")
        self.assertEqual(recipient, "")
        self.assertTrue(problem)

    def test_email_marked_as_paypal_is_not_used_for_venmo(self):
        info = "jane@gmail.com - PayPal\nVenmo - Jane-Doe"
        recipient, problem = read_recipient("venmo", info)
        self.assertEqual(recipient, "")
        self.assertTrue(problem)

    def test_empty(self):
        recipient, problem = read_recipient("venmo", "   ")
        self.assertEqual(recipient, "")
        self.assertTrue(problem)


class _ReadyAgentMixin:
    """One Venmo agent owed $10.00 (a $100 completed trip at 10%)."""

    def setUp(self):
        self.vehicle, self.rate, self.customer, self.agent = _bootstrap()
        past = timezone.localtime(timezone.now()).date() - timedelta(days=5)
        self.past = past
        res = _make_reservation(self.rate, self.customer, self.agent, status="completed")
        _add_leg(res, pickup_date=past, status="completed")
        self.reservation = res

    def _agent(self, username, *, method, info, agency=None, agency_pays=False):
        user = User.objects.create_user(username=username, email=f"{username}@example.com")
        agent = TravelAgent.objects.create(
            user=user, agent_name=username.title(), phone="555-0100",
            commission_rate=Decimal("10.00"), payment_method=method, payment_info=info,
            agency=agency, agency_handles_payment=agency_pays,
        )
        agent.refresh_from_db()
        res = _make_reservation(self.rate, self.customer, agent, status="completed")
        _add_leg(res, pickup_date=self.past, status="completed")
        return agent


class BuildBatchTests(_ReadyAgentMixin, TestCase):
    def test_owed_venmo_agent_is_in_the_file(self):
        rows, skipped = build_batch()
        self.assertEqual(skipped, [])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row.kind, row.id, row.amount), ("agent", self.agent.id, Decimal("10.00")))
        line = csv_line(row)
        self.assertEqual(line[:4], ["@agent", "10.00", "USD", f"AGT{self.agent.id}"])
        self.assertTrue(line[4])  # Venmo requires a note
        self.assertEqual(line[5:], ["VENMO", "PRIVATE"])

    def test_paypal_row_has_no_venmo_columns(self):
        agent = self._agent("pp", method="paypal", info="pp@gmail.com")
        rows, _ = build_batch()
        row = next(r for r in rows if r.id == agent.id)
        self.assertEqual(csv_line(row)[5:], ["PAYPAL"])

    def test_unreadable_handle_is_skipped_not_guessed(self):
        agent = self._agent("joe", method="venmo", info="Joseph")
        rows, skipped = build_batch()
        self.assertNotIn(agent.id, [r.id for r in rows])
        self.assertEqual([s.id for s in skipped], [agent.id])
        self.assertTrue(skipped[0].problem)

    def test_nothing_owed_is_left_out(self):
        Reservation.objects.filter(travel_agent=self.agent).update(commission_paid=True)
        self.assertEqual(build_batch(), ([], []))

    def test_other_methods_are_left_out(self):
        self.agent.payment_method = "zelle"
        self.agent.save()
        self.assertEqual(build_batch(), ([], []))

    def test_agency_paid_agents_roll_up_to_one_agency_row(self):
        agency = Agency.objects.create(name="Ears Travel", payment_method="paypal", payment_info="ears@agency.com")
        a = self._agent("ann", method="agency", info="", agency=agency, agency_pays=True)
        b = self._agent("bob", method="agency", info="", agency=agency, agency_pays=True)
        rows, _ = build_batch()
        agency_rows = [r for r in rows if r.kind == "agency"]
        self.assertEqual(len(agency_rows), 1)
        self.assertEqual(agency_rows[0].amount, Decimal("20.00"))
        self.assertEqual(agency_rows[0].recipient, "ears@agency.com")
        self.assertNotIn(a.id, [r.id for r in rows if r.kind == "agent"])
        self.assertNotIn(b.id, [r.id for r in rows if r.kind == "agent"])


class ExpectedAmountGuardTests(_ReadyAgentMixin, TestCase):
    def test_matching_amount_is_paid(self):
        [result] = process_bulk_payouts(
            [{"type": "agent", "id": self.agent.id, "expected_amount": "10.00"}], sent_by=None
        )
        self.assertTrue(result["ok"], result)
        self.reservation.refresh_from_db()
        self.assertTrue(self.reservation.commission_paid)

    def test_changed_amount_is_not_paid(self):
        [result] = process_bulk_payouts(
            [{"type": "agent", "id": self.agent.id, "expected_amount": "7.50"}], sent_by=None
        )
        self.assertFalse(result["ok"])
        self.assertEqual((result["expected_amount"], result["owed_now"]), ("7.50", "10.00"))
        self.reservation.refresh_from_db()
        self.assertFalse(self.reservation.commission_paid)

    def test_agency_changed_amount_is_not_paid(self):
        agency = Agency.objects.create(name="Ears Travel", payment_method="paypal", payment_info="ears@agency.com")
        self._agent("ann", method="agency", info="", agency=agency, agency_pays=True)
        [result] = process_bulk_payouts(
            [{"type": "agency", "id": agency.id, "expected_amount": "5.00"}], sent_by=None
        )
        self.assertFalse(result["ok"])
        [result] = process_bulk_payouts(
            [{"type": "agency", "id": agency.id, "expected_amount": "10.00"}], sent_by=None
        )
        self.assertTrue(result["ok"], result)


class BatchPageTests(_ReadyAgentMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_user("disp", password="x", is_staff=True))
        self.url = reverse("paypal_batch")

    def _token(self):
        return self.client.get(self.url).context["token"]

    def test_page_lists_the_agent(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "@agent")

    def test_download_returns_the_file(self):
        response = self.client.post(self.url, {"token": self._token(), "action": "download"})
        self.assertEqual(response["Content-Type"], "text/csv")
        body = response.content.decode()
        self.assertTrue(body.startswith(f"@agent,10.00,USD,AGT{self.agent.id},"))
        self.assertTrue(body.rstrip().endswith("VENMO,PRIVATE"))

    def test_download_refuses_when_amounts_moved(self):
        token = self._token()
        res = _make_reservation(self.rate, self.customer, self.agent, status="completed")
        _add_leg(res, pickup_date=self.past, status="completed")
        response = self.client.post(self.url, {"token": token, "action": "download"})
        self.assertRedirects(response, self.url)

    def test_mark_paid_pays_what_was_listed(self):
        response = self.client.post(self.url, {"token": self._token(), "action": "mark_paid"})
        self.assertRedirects(response, self.url)
        self.reservation.refresh_from_db()
        self.assertTrue(self.reservation.commission_paid)
        payout = CommissionPayout.objects.get(agent=self.agent)
        self.assertEqual(payout.payment_method_used, "venmo")
        self.assertTrue(payout.payment_reference.startswith("PayPal batch"))

    def test_mark_paid_explains_a_moved_amount(self):
        token = self._token()
        res = _make_reservation(self.rate, self.customer, self.agent, status="completed")
        _add_leg(res, pickup_date=self.past, status="completed")
        response = self.client.post(self.url, {"token": token, "action": "mark_paid"}, follow=True)
        self.assertContains(response, "send them the other $10.00")
        self.reservation.refresh_from_db()
        self.assertFalse(self.reservation.commission_paid)

    def _with_agency(self):
        agency = Agency.objects.create(name="Ears Travel", payment_method="paypal", payment_info="ears@agency.com")
        child = self._agent("ann", method="agency", info="", agency=agency, agency_pays=True)
        return agency, Reservation.objects.get(travel_agent=child)

    def test_agents_only_leaves_agencies_out_of_the_file_and_unpaid(self):
        _, agency_res = self._with_agency()
        token = self.client.get(self.url, {"who": "agents"}).context["token"]

        download = self.client.post(self.url, {"token": token, "action": "download"})
        self.assertNotIn("ears@agency.com", download.content.decode())
        self.assertIn("agents-", download["Content-Disposition"])

        response = self.client.post(self.url, {"token": token, "action": "mark_paid"})
        self.assertRedirects(response, f"{self.url}?who=agents")
        self.reservation.refresh_from_db()
        agency_res.refresh_from_db()
        self.assertTrue(self.reservation.commission_paid)
        self.assertFalse(agency_res.commission_paid)

    def test_agencies_only_leaves_direct_agents_unpaid(self):
        _, agency_res = self._with_agency()
        token = self.client.get(self.url, {"who": "agencies"}).context["token"]
        self.client.post(self.url, {"token": token, "action": "mark_paid"})
        self.reservation.refresh_from_db()
        agency_res.refresh_from_db()
        self.assertFalse(self.reservation.commission_paid)
        self.assertTrue(agency_res.commission_paid)

    def test_switch_shows_counts_for_each_group(self):
        self._with_agency()
        groups = {g["key"]: g["count"] for g in self.client.get(self.url).context["groups"]}
        self.assertEqual(groups, {"all": 2, "agents": 1, "agencies": 1})

    def test_names_link_to_agent_and_agency_profiles(self):
        agency, _ = self._with_agency()
        # Paid directly, but still a member of the agency.
        member = self._agent("dee", method="paypal", info="dee@gmail.com", agency=agency)
        response = self.client.get(self.url)
        for url in (
            reverse("admin_travel_agent_detail", args=[self.agent.id]),
            reverse("admin_travel_agent_detail", args=[member.id]),
            reverse("admin_travel_agency_detail", args=[agency.id]),
        ):
            self.assertContains(response, f'href="{url}"')

    def test_forged_token_is_rejected(self):
        response = self.client.post(self.url, {"token": "nope", "action": "mark_paid"})
        self.assertRedirects(response, self.url)
        self.reservation.refresh_from_db()
        self.assertFalse(self.reservation.commission_paid)

    def test_non_staff_cannot_open_it(self):
        self.client.force_login(User.objects.create_user("guest", password="x"))
        self.assertNotEqual(self.client.get(self.url).status_code, 200)
