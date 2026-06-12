"""Shared HTML rendering for the Farm-Out Opportunity-Cost Optimizer report.

PURE functions of the report dict produced by
``dispatching.farmout_optimizer.summarize_savings_range`` and its Recommendation objects --
no Django request/response, no ORM, no I/O. Two consumers:

  * ``render_report_page`` -- the management command ``analyze_farmout_savings``'s
    self-contained ``.html`` artifact (range reports).
  * ``build_page_context`` -- the staff page's template context (single date), including the
    ready-to-POST apply plans / affiliate options each card carries (formatting + selection
    only; every dollar comes verbatim from the engine, and the apply endpoint re-derives them).

This module itself never writes to the database or mutates the schedule.
"""
import html as _html
import json as _json
import re

_AIRPORT_CODE_RE = re.compile(r"\(([A-Z]{3})\)")


def _money(d) -> str:
    return f"${d:,.2f}" if d is not None else "-"


def _short_loc(s) -> str:
    """Trim a full address to a readable landmark for display.

    'Orlando International Airport (MCO), Jeff Fuqua Blvd, Orlando, FL, USA' -> 'MCO';
    'Caribe Royale Orlando, World Center Dr, Orlando, FL, USA'              -> 'Caribe Royale Orlando'.
    Keeps the airport code if present, else the text before the first comma.
    """
    if not s:
        return "?"
    m = _AIRPORT_CODE_RE.search(s)
    if m:
        return m.group(1)
    head = s.split(",")[0].strip()
    return head or s.strip()


def _short_route(d, e) -> str:
    """Escaped 'PICKUP -> DROPOFF' using short landmark names, from a leg-display dict."""
    if not d:
        return "&mdash;"
    return f"{e(_short_loc(d.get('pickup')))} &rarr; {e(_short_loc(d.get('dropoff')))}"


def _dur(mins) -> str:
    """Minutes, broken into hours past 60: 22 -> '22m', 60 -> '1hr', 80 -> '1hr 20m'.
    Sign-agnostic — pass abs() and prefix the sign yourself for slack."""
    mins = int(mins)
    if mins < 60:
        return f"{mins}m"
    h, mm = divmod(mins, 60)
    return f"{h}hr" if mm == 0 else f"{h}hr {mm}m"


def _kept_slack(rec):
    """The tightest slack (minutes) on the leg being KEPT in-house, pulled from the captured board
    feasibility — so the plan headline can say '+N min spare'. None if not available."""
    for bd in (rec.detail or {}).get("boards") or []:
        for row in bd.get("rows", []):
            if row.get("role") in ("kept", "moved_in"):
                feas = row.get("feas") or {}
                slacks = [z["slack_min"] for z in (feas.get("preceding"), feas.get("following")) if z]
                if slacks:
                    return min(slacks)
    return None


# ════════════════════════════════════════════════════════════════════════════
# HTML RENDER (self-contained, no server, read-only artifact)
# ════════════════════════════════════════════════════════════════════════════
def render_report_page(r) -> str:
    """Return the COMPLETE self-contained ``<!doctype html>`` report document for report dict ``r``."""
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
        route = f"{e(_short_loc(disp.get('pickup')))} &rarr; {e(_short_loc(disp.get('dropoff')))}"
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
            cards.append(_card_html(rec, leg_line, m, e))
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
  .legid {{ font-size:13px; color:var(--muted); margin-left:auto; }}
  .save {{ font-size:11px; font-weight:700; padding:2px 9px; border-radius:999px; white-space:nowrap;
           background:var(--greenbg); color:var(--green); letter-spacing:.01em; }}
  .save.muted {{ background:var(--amberbg); color:var(--amber); }}
  .plan {{ font-size:13.5px; color:#1f2937; background:#fbfcfe; border:1px solid var(--line);
           border-left:3px solid var(--blue); border-radius:8px; padding:10px 12px; margin:6px 0 10px;
           line-height:1.55; }}
  .plan b {{ color:var(--ink); }}
  .plan-sub {{ font-size:12px; color:var(--muted); margin-top:6px; }}
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
  /* dashed strike = "this in-house job leaves the board (gets farmed)" — details stay readable */
  tr.r-farmedout td {{ text-decoration:line-through; text-decoration-style:dashed; text-decoration-color:var(--amber); }}
  .rolemark {{ display:inline-block; font-size:10px; font-weight:700; letter-spacing:.03em; padding:1px 5px;
               border-radius:5px; text-decoration:none; }}
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


