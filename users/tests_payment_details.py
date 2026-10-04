"""Tests for structured payment details: sign-up, the agent's profile, and the rules.

Run with:  ./manage.py test users.tests_payment_details
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from users.models import Agency, TravelAgent
from users.payment_details import PaymentDetailsForm, routing_number_ok

BANK = {
    "pay-payment_method": "bank", "pay-bank_account_holder": "Jane Doe", "pay-bank_account_type": "savings",
    "pay-bank_routing_number": "011000015", "pay-bank_account_number": "12345678",
    "pay-bank_account_number_confirm": "12345678",
}


def form(data, agent=None, **kw):
    return PaymentDetailsForm(data, agent=agent, **kw)


class RoutingNumberTests(SimpleTestCase):
    def test_real_routing_numbers_pass(self):
        for number in ("021000021", "011000015", "121000358"):
            self.assertTrue(routing_number_ok(number), number)

    def test_bad_ones_fail(self):
        for number in ("123456789", "02100002", "0210000211", "abcdefghi", ""):
            self.assertFalse(routing_number_ok(number), number)


class FormRuleTests(SimpleTestCase):
    def test_a_method_is_required_for_agents(self):
        self.assertFalse(form({"pay-payment_method": ""}).is_valid())
        self.assertTrue(form({"pay-payment_method": ""}, require_method=False).is_valid())

    def test_paypal_needs_an_email(self):
        self.assertFalse(form({"pay-payment_method": "paypal"}).is_valid())
        f = form({"pay-payment_method": "paypal", "pay-paypal_email": "jane@gmail.com"})
        self.assertTrue(f.is_valid())
        self.assertEqual(f.summary(), "jane@gmail.com")

    def test_venmo_needs_a_us_phone_and_the_username_is_optional(self):
        self.assertFalse(form({"pay-payment_method": "venmo", "pay-venmo_username": "@jane"}).is_valid())
        f = form({"pay-payment_method": "venmo", "pay-venmo_phone": "+1 (407) 555-0123", "pay-venmo_username": "@Jane-Doe"})
        self.assertTrue(f.is_valid(), f.errors)
        self.assertEqual((f.summary(), f.cleaned_data["venmo_username"]), ("407-555-0123", "Jane-Doe"))

    def test_venmo_username_must_look_like_one(self):
        f = form({"pay-payment_method": "venmo", "pay-venmo_phone": "4075550123", "pay-venmo_username": "jane doe!"})
        self.assertIn("venmo_username", f.errors)

    def test_zelle_takes_an_email_or_phone(self):
        self.assertEqual(form({"pay-payment_method": "zelle", "pay-zelle_contact": "4075550123"}).is_valid(), True)
        f = form({"pay-payment_method": "zelle", "pay-zelle_contact": "jane@bank.com"})
        self.assertTrue(f.is_valid())
        self.assertEqual(f.summary(), "jane@bank.com")
        self.assertFalse(form({"pay-payment_method": "zelle", "pay-zelle_contact": "Jane at my bank"}).is_valid())

    def test_bank_checks_routing_and_matching_account(self):
        f = form(BANK)
        self.assertTrue(f.is_valid(), f.errors)
        self.assertEqual(f.summary(), "Savings ••••5678 · Jane Doe")
        self.assertIn("bank_routing_number", form({**BANK, "pay-bank_routing_number": "123456789"}).errors)
        self.assertIn("bank_account_number_confirm",
                      form({**BANK, "pay-bank_account_number_confirm": "12345679"}).errors)
        self.assertIn("bank_account_holder", form({**BANK, "pay-bank_account_holder": ""}).errors)

    def test_retired_methods_cannot_be_picked_fresh(self):
        self.assertFalse(form({"pay-payment_method": "cashapp"}).is_valid())
        self.assertFalse(form({"pay-payment_method": "check"}).is_valid())
        self.assertFalse(form({"pay-payment_method": "other"}).is_valid())


def make_agent(username="jane", **fields):
    user = User.objects.create_user(username=username, email=f"{username}@example.com", password="pw-12345!")
    data = dict(user=user, agent_name="Jane Doe", phone="407-555-0100", commission_rate=Decimal("10"),
                payment_method="paypal", payment_info="jane@paypal.example")
    data.update(fields)
    agent = TravelAgent.objects.create(**data)
    agent.refresh_from_db()
    return agent


class ApplyTests(TestCase):
    def test_switching_away_from_bank_clears_the_numbers(self):
        agent = make_agent(payment_method="bank", payment_info="Savings ••••5678 · Jane Doe",
                           bank_account_holder="Jane Doe", bank_account_type="savings",
                           bank_routing_number="011000015", bank_account_number="12345678")
        f = form({"pay-payment_method": "paypal", "pay-paypal_email": "jane@gmail.com"}, agent=agent)
        self.assertTrue(f.is_valid())
        f.apply(agent)
        self.assertEqual((agent.bank_routing_number, agent.bank_account_number), ("", ""))

    def test_agency_links_on_an_exact_name(self):
        agency = Agency.objects.create(name="Best Day Ever Vacations")
        agent = make_agent(agency_name="best day ever vacations")
        f = form({"pay-payment_method": "agency"}, agent=agent)
        self.assertTrue(f.is_valid())
        f.apply(agent)
        self.assertEqual((agent.agency, agent.agency_handles_payment), (agency, True))

    def test_agency_with_an_unknown_name_is_left_for_staff(self):
        agent = make_agent(agency_name="Pixie Vacations")
        f = form({"pay-payment_method": "agency"}, agent=agent)
        f.is_valid()
        f.apply(agent)
        self.assertEqual((agent.agency_id, agent.agency_handles_payment), (None, False))

    def test_handle_only_venmo_offers_the_profile_phone(self):
        agent = make_agent(payment_method="venmo", payment_info="@Jane-Doe", phone="(407) 555-0100")
        f = PaymentDetailsForm(agent=agent)
        self.assertEqual((f.initial["venmo_phone"], f.initial["venmo_username"]), ("407-555-0100", "@Jane-Doe"))

    def test_an_agent_on_a_retired_method_can_keep_it(self):
        agent = make_agent(payment_method="cashapp", payment_info="$JaneDoe")
        f = form({"pay-payment_method": "cashapp", "pay-legacy_details": "$JaneDoe"}, agent=agent)
        self.assertTrue(f.is_valid(), f.errors)
        f.apply(agent)
        self.assertEqual((agent.payment_method, agent.payment_info), ("cashapp", "$JaneDoe"))


class SignUpTests(TestCase):
    url = reverse("register_agent")

    def _signup(self, **pay):
        data = {"username": "newagent", "email": "new@example.com", "agent_name": "New Agent",
                "agency_name": "Solo", "phone": "407-555-0111", "password1": "Str0ng-pass!", "password2": "Str0ng-pass!"}
        data.update(pay)
        return self.client.post(self.url, data)

    def test_page_shows_the_new_fields(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Phone number on your Venmo")
        self.assertNotContains(response, "Cash App")

    def test_venmo_sign_up_saves_the_phone(self):
        self._signup(**{"pay-payment_method": "venmo", "pay-venmo_phone": "407 555 0123", "pay-venmo_username": "new-agent"})
        agent = TravelAgent.objects.get(user__username="newagent")
        self.assertEqual((agent.payment_method, agent.payment_info, agent.venmo_username),
                         ("venmo", "407-555-0123", "new-agent"))

    def test_bad_payment_details_create_nothing(self):
        response = self._signup(**{"pay-payment_method": "venmo", "pay-venmo_username": "@new-agent"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10-digit US phone number on your Venmo")
        self.assertFalse(User.objects.filter(username="newagent").exists())


class AgentOwnProfileTests(TestCase):
    url = reverse("agent_profile")

    def _post(self, **pay):
        data = {"agent_name": "Jane Doe", "agency_name": "Solo", "phone": "407-555-0100"}
        data.update(pay)
        return self.client.post(self.url, data)

    def test_agent_switches_to_bank(self):
        agent = make_agent()
        self.client.force_login(agent.user)
        self._post(**BANK)
        agent.refresh_from_db()
        self.assertEqual((agent.payment_method, agent.bank_account_number, agent.payment_info),
                         ("bank", "12345678", "Savings ••••5678 · Jane Doe"))

    def test_agency_paid_agent_sees_no_payment_fields_and_cannot_redirect_pay(self):
        agency = Agency.objects.create(name="Ears Travel")
        agent = make_agent(agency=agency, agency_handles_payment=True, payment_method="agency", payment_info="")
        self.client.force_login(agent.user)
        response = self.client.get(self.url)
        self.assertContains(response, "Your commission is paid to your agency")
        self.assertNotContains(response, "pay-payment_method")
        self._post(**{"pay-payment_method": "paypal", "pay-paypal_email": "me@gmail.com"})
        agent.refresh_from_db()
        self.assertEqual((agent.payment_method, agent.agency_handles_payment), ("agency", True))

    def test_an_empty_agency_name_is_not_shown_as_none(self):
        agent = make_agent(agency_name=None)
        self.client.force_login(agent.user)
        self.assertNotContains(self.client.get(self.url), 'value="None"')

    def test_rejected_details_save_nothing(self):
        agent = make_agent()
        self.client.force_login(agent.user)
        response = self._post(**{"pay-payment_method": "paypal", "pay-paypal_email": "not-an-email"})
        self.assertEqual(response.status_code, 200)
        agent.refresh_from_db()
        self.assertEqual(agent.payment_info, "jane@paypal.example")
