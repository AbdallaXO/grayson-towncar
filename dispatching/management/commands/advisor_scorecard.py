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
import math
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

#: Below this many graded warnings a percentage is noise, not a finding.
MIN_TO_SPEAK = 20
#: The bar D5 sets for a warning class to be worth a dispatcher's screen.
BAR = 70.0


def _wilson(k, n):
    """95% interval for k of n, Wilson — behaves at small n where the textbook
    formula gives nonsense like 'between -4% and 31%'."""
    if not n:
        return 0.0, 100.0
    p, z = k / n, 1.96
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, (c - h) * 100), min(100.0, (c + h) * 100)


def _verdict(k, n):
    """What can honestly be said about this class yet."""
    if n < MIN_TO_SPEAK:
        return "too early", ""
    lo, hi = _wilson(k, n)
    if lo >= BAR:
        return "PASSES the bar", f"({lo:.0f}-{hi:.0f}%)"
    if hi < BAR:
        return "FAILS the bar", f"({lo:.0f}-{hi:.0f}%)"
    return "not sure yet", f"({lo:.0f}-{hi:.0f}%)"


class Command(BaseCommand):
    help = "How often the day-of warnings were right, and how sure we can be."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=14,
                            help="How many service days back to look (default 14).")
        parser.add_argument("--csv", default="",
                            help="Also write the per-class rows to this file.")

    def handle(self, *args, **opts):
        from dispatching.models import AdvisorEvent, DispatchEtaSample

        today = timezone.localdate()
        since = today - timedelta(days=opts["days"])
        w = self.stdout.write

        w("")
        w(self.style.MIGRATE_HEADING(
            f"  THE WARNINGS  ({since} to {today})"))
        w("")

        rows = list(AdvisorEvent.objects.filter(service_date__gte=since)
                    .values("kind", "severity", "basis", "leg_count",
                            "outcome_quality", "outcome_late_min",
                            "service_date", "had_plans", "applied_at"))
        if not rows:
            w("  Nothing recorded yet.")
            w("")
            w("  That is expected until this has been live through a full day. The")
            w("  warnings record themselves as they are raised, and the outcome is")
            w("  filled in after the service date has ended.")
            self._gps(DispatchEtaSample, since, today)
            return

        days = len({r["service_date"] for r in rows})
        graded = [r for r in rows if r["outcome_quality"] == "ok"]
        waiting = sum(1 for r in rows if not r["outcome_quality"])
        ungradeable = len(rows) - len(graded) - waiting

        w(f"  {len(rows):,} warnings over {days} day(s)"
          f"  —  {len(graded):,} graded, {waiting:,} waiting for the day to end,")
        w(f"  {ungradeable:,} that can never be graded (the driver never tapped"
          f" 'on location').")
        w("")

        buckets = {}
        for r in graded:
            key = (r["kind"], r["severity"], r["basis"] or "-")
            b = buckets.setdefault(key, {"n": 0, "right": 0, "solo": 0, "plans": 0})
            b["n"] += 1
            if (r["outcome_late_min"] or 0) > 15:
                b["right"] += 1
            if (r["leg_count"] or 0) <= 1:
                b["solo"] += 1
            if r["had_plans"]:
                b["plans"] += 1

        if not buckets:
            w("  No warning has been graded yet — every one is still waiting for")
            w("  its service day to finish.")
            self._gps(DispatchEtaSample, since, today)
            return

        w(f"  {'what it warned about':<34}{'graded':>7}{'right':>7}"
          f"{'how sure':>26}")
        w(f"  {'-' * 74}")
        out = []
        for key, b in sorted(buckets.items(), key=lambda kv: -kv[1]["n"]):
            label = self._label(*key)
            verdict, band = _verdict(b["right"], b["n"])
            pct = f"{100 * b['right'] / b['n']:.0f}%" if b["n"] else "-"
            w(f"  {label:<34}{b['n']:>7}{pct:>7}   {verdict + ' ' + band:<23}")
            lo, hi = _wilson(b["right"], b["n"])
            out.append({"kind": key[0], "severity": key[1], "basis": key[2],
                        "graded": b["n"], "right": b["right"],
                        "pct_right": round(100 * b["right"] / b["n"], 1),
                        "low": round(lo, 1), "high": round(hi, 1),
                        "verdict": verdict,
                        "pct_single_leg": round(100 * b["solo"] / b["n"], 1),
                        "pct_with_plans": round(100 * b["plans"] / b["n"], 1)})
        w("")
        w(f"  \"Right\" means the trip the warning named really did reach its pickup")
        w(f"  more than 15 minutes late. The bar this project set is {BAR:.0f}%.")

        thin = [r for r in out if r["verdict"] == "too early"]
        if thin:
            w("")
            w(f"  {len(thin)} of {len(out)} types have under {MIN_TO_SPEAK} graded"
              f" warnings so far. Those need more")
            w("  days before a percentage means anything — a small sample can look")
            w("  like anything at all.")

        selfscored = [r for r in out if r["pct_single_leg"] >= 50]
        if selfscored:
            w("")
            w("  Careful with these — they mostly grade themselves:")
            for r in selfscored:
                w(f"    {self._label(r['kind'], r['severity'], r['basis']):<34}"
                  f"{r['pct_single_leg']:.0f}% are about a trip that was already"
                  f" late when the warning fired")
            w("  A warning that says 'this trip is late' about a trip that is late")
            w("  is reading back the screen, not forecasting.")

        applied = sum(1 for r in rows if r["applied_at"])
        w("")
        w(f"  Fixes actually applied from a warning: {applied}")

        self._gps(DispatchEtaSample, since, today)

        if opts["csv"] and out:
            cols = list(out[0].keys())
            with open(opts["csv"], "w", newline="", encoding="utf-8") as fh:
                writer = _csv.DictWriter(fh, fieldnames=cols)
                writer.writeheader()
                writer.writerows(out)
            w("")
            w(f"  Wrote {opts['csv']}")
        w("")

    # ------------------------------------------------------------------
    def _label(self, kind, severity, basis):
        """The engine's class names, in words a dispatcher would use."""
        names = {
            "late_cascade": "driver running late",
            "overlap": "turn won't work",
            "flight_change": "flight moved",
            "overrun": "job running long",
            "unassigned": "no driver yet",
            "farm_pending": "farmed, awaiting confirm",
        }
        tail = " (on his tap)" if basis == "recorded_pickup" else (
            " (on GPS)" if basis and basis.startswith("gps") else "")
        return f"{names.get(kind, kind)}{tail} — {severity}"

    def _gps(self, model, since, today):
        w = self.stdout.write
        w("")
        w(self.style.MIGRATE_HEADING("  THE GPS READINGS"))
        w("")
        qs = model.objects.filter(sampled_at__date__gte=since)
        n = qs.count()
        if not n:
            w("  Nothing recorded yet. These fill up every 3 minutes while cars")
            w("  are working, so a full day should show several thousand.")
            return
        days = len({d for d in qs.values_list("sampled_at__date", flat=True)
                    .distinct()})
        carried = qs.filter(eta_carried=True).count()
        legs = qs.values("leg_id_ref").distinct().count()
        w(f"  {n:,} readings over {days} day(s), covering {legs:,} trips"
          f"  ({n / max(1, days):,.0f} a day).")
        w(f"  {100 * carried / n:.0f}% repeat the previous reading unchanged"
          f" — no new information about the road.")
        tight = qs.filter(slack_minutes__lt=0).count()
        w(f"  {tight:,} readings where the car could not make its next stop"
          f" on time.")
        w("")
        w("  These are the raw material, not a verdict. Scoring them against what")
        w("  actually happened is analysis/07's job, and it needs the trips to have")
        w("  finished first.")
