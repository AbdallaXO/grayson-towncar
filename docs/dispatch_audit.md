Dispatch Board Audit & Situational-Awareness Plan
Context
The founder asked for an audit of the dispatch board (the "Legs Dashboard") from the perspective of a live-operations dispatcher, plus recommendations that help dispatchers notice operational risks faster — explicitly not auto-dispatch, not software making decisions, no automation acting on drivers. The board must stay fast to scan and uncluttered.

Audit method: three deep code explorations (board UI, data/logic layer, real-time behavior) + direct verification of the key claims. Main board: dispatching/views.py:110 (index) → dispatching/templates/dispatching/legs_filter.html (5,338-line template). Secondary timeline board: schedule_board.html.

Part 1 — Audit Findings
What the board already does well
One-page temporal flow: all of the day's legs, sorted by pickup time, no pagination — matches how dispatchers think ("what's now, what's next"). Render is fast: ~5–8 indexed queries even at 140 legs, zero external API calls at render (flight + GPS risk precomputed into columns by background threads).
Rich per-leg signal vocabulary already exists: flight status colors, flight-timing alert/watch left-bars, purple "⏰ was 3:30 PM" time-changed pill with ack, red "Conflict → task" badge, VIP gold, refund-pending, after-hours amber, Samsara live-GPS panel (At risk / Tight / On track) + "~N min drive" ETA badge, Dim Done toggle, driver coverage bar, per-driver Gantt with gap-critical/tight/big chips.
Strong invisible machinery: feasibility engine with MCO deplaning-grace turnaround math, travel-time-aware conflict detection, 3-min Samsara ETA sweep persisting Leg.dispatch_risk_status, 30-min AeroAPI flight refresh, ops task queue, ntfy phone alerts, driver PWA self-reporting statuses (which appear in LegStatus history).
Good conventions worth preserving: purple = state-change (not severity), gold = VIP (not severity), pickup moves are dispatcher-initiated only ("Match" click), audited via pickup_moves.py.
Where dispatchers spend unnecessary mental effort (the core problems)
The board computes the "why" but never shows it. detect_leg_flags() (dispatching/utils.py:684) produces render-ready reasons ("Not on the way — 12 min past pickup") and index computes turnaround_warning ("Overlap: conflicts by 8 min") — no template renders either (verified by grep). The dispatcher sees an unexplained red/amber row tint and must reverse-engineer the reason. This is pure waste: the highest-value fix in the whole audit is ~20 lines of template.
No live refresh. The board is fully server-rendered; the only data refresh is manual F5. Drivers self-report status via the PWA, Samsara writes risk every 3 min, flights refresh every 30 min — none of it appears until reload. The founder's 1:50 PM scenario is invisible unless someone happens to refresh. (Ironically, the driver portal already polls a fingerprint endpoint every 60s — drivers/views.py:514.)
The board's tightness math contradicts the real engine. The inline pass (views.py:502-526) uses raw gap < 0/10/20 and ignores required_turnaround()/deplaning grace — so a legitimate same-terminal MCO drop→arrival can be labeled "Critical" while the feasibility engine considers it fine, and a turn needing a 35-min reposition can look OK. Violates the saved design constraint (labels must reuse the feasibility engine). Same divergence in the timeline gap chips (views.py:727-728).
Driver-chain risk requires mental joins. The table is leg-centric; answering "will Marcos make his next pickup?" means finding his rows scattered down the page and doing gap math in your head. The per-driver Gantt exists but is collapsed by default and has no live status-vs-time context ("falling behind" state).
Nothing is forward-looking. All flags are current-state ("not on the way now"). Nothing answers "what breaks in 15–60 min if nothing changes" — no propagation of a driver's projected clear time (or a delayed flight's new arrival) through their remaining chain.
Conflict signal latency. The red conflict badge appears only after the 30-min background ops scan (except after "Match", which rechecks synchronously). A bad reassignment or a driver running long can be a stale 25 minutes.
Attention is scattered. Risk lives in: row tints, five different badge locations across three columns, the Samsara panel, the navbar Tasks badge, and phone ntfy alerts. There is no single "what deserves my attention right now / in 10 minutes" surface.
Threshold fragmentation. Deplaning grace 10 (engine) vs 15 (settings + ops); "tight" is variously <10/<15/<20 depending on subsystem. Labels can disagree with each other.
Easy-to-miss risks (consequences of the above)
Driver slowly falling behind across several legs (each leg individually fine).
Downstream impact of a flight delay on the driver's later legs (badge shows on the delayed leg only).
Unassigned leg approaching pickup (counts exist up top, but no per-row urgency escalation, and detect_leg_flags returns early for driverless legs).
Fragile no-buffer chains built earlier in the day (visible only if the Gantt is open and you read gap chips).
Unnecessary / distracting (candidates for progressive disclosure — not removal)
Always-visible travel-agent boxed panel and customer email in every row; three note types always fully expanded (15% column); payment detail beyond status+amount. Dead code: the pagination block (legs_filter.html:~2100-2149 references page_obj that index never provides).
Risk of "wall of amber": table-warning currently mixes real risk with after-hours-fee bookkeeping; on a busy day tint stops carrying signal.
Part 2 — Recommended Approach
Key insight: ~80% of what's needed already exists server-side. The work is (a) rendering what's computed, (b) making the board fresh, (c) consolidating risk math onto the real engine, (d) one triage surface. All advisory-only; dispatcher stays the decision-maker; no writes to drivers.

