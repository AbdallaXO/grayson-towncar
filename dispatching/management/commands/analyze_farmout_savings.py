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
import html as _html
import json
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from dispatching import farmout_optimizer as fo


def _parse(d: str) -> date:
    try:
        return datetime.strptime(d, "%Y-%m-%d").date()
    except ValueError:
        raise CommandError(f"Invalid date '{d}', expected YYYY-MM-DD")


def _money(d) -> str:
    return f"${d:,.2f}" if d is not None else "-"


class Command(BaseCommand):
    help = "Read-only farm-out opportunity-cost recommendations (keep-in-house vs farm-cheaper)."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="Single service date (YYYY-MM-DD).")
        parser.add_argument("--start", help="Range start (YYYY-MM-DD).")
        parser.add_argument("--end", help="Range end (YYYY-MM-DD).")
        parser.add_argument("--days", type=int, help="With --end (or today), look back N days.")
        parser.add_argument("--min-savings", type=str, default=None,
                            help="Discretionary savings threshold in dollars (default 100).")
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
    # ════════════════════════════════════════════════════════════════════════
    def _build_html(self, r):
        e = _html.escape
        rng, t, audit = r["range"], r["totals"], r["audit"]

        def m(d):
            return _money(d)

        def leg_line(disp):
            """One-line 'customer · pickup -> dropoff · time · vehicle [tag]' from a display dict."""
            if not disp:
                return "&mdash;"
            bits = []
            if disp.get("customer"):
                bits.append(f"<b>{e(disp['customer'])}</b>")
            route = f"{e(disp.get('pickup') or '?')} &rarr; {e(disp.get('dropoff') or '?')}"
            bits.append(route)
            meta = []
            if disp.get("time"):
                meta.append(e(disp["time"]))
            if disp.get("vehicle_type"):
                meta.append(e(disp["vehicle_type"]))
            if disp.get("direction_tag"):
                meta.append(f"<span class='tag dir'>{e(disp['direction_tag'])}</span>")
            if meta:
                bits.append("<span class='meta'>" + " &middot; ".join(meta) + "</span>")
            return " &middot; ".join(bits)

        cards = []
        for d in r["days"]:
            af = d.get("abstained_far") or []
            if not d["recommendations"] and not af:
                continue
            _ev = d.get('evaluated', d.get('farmed_targets', 0))
            _un = d.get('unassigned_targets', 0)
            cards.append(f"<h2 class='day'>{e(str(d['day']))} "
                         f"<span class='sub'>{d['legs']} legs &middot; {_ev} evaluated "
                         f"({_un} unassigned / {_ev - _un} farmed) &middot; {d.get('farm_only', 0)} farm-only "
                         f"&middot; {d.get('inhouse_deployable','?')}/{d.get('inhouse_total','?')} in-house "
                         f"deployable</span></h2>")
            for rec in d["recommendations"]:
                cards.append(self._card_html(rec, leg_line, m, e))
            if af:
                items = "".join(
                    f"<li>leg <b>{a['leg_id']}</b>: {e(a['route'])} "
                    f"<span class='meta'>[{e(a['pickup_cat'])} &rarr; {e(a['dropoff_cat'])}]</span></li>"
                    for a in af)
                cards.append(
                    "<div class='abstain'><b>Abstained &mdash; far/unknown drive (uncomputable, "
                    f"Approach A): {len(af)} leg(s).</b> Review for genuinely-LOCAL hotels/homes wrongly "
                    f"excluded.<ul>{items}</ul></div>")

        warn_html = ""
        if r["affiliate_warnings"]:
            warn_html = "<div class='warns'>" + "".join(
                f"<div class='warn'>! {e(x)}</div>" for x in r["affiliate_warnings"]) + "</div>"
        # Roster + roster-gap notes (Architecture B).
        roster = r.get("roster") or []
        roster_rows = "".join(
            f"<tr><td>{e(a['name'])}</td><td>{e(a['mode'])}</td>"
            f"<td>{e(a['max_vehicle_tier'] or '&mdash;')}</td>"
            f"<td>{a['daily_cap'] if a['daily_cap'] is not None else '&mdash;'}</td>"
            f"<td>{a['rate_rows']}</td><td>{'yes' if a['has_profile'] else 'NO'}</td></tr>"
            for a in roster)
        ra = r.get("roster_audit") or {}
        gap_notes = []
        if ra.get("profileless_flat"):
            gap_notes.append("<div class='note'><b>Flat card, no capability cap</b> (mispricing risk "
                             "&mdash; their all-vehicle row matches every class incl. 14-pax): "
                             f"{e(', '.join(ra['profileless_flat']))}. Set AffiliateProfile.max_vehicle_tier.</div>")
        if ra.get("uncarded_with_volume"):
            shown = ", ".join(f"{e(n)} ({c})" for n, c in ra["uncarded_with_volume"][:12])
            gap_notes.append("<div class='warn'>! <b>Got farm-out legs in range but have NO card</b> "
                             f"&rarr; abstained (not priceable): {shown}. Add DriverPayRate rows.</div>")
        gap_html = "".join(gap_notes)

        # Per-day affiliate load table (total legs our recs would farm across the roster).
        load_rows = "".join(
            f"<tr><td>{e(str(d['day']))}</td><td>{d['legs']}</td>"
            f"<td>{d.get('evaluated', d.get('farmed_targets', 0))}</td>"
            f"<td>{d.get('unassigned_targets', 0)}</td>"
            f"<td>{d.get('inhouse_deployable','?')}/{d.get('inhouse_total','?')}</td>"
            f"<td>{len(d['recommendations'])}</td><td>{d.get('farm_only', 0)}</td>"
            f"<td>{sum(d['ledger_load'].values())}</td></tr>"
            for d in r["days"])

        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Farm-Out Opportunity-Cost &mdash; {e(str(rng['start']))} to {e(str(rng['end']))}</title>
