# 04 — Planner and Build Plan (the Phase-2 spec)

**Exactly what gets built, in what order, with every threshold defined — the document a builder
follows without guessing.** Satisfies the original brief's `PLANNER_AND_BUILD_PLAN.md`
requirement; numbered 04 for ordering.

| | |
|---|---|
| Produced | 2026-08-23 |
| Status | **Awaiting Gate 3** — founder approves this together with 02 (and the already-reviewed 00/01/03); building starts only after that approval, in a fresh session. Deviations during the build go back for review, not judgment calls (per the brief). |
| Governed by | [`01_REVISED_SCOPE_AND_PLAN.md`](01_REVISED_SCOPE_AND_PLAN.md) decisions **D1–D12**; model config from [`03`](03_STANDBY_AND_HANDOFF_MODEL.md); evidence from [`02`](02_BENCHMARK_AND_EVIDENCE.md) |

**The build order (D9/D12, founder-set): Build 1 quick fixes → Build 2 split/handoff in Day
Setup → Build 3 the batch day-builder. Each ships behind its own review; each is additive (D11)
— the current workflow keeps working untouched, always.**

---

## 1. Global rules (apply to every build)

1. **Additive, propose-only, no auto-apply.** Nothing writes a `Leg.driver` or
   `DriverVehicleAssignment` row except through the existing validated doors, on an explicit
   dispatcher action. Existing Day Setup / Build Schedule behavior is unchanged unless a section
   below names the change.
2. **Alert precision bar (D5): a warning class ships visibly only after demonstrating ≥70–80%
   precision on replayed dates** (scored with `analysis/09`'s conflict definitions). Below the
   bar it demotes to a passive info row, not a warning. Rationale: the shipped risk band false-
   alarms 66–97% and is ignored (00 §A4.5).
3. **Labeling:** every figure shown to a dispatcher carries its 00-convention label where
   non-obvious — modeled estimates say so in the tooltip ("estimate from booked times"), never
   dressed as fact.
4. **Config homes:** founder-tunable scalars → new fields on the existing `SchedulerSettings`
   singleton (live-editable, already the pattern; 00 §B4). Structured data (the zone chain
   matrix, occupancy lead/tail dict) → a new version-controlled module
   `dispatching/handoff_chain.py` holding the labeled tables from 03, imported everywhere —
   never duplicated. Nothing is hard-coded at a call site.
5. **Every dispatcher-visible change ships with a release note** per CLAUDE.md; templates read
   `docs/claude.md` first; modal DOM must not collide with `.ds-row`/`.ds-check`/`.ds-veh`
   (00 §B3 payload contracts — adding payload keys is safe, renaming is not).
6. **The frontend of each build is tested in a browser on a real date** before it's called done.

## 2. Build 1 — quick fixes (small, independent, first)

**1a. Warn-only validation on the manual assign path.**
- Where: `update_leg_assignment` (`dispatching/views.py:2719`), immediately before the
  `set_leg_driver` call on the `field == "driver"` branch (both assign and unassign paths
  return early today with no checks — verified 2026-08-23).
- What: compute (i) turn slack vs the driver's adjacent legs that day using
  `board_validation.turn_slack_minutes()` (the one slack formula, 00 §B3) and
  `fg.required_turnaround()`; (ii) the **co-driver car-share check**: if the driver's DVA
  vehicle that date is shared, the two drivers' occupancy blocks (03's lead/tail) must not
  overlap or interleave, and adjacent cross-driver pickups must clear
  `vehicle_share_pad_min` (see 1c).
- Behavior: **never blocks.** The JSON response gains a `warnings: [...]` list; the board UI
  shows them as a dismissible toast. Feature flag `manual_assign_warnings` (SchedulerSettings
  boolean, default ON).
- Precision gate: replay the warning logic over the 28 regime days with `analysis/09`; ship only
  if ≥70% of fired warnings correspond to a real hard/tight pair. (Expected to pass easily —
  it uses the same shipped constants 09 validated.)
- Note: no incident is attributed to this gap (the unit-#14 case was a vehicle swap — 01 Track B
  correction). This ships on code-verification grounds alone.

