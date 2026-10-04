"""Tests for editing an agent on the dispatch-side profile page, and for Agent View.

Run with:  ./manage.py test users.tests_agent_profile
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from reservations.models import AuditLog
from users.models import Agency, TravelAgent


def make_agent(**fields):
    user = User.objects.create_user(username="jane", email="jane@example.com")
    data = dict(user=user, agent_name="Jane Doe", phone="407-555-0100", commission_rate=Decimal("10.00"),
                payment_method="paypal", payment_info="jane@paypal.example")
    data.update(fields)
    agent = TravelAgent.objects.create(**data)
    agent.refresh_from_db()
    return agent


class ProfileEditTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("disp", password="x", is_staff=True)
        self.client.force_login(self.staff)
        self.agent = make_agent()
        self.url = reverse("admin_travel_agent_detail", args=[self.agent.pk])

    def _post(self, **changes):
        data = {
            "agent_name": self.agent.agent_name, "email": self.agent.user.email, "phone": self.agent.phone,
            "commission_rate": str(self.agent.commission_rate), "is_active": "on",
            "pay-payment_method": "paypal", "pay-paypal_email": "jane@paypal.example",
        }
        data.update(changes)
        data = {k: v for k, v in data.items() if v is not None}
        return self.client.post(self.url, data)

    def _messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def test_page_offers_edit_instead_of_admin(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Edit profile")
        self.assertNotContains(response, "Edit in Admin")

    def test_saves_changes_and_logs_each_field(self):
        response = self._post(**{"pay-payment_method": "venmo", "pay-venmo_phone": "(407) 555-0123",
                                 "pay-venmo_username": "@Jane-Doe"},
                              phone="407-555-0199", email="jane.new@example.com")
        self.assertRedirects(response, self.url)
        self.agent.refresh_from_db()
        self.assertEqual((self.agent.payment_method, self.agent.payment_info, self.agent.venmo_username,
                          self.agent.phone), ("venmo", "407-555-0123", "Jane-Doe", "407-555-0199"))
        self.assertEqual(self.agent.user.email, "jane.new@example.com")
        logged = set(AuditLog.objects.filter(model_name="TravelAgent", object_id=self.agent.pk)
                     .values_list("field_name", flat=True))
        self.assertEqual(logged, {"payment_method", "payment_info", "venmo_username", "phone", "email"})
        info = AuditLog.objects.get(object_id=self.agent.pk, field_name="payment_info")
        self.assertEqual((info.old_value, info.new_value, info.username),
                         ("jane@paypal.example", "407-555-0123", "disp"))

    def test_nothing_changed(self):
        response = self._post()
        self.assertIn("Nothing changed.", self._messages(response))
        self.assertFalse(AuditLog.objects.filter(model_name="TravelAgent").exists())

    def test_bad_rate_keeps_editor_open_and_saves_nothing(self):
        response = self._post(commission_rate="150", phone="111")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["edit_open"])
        self.assertContains(response, "between 0 and 100")
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.phone, "407-555-0100")

    def test_email_used_by_someone_else_is_refused(self):
        User.objects.create_user("other", email="taken@example.com")
        response = self._post(email="TAKEN@example.com")
        self.assertContains(response, "Another account already uses this email.")

    def test_agency_pays_needs_an_agency(self):
        response = self._post(agency_handles_payment="on")
        self.assertContains(response, "Link them to an agency first")

    def test_agency_pays_with_an_agency(self):
        agency = Agency.objects.create(name="Ears Travel", payment_method="paypal", payment_info="ears@x.com")
        TravelAgent.objects.filter(pk=self.agent.pk).update(agency=agency)
        self._post(agency_handles_payment="on")
        self.agent.refresh_from_db()
        self.assertTrue(self.agent.agency_handles_payment)

    def test_venmo_needs_a_phone(self):
        response = self._post(**{"pay-payment_method": "venmo", "pay-venmo_phone": "555-0100"})
        self.assertContains(response, "10-digit US phone number on your Venmo")
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.payment_method, "paypal")

    def test_bank_details_saved_and_account_number_masked_in_log(self):
        response = self._post(**{
            "pay-payment_method": "bank", "pay-bank_account_holder": "Jane Doe",
            "pay-bank_account_type": "checking", "pay-bank_routing_number": "021000021",
            "pay-bank_account_number": "000123456789", "pay-bank_account_number_confirm": "000123456789",
        })
        self.assertRedirects(response, self.url)
        self.agent.refresh_from_db()
        self.assertEqual((self.agent.bank_routing_number, self.agent.bank_account_number),
                         ("021000021", "000123456789"))
        self.assertEqual(self.agent.payment_info, "Checking ••••6789 · Jane Doe")
        log = AuditLog.objects.get(object_id=self.agent.pk, field_name="bank_account_number")
        self.assertEqual(log.new_value, "••••6789")
        self.assertFalse(AuditLog.objects.filter(new_value__contains="000123456789").exists())

    def test_staff_picking_agency_does_not_link_on_its_own(self):
        Agency.objects.create(name="Ears Travel")
        TravelAgent.objects.filter(pk=self.agent.pk).update(agency_name="Ears Travel")
        self._post(**{"pay-payment_method": "agency"})
        self.agent.refresh_from_db()
        self.assertEqual((self.agent.payment_method, self.agent.agency_id), ("agency", None))

    def test_switching_off(self):
        self._post(is_active=None)
        self.agent.refresh_from_db()
        self.assertFalse(self.agent.is_active)

    def test_non_staff_cannot_edit(self):
        self.client.force_login(User.objects.create_user("guest", password="x"))
        self._post(phone="000")
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.phone, "407-555-0100")


class AgentViewAccessTests(TestCase):
    """The "Agent View" button on the profile used to 403 for staff."""

    def setUp(self):
        self.agent = make_agent()
        self.url = reverse("agent_detail", args=[self.agent.pk])

    def test_staff_can_open_it(self):
        self.client.force_login(User.objects.create_user("disp", password="x", is_staff=True))
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_the_agent_can_open_it(self):
        self.client.force_login(self.agent.user)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_another_agent_cannot(self):
        other = User.objects.create_user("bob", password="x")
        self.client.force_login(other)
        self.assertEqual(self.client.get(self.url).status_code, 403)