<style>
  :root {{ --ink:#1d2330; --muted:#6b7280; --line:#e5e7eb; --bg:#f6f7f9; --card:#fff;
           --green:#0f7a45; --greenbg:#e8f6ee; --amber:#92610a; --amberbg:#fdf3e0;
           --blue:#1d4ed8; --bluebg:#e8eefc; --red:#9a2828; }}
  * {{ box-sizing:border-box; }}
  body {{ font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; color:var(--ink);
          background:var(--bg); margin:0; padding:32px; }}
  .wrap {{ max-width:980px; margin:0 auto; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  .scope {{ background:var(--amberbg); color:var(--amber); border:1px solid #f0d9a8; border-radius:8px;
            padding:12px 14px; margin:14px 0; font-size:13px; }}
  .note {{ font-size:13px; color:var(--muted); margin-top:6px; }}
  .warns {{ margin:10px 0; }} .warn {{ color:var(--red); font-size:13px; }}
  .abstain {{ background:var(--amberbg); color:var(--amber); border:1px solid #f0d9a8; border-radius:8px;
              padding:10px 13px; margin:10px 0; font-size:13px; }}
  .abstain ul {{ margin:6px 0 0; padding-left:20px; }} .abstain li {{ margin:2px 0; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:16px 0; }}
  .stat {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px 14px; }}
  .stat .k {{ font-size:12px; color:var(--muted); }} .stat .v {{ font-size:20px; font-weight:600; }}
  .stat.green .v {{ color:var(--green); }}
  table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line);
           border-radius:8px; overflow:hidden; font-size:13px; margin:8px 0 4px; }}
  th,td {{ text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); }}
  th {{ background:#fafbfc; color:var(--muted); font-weight:600; }}
  h2.day {{ font-size:16px; margin:26px 0 10px; padding-bottom:6px; border-bottom:2px solid var(--line); }}
  h2.day .sub {{ font-size:12px; color:var(--muted); font-weight:400; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px;
           margin:12px 0; }}
  .card .head {{ display:flex; align-items:center; gap:8px; margin-bottom:8px; }}
  .pill {{ font-size:11px; font-weight:700; padding:2px 8px; border-radius:999px; letter-spacing:.02em; }}
  .pill.free {{ background:var(--greenbg); color:var(--green); }}
  .pill.swap {{ background:var(--bluebg); color:var(--blue); }}
  .pill.dep {{ background:var(--amberbg); color:var(--amber); }}
  .legid {{ font-size:13px; color:var(--muted); }}
  .target {{ font-size:14px; margin:2px 0 10px; }}
  .ba {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; margin:10px 0; }}
  .col {{ border:1px solid var(--line); border-radius:8px; padding:9px 11px; }}
  .col h4 {{ margin:0 0 6px; font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }}
  .col.now {{ background:#fcfcfd; }} .col.prop {{ background:var(--greenbg); border-color:#cdeBd9; }}
  .col .farm {{ font-weight:600; }}
  .reason {{ font-size:13px; color:#374151; margin:8px 0 4px; }}
  .delta {{ font-size:13px; font-weight:600; color:var(--green); }}
  .meta {{ color:var(--muted); font-size:12px; }}
  .tag {{ font-size:11px; padding:1px 6px; border-radius:6px; background:#eef1f5; color:#4b5563; }}
  .tag.dir {{ background:#eaf2ff; color:#1d4ed8; }}
  .chain {{ font-size:12px; color:var(--muted); margin-top:6px; }}
  .chain li {{ margin:1px 0; }}
  .boards {{ margin:12px 0 2px; display:grid; gap:10px; }}
  .bd {{ border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
  .bd h5 {{ margin:0; padding:6px 10px; background:#fafbfc; font-size:12px; color:#374151;
            border-bottom:1px solid var(--line); }}
  table.brd {{ width:100%; border-collapse:collapse; font-size:12px; margin:0; border:0; border-radius:0; }}
  table.brd td {{ padding:5px 9px; border-bottom:1px solid #f0f1f3; vertical-align:top; }}
  table.brd tr:last-child td {{ border-bottom:0; }}
  table.brd .tm {{ white-space:nowrap; color:#4b5563; font-variant-numeric:tabular-nums; width:1%; }}
  table.brd .vh {{ color:var(--muted); text-transform:uppercase; font-size:11px; width:1%; white-space:nowrap; }}
  tr.r-kept {{ background:var(--greenbg); }} tr.r-kept td {{ font-weight:600; }}
  tr.r-movedin {{ background:var(--bluebg); }}
  tr.r-movedout td {{ color:var(--muted); text-decoration:line-through; }}
  tr.r-farmedout {{ background:var(--amberbg); }}
  .rolemark {{ font-size:10px; font-weight:700; letter-spacing:.03em; padding:1px 5px; border-radius:5px; }}
  .rolemark.kept {{ background:var(--green); color:#fff; }}
  .rolemark.movedin {{ background:var(--blue); color:#fff; }}
  .rolemark.movedout {{ background:#e5e7eb; color:#4b5563; }}
  .rolemark.farmedout {{ background:var(--amber); color:#fff; }}
  .feas {{ font-size:11px; color:#52607a; margin:3px 0 0; font-weight:400; }}
  .feas .ok {{ color:var(--green); font-weight:600; }} .feas .bad {{ color:var(--red); font-weight:600; }}
  .vipdot {{ color:#b8860b; font-weight:700; font-size:11px; }}
  footer {{ color:var(--muted); font-size:12px; margin:28px 0 0; border-top:1px solid var(--line); padding-top:12px; }}
</style></head>
<body><div class="wrap">
  <h1>Farm-Out Opportunity-Cost &mdash; {e(str(rng['start']))} &rarr; {e(str(rng['end']))} ({rng['days']}d)</h1>
  <div class="scope">
    <b>Data-driven roster (Architecture B).</b> Each farm-out is priced against the WHOLE carded
    affiliate roster, cheapest eligible winning. Rates from real DriverPayRate rows; capability /
    capacity / permits from AffiliateProfile. Port Canaveral &amp; Sanford are their OWN categories
    &mdash; NOT protected as departures; judged purely on net-spend math. Threshold = {m(r['min_savings'])}.
    <br><b>Drive times calibrated</b> to real Orlando times (MCO&harr;Disney 30, MCO&harr;Universal 25,
    MCO&harr;SFB 60, Disney&harr;Port 72). <b>FAR/UNKNOWN destinations</b> (Other / Residential / Other
    Hotel) are treated as <b>UNCOMPUTABLE and ABSTAINED</b> &mdash; Approach A; live-distance verification
    (Approach B) deferred.
  </div>
  {warn_html}

  <h2 class="day">Roster ({len(roster)} rate-ready affiliates)</h2>
  <table>
    <tr><th>Affiliate</th><th>Capacity mode</th><th>Max vehicle</th><th>Daily cap</th><th>Rate rows</th><th>Profile</th></tr>
    {roster_rows}
  </table>
  {gap_html}

  <div class="grid">
    <div class="stat"><div class="k">Legs evaluated</div><div class="v">{t['targets']}</div>
      <div class="meta">{t.get('unassigned_targets', 0)} unassigned / {t['targets'] - t.get('unassigned_targets', 0)} farmed</div></div>
    <div class="stat"><div class="k">Keep in-house (recs)</div><div class="v">{t['recommendations']}</div></div>
    <div class="stat"><div class="k">Free in-house rescues</div><div class="v">{t['free_rescue']}</div></div>
    <div class="stat"><div class="k">Opportunity swaps</div><div class="v">{t['opportunity_swap']}</div></div>
    <div class="stat"><div class="k">Policy departure rescues</div><div class="v">{t['policy_departure_rescue']}</div></div>
    <div class="stat"><div class="k">Farm-only (must farm)</div><div class="v">{t.get('farm_only', 0)}</div>
      {f"<div class='meta'>{t['stuck']} stuck (alert)</div>" if t.get('stuck') else ""}</div>
    <div class="stat green"><div class="k">Free-rescue farm-$ avoided</div><div class="v">{m(t.get('free_rescue_avoided'))}</div>
      {f"<div class='meta'>+{m(t.get('free_rescue_avoided_hypothetical'))} hypothetical (leftovers)</div>" if t.get('free_rescue_avoided_hypothetical') else ""}</div>
    <div class="stat green"><div class="k">Swap/policy net savings</div><div class="v">{m(t['est_savings'])}</div></div>
    <div class="stat"><div class="k">VIP protected</div><div class="v">{t['vip_protected']}</div></div>
    <div class="stat"><div class="k">Abstained (far/unknown)</div><div class="v">{t.get('abstained_uncomputable_far', 0)}</div></div>
  </div>

  <h2 class="day">Port / Sanford behavior audit</h2>
  <table>
    <tr><th>Signal</th><th>Count</th></tr>
    <tr><td>True departures protected (dropoff = airport, non-Port/Sanford)</td><td>{audit['true_departures_protected']}</td></tr>
    <tr><td>Port Canaveral legs &mdash; to-port / from-port (now farmable)</td><td>{audit['port_to']} / {audit['port_from']}</td></tr>
    <tr><td>Sanford (SFB) legs &mdash; to-sanford / from-sanford (now farmable)</td><td>{audit['sanford_to']} / {audit['sanford_from']}</td></tr>
    <tr><td><b>Port/Sanford PICKUPS among targets</b> (excluded from drop-off-only affiliates)</td><td>{audit['waleed_excluded_pickups']}</td></tr>
    <tr><td>VIP legs seen among targets (never farmed)</td><td>{audit['vip_targets_seen']}</td></tr>
  </table>

  <h2 class="day">Affiliate load per day</h2>
  <table>
    <tr><th>Date</th><th>Legs</th><th>Evaluated</th><th>Unassigned</th><th>In-house depl.</th><th>Recs</th><th>Farm-only</th><th>To affiliates</th></tr>
    {load_rows}
  </table>

  {''.join(cards) if cards else "<p class='note'>No recommendations in this range.</p>"}

  <footer>
    Read-only <b>retrospective grade</b> &mdash; judges PAST farm decisions on decision-time info and
    never un-farms a committed leg. Feasibility uses the <b>SCHEDULED</b> (decision-time) flight
    arrival, not hindsight actual/estimated, and bounds each in-house rescue to the driver's
    <b>real worked day</b> (assigned-leg span), not stub windows. Tier-2 displacement is DEPTH-1 (one
    displaced leg per swap) this phase &mdash; deeper bundles deferred. It also runs as
    <b>decision support</b>: for an <b>unassigned leftover</b> (the founder hand-built the in-house
    schedule and left it) it decides keep-in-house vs farm. State A = the amount actually paid to farm
    an affiliate target, or the hypothetical cheapest-affiliate cost for an unassigned leftover (nothing
    was paid).
    <br>Drive times are <b>calibrated</b> to real Orlando times; far/unknown destinations
    (Other / Residential / Other Hotel) are treated as <b>uncomputable and abstained</b> (Approach A) so
    no recommendation rests on a guessed drive time. Live-distance verification (Approach B) is deferred
    &mdash; far endpoints on a <i>displaced/neighbor</i> leg still use the coarse table until then.
  </footer>
</div></body></html>"""

    def _card_html(self, rec, leg_line, m, e):
        disp = (rec.detail or {}).get("display", {}) or {}
        target = disp.get("target")
        pill = {"free_rescue": ("free", "FREE RESCUE"),
                "opportunity_swap": ("swap", "OPPORTUNITY SWAP"),
                "policy_departure_rescue": ("dep", "DEPARTURE / POLICY")}.get(rec.kind, ("swap", "?"))
        dep = " &middot; <span class='tag'>DEPARTURE</span>" if rec.target_is_departure else ""
        # Affiliate target: "Now" it is FARMED at the actual paid cost. Unassigned leftover: "Now" it is
        # an UNPLACED leftover and the figure is the hypothetical cost to farm it.
        unassigned = getattr(rec, "target_is_unassigned", False)
        actual = rec.target_actual_farm_cost
        hyp = rec.target_hypothetical_farm_cost
        now_cost = hyp if unassigned else actual            # figure to show in the "Now" column
        now_label = "UNASSIGNED leftover" if unassigned else "FARMED"
        now_cost_txt = (f"would farm ~{m(now_cost)}" if unassigned else f"farm cost {m(now_cost)}")

        if rec.kind == "free_rescue":
            keep = e(disp.get("keep_driver_name", f"driver {rec.keep_in_house_driver_id}"))
            now = (f"<div class='col now'><h4>Now</h4>Target <b>{now_label}</b><br>"
                   f"<span class='farm'>{now_cost_txt}</span></div>")
            prop = (f"<div class='col prop'><h4>Proposed</h4>In-house on <b>{keep}</b><br>"
                    f"<span class='farm'>farm $0</span> &middot; +1 in-house leg</div>")
            chain = ""
            resh = disp.get("reshuffled") or []
            if resh:
                items = "".join(
                    f"<li>{leg_line(x['leg'])} &rarr; <b>{e(x['to_driver'])}</b></li>" for x in resh)
                chain = f"<div class='chain'>Reshuffled in-house ({len(resh)}):<ul>{items}</ul></div>"
            _verb = "Avoids the ~" if unassigned else "Saves the whole farm cost (~"
            delta = (f"<div class='delta'>{_verb}{m(now_cost)} farm cost</div>"
                     if now_cost is not None else "")
        else:
            keep = e(disp.get("keep_driver_name", f"driver {rec.keep_in_house_driver_id}"))
            aff = e(disp.get("affiliate", "an affiliate"))
            displaced = disp.get("displaced")
            now = (f"<div class='col now'><h4>Now</h4>Target <b>{now_label}</b> "
                   f"(<span class='farm'>{now_cost_txt}</span>)<br>"
                   f"Displaced leg in-house</div>")
            prop = (f"<div class='col prop'><h4>Proposed</h4>Target in-house on <b>{keep}</b><br>"
                    f"Farm <b>{leg_line(displaced)}</b> &rarr; <b>{aff}</b> "
                    f"(<span class='farm'>{m(rec.state_b_farm_base)}</span>)</div>")
            chain = (f"<div class='chain'>Chain: displaced leg leaves the board (&rarr; {aff}); "
                     f"target chains onto {keep}.</div>")
            if rec.net_savings is not None:
                _basis = (f"~{m(now_cost)} to farm" if unassigned else f"{m(actual)} actually paid")
                delta = f"<div class='delta'>Net saves {m(rec.net_savings)} vs the {_basis}</div>"
            else:
                delta = "<div class='delta'>Policy rescue (economics uncomputable)</div>"

        return f"""<div class="card">
  <div class="head"><span class="pill {pill[0]}">{pill[1]}</span>
    <span class="legid">leg {rec.target_leg_id}{dep}</span></div>
  <div class="target">{leg_line(target)}</div>
  <div class="ba">{now}{prop}</div>
  <div class="reason">{e(rec.reason)}</div>
  {delta}
  {chain}
  {self._board_html(rec, e)}
</div>"""

    def _board_html(self, rec, e):
        """Render the affected drivers' real days as board-style rows so the reshuffle is VISIBLE and
        CHECKABLE: kept/moved/existing legs highlighted, with the feasibility math (prior clear -> assumed
        drive from->to + turnaround -> slack) exposed under each placed leg. Empty string if no boards."""
        boards = (rec.detail or {}).get("boards")
        if not boards:
            return ""

        def trunc(s, n=30):
            s = s or ""
            return s if len(s) <= n else s[:n - 1] + "…"

        def feas_line(feas):
            if not feas:
                return ""
            parts = []
            for side, fz in (("after", feas.get("preceding")), ("before", feas.get("following"))):
                if not fz:
                    continue
                slack = fz["slack_min"]
                cls = "ok" if slack >= 0 else "bad"
                sign = f"+{slack}" if slack >= 0 else str(slack)
                anchor = (f"after leg {fz['other_leg_id']} (clears {fz['other_time']})" if side == "after"
                          else f"before leg {fz['other_leg_id']} ({fz['other_time']})")
                parts.append(
                    f"{anchor} &rarr; drive {e(trunc(fz['drive_from'], 24))}&rarr;{e(trunc(fz['drive_to'], 24))} "
                    f"&asymp;{fz['drive_min']}m + {fz['turnaround_min']}m turn &rarr; "
                    f"<span class='{cls}'>{sign}m slack</span>")
            return ("<div class='feas'>fits: " + " &nbsp;&middot;&nbsp; ".join(parts) + "</div>") if parts else ""

        ROLE = {"kept": ("r-kept", "kept", "KEEP"), "moved_in": ("r-movedin", "movedin", "MOVED IN"),
                "moved_out": ("r-movedout", "movedout", "MOVED OUT"),
                "farmed_out": ("r-farmedout", "farmedout", "FARM"), "existing": ("", "", "")}
        blocks = []
        for bd in boards:
            rows_html = []
            for r in bd["rows"]:
                rcls, mcls, mlabel = ROLE.get(r["role"], ("", "", ""))
                vip = " <span class='vipdot'>&#9733; VIP</span>" if r.get("vip") else ""
                mark = f" <span class='rolemark {mcls}'>{mlabel}</span>" if mlabel else ""
                note = f" <span class='meta'>{e(r['note'])}</span>" if r.get("note") else ""
                route = f"{e(trunc(r['from']))} &rarr; {e(trunc(r['to']))}"
                rows_html.append(
                    f"<tr class='{rcls}'><td class='tm'>{e(r['pickup'])}</td>"
                    f"<td class='tm'>{e(r['clear'])}</td>"
                    f"<td>{route}{vip}{mark}{note}{feas_line(r.get('feas'))}</td>"
                    f"<td class='vh'>{e(r['vehicle'])}</td></tr>")
            title = e(bd["driver_name"]) + (f" &middot; {e(bd['vehicle'])}" if bd.get("vehicle") else "")
            blocks.append(f"<div class='bd'><h5>{title}</h5><table class='brd'>{''.join(rows_html)}</table></div>")
        return "<div class='boards'>" + "".join(blocks) + "</div>"