def _card_html(rec, leg_line, m, e):
    """One recommendation as a plain-English PLAN (what to do + why) above the affected driver's real
    timeline. Short landmark names throughout so the swap is easy to picture at a glance."""
    disp = (rec.detail or {}).get("display", {}) or {}
    target = disp.get("target")
    pill = {"free_rescue": ("free", "FREE KEEP"),
            "opportunity_swap": ("swap", "OPPORTUNITY SWAP"),
            "policy_departure_rescue": ("dep", "DEPARTURE / POLICY")}.get(rec.kind, ("swap", "?"))
    dep = " &middot; <span class='tag'>DEPARTURE</span>" if rec.target_is_departure else ""

    # Cost basis to keep the target in-house: the amount actually paid to farm an affiliate target, or
    # the hypothetical cheapest-affiliate cost for an unassigned leftover (nothing was paid yet).
    unassigned = getattr(rec, "target_is_unassigned", False)
    now_cost = rec.target_hypothetical_farm_cost if unassigned else rec.target_actual_farm_cost

    keep = e(disp.get("keep_driver_name", f"driver {rec.keep_in_house_driver_id}"))
    slack = _kept_slack(rec)
    fit = f" &mdash; fits, <b>+{_dur(slack)} spare</b>" if (slack is not None and slack >= 0) else " &mdash; fits"

    t_route = _short_route(target, e)
    t_meta = []
    if target:
        if target.get("time"):
            t_meta.append(e(target["time"]))
        if target.get("vehicle_type"):
            t_meta.append(e(target["vehicle_type"]))
    t_meta_txt = (" (" + " &middot; ".join(t_meta) + ")") if t_meta else ""

    if rec.kind == "free_rescue":
        chip_cls = ""
        chip = f"free &middot; avoids ~{m(now_cost)}" if now_cost is not None else "free"
        plan = (f"Keep <b>{t_route}</b>{t_meta_txt} in-house on <b>{keep}</b>{fit} &mdash; farm <b>$0</b>"
                + (f", avoiding the ~{m(now_cost)} it would cost to farm." if now_cost is not None else "."))
        resh = disp.get("reshuffled") or []
        if resh:
            moves = "; ".join(f"{_short_route(x['leg'], e)} &rarr; <b>{e(x['to_driver'])}</b>" for x in resh)
            plan += f"<div class='plan-sub'>Shifts {len(resh)} in-house job(s) to make room: {moves}.</div>"
    else:
        aff = e(disp.get("affiliate", "an affiliate"))
        displaced = disp.get("displaced")
        d_route = _short_route(displaced, e)
        d_time = f" ({e(displaced['time'])})" if (displaced and displaced.get("time")) else ""
        farm_cost = m(rec.state_b_farm_base)
        if rec.kind == "policy_departure_rescue":
            chip_cls, chip = ("", f"save {m(rec.net_savings)}") if rec.net_savings is not None else ("muted", "policy rescue")
            lead = f"Keep the departure <b>{t_route}</b>{t_meta_txt} in-house on <b>{keep}</b>{fit}."
        else:
            chip_cls, chip = ("", f"save {m(rec.net_savings)}") if rec.net_savings is not None else ("muted", "swap")
            lead = f"Put <b>{t_route}</b>{t_meta_txt} on <b>{keep}</b>{fit}."
        basis = (f"the ~{m(now_cost)} the kept job would cost to farm" if now_cost is not None
                 else "farming it directly")
        plan = (f"{lead}<br>Farm <b>{d_route}</b>{d_time} to <b>{aff}</b> for <b>{farm_cost}</b> instead "
                f"&mdash; cheaper than {basis}.")

    return f"""<div class="card">
  <div class="head"><span class="pill {pill[0]}">{pill[1]}</span>
    <span class="save {chip_cls}">{chip}</span>
    <span class="legid">leg {rec.target_leg_id}{dep}</span></div>
  <div class="target">{leg_line(target)}</div>
  <div class="plan">{plan}</div>
  {_board_html(rec, e)}
</div>"""


