"""Folding travel agents' duplicate logins (users 0036), and the index that
stops new ones.

Run:  ENABLE_DEBUG_TOOLBAR=0 python manage.py test users.tests_merge_agents
"""
import importlib
from datetime import date
from decimal import Decimal

from django.apps import apps
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase

from users.models import CommissionPayout, TravelAgent

migration = importlib.import_module("users.migrations.0036_username_unique_ignoring_case")


class UsernameIndexTests(TestCase):
    def test_two_logins_one_capital_apart_are_refused_by_the_database(self):
        User.objects.create_user("JamieTodd", password="x")
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user("jamietodd", password="y")

    def test_different_names_are_fine(self):
        User.objects.create_user("jamie", password="x")
        User.objects.create_user("jamie2", password="y")


class MergePairsTests(TestCase):
    """The migration's merge, run with test pairs. The index forbids a real
    case-duplicate pair here, and the merge does not care about case, so the
    pair is simply two differently named logins."""

    def setUp(self):
        self.keep_user = User.objects.create_user("keeper", email="k@example.com", password="k")
        self.old_user = User.objects.create_user("oldone", email="o@example.com", password="o")
        self.keep = TravelAgent.objects.create(user=self.keep_user, agent_name="Keeper",
                                               agency_name="", payment_method="venmo",
                                               unpaid_commissions=Decimal("19.50"))
        self.old = TravelAgent.objects.create(user=self.old_user, agent_name="Old",
                                              agency_name="Old Travel", payment_method="check",
                                              unpaid_commissions=Decimal("4.00"))
        # A fresh instance holds the float default; the payout signal adds a
        # Decimal to it. Loaded from the database it is a Decimal.
        self.old.refresh_from_db()
        CommissionPayout.objects.create(agent=self.old, total_amount=Decimal("10.50"),
                                        payout_period_start=date(2026, 1, 1),
                                        payout_period_end=date(2026, 1, 31))
        self.pairs = [(self.keep_user.id, "keeper", self.old_user.id, "oldone")]

    def merge(self, pairs=None):
        migration.merge_pairs(apps, None, pairs=pairs if pairs is not None else self.pairs,
                              log=lambda *_: None)

    def test_rows_move_and_the_old_login_is_retired(self):
        self.merge()
        self.assertEqual(CommissionPayout.objects.get().agent, self.keep)
        self.old_user.refresh_from_db()
        self.assertFalse(self.old_user.is_active)
        self.assertEqual(self.old_user.username, f"oldone.merged-{self.keep_user.id}")
        self.old.refresh_from_db()
        self.assertFalse(self.old.is_active)
        self.assertEqual(self.old.total_paid_commission, Decimal("0"))
        self.assertEqual(self.old.unpaid_commissions, Decimal("0"))
        self.keep.refresh_from_db()
        self.assertEqual(self.keep.total_paid_commission, Decimal("10.50"))  # from the payouts
        self.assertEqual(self.keep.unpaid_commissions, Decimal("23.50"))     # carried with the rows
        self.assertEqual(self.keep.agency_name, "Old Travel")                # blank filled
        self.assertEqual(self.keep.payment_method, "venmo")                  # set one kept

    def test_the_old_login_can_no_longer_sign_in_but_the_kept_one_can(self):
        self.merge()
        self.assertFalse(self.client.login(username="oldone", password="o"))
        self.assertTrue(self.client.login(username="keeper", password="k"))

    def test_running_it_twice_is_harmless(self):
        self.merge()
        self.merge()
        self.assertEqual(User.objects.filter(username__startswith="oldone").count(), 1)

    def test_a_pair_that_does_not_match_exactly_is_left_alone(self):
        self.merge([(self.keep_user.id, "keeper", self.old_user.id, "someone-else")])
        self.old_user.refresh_from_db()
        self.assertEqual(self.old_user.username, "oldone")
        self.assertTrue(self.old_user.is_active)

    def test_the_real_pairs_are_a_no_op_on_a_fresh_database(self):
        self.merge(migration.PAIRS)
        self.old_user.refresh_from_db()
        self.assertTrue(self.old_user.is_active)
