"""
Is the day-of warning system any good? Ask the two ledgers.

    python manage.py advisor_scorecard
    python manage.py advisor_scorecard --days 7      # window, default 14
    python manage.py advisor_scorecard --csv out.csv # same numbers, for a spreadsheet

Why this exists
---------------
Two things record themselves in production and nothing reads them back:
``AdvisorEvent`` (every warning the Recovery Advisor raised, and whether the
trip it was about actually ran late) and ``DispatchEtaSample`` (every GPS
reading the 180 s sweep used to destroy). Both were built to answer one
question — "is this thing right often enough to act on?" — and an instrument
nobody can read is half an instrument.

Read-only. Touches no external service, writes nothing, and is safe to run
against production.

The same numbers render at the top of the AdvisorEvent admin page — both call
``advisor_events.scorecard()``, so a browser and a terminal cannot disagree.

WHAT IT REFUSES TO DO. It will not print a percentage it cannot stand behind.
The bar this project judges warnings against is 70%, and telling those apart
takes more evidence than a couple of busy days: about 50 graded warnings of ONE
type puts the answer inside roughly +/- 13 points, which cannot separate a pass
from a fail. So every line says how sure it is, and a line with too little
behind it says "too early" instead of a number. That is the whole point — the
system it is measuring already lost trust once by sounding confident and being
wrong 3 times in 4.
"""
import csv as _csv

from django.core.management.base import BaseCommand

from dispatching import advisor_events as ae


class Command(BaseCommand):
    help = "How often the day-of warnings were right, and how sure we can be."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=14,
                            help="How many service days back to look (default 14).")
        parser.add_argument("--csv", default="",
                            help="Also write the per-class rows to this file.")

    def handle(self, *args, **opts):
        w = self.stdout.write
        sc = ae.scorecard(days=opts["days"])
        t, rows = sc["totals"], sc["rows"]

        w("")
        w(self.style.MIGRATE_HEADING(
            f"  THE WARNINGS  ({t['since']} to {t['until']})"))
        w("")
        if not t["warnings"]:
            w("  Nothing recorded yet.")
            w("")
            w("  Expected until this has been live through a full day: warnings")
            w("  record themselves as they are raised, and the outcome is filled")
            w("  in after the service date has ended.")
            self._gps(opts["days"])
            return

        w(f"  {t['warnings']:,} warnings over {t['days_seen']} day(s)"
          f"  —  {t['graded']:,} graded, {t['waiting']:,} waiting for the day"
          f" to end,")
        w(f"  {t['ungradeable']:,} that can never be graded (the driver never"
          f" tapped 'on location').")
        w("")

        if not rows:
            w("  No warning has been graded yet — every one is still waiting for")
            w("  its service day to finish.")
            self._gps(opts["days"])
            return

        w(f"  {'what it warned about':<38}{'graded':>7}{'right':>7}"
          f"{'how sure':>24}")
        w(f"  {'-' * 76}")
        for r in rows:
            pct = "-" if r["verdict"] == "too early" else f"{r['pct_right']:.0f}%"
            band = ("" if r["low"] is None
                    else f" ({r['low']:.0f}-{r['high']:.0f}%)")
            w(f"  {r['label']:<38}{r['graded']:>7}{pct:>7}"
              f"   {r['verdict'] + band:<21}")
        w("")
        w(f"  \"Right\" means the trip the warning named really did reach its"
          f" pickup more than")
        w(f"  {ae.LATE_BAR_MIN} minutes late. The bar this project set is"
          f" {t['bar']:.0f}%.")

        thin = [r for r in rows if r["verdict"] == "too early"]
        if thin:
            w("")
            w(f"  {len(thin)} of {len(rows)} types have under {t['min_to_speak']}"
              f" graded warnings so far. Those")
            w("  need more days before a percentage means anything.")

        selfscored = [r for r in rows if r["self_scoring"]]
        if selfscored:
            w("")
            w("  Careful with these — they mostly grade themselves:")
            for r in selfscored:
                w(f"    {r['label']:<38}{r['pct_single_leg']:.0f}% are about a"
                  f" trip already late when it fired")
            w("  A warning that says 'this trip is late' about a trip that is")
            w("  late is reading back the screen, not forecasting.")

        w("")
        w(f"  Fixes actually applied from a warning: {t['applied']}")
        self._gps(opts["days"])

        if opts["csv"]:
            cols = list(rows[0].keys())
            with open(opts["csv"], "w", newline="", encoding="utf-8") as fh:
                writer = _csv.DictWriter(fh, fieldnames=cols)
                writer.writeheader()
                writer.writerows(rows)
            w("")
            w(f"  Wrote {opts['csv']}")
        w("")

    def _gps(self, days):
        w = self.stdout.write
        e = ae.eta_summary(days=days)
        w("")
        w(self.style.MIGRATE_HEADING("  THE GPS READINGS"))
        w("")
        if not e["readings"]:
            w("  Nothing recorded yet. These fill up every 3 minutes while cars")
            w("  are working, so a full day should show several thousand.")
            return
        w(f"  {e['readings']:,} readings over {e['days_seen']} day(s), covering"
          f" {e['trips']:,} trips ({e['per_day']:,} a day).")
        w(f"  {e['pct_carried']:.0f}% repeat the previous reading unchanged"
          f" — no new information about the road.")
        w(f"  {e['cannot_make_it']:,} caught a car that could not reach its next"
          f" stop in time.")
        w("")
        w("  Raw material, not a verdict. Scoring these against what actually")
        w("  happened is analysis/07's job, and it needs the trips finished.")
