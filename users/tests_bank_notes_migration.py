"""Tests for the one-time cleanup that moves bank numbers out of old notes (users 0038).

Run with:  ./manage.py test users.tests_bank_notes_migration
"""
import importlib
from decimal import Decimal

from django.apps import apps
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from reservations.models import AuditLog
from users.bank_notes import read_bank_note
from users.models import TravelAgent

migration = importlib.import_module("users.migrations.0038_convert_old_bank_notes")

NOTES = [
    "Chase - Routing: 021000021  Acct: 000123456789 (checking)",
    "12345678 / 011000015",
    "routing 0210-00021 account 1234-5678-9012",
    "routing 021000021 account 1234 5678 9012",
    "routing 123456789 account 987654321",
    "021000021 011000015 12345678",
    "021000021 / 12345678 / call 407-555-0123",
    "Bank of America",
    "",
]


class SameRulesAsTheAppTests(SimpleTestCase):
    """The migration carries its own copy of the rules; it must agree with users.bank_notes."""

    def test_agrees_on_every_sample(self):
        for note in NOTES:
            found, _ = read_bank_note(note)
            expected = (found["routing"], found["account"], found["type"]) if found else None
            self.assertEqual(migration._read(note), expected, note)


def make_agent(username, **fields):
    user = User.objects.create_user(username=username, email=f"{username}@example.com")
    data = dict(user=user, agent_name=username.title(), phone="407-555-0100", commission_rate=Decimal("10"))
    data.update(fields)
    return TravelAgent.objects.create(**data)


class ConvertTests(TestCase):
    def test_clear_note_is_moved_and_masked(self):
        agent = make_agent("jane", payment_method="bank",
                           payment_info="Chase routing 021000021 acct 000123456789 checking")
        migration.convert(apps, None)
        agent.refresh_from_db()
        self.assertEqual((agent.bank_routing_number, agent.bank_account_number, agent.bank_account_type,
                          agent.bank_account_holder), ("021000021", "000123456789", "checking", "Jane"))
        self.assertEqual(agent.payment_info, "Checking ••••6789 · Jane")
        log = AuditLog.objects.get(model_name="TravelAgent", object_id=agent.pk)
        self.assertNotIn("000123456789", log.old_value)
        self.assertNotIn("021000021", log.old_value)
        self.assertIn("Chase", log.old_value)

    def test_dashed_account_number_is_masked_in_the_log(self):
        agent = make_agent("dee", payment_method="bank", payment_info="Routing 0210-00021 / Account 1234-5678-9012")
        migration.convert(apps, None)
        agent.refresh_from_db()
        self.assertEqual(agent.bank_account_number, "123456789012")
        old = AuditLog.objects.get(object_id=agent.pk).old_value
        self.assertNotIn("1234-5678", old)
        self.assertNotIn("5678-9012", old)
        self.assertIn("••••9012", old)

    def test_unclear_note_is_left_alone(self):
        note = "routing 021000021 account 1234 5678 9012"
        agent = make_agent("bo", payment_method="bank", payment_info=note)
        migration.convert(apps, None)
        agent.refresh_from_db()
        self.assertEqual((agent.payment_info, agent.bank_account_number), (note, ""))
        self.assertFalse(AuditLog.objects.exists())

    def test_only_bank_agents_without_numbers_are_touched(self):
        note = "021000021 12345678"
        paypal = make_agent("pp", payment_method="paypal", payment_info=note)
        done = make_agent("done", payment_method="bank", payment_info="Savings ••••9999 · Done",
                          bank_routing_number="011000015", bank_account_number="99999999")
        migration.convert(apps, None)
        paypal.refresh_from_db()
        done.refresh_from_db()
        self.assertEqual(paypal.payment_info, note)
        self.assertEqual(done.bank_account_number, "99999999")

    def test_running_twice_changes_nothing_more(self):
        make_agent("jane", payment_method="bank", payment_info="021000021 12345678")
        migration.convert(apps, None)
        migration.convert(apps, None)
        self.assertEqual(AuditLog.objects.count(), 1)