Phase 0 — Quick wins: render the WHY (smallest effort, largest value)
Files: legs_filter.html, new includes/_leg_reason_chips.html, new includes/_board_attention_css.html, views.py (~9 lines).

Reason chips: render leg.dispatch_flags (already dicts with level/icon/text) + leg.turnaround_warning as compact pills in the Pickup Time cell (next to the time, before the time-changed pill at :1260). Mirror into the mobile card view.
Feeds-next chip: in the existing turnaround loop (views.py:512-526), also stamp prev_leg.feeds_next_gap/level/leg_id (3 lines) → chip "→ 12m to next PU" on the leg that feeds a tight link; click scrolls to the next leg.
flashLegRow(id) JS helper: scroll + re-trigger the existing 3s gold .leg-highlight flash; also wire into timeline slot clicks (driver_timeline.html:89).
Scale/scan fixes: sticky table header; data-att severity attribute on rows/cards; "Problems only" filter pill (sessionStorage, shows live count); Dim Done default-ON for new sessions (:3018, treat null as on — it already persists in localStorage); delete the dead pagination block.
Severity CSS tokens: color = severity (critical red / warning amber / watch blue, no tint for watch), icon/chip = category; purple (time-changed) and gold (VIP) stay distinct non-severity conventions.
Phase 1 — Live freshness (fingerprint + detail polling)
Files: new dispatching/board_state.py, dispatching/urls.py, new includes/_board_live.html, small anchors in legs_filter.html.

