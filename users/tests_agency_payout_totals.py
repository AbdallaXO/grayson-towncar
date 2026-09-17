"""Tests that an agency payout is counted once, not twice.

Run with:  ./manage.py test users.tests_agency_payout_totals

users.signals.handle_agency_payout_changes adds each new AgencyCommissionPayout's
amount to Agency.total_paid_commission. Both callers used to add it a SECOND time
to the very object the signal had just updated, which left 22 of 50 agencies
showing exactly 2x the money that actually left the bank ($14,170.74).
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase

from users.models import Agency, AgencyCommissionPayout

PERIOD = dict(payout_period_start=date(2026, 8, 1), payout_period_end=date(2026, 8, 31))


class AgencyPayoutTotalTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(name="Traveling Ears Vacations")

    def _payout(self, amount):
        return AgencyCommissionPayout.objects.create(
            agency=self.agency, total_amount=Decimal(amount), **PERIOD
        )

    def test_single_payout_counts_once(self):
        """The regression: one $92.50 payout must not read as $185.00."""
        self._payout("92.50")
        self.agency.refresh_from_db()
        self.assertEqual(self.agency.total_paid_commission, Decimal("92.50"))

    def test_several_payouts_sum_to_their_total(self):
        for amount in ("92.50", "263.50", "861.50"):
            self._payout(amount)
        self.agency.refresh_from_db()
        self.assertEqual(self.agency.total_paid_commission, Decimal("1217.50"))

    def test_sync_repairs_an_already_doubled_total(self):
        """What the repair command relies on, for agencies paid before the fix."""
        self._payout("92.50")
        Agency.objects.filter(pk=self.agency.pk).update(
            total_paid_commission=Decimal("185.00")
        )
        self.agency.refresh_from_db()

        changed = self.agency.sync_paid_commission()

        self.assertTrue(changed)
        self.agency.refresh_from_db()
        self.assertEqual(self.agency.total_paid_commission, Decimal("92.50"))

    def test_sync_is_idempotent(self):
        self._payout("92.50")
        self.agency.refresh_from_db()
        self.assertFalse(self.agency.sync_paid_commission())
        self.agency.refresh_from_db()
        self.assertEqual(self.agency.total_paid_commission, Decimal("92.50"))

    def test_agency_with_no_payouts_reads_zero(self):
        self.assertFalse(self.agency.sync_paid_commission())
        self.agency.refresh_from_db()
        self.assertEqual(self.agency.total_paid_commission or Decimal("0"), Decimal("0"))