def _board_html(rec, e):
    """Render the affected drivers' real days as board-style rows so the reshuffle is VISIBLE and
    CHECKABLE: kept/moved/existing legs highlighted, with the feasibility math (prior clear -> assumed
    drive from->to + turnaround -> slack) exposed under each placed leg. Empty string if no boards."""
    boards = (rec.detail or {}).get("boards")
    if not boards:
        return ""

    def feas_line(feas):
        if not feas:
            return ""
        parts = []
        for side, fz in (("after", feas.get("preceding")), ("before", feas.get("following"))):
            if not fz:
                continue
            slack = fz["slack_min"]
            cls = "ok" if slack >= 0 else "bad"
            sign = "+" if slack >= 0 else "-"
            anchor = (f"after leg {fz['other_leg_id']} (clears {fz['other_time']})" if side == "after"
                      else f"before leg {fz['other_leg_id']} ({fz['other_time']})")
            parts.append(
                f"{anchor} &rarr; drive {e(_short_loc(fz['drive_from']))}&rarr;{e(_short_loc(fz['drive_to']))} "
                f"&asymp;{_dur(fz['drive_min'])} + {_dur(fz['turnaround_min'])} turn &rarr; "
                f"<span class='{cls}'>{sign}{_dur(abs(slack))} slack</span>")
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
            route = f"{e(_short_loc(r['from']))} &rarr; {e(_short_loc(r['to']))}"
            rows_html.append(
                f"<tr class='{rcls}'><td class='tm'>{e(r['pickup'])}</td>"
                f"<td class='tm'>{e(r['clear'])}</td>"
                f"<td>{route}{vip}{mark}{note}{feas_line(r.get('feas'))}</td>"
                f"<td class='vh'>{e(r['vehicle'])}</td></tr>")
        title = e(bd["driver_name"]) + (f" &middot; {e(bd['vehicle'])}" if bd.get("vehicle") else "")
        blocks.append(f"<div class='bd'><h5>{title}</h5><table class='brd'>{''.join(rows_html)}</table></div>")
    return "<div class='boards'>" + "".join(blocks) + "</div>"


# ════════════════════════════════════════════════════════════════════════════
# PAGE CONTEXT ADAPTER  (for the redesigned single-page Django template)
#
# Shapes the read-only ``report`` dict + Recommendation objects into a flat,
# template-ready context (plain dicts / lists / pre-formatted strings). It does
# NO math and recomputes NO dollars -- it only formats and selects fields the
# optimizer already produced, so the template stays a dumb renderer.
# ════════════════════════════════════════════════════════════════════════════
_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MON = ["January", "February", "March", "April", "May", "June", "July",
        "August", "September", "October", "November", "December"]


def _title_vehicle(v) -> str:
    """'mini_van' -> 'Mini Van', 'suv' -> 'SUV', 'towncar' -> 'Towncar'. '' -> ''."""
    if not v:
        return ""
    return v.replace("_", " ").title().replace("Suv", "SUV")


def _money0(d) -> str:
    """'$40' for whole dollars, '$40.50' otherwise. None -> ''."""
    if d is None:
        return ""
    from decimal import Decimal
    d = Decimal(str(d))
    return f"${int(d):,}" if d == d.to_integral_value() else f"${d:,.2f}"


def _compact_range(start, end) -> str:
    """'9:16 AM','11:04 AM' -> '9:16-11:04a'  (single-letter am/pm on the end only)."""
    def part(t):
        t = (t or "").strip()
        if not t:
            return ("", "")
        bits = t.split()
        hm = bits[0].lstrip("0") or bits[0]
        ap = bits[1][:1].lower() if len(bits) > 1 else ""
        return (hm, ap)
    sh, _ = part(start)
    eh, ea = part(end)
    if not sh and not eh:
        return ""
    return f"{sh}–{eh}{ea}"


def _fit_note(feas) -> str:
    """Plain-language fit line for a KEPT leg, no leg numbers."""
    if not feas:
        return ""
    fz = feas.get("preceding") or feas.get("following")
    if not fz:
        return ""
    drive = _dur(fz["drive_min"])
    turn = fz["turnaround_min"]
    turn_s = f"+ {_dur(turn)}" if turn >= 0 else f"− {_dur(abs(turn))}"
    slack = fz["slack_min"]
    slack_s = f"+{_dur(slack)}" if slack >= 0 else f"−{_dur(abs(slack))}"
    anchor = "after prior job" if feas.get("preceding") else "before next job"
    return (f"fits {anchor} (clears {fz['other_time']}) · ≈{drive} drive "
            f"{turn_s} turn → {slack_s} slack")


