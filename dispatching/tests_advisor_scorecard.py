"""Scorecard tests — the readout must never sound surer than the evidence.

Run with:  ENABLE_DEBUG_TOOLBAR=0 ./manage.py test dispatching.tests_advisor_scorecard

The thing being measured lost trust once by sounding confident and being wrong
three times in four. A readout that prints "83%" off six warnings would repeat
exactly that mistake, so the refusals are pinned as hard as the arithmetic:
under MIN_TO_SPEAK graded warnings a class says "too early" and no percentage
is offered, and a class only reads PASSES or FAILS when the whole confidence
interval sits on one side of the 70% bar.
"""
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from dispatching.management.commands.advisor_scorecard import _verdict, _wilson
from dispatching.models import AdvisorEvent

DAY = timezone.localdate() - timedelta(days=2)


def _event(i, *, kind="overlap", severity="critical", basis="recorded_pickup",
           late=None, quality="ok", legs=2):
    now = timezone.now()
    return AdvisorEvent.objects.create(
        service_date=DAY, card_id=f"{kind}:{i}", kind=kind, severity=severity,
        basis=basis, severity_last=severity, basis_last=basis,
        leg_count=legs, first_seen_at=now, last_seen_at=now,
        outcome_quality=quality, outcome_late_min=late,
        outcome_filled_at=now if quality else None)


class VerdictTests(TestCase):
    def test_a_thin_class_refuses_to_give_a_number(self):
        verdict, band = _verdict(5, 6)          # 83% off six warnings
        self.assertEqual(verdict, "too early")
        self.assertEqual(band, "")

    def test_a_clear_pass_is_called_a_pass(self):
        verdict, _ = _verdict(95, 100)
        self.assertEqual(verdict, "PASSES the bar")

    def test_a_clear_fail_is_called_a_fail(self):
        verdict, _ = _verdict(20, 100)
        self.assertEqual(verdict, "FAILS the bar")

    def test_a_borderline_class_says_it_does_not_know(self):
        """70% off 30 warnings cannot be told from 60% or 80%. Saying 'passes'
        here is how a tool talks itself into being trusted."""
        verdict, _ = _verdict(21, 30)
        self.assertEqual(verdict, "not sure yet")

    def test_the_interval_behaves_at_the_edges(self):
        lo, hi = _wilson(0, 5)
        self.assertGreaterEqual(lo, 0.0)
        self.assertLessEqual(hi, 100.0)
        lo, hi = _wilson(5, 5)
        self.assertLessEqual(hi, 100.0)
        self.assertGreater(lo, 0.0)


class OutputTests(TestCase):
    def _run(self, **kw):
        out = StringIO()
        call_command("advisor_scorecard", stdout=out, **kw)
        return out.getvalue()

    def test_an_empty_ledger_explains_itself_rather_than_printing_zeroes(self):
        text = self._run()
        self.assertIn("Nothing recorded yet", text)
        self.assertNotIn("%", text.split("THE GPS")[0])

    def test_ungraded_warnings_are_counted_separately_from_graded_ones(self):
        for i in range(3):
            _event(i, quality="", late=None)
        text = self._run()
        self.assertIn("waiting for the day to end", text)

    def test_a_class_with_enough_evidence_reports_a_verdict(self):
        for i in range(40):
            _event(i, late=40.0)                 # all genuinely late
        text = self._run()
        self.assertIn("PASSES the bar", text)
        self.assertIn("100%", text)

    def test_unscorable_warnings_are_named_not_dropped(self):
        """A denominator that quietly loses them would overstate the tool."""
        for i in range(5):
            _event(i, quality="none", late=None)
        text = self._run()
        self.assertIn("can never be graded", text)

    def test_self_scoring_classes_are_called_out(self):
        for i in range(25):
            _event(i, kind="late_cascade", basis="clock_only", legs=1, late=40.0)
        text = self._run()
        self.assertIn("grade themselves", text)
        self.assertIn("reading back the screen", text)

    def test_the_window_is_respected(self):
        _event(1, late=40.0)
        old = _event(2, late=40.0)
        AdvisorEvent.objects.filter(pk=old.pk).update(
            service_date=timezone.localdate() - timedelta(days=90))
        self.assertIn("1 warnings over 1 day", self._run(days=14))

    def test_csv_carries_the_same_numbers(self):
        import csv, tempfile, os
        for i in range(25):
            _event(i, late=40.0)
        path = os.path.join(tempfile.mkdtemp(), "score.csv")
        self._run(csv=path)
        rows = list(csv.DictReader(open(path)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["graded"]), 25)
        self.assertEqual(float(rows[0]["pct_right"]), 100.0)


class AdminTests(TestCase):
    """The ledgers are browsable and NOT editable. A ledger you can edit is not
    evidence — deleting a row deletes the denominator behind a percentage this
    project publishes."""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth.models import User
        cls.boss = User.objects.create_superuser("sc_boss", "b@x.com", "x")

    def test_both_ledgers_are_registered(self):
        from django.contrib import admin as dj
        from dispatching.models import AdvisorEvent, DispatchEtaSample
        self.assertIn(AdvisorEvent, dj.site._registry)
        self.assertIn(DispatchEtaSample, dj.site._registry)

    def test_nobody_can_add_change_or_delete_not_even_a_superuser(self):
        from django.contrib import admin as dj
        from dispatching.models import AdvisorEvent, DispatchEtaSample

        class _Req:
            user = self.boss
        for model in (AdvisorEvent, DispatchEtaSample):
            ma = dj.site._registry[model]
            self.assertFalse(ma.has_add_permission(_Req()), model)
            self.assertFalse(ma.has_change_permission(_Req()), model)
            self.assertFalse(ma.has_delete_permission(_Req()), model)

    def test_the_list_page_loads(self):
        _event(1, late=40.0)
        self.client.force_login(self.boss)
        for url in ("/admin/dispatching/advisorevent/",
                    "/admin/dispatching/dispatchetasample/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_an_ungradeable_warning_shows_a_dash_not_a_no(self):
        """Three answers, not two: right, wrong, and 'the driver never tapped
        so nobody can say'. Rendering the third as 'no' would quietly move a
        precision figure."""
        from django.contrib import admin as dj
        from dispatching.models import AdvisorEvent
        ma = dj.site._registry[AdvisorEvent]
        self.assertIsNone(ma.was_right(_event(9, quality="none", late=None)))
        self.assertIs(ma.was_right(_event(10, late=40.0)), True)
        self.assertIs(ma.was_right(_event(11, late=2.0)), False)
