"""
Offline harness for the Fleet Capacity Intelligence System (Phase A).

Read-only: computes farm-out economics + binding-constraint classification for a date range
and prints a report. NO writes. Validate the numbers here before wiring the dashboard.

Examples:
    python manage.py analyze_farmouts --date 2026-05-09
    python manage.py analyze_farmouts --start 2026-05-01 --end 2026-05-31
    python manage.py analyze_farmouts --date 2026-06-01 --json
    python manage.py analyze_farmouts --date 2026-05-09 --no-classify   # faster, economics only

Reconcile the in_house/farm_out/unassigned counts against the capacity planner's
``get_coverage_stats`` for the same date as a sanity check.
"""
import json
from datetime import date, datetime, timedelta

from django.core.management.base import BaseCommand, CommandError

from dispatching import fleet_intel as fi


def _parse(d: str) -> date:
    try:
        return datetime.strptime(d, "%Y-%m-%d").date()
    except ValueError:
        raise CommandError(f"Invalid date '{d}', expected YYYY-MM-DD")


def _money(d) -> str:
    return f"${d:,.2f}" if d is not None else "-"


class Command(BaseCommand):
    help = "Read-only farm-out economics + binding-constraint classification for a date range."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="Single service date (YYYY-MM-DD).")
        parser.add_argument("--start", help="Range start (YYYY-MM-DD).")
        parser.add_argument("--end", help="Range end (YYYY-MM-DD).")
        parser.add_argument("--days", type=int, help="With --end (or today), look back N days.")
        parser.add_argument("--json", action="store_true", help="Emit raw JSON instead of a table.")
        parser.add_argument("--no-classify", action="store_true",
                            help="Skip the engine classification (economics only, faster).")
        parser.add_argument("--swaps", action="store_true",
                            help="Enable the bounded swap-recovery UPPER BOUND (SCHEDULING_PROCESS_LEAK). "
                                 "Slower; over-counts preventability (legs share slack) — read with care.")

    def handle(self, *args, **opts):
        start, end = self._resolve_range(opts)
        if start > end:
            raise CommandError("start must be <= end")

        report = fi.summarize_range(
            start, end, classify=not opts["no_classify"], use_swaps=opts["swaps"])

        if opts["json"]:
            self.stdout.write(json.dumps(report, indent=2, default=str))
            return
        self._render(report, classified=not opts["no_classify"])

    def _resolve_range(self, opts):
        if opts["date"]:
            d = _parse(opts["date"])
            return d, d
        if opts["start"] and opts["end"]:
            return _parse(opts["start"]), _parse(opts["end"])
        if opts["days"]:
            end = _parse(opts["end"]) if opts["end"] else date.today()
            return end - timedelta(days=opts["days"] - 1), end
        raise CommandError("Provide --date, or --start and --end, or --days (with optional --end).")

    # ── Rendering ──
    def _render(self, r, *, classified):
        op, fin = r["operational"], r["financial"]
        rng = r["range"]
        w = self.stdout.write
        line = "-" * 64

        w(line)
        w(self.style.MIGRATE_HEADING(
            f"FLEET CAPACITY INTELLIGENCE -- {rng['start']} -> {rng['end']} ({rng['days']}d)"))
        w(line)
        w("OPERATIONAL")
        w(f"  Total legs performed : {op['total']}")
        w(f"  In-house             : {op['in_house']}  ({op['in_house_rate']}%)")
        w(f"  Farmed out           : {op['farm_out']}  ({op['farm_out_rate']}%)")
        w(f"  Unassigned           : {op['unassigned']}")
        w("")
        w("FINANCIAL (driver-pay-only recovered margin; gratuity excluded)")
        w(f"  Revenue (all)        : {_money(fin['revenue_total'])}")
        w(f"  Farm-out revenue     : {_money(fin['farm_out_revenue'])}")
        w(f"  Affiliate cost paid  : {_money(fin['affiliate_cost_total'])}")
        w(f"  In-house counterfact : {_money(fin['inhouse_counterfactual_total'])}")
        w(self.style.SUCCESS(
            f"  Recovered margin NET : {_money(fin['recovered_net'])}"))
        w(f"    + positive (recoverable) : {_money(fin['recovered_positive'])}")
        w(f"    - negative (validated)   : {_money(fin['recovered_negative'])}")
        w(f"  Counterfactual coverage  : {fin['counterfactual_coverage_pct']}% "
          f"of farmed legs ({fin['counterfactual_available']}/{fin['farm_legs']})")
        w("")

        self._table(w, "RECOVERED MARGIN BY VEHICLE TYPE", r["by_vehicle_type"])
        if classified:
            self._reason_table(w, r)
        self._table(w, "BY PICKUP ZONE", r["by_zone_pickup"])
        self._table(w, "TOP AFFILIATES", r["by_affiliate"], limit=10)

        w("FLEET SIZE BY TYPE")
        for vt, n in sorted(r["fleet_size_by_type"].items(), key=lambda x: -x[1]):
            w(f"  {vt:<14} {n}")
        w(line)
        if classified:
            w(self.style.WARNING(
                "Classification confidence is STUB-window-limited (USE_STUB_WINDOWS=True); "
                "treat DRIVER_IDLE vs UNIT_CAPACITY as indicative. Local DB lacks LegStatus "
                "history -- validate on prod-representative data. Per-leg preventability is an "
                "UPPER BOUND; true absorbable count needs the Phase C +1 simulation."))

    def _table(self, w, title, data, limit=None):
        if not data:
            return
        w(title)
        rows = sorted(data.items(), key=lambda kv: kv[1]["count"], reverse=True)
        if limit:
            rows = rows[:limit]
        w(f"  {'key':<22}{'legs':>6}{'paid':>13}{'keep(net)':>13}{'%':>6}")
        for k, v in rows:
            spend = v.get("spend", 0) or 0
            pct = f"{(float(v['net']) / float(spend) * 100):.0f}%" if spend else "-"
            w(f"  {str(k):<22}{v['count']:>6}{_money(spend):>13}{_money(v['net']):>13}{pct:>6}")
        w("")

    def _reason_table(self, w, r):
        w("BINDING CONSTRAINT (computed)")
        labels = r["reason_labels"]
        rows = sorted(r["by_reason"].items(), key=lambda kv: kv[1]["count"], reverse=True)
        w(f"  {'reason':<42}{'legs':>6}{'net':>14}")
        for reason, v in rows:
            w(f"  {labels.get(reason, reason):<42}{v['count']:>6}{_money(v['net']):>14}")
        w("")
        w("  Family rollup:")
        for fam, v in sorted(r["by_family"].items(), key=lambda kv: kv[1]["count"], reverse=True):
            w(f"    {fam:<12}{v['count']:>5} legs   net {_money(v['net'])}")
        w("")