def _timeline(rec):
    """The affected driver's real day from rec.detail['boards'], with each row's role flagged so the
    one job actually being KEPT in-house is visually distinct from the make-room shuffle around it:
      keep    -> the recommendation's target (the single job we keep in-house)
      movein  -> a different job relocated ONTO this driver to clear room (note = '<- from <donor>')
      moveout -> a job leaving this driver to a receiver (note = '-> <receiver>')
      farm    -> a displaced job sent to an affiliate (farm_to = affiliate)
    Collapsing movein into keep (the old behavior) made a cascade read as several separate KEEPs."""
    aff = ((rec.detail or {}).get("display", {}) or {}).get("affiliate", "")
    _KIND = {"kept": "keep", "moved_in": "movein", "moved_out": "moveout", "farmed_out": "farm"}
    out = []
    for bd in (rec.detail or {}).get("boards") or []:
        rows = []
        for r in bd.get("rows", []):
            kind = _KIND.get(r.get("role"), "")
            rows.append({
                "time": _compact_range(r.get("pickup"), r.get("clear")),
                "route": f"{_short_loc(r.get('from'))} → {_short_loc(r.get('to'))}",
                "vehicle": _title_vehicle(r.get("vehicle")),
                "kind": kind,
                # feasibility fit is only meaningful where a leg was actually placed (target or relocated-in)
                "fit": _fit_note(r.get("feas")) if kind in ("keep", "movein") else "",
                # where a moved leg came from / went to ('<- from X' / '-> X'), already built upstream
                "note": r.get("note", "") if kind in ("movein", "moveout") else "",
                "farm_to": aff if kind == "farm" else "",
            })
        out.append({
            "head_name": bd.get("driver_name", ""),
            "head_vehicle": _title_vehicle(bd.get("vehicle")),
            "rows": rows,
        })
    return out


# Human wording for quote_affiliate_options skip reasons — shown under the affiliate pickers so
# "why isn't X offered for this job?" is answered on the page (permit rules, capability tiers,
# missing rate cards and consumed capacity are otherwise invisible to the founder).
_SKIP_REASONS = {
    "no_route": "no matched route",
    "vehicle_tier": "can't take this vehicle class",
    "port_pickup_permit": "no Port/Sanford pickup permit",
    "no_rate": "no rate for this route/vehicle",
    "over_capacity": "no capacity left that day (or time conflict)",
}


def _skipped_line(skipped) -> str:
    """'Oualid — no Port/Sanford pickup permit · Anthony — no capacity left…' ('' if none)."""
    if not skipped:
        return ""
    return " · ".join(f"{s.get('name', '?')} — {_SKIP_REASONS.get(s.get('reason'), s.get('reason'))}"
                      for s in skipped)


def _swap_card(rec, num, date_iso):
    """One keep-in-house recommendation (free rescue / opportunity swap / policy departure) as a
    template-ready card dict. Dollars taken verbatim from the optimizer -- no recompute. Carries
    the engine's ready-to-POST apply plan (ids only) plus the affiliate-override options so the
    page's Apply buttons can act on it; the endpoint re-validates everything server-side."""
    disp = (rec.detail or {}).get("display", {}) or {}
    target = disp.get("target") or {}
    displaced = disp.get("displaced") or {}
    unassigned = getattr(rec, "target_is_unassigned", False)
    now_cost = rec.target_hypothetical_farm_cost if unassigned else rec.target_actual_farm_cost
    keep_name = disp.get("keep_driver_name") or f"driver {rec.keep_in_house_driver_id}"
    is_free = (rec.kind == "free_rescue")
    save_val = now_cost if is_free else rec.net_savings
    # A "free" rescue can still require shuffling other in-house jobs to clear room. Count them so the
    # panel says "shifts N jobs to make room" instead of the misleading "no swap needed" when it isn't true.
    reshuffle_count = len(disp.get("reshuffled") or [])

    plan = dict((rec.detail or {}).get("apply") or {})
    if plan:
        plan["date"] = date_iso
    suggested_id = plan.get("suggested_affiliate_id")
    farm_options = [{
        "driver_id": o["driver_id"],
        "label": f"{o['name']} — {_money0(o['base'])}"
                 + (" · suggested" if o["driver_id"] == suggested_id else ""),
        "suggested": o["driver_id"] == suggested_id,
    } for o in (rec.detail or {}).get("farm_options") or []]
    cur = (rec.detail or {}).get("target_current") or {}

    return {
        "num": num,
        "kind": rec.kind,
        "is_free": is_free,
        "reshuffle_count": reshuffle_count,
        "keep_driver": keep_name,
        "keep_driver_first": (keep_name.split() or [keep_name])[0],
        "save": _money0(save_val),
        "save_label": "keep free" if is_free else "save",
        "route": f"{_short_loc(target.get('pickup'))} → {_short_loc(target.get('dropoff'))}",
        "time": target.get("time") or "",
        "vehicle": _title_vehicle(target.get("vehicle_type")),
        "tag": target.get("direction_tag") or "",
        "before_price": f"~{_money0(now_cost)}" if now_cost is not None else "",
        "after_farm_route": (f"{_short_loc(displaced.get('pickup'))} → "
                             f"{_short_loc(displaced.get('dropoff'))}") if (not is_free and displaced) else "",
        "affiliate": disp.get("affiliate") or "",
        "after_price": "$0" if is_free else _money0(rec.state_b_farm_base),
        "timeline": _timeline(rec),
        # --- apply actions ---
        "leg_id": rec.target_leg_id,
        "can_apply": bool(plan),
        "plan_json": _json.dumps(plan, default=str) if plan else "",
        "farm_options": farm_options,
        "farm_skipped_line": _skipped_line((rec.detail or {}).get("farm_skipped")),
        "keep_vehicle": _title_vehicle(disp.get("keep_driver_vehicle")),
        "is_currently_farmed": bool(cur.get("driver_id")),
        "current_name": cur.get("name") or "",
    }