**1b. Fix the phantom-writing signal.**
- Where: `reservations/signals.py:751` skip-guard + the assignment logger at `:794–812`.
- What: when `update_fields` is present and names neither `driver` nor `status`, the handler
  must not emit `driver_assigned`/`driver_unassigned` rows at all (today it emits with
  `old=NULL`, fabricating 30.8% of the assignment log — 00 §A4.6). Keep emission logic otherwise
  byte-identical.
- Test: re-run the nightly confirmation-SMS path in a test; assert zero new assignment rows.
  Invisible to dispatchers → `Release-Note: none`.

**1c. Retire the flat 60-minute share pad.**
- `VEHICLE_SHARE_PAD_MIN = 60` (`scheduler.py:138`) becomes SchedulerSettings
  `vehicle_share_pad_min`, **default 120** (the empirical anchor; 60 sits at ~P9 of real
  handoffs). Build 2 upgrades the check from a flat pad to the zone chain; this fix just stops
  the engine being optimistic nine times in ten in the meantime.

**1d. The `'canceled'` one-L spelling** in `day_setup.py:122` joins the two-L exclusion (00 §A6).

## 3. Build 2 — split-shift & handoff support in Day Setup (the first real feature)

*The verified ~$78k/yr lever (02 §3), built into the tool schedulers already use, formalizing
what they already do by hand (D9). Everything here extends `suggest_day_setup`'s payload
additively and the Apply path explicitly.*

**2a. Record the plan.** When Apply saves a shared car (two drivers, one unit — allowed today
with no window), it now also writes `planned_start_hour`/`planned_end_hour` on both DVA rows
(the columns built for exactly this, NULL on all 2,591 rows to date). The AM/PM boundary comes
from the suggested cut (2c) or the dispatcher's edit. **Server-side hard rule: ≤2 drivers per
vehicle-date** (never observed above 2; currently unenforced — the share dropdown performs no
uniqueness check).

**2b. Handoff feasibility — green/amber/red.** For every shared car on the date, compute the
03 §3.2 rule from `dispatching/handoff_chain.py`: **GREEN** (clears the central zone chain),
**AMBER** (below central but ≥ the skip-wash floor — renders with the required explicit plan:
"wash the evening before / hand off at MCO"), **RED** (infeasible — shown, never suggested).
Arrival-anchored handoffs apply the volatility guard (03 §3.3): GREEN must survive a P75 retime
(13 min). Payload: new keys per shared unit (`handoff_band`, `handoff_ready_at`,
`handoff_reason`); existing keys untouched.

**2c. Standby suggestions — the mint proposal.** For the date, compute the adopted-rule standby
pool (03 §1: active, zero legs, zero DVA, no time-off, rest 510 both sides against actual
adjacent boards — logic identical to `analysis/10`, extracted into a shared helper so script and
product cannot drift). Where the day's demand leaves feasible free sides on cars (or a driver-day
exceeds 13.5 h and shedding its edge would create one), the panel proposes: *"Second shift on
unit N: [driver] 16:30–23:00, catches legs X, Y — saves ≈ $Z farm-out."* Soft ≥2-job packing
(D6); single-job proposals carry the thin-shift flag ("thin — worth it?"). **No daily call-out
cap** (founder). Propose-only: nothing is written until the dispatcher ticks and Applies through
the existing door.

**2d. Hours structure (D4).** The per-driver preview rows gain a span readout against 13.5 h;
a proposal may exceed it only as the **priced crunch exception**: "+1.5 h on [driver] keeps
2 legs in-house (≈ $142)" — explicit, per-driver, capped at 15.0 h hard, rendered as a choice,
never a default.

**2e. What in `day_setup.py` is touched vs. left alone.** Touched: the suggest payload (new keys
only), the Apply handler (planned-hours write + the ≤2 rule), the share-cut hour (the hard-coded
15:00 split moves to SchedulerSettings `share_split_hour`, default 16 — the measured modal
handoff hour). Left alone: `peak_concurrency`, `parkable_units`/`must_run`, the availability
resolver hard gate, unit-affinity, the pre-checked-vs-listed distinction, locked-row handling,
and every existing payload key. The `INLINE_RESOLVER` billed-call path (00 §B3) is out of scope
here and explicitly not worsened: the new code performs no drive-time lookups on unknown routes
(the zone chain is a static table).

