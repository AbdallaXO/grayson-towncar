"""Tests for linking agents who chose "Agency" to the agency they typed.

Run with:  ./manage.py test users.tests_agency_links

The rule that matters most: only an agent whose OWN payment method is "Agency"
is ever routed through an agency. Someone in Best Day Ever Vacations who picked
Venmo is paid directly, and must never be moved.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from reservations.models import AuditLog
from users.agency_links import CHECK, MISSING, READY, LinkRefused, link_to_agency, proposals
from users.models import Agency, TravelAgent


def make_agent(username, *, method="agency", typed="", agency=None, agency_pays=False):
    user = User.objects.create_user(username=username, email=f"{username}@example.com")
    agent = TravelAgent.objects.create(
        user=user, agent_name=username.title(), phone="555-0100", commission_rate=Decimal("10"),
        payment_method=method, agency_name=typed, agency=agency, agency_handles_payment=agency_pays,
    )
    agent.refresh_from_db()
    return agent


class ProposalTests(TestCase):
    def setUp(self):
        self.bdev = Agency.objects.create(name="Best Day Ever Vacations", payment_method="paypal",
                                          payment_info="pay@bdev.example")

    def _status(self, agent):
        return next(p for p in proposals() if p.agent.id == agent.id)

    def test_exact_name_is_ready(self):
        agent = make_agent("ann", typed="best day ever vacations ")
        p = self._status(agent)
        self.assertEqual((p.status, p.agency), (READY, self.bdev))

    def test_close_spelling_needs_a_check(self):
        agent = make_agent("bo", typed="Best Day Ever Vacations LLC")
        p = self._status(agent)
        self.assertEqual((p.status, p.agency), (CHECK, self.bdev))

    def test_unknown_agency_is_missing(self):
        agent = make_agent("cy", typed="Pixie Vacations")
        self.assertEqual(self._status(agent).status, MISSING)

    def test_linked_with_agency_pays_off_is_ready(self):
        agent = make_agent("di", agency=self.bdev)
        self.assertEqual(self._status(agent).status, READY)

    def test_venmo_agent_in_an_agency_is_never_proposed(self):
        """Abdalla is in Best Day Ever but picked Venmo: we pay him, not the agency."""
        agent = make_agent("abdalla", method="venmo", typed="Best Day Ever Vacations", agency=self.bdev)
        self.assertNotIn(agent.id, [p.agent.id for p in proposals()])

    def test_already_paid_through_agency_is_not_proposed(self):
        agent = make_agent("ed", agency=self.bdev, agency_pays=True)
        self.assertNotIn(agent.id, [p.agent.id for p in proposals()])

    def test_switched_off_agency_is_not_matched(self):
        self.bdev.is_active = False
        self.bdev.save()
        agent = make_agent("fy", typed="Best Day Ever Vacations")
        self.assertEqual(self._status(agent).status, MISSING)


class LinkTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("disp", password="x", is_staff=True)
        self.bdev = Agency.objects.create(name="Best Day Ever Vacations")

    def test_link_routes_pay_through_agency_and_logs_it(self):
        agent = make_agent("ann", typed="Best Day Ever Vacations")
        link_to_agency(agent.id, self.bdev.id, user=self.staff)
        agent.refresh_from_db()
        self.assertEqual((agent.agency_id, agent.agency_handles_payment), (self.bdev.id, True))
        log = AuditLog.objects.get(model_name="TravelAgent", object_id=agent.id)
        self.assertEqual(log.username, "disp")
        self.assertIn("Best Day Ever", log.new_value)

    def test_refuses_an_agent_who_switched_to_venmo(self):
        agent = make_agent("ann", typed="Best Day Ever Vacations")
        TravelAgent.objects.filter(pk=agent.pk).update(payment_method="venmo")
        with self.assertRaises(LinkRefused):
            link_to_agency(agent.id, self.bdev.id, user=self.staff)
        agent.refresh_from_db()
        self.assertIsNone(agent.agency_id)

    def test_refuses_when_linked_to_a_different_agency(self):
        other = Agency.objects.create(name="Other Travel")
        agent = make_agent("ann", agency=other)
        with self.assertRaises(LinkRefused):
            link_to_agency(agent.id, self.bdev.id, user=self.staff)


class PageTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user("disp", password="x", is_staff=True))
        self.url = reverse("agency_links")
        self.bdev = Agency.objects.create(name="Best Day Ever Vacations")

    def test_page_lists_and_links_only_what_was_ticked(self):
        ann = make_agent("ann", typed="Best Day Ever Vacations")
        bo = make_agent("bo", typed="Best Day Ever Vacations")
        response = self.client.get(self.url)
        self.assertContains(response, reverse("admin_travel_agent_detail", args=[ann.id]))

        response = self.client.post(self.url, {"action": "link", "pair": [f"{ann.id}:{self.bdev.id}"]})
        self.assertRedirects(response, self.url)
        ann.refresh_from_db()
        bo.refresh_from_db()
        self.assertTrue(ann.agency_handles_payment)
        self.assertFalse(bo.agency_handles_payment)

    def test_add_agency_and_link(self):
        cy = make_agent("cy", typed="Pixie Vacations")
        self.client.post(self.url, {"action": "create", "agent": cy.id})
        cy.refresh_from_db()
        self.assertEqual(cy.agency.name, "Pixie Vacations")
        self.assertTrue(cy.agency_handles_payment)

    def test_add_agency_reuses_one_with_the_same_name(self):
        Agency.objects.create(name="Pixie Vacations")
        a = make_agent("cy", typed="pixie vacations")
        b = make_agent("dee", typed="Pixie Vacations")
        for agent in (a, b):
            self.client.post(self.url, {"action": "create", "agent": agent.id})
        self.assertEqual(Agency.objects.filter(name__iexact="pixie vacations").count(), 1)

    def test_non_staff_cannot_open_it(self):
        self.client.force_login(User.objects.create_user("guest", password="x"))
        self.assertNotEqual(self.client.get(self.url).status_code, 200)
