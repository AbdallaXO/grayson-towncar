# Grayson Towncar — Driver Scheduling Redesign (Research, Build Plan, Planner Integration)

You're an operations-research analyst and scheduling-systems engineer working inside Grayson
Towncar's Django backend. The end goal is a **live feature dispatchers use every day**: open a
date in Day Setup and see, alongside the existing roster/vehicle suggestions, how that day's
actual staffing compares to what demand says it should be.

This is a **three-phase engagement with a hard stop for review between every phase.** Do not
start a phase until told the previous one is approved.

- **Phase 1 — Research (read-only).** Understand demand and current waste from history. No
  design of a build yet, no code.
- **Phase 2 — Build Plan (read-only).** Only after Phase 1 is approved: translate the approved
  research into an exact spec for what Phase 3 builds.
- **Phase 3 — Build.** Only after Phase 2 is approved: implement the spec inside Day Setup.

---

## PHASE 1 — Research

### Ground rules

1. **Read-only.** No changes to scheduling logic or historical data. Analysis scripts live in
   their own folder, are reproducible, and document every assumption.
2. **Don't reinvent what's already built.** Before analyzing anything, inventory the current
   platform and **reconcile with it explicitly** — don't stand up a second, conflicting version
   of something that already exists:
   - Tight-turn feasibility engine + guards — `dispatching/feasibility_guards.py`,
     `dispatching/pickup_policy.py` (the one source of truth for "late"; MCO arrival = gate+10
     at the in-terminal meet point).
   - **Three demand-aware staffing advisors that already do pieces of this job** — read all
     three in full first, they are the closest prior art:
     - `dispatching/shift_advisor.py` — Second-Shift Advisor: detects when a day needs another
       driver, proposes a concrete in-house source (idle driver + spare unit, or a freed unit
       after another driver clears).
     - `dispatching/fold_advisor.py` — Fold-Out Advisor: detects thin working drivers and
       proposes releasing them, whole-day-or-nothing, validated through the same gate stack as
       the live engine.
     - `dispatching/rebalance_advisor.py` — Rebalance Advisor: the founder's relative-balance
       rule (spread work evenly, keep days dense, never let one driver feel cheated next to
       another).
   - `dispatching/day_setup.py` — the per-day roster + vehicle-plan suggester Phase 3 will
     extend. Read it in full now: understand its availability-resolver hard gate, unit-affinity
     logic, pre-checked-vs-listed distinction, and its "pure function of (date, DB), no writes,
     DVA rows only via explicit Apply" contract. Everything downstream must fit this contract.
   - KEOI flags, Recovery Advisor, the Staffing Board (`ops/scheduling.py`, `ops/coverage.py`),
     Rest Advisor (`dispatching/rest_advisor.py`, built but currently off, default
     `rest_min_gap_minutes=510`), Fleet Capacity Intelligence
     (`docs/fleet-capacity-intelligence/README.md` — buy-vehicle vs. hire-driver vs.
     keep-farming, already has a config-driven margin engine), Farm-Out Opportunity-Cost
     Optimizer (`dispatching/farmout_optimizer.py`,
     `docs/farmout-optimizer/ARCHITECTURE-B-HANDOFF.md` — read-only, answers "cheaper to farm
     this leg or keep it and farm something else instead"), and Samsara GPS
     (`dispatching/fleet_sync.py`, `dispatching/samsara_scheduler.py`).
   - Where handoff feasibility, staffing sufficiency, or farm-out economics are already
     modeled, **validate and critique those models against history** rather than re-deriving
     them from zero.
3. **Demand drives everything — derive it from scratch.** Base required staffing on
   reservation/leg demand, not on how we've historically scheduled (that's the thing we suspect
   is inefficient). Re-derive demand curves and shift shapes from raw trip data.
4. **Data window: the most recent 6–8 months** (roughly late Dec 2025/Jan 2026 → present). The
   business has grown fast; data from a year ago reflects a materially different, earlier-stage
   company and would bias the shift shapes. Use whatever exact cutoff the data supports inside
   that range and state it.
5. **Be honest about the data — start from, don't redo, the existing audit.**
   `docs/operational-data-audit.md` (generated 2026-07-31) is the existing data-reliability
   audit: `Leg` rows have no actual times — everything about what really happened is derived by
   differencing `reservations_legstatus` taps (Accept → On the Way → On Location → Picked Up →
   Complete). It documents a status-fabricating driver cohort to exclude, a validated "gold"
   cohort to use as ground truth, and that drive times are accurate at the median but
   tail-heavy. **Confirm it's still valid for the new window, note deltas, and build on it —
   don't re-run the same discovery.** Use conservative percentiles (P75/P90), never medians,
   for any operational buffer. Label every figure measured / inferred / modeled / unavailable.
   Never invent a clear time to make a handoff look feasible.

### Objective function — read this before analyzing "waste"

Both in-house and affiliate drivers are paid **per trip**, not hourly — so an idle hour inside
an in-house driver's shift is **not** a direct payroll cost the way it would be for hourly
staff. In-house drivers use **company-owned vehicles** (the company pays gas, insurance,
maintenance); affiliates use their own vehicles and are paid a materially higher rate per leg.
The real dollar cost of poor shift structure is:

