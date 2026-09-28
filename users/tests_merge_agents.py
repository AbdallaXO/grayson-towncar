"""Folding travel agents' duplicate accounts, and the index that stops new ones.

Run:  ENABLE_DEBUG_TOOLBAR=0 python manage.py test users.tests_merge_agents
"""
from datetime import date
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase

from users.models import CommissionPayout, TravelAgent


class UsernameIndexTests(TestCase):
    def test_two_logins_one_capital_apart_are_refused_by_the_database(self):
        User.objects.create_user("JamieTodd", password="x")
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user("jamietodd", password="y")

    def test_different_names_are_fine(self):
        User.objects.create_user("jamie", password="x")
        User.objects.create_user("jamie2", password="y")


class MergeCommandTests(TestCase):
    def setUp(self):
        self.keep_user = User.objects.create_user("keeper", email="k@example.com", password="k")
        self.old_user = User.objects.create_user("oldone", email="o@example.com", password="o")
        self.keep = TravelAgent.objects.create(user=self.keep_user, agent_name="Keeper",
                                               agency_name="", payment_method="venmo")
        self.old = TravelAgent.objects.create(user=self.old_user, agent_name="Old",
                                              agency_name="Old Travel", payment_method="check")
        # A fresh instance holds the float default; the payout signal adds a
        # Decimal to it. Loaded from the database (as in production) it is a
        # Decimal, so reload before the payout is created.
        self.old.refresh_from_db()
        CommissionPayout.objects.create(agent=self.old, total_amount=Decimal("10.50"),
                                        payout_period_start=date(2026, 1, 1),
                                        payout_period_end=date(2026, 1, 31))
        self.pairs = [(self.keep_user.id, "keeper", self.old_user.id, "oldone", "test")]

    def run_cmd(self, *args):
        out = StringIO()
        with patch("users.management.commands.merge_duplicate_agents.PAIRS", self.pairs):
            call_command("merge_duplicate_agents", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_changes_nothing(self):
        out = self.run_cmd()
        self.assertIn("DRY RUN", out)
        self.assertIn("moved   1 x users.CommissionPayout.agent", out)
        self.old_user.refresh_from_db()
        self.assertEqual(self.old_user.username, "oldone")
        self.assertTrue(self.old_user.is_active)
        self.assertEqual(CommissionPayout.objects.get().agent, self.old)

    def test_apply_moves_the_rows_and_retires_the_old_login(self):
        self.run_cmd("--apply")
        self.assertEqual(CommissionPayout.objects.get().agent, self.keep)
        self.old_user.refresh_from_db()
        self.assertFalse(self.old_user.is_active)
        self.assertEqual(self.old_user.username, f"oldone.merged-{self.keep_user.id}")
        self.old.refresh_from_db()
        self.assertFalse(self.old.is_active)
        self.keep.refresh_from_db()
        self.assertEqual(self.keep.total_paid_commission, Decimal("10.50"))   # recomputed
        self.assertEqual(self.keep.agency_name, "Old Travel")                # blank filled
        self.assertEqual(self.keep.payment_method, "venmo")                  # set one kept

    def test_running_it_twice_is_harmless(self):
        self.run_cmd("--apply")
        out = self.run_cmd("--apply")
        self.assertIn("already merged", out)

    def test_an_unexpected_account_aborts_everything(self):
        self.pairs = self.pairs + [(self.keep_user.id, "keeper", self.old_user.id, "someone-else", "x")]
        with self.assertRaises(CommandError):
            self.run_cmd("--apply")
        self.assertEqual(CommissionPayout.objects.get().agent, self.old)      # first pair rolled back