**New SchedulerSettings fields (all live-editable):** `vehicle_share_pad_min` (120),
`share_split_hour` (16), `handoff_gap_green_pct`/`amber_floor` (from 03's central/low),
`mint_min_jobs_soft` (2), `span_exception_max_hours` (15.0), `manual_assign_warnings` (bool).
Occupancy lead/tail + the zone matrix live in `handoff_chain.py` [structured, version-controlled].

## 4. Build 3 — the batch day-builder (after Build 2 proves out)

*Architecture: the Candidate-Plan Outer Loop (01 §A3) — the optimizer materializes a candidate
(roster, pairing, share cuts) as unsaved DVA objects and evaluates it through the SHIPPED engine,
so feasibility can never diverge from production. Scope-checked here; detailed build tickets are
written when Build 2 is live and reviewed.*

**Prerequisite P1 — extract the pipeline.** The five-pass build lives inside the request handler
(`views.py:12240–12520`, ~700 lines, with six unparameterised `build_driver_schedules` call
sites in `scheduler.py` (:2410, :2460, :2606, :2783, :3042, :3271) and eight view-level ones).
Extract `run_assignment_pipeline(legs, drivers, target_date, windows, locked, *, dva_rows=None)`
returning `(assignments, warnings, moves)`; `auto_assign_drivers` becomes a caller. **Gate:
byte-identical output on 10 replayed dates before anything builds on it.**

**Prerequisite P2 — the shared co-driver gate.** One function, used by the replay scripts, the
engine, and Build 1a's warnings (its absence minted ~2 physically impossible legs/day in the
first replay — 01 §A3).

**The objective (the critic's inversion, protecting D-"available ≠ required"):** Pass A minimizes
**driver-days** subject to farm-outs ≤ the current suggest+build baseline; Pass B, at that roster,
minimizes farm cost then quality terms (span, fairness, handoff risk) — with the epsilon dial
("allow up to N more farm-outs to buy a better day") surfaced to the dispatcher, not hidden in a
weight. **Gate before the roster search ships:** the surrogate-noise test — between-roster-size
score differences must exceed within-size jitter spread on P50 and P90 days, or the roster ladder
is cut from v1 and the builder optimizes at the dispatcher-chosen roster size.

**Runtime:** an async job using the existing background-thread pattern (three daemon loops
already run in-process — 00 §B3); computed on demand from Day Setup with a visible
computed-at timestamp; never inside the request cycle (60 s gunicorn timeout).

**Write contract:** v1 is propose-only. Applying a plan routes through `apply_day_setup` +
`auto_assign_drivers(apply=True)` exactly as a human's plan would. Two known door gaps are
**explicit non-goals for v1**, resolved before any v2 one-click apply: `apply_day_setup` cannot
delete a DVA row ("leave driver off" is applied by the dispatcher unticking, as today), and it
has no held-date branch (on a held date the builder refuses to apply and says why — the sandbox
no-leak invariant is never risked).

## 5. Acceptance (Gate 4, per build)

- **Build 1:** warnings ≥70% precision on replay; phantom test green; no behavior change with
  flags off.
- **Build 2:** on 10 replayed dates the panel's proposals reproduce `analysis/10`'s feasibility
  verdicts (no proposal 10 would reject); browser-tested on a real date; release note shipped;
  planned-hours rows actually written on a real applied share.
- **Build 3:** on replayed dates, proposed plans ≥ the hand-finished board on in-house coverage
  with 0 hard conflicts, 0 days > 15 h, exceptions priced and visible (01 Gate 4). Promotion
  beyond "additional tool" is the founder's judgment (D11) — no metric auto-promotes it.

## 6. Non-goals (all builds — scope cannot creep past these)

No auto-apply anywhere. No optimizer-initiated `DriverVehicleAssignment` or `Leg` writes. No
driver-facing surface of any kind (standing rule: nothing acts on drivers). No affiliate/vendor
selection changes. No capacity planning (hire/buy — the D8 follow-on). No simulator (D10,
deferred). No new periodic daemon (reuse an existing loop if scheduling is ever needed). No
GPS reactivation (D3). No changes to pricing, payments, or the sandbox publish flow.

---

*Gate 3 = founder approval of this document (with 02). The build then starts in a fresh session:
Build 1 first, per §2.*