- **The farm-out premium** — legs pushed to affiliates at the higher rate that idle in-house
  capacity could plausibly have covered. This is the primary money metric.
- **Idle company vehicle carrying cost** — insurance/maintenance/depreciation accruing on a car
  that's on the road (assigned) but not producing trips.
- **Driver fairness/retention** (not payroll) — the Rebalance Advisor's rule that no driver
  should feel cheated relative to the one next to him; a shift template that's mostly idle
  hurts a per-trip driver's effective hourly take-home even though nobody pays for the idle
  time directly.

Do **not** frame "waste" or replay wins primarily as "driver-hours saved" — frame them as
farm-out dollars avoided, vehicle-hours freed for reassignment, and driver-day density/fairness.
Reuse Fleet Capacity Intelligence's margin math for the dollar side rather than inventing a
parallel costing model.

### Farm-outs are demand — quantify what it would take to recapture more of it

Count **all** farmed-out legs as demand the proposed shift structure is measured against, not
just departures. Not all farm-out is avoidable — it's a normal release valve during crunch, not
automatically a staffing failure — so the ask is quantitative: for a range of staffing
increases (e.g. +1, +2, +3 in-house driver/vehicle pairs, by tier), estimate how much farm-out
volume and dollar cost that would recapture, and at what marginal cost (vehicle carrying cost +
the driver's per-trip pay it displaces). Reconcile against Fleet Capacity Intelligence's
buy/hire/farm engine and the Farm-Out Optimizer's per-leg analysis rather than re-deriving the
comparison from scratch.

### The one constraint you must not get wrong: turnaround-aware handoffs

A handoff is **not** "Driver A's last pickup is 2:00 → the vehicle is free at 2:00." Model the
full chain, and never count a handoff that doesn't survive it:

- **Outgoing:** final pickup → estimated clear (trip complete, at the drop-off location) →
  reposition + wash/fuel (default **30 min**, configurable, tune from history) → return to
  base (**MCO ↔ base ≈ 12 min** default; use actual routing where available; the outgoing
  driver often clears far from the airport — Disney, Universal, a resort — so base-return time
  varies a lot) → operational buffer → **Vehicle Ready at base.**
- **Incoming:** Vehicle Ready → take possession + inspect → reposition from base → first
  pickup. Job-type specific: an **airport arrival** needs circulation, parking, and a
  meet-buffer; a **departure** needs base→pickup travel plus a pre-pickup buffer. → **first
  feasible pickup.**
- Classify every handoff by slack (first-feasible pickup vs. the actual next pickup):
  **INVALID / CRITICAL / TIGHT / HEALTHY / EXCESSIVE.** Reconcile these thresholds and buffers
  against `dispatching/feasibility_guards.py` and `dispatching/pickup_policy.py` rather than
  inventing new ones.
- Only recommend a handoff when its rest/work-span/utilization benefit outweighs turnaround
  cost + deadhead + risk. The analysis must be able to conclude "keep the same driver and
  vehicle" — don't force handoffs.

### What to figure out (plan the "how" yourself)

- **The real demand curve** by day-of-week and time-of-day — distribution, not just average
  (median/P75/P90/max), broken down by vehicle class — and the natural demand periods
  (discover them from data; don't assume a morning/evening shape).
- **Where current schedules waste money and driver goodwill:** work-span vs. actual productive
  time, idle gaps, island jobs, avoidable late extensions/early starts, rest compression,
  vehicles locked to a driver on a long break, unnecessary vehicle swaps, and — per the
  objective function above — where idle in-house capacity coincided with same-window farm-outs.
- **A small set of demand-derived shift templates** — name, start/end, target demand period,
  days it's useful, min/typical/peak staffing, expected utilization, core vs. flex. Minimize
  distinct start/end times while still covering demand. Define flex boundaries; identify where
  split shifts are genuinely justified (and where they aren't).
- **Recurring fleet turnaround/handoff windows** — periods where several vehicles can
  realistically cycle through base without dropping coverage.
- **Proof by replay.** Replay historical days under the proposed structure vs. what actually
  happened: trip coverage/uncovered demand, farm-out dollars avoided (primary), drivers
  required, vehicle-hours idle vs. producing, average work span, long days, rest compression,
  vehicle utilization, number of distinct start/end times, flex drivers needed. **Only
  recommend the model if the replay shows it wins.** Include concrete dated examples —
  including cases where a restructure does *not* help.
- **Anything the data reveals that wasn't asked about** — bad vehicle allocation, recurring
  shortages, avoidable farm-outs, unnecessary deadhead, dispatch habits, vehicle-capability
  gaps. Flag it.

Do **not** design the Day Setup integration in this phase. Note anything you learn that seems
relevant to it, but the spec itself is Phase 2's job, done after this research is reviewed.

### Deliverables (Phase 1)

Commit everything to the repo under `docs/scheduling-redesign/` — this is backend analysis
invisible to dispatchers/chauffeurs, so per `CLAUDE.md` it ships with `Release-Note: none` in
the commit body, not a release note.

- **`00_DATA_AUDIT_AND_INVENTORY.md`** — data reliability for the chosen window (reconciled
  against `docs/operational-data-audit.md`), key assumptions, and the inventory of existing
  scheduling intelligence this work builds on (including the three staffing advisors and
  `day_setup.py` above). **Produce this first and stop for review before the deep analysis** —
  it's the go/no-go on everything else.
- **`DEMAND_AND_UTILIZATION.md`** — demand curves + where current scheduling wastes money and
  driver goodwill (framed per the objective function above).
- **`SHIFT_ARCHITECTURE.md`** — recommended templates, flex/split rules, turnaround/handoff
  windows.
- **`FARMOUT_RECAPTURE.md`** — the +1/+2/+3 in-house capacity analysis: farm-out volume/dollars
  recapturable per increment, marginal cost, reconciled against Fleet Capacity Intelligence and
  the Farm-Out Optimizer.
- **`REPLAY_AND_EVIDENCE.md`** — actual-vs-proposed deltas and concrete dated examples.
- **Charts + machine-readable CSV/JSON** only where they show a real pattern (demand heatmap,
  supply-vs-demand, span/idle/gap distributions, per-driver and per-vehicle utilization,
  actual-vs-proposed, farm-out recapture curve) — not for decoration.

**Stop after all Phase 1 deliverables are complete and wait for review of the full research
bundle before starting Phase 2.**

### Start here

Read the three staffing advisors, `day_setup.py`, and the two farm-out/fleet-capacity docs in
full, inventory the rest of the platform, produce `00_DATA_AUDIT_AND_INVENTORY.md`, and stop
for review. Flag anything unsure about. Prefer measured data over intuition; don't assume
current schedules, vehicle assignments, or driver flexibility are optimal; don't pick shift
times until the demand data reveals them.

---

## PHASE 2 — Build Plan (read-only; only after Phase 1 is approved)

**Do not begin this section until explicitly told Phase 1's research is approved.**

Translate the approved research into an exact, buildable spec — this phase produces a document,
not code.

- **`PLANNER_AND_BUILD_PLAN.md`** must define:
  - Exactly what the Day Setup diagnostic computes and shows for a given date: the fields, the
    comparison logic ("today calls for X, you have Y scheduled, the gap is Z"), and how farm-out
    exposure implied by a gap is surfaced (tie back to `FARMOUT_RECAPTURE.md`).
  - Every metric/threshold definition used, precisely — no ambiguity a builder would have to
    guess at.
  - Every config value (turnaround time, buffers, percentiles, flex ranges, shift templates
    themselves) exposed as an editable parameter, not hard-coded — and where it lives (a
    settings module vs. a DB-backed config table; justify the choice).
  - What's computed live vs. precomputed, and at what latency/staleness is acceptable.
  - How each figure is labeled measured / inferred / modeled / unavailable when shown to a
    dispatcher.
  - What in `dispatching/day_setup.py` is touched vs. left alone, and confirmation the new
    logic fits its existing "pure function of (date, DB), no writes" contract.
  - Explicit non-goals for Phase 3 — e.g. no auto-apply, no new `DriverVehicleAssignment`
    writes — so scope can't creep during the build.

**Stop after this document is complete and wait for review before starting Phase 3.**

---

## PHASE 3 — Build (only after Phase 2's plan is approved)

**Do not begin this section until explicitly told Phase 2's plan is approved.**

### What ships

Extend `dispatching/day_setup.py`'s existing per-day view exactly per `PLANNER_AND_BUILD_PLAN.md`:
alongside the current roster and vehicle-plan suggestions, show the demand-derived shift
template for that date next to what's actually rostered, with a sufficiency/gap signal.

### Behavior contract — diagnostic only, matching every existing advisor's pattern

- **Read-only, propose-nothing, no auto-apply.** It shows the gap; it never assigns a driver,
  never pre-checks a box, never writes a `DriverVehicleAssignment` row. This matches Day
  Setup's own "pure function of (date, DB), no writes" contract, and the propose-only pattern
  every staffing advisor above already follows.
- **Additive to the existing modal**, not a replacement — the current availability/roster/unit
  suggestions keep working exactly as they do today. If unsure whether something in Day Setup
  is load-bearing, ask before touching it.
- Follow `PLANNER_AND_BUILD_PLAN.md`'s spec exactly — deviations go back for review, not
  judgment calls made mid-build.

### Ground rules for this phase

- This **is** a dispatcher-visible change (a new signal in a tool dispatchers use daily) — it
  ships with a release note per `CLAUDE.md`: copy `docs/release-notes/_TEMPLATE.md`, read
  `docs/release-notes/README.md` for voice rules, write the note as part of this work, `git add`
  it alongside the change. Say what did *not* change (no auto-apply, no new writes, the existing
  roster/vehicle suggestions are unaffected).
- Read `docs/claude.md` before touching any template/HTML/CSS for the modal — this is a
  luxury-brand product, and the new signal needs to feel like part of Day Setup, not a
  bolted-on widget.
- Test the modal in a browser on a real date before calling this done, not just via the test
  suite.
