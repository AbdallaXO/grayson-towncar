"""
Offline harness for the Farm-Out Opportunity-Cost Optimizer (read-only).

For a date range, replays each day's actual in-house board and, for every leg that was actually
FARMED, asks: could we have kept it in-house and farmed something CHEAPER instead? Prints the
recommendations with dollars shown (no black-box score). NO writes. Validate the numbers here
before any dashboard / apply phase.

ROSTER (Architecture B): the farm-cost waterfall prices each leg against the WHOLE carded affiliate
roster, cheapest eligible winning. Rates come from real DriverPayRate rows; per-affiliate capability
(vehicle classes), capacity (single-vehicle chain / count cap / fleet), and route/permit rules come
from drivers.models.AffiliateProfile. Affiliates with no card are abstained (never invented a price)
and surfaced in the roster audit. See the loud header in dispatching/farmout_optimizer.py.

Examples:
    python manage.py analyze_farmout_savings --date 2026-05-02
    python manage.py analyze_farmout_savings --date 2026-05-02 --html report.html
    python manage.py analyze_farmout_savings --start 2026-05-01 --end 2026-05-31
    python manage.py analyze_farmout_savings --date 2026-05-09 --min-savings 150 --json
"""
import json
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from dispatching import farmout_optimizer as fo
from dispatching import farmout_report
# Shared HTML render (also used by the staff web view) + the money helper used by the text render.
from dispatching.farmout_report import _money


def _parse(d: str) -> date:
    try:
        return datetime.strptime(d, "%Y-%m-%d").date()
    except ValueError:
        raise CommandError(f"Invalid date '{d}', expected YYYY-MM-DD")


