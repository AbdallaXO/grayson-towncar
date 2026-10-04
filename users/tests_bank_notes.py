"""Tests for reading bank numbers out of old free-text notes.

Run with:  ./manage.py test users.tests_bank_notes
"""
from django.test import SimpleTestCase

from users.bank_notes import read_bank_note


class ReadBankNoteTests(SimpleTestCase):
    def test_labelled_note(self):
        found, why = read_bank_note("Chase - Routing: 021000021  Acct: 000123456789 (checking)")
        self.assertEqual(why, "")
        self.assertEqual(found, {"routing": "021000021", "account": "000123456789", "type": "checking"})

    def test_unlabelled_order_does_not_matter(self):
        found, _ = read_bank_note("12345678 / 011000015")
        self.assertEqual((found["routing"], found["account"], found["type"]), ("011000015", "12345678", ""))

    def test_dashes_inside_numbers(self):
        found, _ = read_bank_note("routing 0210-00021 account 1234-5678-9012")
        self.assertEqual((found["routing"], found["account"]), ("021000021", "123456789012"))

    def test_spaced_out_account_goes_to_a_person(self):
        found, why = read_bank_note("routing 021000021 account 1234 5678 9012")
        self.assertIsNone(found)

    def test_no_valid_routing_number(self):
        found, why = read_bank_note("routing 123456789 account 987654321")
        self.assertIsNone(found)
        self.assertIn("routing", why)

    def test_two_possible_routing_numbers(self):
        found, why = read_bank_note("021000021 011000015 12345678")
        self.assertIsNone(found)

    def test_two_possible_account_numbers(self):
        found, why = read_bank_note("routing 021000021 acct 12345678 or 87654321")
        self.assertIsNone(found)
        self.assertIn("account", why)

    def test_phone_number_counts_as_a_competing_number(self):
        # A phone in the note could be mistaken for the account: refuse rather than guess.
        found, _ = read_bank_note("021000021 / 12345678 / call 4075550123")
        self.assertIsNone(found)

    def test_short_numbers_are_ignored(self):
        found, _ = read_bank_note("Branch 12, routing 021000021, account 12345678")
        self.assertEqual(found["account"], "12345678")

    def test_both_checking_and_savings_leaves_type_blank(self):
        found, _ = read_bank_note("checking or savings 021000021 12345678")
        self.assertEqual(found["type"], "")

    def test_nothing(self):
        self.assertIsNone(read_bank_note("")[0])
        self.assertIsNone(read_bank_note("Bank of America")[0])