def build_page_context(report) -> dict:
    """Adapt the read-only ``report`` (single service date) into the redesigned page's context."""
    from datetime import timedelta
    t = report["totals"]
    audit = report["audit"]
    rng = report["range"]
    day = (report["days"] or [{}])[0]

    start = rng["start"]
    has_iso = hasattr(start, "isoformat")
    date_iso = start.isoformat() if has_iso else str(start)
    try:
        date_str = f"{_DOW[start.weekday()]}, {_MON[start.month - 1]} {start.day}, {start.year}"
    except Exception:
        date_str = str(start)

    recs = day.get("recommendations") or []
    cards = [_swap_card(rec, i + 1, date_iso) for i, rec in enumerate(recs)]

    # ── Farm-as-planned items (the page's per-job Farm buttons) ─────────────────────────
    # Three buckets: actionable (unassigned, farmable -> select + Farm button), already farmed
    # (display-only confirmation), and blocked (VIP / departure / no priceable affiliate --
    # listed with the reason, never given a farm action; hard rules live server-side too).
    farm_rows, farmed_rows = [], []
    for it in (day.get("farm_items") or []):
        d_ = it.get("display") or {}
        options = it.get("options") or []
        blocked = ("VIP — never farmed" if it.get("vip")
                   else "Departure — keep in-house, never farmed" if it.get("is_departure")
                   else "No affiliate can price this job" if not options
                   else "")
        plan = {
            "kind": "farm_direct",
            "date": date_iso,
            "target_leg_id": it["leg_id"],
            "farm_affiliate_id": (options[0]["driver_id"] if options else None),
            "expected": {str(it["leg_id"]): it.get("current_driver_id")},
        }
        row = {
            "leg_id": it["leg_id"],
            "route": f"{_short_loc(d_.get('pickup'))} → {_short_loc(d_.get('dropoff'))}",
            "time": d_.get("time") or "",
            "vehicle": _title_vehicle(d_.get("vehicle_type")),
            "customer": d_.get("customer") or "",
            "tag": d_.get("direction_tag") or "",
            "abstained_far": bool(it.get("abstained_far")),
            "blocked": blocked,
            "options": [{"driver_id": o["driver_id"],
                         "label": f"{o['name']} — {_money0(o['base'])}"
                                  + (" · cheapest" if i == 0 else "")}
                        for i, o in enumerate(options)],
            "skipped_line": _skipped_line(it.get("skipped")),
            "plan_json": _json.dumps(plan, default=str) if (options and not blocked) else "",
        }
        if it.get("already_farmed"):
            row["farmed_to"] = it.get("current_driver_name") or "an affiliate"
            farmed_rows.append(row)
        else:
            farm_rows.append(row)
    farm_actionable = sum(1 for r in farm_rows if r["plan_json"])

    targets = t.get("targets", 0)
    keep_count = t.get("recommendations", 0)
    swap_count = t.get("opportunity_swap", 0)
    farm_only = t.get("farm_only", 0)
    abstained = t.get("abstained_uncomputable_far", 0)
    roster = report.get("roster") or []
    inhouse = f"{day.get('inhouse_deployable', '?')}/{day.get('inhouse_total', '?')}"

    # "Schedule not built yet" guard: if NO in-house drivers are on the board, every job is unassigned
    # only because the founder hasn't built the day yet -- there is nothing to weigh farm-outs against,
    # so do NOT present them as "farm as planned" (that would be a hallucinated recommendation).
    depl = day.get("inhouse_deployable")
    legs_total = day.get("legs", 0) or 0
    inhouse_built = isinstance(depl, int) and depl > 0
    unbuilt = (legs_total > 0) and not inhouse_built
    unassigned_count = day.get("unassigned_targets", 0) or targets

    keep_v = "is" if keep_count == 1 else "are"
    farm_v = "has" if farm_only == 1 else "have"
    farm_obj = "it" if farm_only == 1 else "them"
    sub_parts = [
        f"Of the <b>{targets} {'job' if targets == 1 else 'jobs'} headed for farm-out</b>, "
        f"<b>{keep_count}</b> {keep_v} cheaper to keep on your own drivers."
    ]
    if farm_only:
        sub_parts.append(f" The other <b>{farm_only}</b> {farm_v} no in-house room — farm {farm_obj} as planned.")
    if abstained:
        sub_parts.append(f" {abstained} couldn’t be priced (far / unknown) — see metrics.")

    def metric(v, label, *, zero=False, pos=False):
        is_zero = zero and (not v or v in (0, "0", "$0.00", "$0", "-"))
        return {"v": v, "label": label, "zero": is_zero, "pos": pos}

    metrics = [
        metric(targets, f"Legs evaluated ({t.get('unassigned_targets', 0)} unassigned / "
                        f"{targets - t.get('unassigned_targets', 0)} farmed)"),
        metric(keep_count, "Keep in-house (recs)"),
        metric(t.get("free_rescue", 0), "Free in-house rescues", zero=True),
        metric(swap_count, "Opportunity swaps"),
        metric(t.get("policy_departure_rescue", 0), "Policy departure rescues", zero=True),
        metric(farm_only, "Farm-only (must farm)"),
        metric(_money(t.get("free_rescue_avoided")), "Free-rescue farm-$ avoided", zero=True),
        metric(_money(t.get("est_savings")), "Swap / policy net savings", pos=True),
        metric(t.get("vip_protected", 0), "VIP protected", zero=True),
        metric(abstained, "Abstained (far / unknown)", zero=True),
    ]

    roster_rows = [{
        "name": a.get("name", ""),
        "mode": (a.get("mode") or "").replace("_", " "),
        "max_vehicle": _title_vehicle(a.get("max_vehicle_tier")) or "—",
        "daily_cap": a.get("daily_cap") if a.get("daily_cap") is not None else "—",
        "rate_rows": a.get("rate_rows", 0),
        "has_profile": bool(a.get("has_profile")),
    } for a in roster]

    audit_rows = [
        {"signal": "True departures protected", "note": "(airport dropoff, non-Port/Sanford)",
         "count": audit.get("true_departures_protected", 0)},
        {"signal": "Port Canaveral legs", "note": "(to-port / from-port)",
         "count": f"{audit.get('port_to', 0)} / {audit.get('port_from', 0)}"},
        {"signal": "Sanford (SFB) legs", "note": "(to / from)",
         "count": f"{audit.get('sanford_to', 0)} / {audit.get('sanford_from', 0)}"},
        {"signal": "Port/Sanford pickups among targets", "note": "",
         "count": audit.get("waleed_excluded_pickups", 0)},
        {"signal": "VIP legs seen among targets", "note": "(never farmed)",
         "count": audit.get("vip_targets_seen", 0)},
    ]

    return {
        "date_iso": date_iso,
        "prev_iso": (start - timedelta(days=1)).isoformat() if has_iso else "",
        "next_iso": (start + timedelta(days=1)).isoformat() if has_iso else "",
        "date_str": date_str,
        "unbuilt": unbuilt,
        "unassigned_count": unassigned_count,
        "legs_total": legs_total,
        "savings": _money0(t.get("est_savings")) or "$0",
        "sub_html": "".join(sub_parts),
        "stats": [
            {"v": keep_count, "label": "Keep & swap"},
            {"v": farm_only, "label": "Farm as planned"},
            {"v": inhouse, "label": "In-house used"},
            {"v": len(roster), "label": "Affiliates priced"},
        ],
        "keep_count": keep_count,
        "swap_count": swap_count,
        "farm_only": farm_only,
        "cards": cards,
        "farm_rows": farm_rows,
        "farmed_rows": farmed_rows,
        "farm_actionable": farm_actionable,
        "roster_rows": roster_rows,
        "roster_count": len(roster),
        "audit_rows": audit_rows,
        "metrics": metrics,
        "min_savings_display": _money0(report.get("min_savings")),
    }