Transport: extend the proven driver-portal pattern — GET /dispatching/api/board-state/?date= returning {fp, day:{leg_ids,total,unassigned}, legs:{id:{st,drv,drv_name,pt,risk,eta,flag,fl,conflict_task,...}}}. Poll every ~60s + on visibilitychange, jittered, backoff on errors, paused when tab hidden. SSE/websockets rejected: gunicorn --workers 3 --threads 4 = 12 request slots; held connections would pin most of them, and there's no redis/channels infra.
Cost: 2 indexed queries per poll (Leg(pickup_date,status) index exists). fp hashes only DB-written fields; time-driven flags recomputed per poll but diffed client-side (a quiet board doesn't "change" because the clock ticked).
Step 1a (ship first): fingerprint + "Board updated — Refresh" pill with one-click reload preserving scroll (sessionStorage) and filters (already in URL). Nothing can destroy in-progress work.
Step 1b: in-place patching — status selects, row tints, risk/ETA chip, "⇄ changed by X" marker on driver changes (never silently swap), conflict badge injection. Guards: skip rows containing document.activeElement or when a modal is open; queue and re-apply on blur. Structural changes (new bookings, >10 rows changed, draft-held day) fall back to the pill; on draft-held days report held:true and suppress driver patching (endpoint reports live world, board shows overlay).
Self-echo suppression: after a local update-leg-assignment POST, re-baseline immediately.
Phase 2 — Unified attention engine (consolidate risk math)
Files: new dispatching/attention.py (pure functions, zero queries, zero writes — same contract as feasibility_guards.py), dispatching/models.py (SchedulerSettings attn_* fields + migration), views.py::index wiring, new dispatching/tests_attention.py.

Data shapes: AttentionItem {leg_id, driver_id, severity: critical|warning|watch, category: status|gps|turnaround|cascade|flight|conflict_task|unassigned, reason, minutes, horizon: now|soon|later, source, task_id} and DriverAttention {state: ok|watch|behind|at_risk, behind_minutes, next_break_leg_id, break_short_minutes, cascade_depth}.
compute_attention(legs, driver_schedules, date, now, conflict_task_by_leg) merges existing primitives (no new turnaround math): detect_leg_flags (status), persisted Samsara fields gated on dispatch_eta_is_fresh (gps), chain_link_slacks = per-adjacent-slot slack via chain_repo_minutes + required_turnaround + chain_clear_dt (turnaround — replaces the crude pass; MCO same-terminal gets the −10 grace, satisfying the saved constraint), flight_timing_flag/flight_disruption_flag/has_unacked_time_change (flight), conflict-task map (deduped into turnaround items with task_id), and unassigned-approaching-pickup (new: critical <60 min, warning <120 — today nothing flags this).
Forward projection (project_driver_forward): anchor on observed reality (status vs clock; fresh GPS wins over static estimate, GPS on_time downgrades stale status-late to watch), replay the remaining chain carrying the delay forward; first slot whose slack goes negative = next_break_leg_id, count cascade_depth. This answers the founder's 1:50 scenario and "if this flight is delayed, what breaks next" (chain_clear_dt is already flight-delay-aware). O(slots) per driver; chains are already built every render (build_driver_schedules, views.py:554).
Wiring: delete views.py:502-526; keep template-compat names (turnaround_warning/level) now engine-backed; also fix timeline gap chips (:727-728) onto the same slacks so chips and labels can never disagree. Board stays ~5–8 queries (enforced by assertNumQueries smoke test).
Thresholds: new attn_* SchedulerSettings fields (tight=15 matching engine, behind=10, horizon=60, soon=15, unassigned 60/120) — tuning them never changes auto-assign; existing engine/ops constants kept (deplaning-grace 10 vs pax-ready 15 are deliberately different semantics; document in help_text).
Tests (run ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching.tests_attention; pollers already gate off under test): MCO same-terminal NOT-critical regression; founder 1:50 scenario → critical cascade on the 2:00 leg; cascade depth; GPS precedence + staleness; conflict-task dedupe; unassigned tiers; zero-query/zero-write guarantee; view smoke test.
Phase 3 — Triage surfaces (the UI for the engine)
Files: new includes/_attention_strip.html, includes/_driver_status_ribbon.html, driver_timeline.html, legs_filter.html.

Attention strip (top of board, after the draft banner): two lanes — ACT NOW (critical / overdue) and NEXT 60 MIN — items as compact chips [dot] [-12m|in 25m] [icon] reason — subject, ordered by time-to-impact, max 6 per lane + "+N more" collapse, click → flashLegRow. Per-chip × = session-local mute keyed by category:leg_id:severity (escalation re-surfaces); durable acks stay where they live (time-change ✓, task queue). Empty state: green "All clear."
Driver status ribbon (under the coverage bar): one chip per active in-house driver — [Marcos ● OTW 4m ago · next 3:15p (+22m)] — colored by DriverAttention.state; a falling-behind driver reads red at a glance. Click → open + scroll the Gantt. (The Gantt stays the deep-dive; ribbon is the always-visible summary. ONE mechanism, no duplication.)
Timeline upgrades: red "now" line; persist open/collapsed state.
Severity-class unification on rows/cards last (chips must exist before tints stop carrying meaning). After-hours amber moves off table-warning to its badge only (kills wall-of-amber).
Phase 4 — Event-driven conflict recompute + opt-in alerts
Files: ops/tasks.py, drivers/views.py:414, dispatching/views.py (update_leg_assignment), dispatching/pickup_moves.py, _board_live.html.

recompute_conflicts_for_leg(leg_id): bounded to one driver's day via existing detect_driver_conflicts; creates/closes DRIVER_CONFLICT tasks. Hooked (threaded via the existing _run_in_background pattern; synchronous under test) after: driver status writes, live assignments (mode != "staged", both old and new driver), and guest-verify pickup moves (Match already rechecks). Conflict latency: ~30 min → ~70 s end-to-end with polling.
Opt-in notification bell (off by default, per-device localStorage): visual / visual+sound for CRITICAL transitions only, 60-min per-(leg,kind) cooldown. Nothing driver-facing; ntfy stays the phone channel.
Phase 5 — Declutter (deferred until the above soaks)
Progressive disclosure only, no column removal: travel-agent panel + email behind a per-row expander; Private Notes/Comments line-clamped with "more"; payment reduced to status+amount. Mobile parity per step (+~40% effort on template-touching steps). Optional later: attention snapshot persisted from the 3-min Samsara sweep for "NEW since last look" markers; schedule_board.html adopting the same poll endpoint.

Critical files
dispatching/views.py — index: delete :502-526, wire compute_attention near :554, timeline chips :727-728, assignment hook :2326
dispatching/templates/dispatching/legs_filter.html + new includes (_leg_reason_chips, _attention_strip, _driver_status_ribbon, _board_live, _board_attention_css)
New: dispatching/attention.py, dispatching/board_state.py, dispatching/tests_attention.py, dispatching/tests_board_state.py
Reused primitives (no changes): feasibility_guards.required_turnaround:150, scheduler.chain_clear_dt:908 / chain_repo_minutes:938 / check_feasibility:966 / build_driver_schedules:1154, ops/tasks.detect_driver_conflicts:293 / classify_turn:211, utils.detect_leg_flags:684, samsara_risk, drivers/views.py:514 (poll pattern)
dispatching/models.py — SchedulerSettings attn_* fields + migration
Verification
ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching (system python; pollers auto-gate off under test). New suites: attention (MCO regression, founder-scenario fixture, GPS precedence, zero-query guarantee), board_state (fp stability, clock-only changes don't flip fp, permissions, hook create/close of conflict tasks).
assertNumQueries ceiling on the board view — attention adds zero queries.
Manual: seed a day with the founder scenario (1:30 departure + 2:00 arrival, driver on-location at 1:50) → row shows "Not picked up" chip, 2:00 leg shows cascade critical, strip shows both, ribbon shows driver red "20m behind"; open two browser sessions, change a status in one → other patches ≤60s; verify draft-held day degrades to pill-only.
Sequencing
Phase 0 (day-scale, immediate value) → Phase 1a (freshness pill) → Phase 2 (engine) → Phase 3 (strip + ribbon) → Phase 1b (in-place patching) → Phase 4 → Phase 5. Each step independently shippable; Phases 0 and 1a have no backend dependencies.