class Command(BaseCommand):
    help = "Read-only farm-out opportunity-cost recommendations (keep-in-house vs farm-cheaper)."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="Single service date (YYYY-MM-DD).")
        parser.add_argument("--start", help="Range start (YYYY-MM-DD).")
        parser.add_argument("--end", help="Range end (YYYY-MM-DD).")
        parser.add_argument("--days", type=int, help="With --end (or today), look back N days.")
        parser.add_argument("--min-savings", type=str, default=None,
                            help="Discretionary savings threshold in dollars (default 20).")
        parser.add_argument("--anthony-cap", type=int, default=None,
                            help="What-if override of Anthony's daily count cap (else his "
                                 "AffiliateProfile.daily_cap, default ~12).")
        parser.add_argument("--departure-premium", type=str, default=None,
                            help="Max extra farm-$ to spend keeping a DEPARTURE in-house via a "
                                 "displace-and-farm swap (default 0 = only free/cheaper rescues).")
        parser.add_argument("--json", action="store_true", help="Emit raw JSON instead of a table.")
        parser.add_argument("--html", type=str, default=None,
                            help="Write a self-contained, browser-openable HTML report to this path.")

    def handle(self, *args, **opts):
        start, end = self._resolve_range(opts)
        if start > end:
            raise CommandError("start must be <= end")

        kwargs = {}
        if opts["min_savings"] is not None:
            kwargs["min_savings"] = Decimal(opts["min_savings"])
        if opts["anthony_cap"] is not None:
            kwargs["anthony_cap"] = opts["anthony_cap"]
        if opts["departure_premium"] is not None:
            kwargs["departure_rescue_max_premium"] = Decimal(opts["departure_premium"])

        report = fo.summarize_savings_range(start, end, **kwargs)

        if opts["html"]:
            path = opts["html"]
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self._build_html(report))
            self.stdout.write(self.style.SUCCESS(f"Wrote HTML report to {path}"))
            if not opts["json"]:
                self._render(report)
            return

        if opts["json"]:
            self.stdout.write(json.dumps(report, indent=2, default=str))
            return
        self._render(report)

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

    # ════════════════════════════════════════════════════════════════════════
    # TEXT RENDER
    # ════════════════════════════════════════════════════════════════════════
    def _render(self, r):
        w = self.stdout.write
        line = "=" * 72
        rng = r["range"]
        t = r["totals"]
        audit = r["audit"]

        w(line)
        w(self.style.MIGRATE_HEADING(
            f"FARM-OUT OPPORTUNITY-COST -- {rng['start']} -> {rng['end']} ({rng['days']}d)"))
        w(line)

        # Roster scope + capability source (Architecture B — data-driven).
        w(self.style.WARNING(
            "DATA-DRIVEN ROSTER (Architecture B): each farm-out is priced against the WHOLE carded "
            "affiliate roster, cheapest eligible winning. Rates from real DriverPayRate rows; "
            "capability / capacity / permits from drivers.models.AffiliateProfile. Port Canaveral & "
            "Sanford are their OWN categories -- NOT protected as departures; judged on net-spend math. "
            f"See dispatching/farmout_optimizer.py. Threshold = {_money(r['min_savings'])}."))
        for warn in r["affiliate_warnings"]:
            w(self.style.ERROR(f"  ! {warn}"))
        w("")

        # Roster table — who is priceable, with their capability/capacity config.
        roster = r.get("roster") or []
        w(f"ROSTER ({len(roster)} rate-ready affiliates)")
        w(f"  {'affiliate':<16}{'mode':>13}{'max veh':>12}{'cap':>6}{'rows':>6}{'profile':>9}")
        for a in roster:
            w(f"  {a['name'][:16]:<16}{a['mode']:>13}{(a['max_vehicle_tier'] or '-'):>12}"
              f"{(str(a['daily_cap']) if a['daily_cap'] is not None else '-'):>6}{a['rate_rows']:>6}"
              f"{('yes' if a['has_profile'] else 'NO'):>9}")
        # Roster gaps — surface what is NOT priceable / risky so the founder sees what config to add.
        ra = r.get("roster_audit") or {}
        flat = ra.get("profileless_flat") or []
        if flat:
            w(self.style.WARNING(
                f"  ! FLAT card, NO capability cap (mispricing risk -- their all-vehicle row matches "
                f"every class incl. 14-pax): {', '.join(flat)}. Set AffiliateProfile.max_vehicle_tier."))
        gap = ra.get("uncarded_with_volume") or []
        if gap:
            shown = ", ".join(f"{n} ({c})" for n, c in gap[:12])
            w(self.style.ERROR(
                f"  ! GOT farm-out legs in range but have NO card -> ABSTAINED (not priceable): {shown}. "
                "Add DriverPayRate rows to bring them into the roster."))
        w("")

        w("SUMMARY")
        w(f"  Legs evaluated (targets)       : {t['targets']} "
          f"({t.get('unassigned_targets', 0)} unassigned leftovers / "
          f"{t['targets'] - t.get('unassigned_targets', 0)} affiliate-farmed)")
        w(f"  Recommendations (keep in-house): {t['recommendations']}")
        w(f"    - free in-house rescues      : {t['free_rescue']}")
        w(f"    - opportunity swaps (>= min) : {t['opportunity_swap']}")
        w(f"    - policy departure rescues   : {t['policy_departure_rescue']}")
        w(f"  Farm-only (no keep rec)        : {t.get('farm_only', 0)} "
          f"(must be farmed)")
        if t.get('stuck'):
            w(self.style.ERROR(
                f"  STUCK leftovers (alert)        : {t['stuck']} "
                "(unplaceable in-house AND unfarmable by any affiliate -- needs a human)"))
        w(self.style.SUCCESS(
            f"  Free-rescue farm-$ avoided      : {_money(t.get('free_rescue_avoided'))} "
            f"(actual cost of the affiliate-leg free rescues)"))
        if t.get('free_rescue_avoided_hypothetical'):
            w(self.style.SUCCESS(
                f"  Free-rescue farm-$ avoided (hyp): "
                f"{_money(t.get('free_rescue_avoided_hypothetical'))} "
                "(hypothetical farm cost of the UNASSIGNED-leftover free rescues)"))
        w(self.style.SUCCESS(
            f"  Opportunity/policy net savings  : {_money(t['est_savings'])} "
            f"(apples-to-apples net of swap + departure rescues)"))
        w(f"  VIP legs protected (excluded)  : {t['vip_protected']}")
        w(f"  Abstained: far/unknown drive   : {t.get('abstained_uncomputable_far', 0)} "
          f"(target touches Other/Residential/Other Hotel -> drive time uncomputable; Approach A)")
        w(f"  Departures rescuable only at a premium (suppressed at "
          f"{_money(r['departure_rescue_max_premium'])}): {t['suppressed_departures']} "
          f"(would cost {_money(t['suppressed_departure_premium'])} extra to rescue all)")
        w("")

        # Port/Sanford behavior audit (Step 3 -- they are their own categories now).
        w("PORT / SANFORD BEHAVIOR AUDIT (expected: TRUE departures protected; Port/Sanford judged on math)")
        w(f"  True departures protected (dropoff=airport, non-Port/Sanford) : {audit['true_departures_protected']}")
        w(f"  Port Canaveral legs (now farmable): to-port {audit['port_to']}, from-port {audit['port_from']}")
        w(f"  Sanford (SFB) legs    (now farmable): to-sanford {audit['sanford_to']}, from-sanford {audit['sanford_from']}")
        w(f"  Port/Sanford PICKUPS among targets (excluded from drop-off-only affiliates): {audit['waleed_excluded_pickups']}")
        w(f"  VIP legs seen among targets (never farmed)                    : {audit['vip_targets_seen']}")
        w("")

        # Affiliate load — per day (total legs OUR recommendations would farm to ALL affiliates) plus a
        # per-affiliate max/total so the founder can calibrate each AffiliateProfile capacity vs reality.
        w("AFFILIATE LOAD per day (incremental legs OUR recommendations would farm)")
        w(f"  {'date':<12}{'legs':>6}{'eval':>6}{'unasn':>6}{'depl':>6}{'recs':>6}{'to affs':>8}")
        per_aff_max, per_aff_total = {}, {}
        for d in r["days"]:
            ld = d["ledger_load"] or {}
            for name, load in ld.items():
                per_aff_max[name] = max(per_aff_max.get(name, 0), load)
                per_aff_total[name] = per_aff_total.get(name, 0) + load
            depl = f"{d.get('inhouse_deployable', '?')}/{d.get('inhouse_total', '?')}"
            w(f"  {str(d['day']):<12}{d['legs']:>6}{d.get('evaluated', d.get('farmed_targets', 0)):>6}"
              f"{d.get('unassigned_targets', 0):>6}{depl:>6}"
              f"{len(d['recommendations']):>6}{sum(ld.values()):>8}")
        busy = sorted(((n, per_aff_max[n], per_aff_total[n]) for n in per_aff_max if per_aff_total[n]),
                      key=lambda x: -x[2])
        if busy:
            w("  per-affiliate (max/day, total across range):")
            for n, mx, tot in busy:
                w(f"    {n[:18]:<18} max/day {mx:>3}   total {tot:>4}")
        w(self.style.WARNING(
            "  Single-vehicle affiliates are limited only by the feasibility chain (overlap+turnaround); "
            "count-cap affiliates by AffiliateProfile.daily_cap. Sanity-check max/day against reality."))
        w("")

        # Per-day recommendation detail.
        for d in r["days"]:
            if not d["recommendations"] and not d.get("abstained_far"):
                continue
            w("-" * 72)
            _ev = d.get('evaluated', d.get('farmed_targets', 0))
            _un = d.get('unassigned_targets', 0)
            _fo = d.get('farm_only', 0)
            w(self.style.HTTP_INFO(
                f"{d['day']}  ({d['legs']} legs, {_ev} evaluated: {_un} unassigned / "
                f"{_ev - _un} farmed; {_fo} farm-only)"))
            for rec in d["recommendations"]:
                tag = {"free_rescue": "[FREE]", "opportunity_swap": "[SWAP]",
                       "policy_departure_rescue": "[DEPARTURE/POLICY]"}.get(rec.kind, "[?]")
                dep = " *DEPARTURE*" if rec.target_is_departure else ""
                w(f"  {tag} leg {rec.target_leg_id}{dep}")
                w(f"      {rec.reason}")
                if rec.farmed_leg_ids:
                    mix = ", ".join(f"{n}x {a}" for a, n in rec.farm_affiliate_mix.items())
                    _a_label = "A farm" if getattr(rec, "target_is_unassigned", False) else "A paid"
                    w(f"      farm: legs {rec.farmed_leg_ids} -> {mix or '-'}  "
                      f"({_a_label} {_money(rec.state_a_farm_base)} | B farm {_money(rec.state_b_farm_base)}"
                      f" | net {_money(rec.net_savings)})")
                if rec.target_actual_farm_cost is not None:
                    w(f"      (actually paid to farm target: {_money(rec.target_actual_farm_cost)})")
                elif getattr(rec, "target_is_unassigned", False) and \
                        rec.target_hypothetical_farm_cost is not None:
                    w(f"      (unassigned leftover -- would cost ~"
                      f"{_money(rec.target_hypothetical_farm_cost)} to farm)")
            af = d.get("abstained_far") or []
            if af:
                w(self.style.WARNING(
                    f"  ABSTAINED (far/unknown drive -- uncomputable, Approach A): {len(af)} leg(s) "
                    "-- review for genuinely-LOCAL hotels/homes wrongly excluded:"))
                for a in af:
                    w(f"    - leg {a['leg_id']}: {a['route']}  "
                      f"[{a['pickup_cat']} -> {a['dropoff_cat']}]")
        w(line)
        w(self.style.WARNING(
            "Read-only RETROSPECTIVE grade -- judges PAST farm decisions on decision-time info; never "
            "un-farms a committed leg. Feasibility uses SCHEDULED (decision-time) flight arrival, not "
            "hindsight actual/estimated, and bounds each in-house rescue to the driver's REAL worked "
            "day (assigned-leg span), not stub windows. Tier-2 displacement is DEPTH-1 (one displaced "
            "leg per swap) in this phase -- deeper bundles deferred."))
        w(self.style.WARNING(
            "DRIVE TIMES calibrated to real Orlando times (MCO<->Disney 30, MCO<->Universal 25, "
            "MCO<->SFB 60, Disney<->Port 72). FAR/UNKNOWN destinations (Other/Residential/Other Hotel) "
            "are treated as UNCOMPUTABLE and ABSTAINED -- Approach A; live-distance verification "
            "(Approach B) deferred. (Far endpoints on displaced/neighbor legs still use the coarse "
            "table -- Approach B closes that.)"))

    # ════════════════════════════════════════════════════════════════════════
    # HTML RENDER (self-contained, no server, read-only artifact)
    # The actual rendering lives in dispatching/farmout_report.py so the staff web
    # view can serve the SAME document; this stays a thin delegator.
    # ════════════════════════════════════════════════════════════════════════
    def _build_html(self, r):
        return farmout_report.render_report_page(r)
