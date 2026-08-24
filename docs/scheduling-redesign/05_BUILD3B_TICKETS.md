# 05 — Build 3b Tickets (the day-builder itself)

**The buildable spec for the optimizer: every threshold named, every config value given a
home, every gate defined. Written after Build 3a landed its two prerequisites, so the
ticket numbers rest on measured runtime and a measured baseline rather than on estimates.**

| | |
|---|---|
| Produced | 2026-08-23 |
| Status | **Awaiting founder review.** Per the Build 3a brief, no 3b session starts until this is reviewed. |
| Governed by | [`04_PLANNER_AND_BUILD_PLAN.md`](04_PLANNER_AND_BUILD_PLAN.md) §4 (Build 3) and §1 (global rules); [`01_REVISED_SCOPE_AND_PLAN.md`](01_REVISED_SCOPE_AND_PLAN.md) §1 + D1–D12 + §A3; model config from [`03`](03_STANDBY_AND_HANDOFF_MODEL.md); scorecard from [`02`](02_BENCHMARK_AND_EVIDENCE.md) §1 |
| Labels | **[measured]** / **[modeled]** / **[founder-supplied]** / **[assumed]** per 00's convention |

---

## 0. What Build 3a landed (the ground this stands on)

| | |
|---|---|
| **P1** | `dispatching/assignment_pipeline.py` — `run_assignment_pipeline(legs, drivers, target_date, windows, locked, *, dva_rows=None)`. All eight build passes, extracted verbatim out of `views.auto_assign_drivers`, which is now a caller. `dva_rows` threads a **hypothetical roster** through every DVA-reading engine call, which is the whole mechanism the Candidate-Plan Outer Loop needs. |
| **P2** | `dispatching/car_share.py` — the one home for every co-driver rule. `scheduler` and `assign_warnings` re-export the names their ~12 callers and both replay scripts import. |
| **Gate (P1)** | `analysis/14_pipeline_parity.py` — the production view's complete JSON response over 10 replayed dates × 4 payload scenarios, before vs after: **0 differences**. Harness determinism proved first by two independent pre-refactor captures diffing to 0. |
| **Gate (P2)** | `analysis/12` and `analysis/13` re-run against one frozen snapshot before and after: both output CSVs **byte-identical**. Full `dispatching` suite **1866 tests OK** (1826 baseline + 40 new). |

**Not unified, deliberately.** The co-driver gate is evaluated under three conventions that
disagree on real inputs, so collapsing them changes verdicts. §9.1 is the discrepancy, with
numbers, for your ruling.

---

## 1. The measured starting point — and why it reframes Build 3b

Running the **shipped pipeline cold** (every driver assignment stripped, the day arriving
exactly as the builder will meet it) at the roster the dispatchers actually set, over the
same 10 gated dates [measured, `analysis/14`, capture in `out/14_pipeline_parity_before.json`]:

| date | legs | in-house | farmed | coverage | driver-days | >13.5 h | >15 h |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2026-07-25 | 125 | 107 | 18 | 85.6% | 15 | 4 | 0 |
| 2026-07-28 | 55 | 55 | 0 | 100.0% | 11 | 0 | 0 |
| 2026-07-31 | 114 | 100 | 14 | 87.7% | 17 | 3 | 0 |
| 2026-08-03 | 104 | 85 | 19 | 81.7% | 14 | 2 | 0 |
| 2026-08-06 | 97 | 79 | 18 | 81.4% | 14 | 0 | 0 |
| 2026-08-10 | 112 | 98 | 14 | 87.5% | 15 | 1 | 0 |
| 2026-08-13 | 103 | 96 | 7 | 93.2% | 17 | 2 | 0 |
| 2026-08-16 | 155 | 118 | 37 | 76.1% | 17 | 3 | 0 |
| 2026-08-19 | 72 | 70 | 2 | 97.2% | 15 | 0 | 0 |
| 2026-08-22 | 186 | 154 | 32 | 82.8% | 17 | 3 | 0 |
| **all** | **1123** | **962** | **161** | **85.7%** | **15.2/day** | **1.8/day** | **0** |

Against state B, the hand-finished board (02 §1, 28 days): **81.3% in-house, 20.18 farmed/day,
15.46 driver-days/day, 4.00 driver-days/day over 13.5 h, 2.18/day over 15 h.**

**Read this carefully before drawing the obvious conclusion.** Three things make the cold
replay flattering, and they are the reason this table is a *baseline*, not a victory:

1. **It plans against the realised clock.** The snapshot holds arrival `pickup_time` as it
   ended up, and 97.5% of arrivals were retimed, mostly in-day (00 §A3.8, 01 §4.5). The cold
   run therefore has foresight the real builder did not. This is the single largest source of
   optimism and it is not small.
2. **Fixed demand, greedy fills.** Every replay gain in this project is a counterfactual
   (01 §4.4); ~46% of specific farm refills drift >30 min by service time.
3. **Ten dates, not twenty-eight**, and they include light days (07-28 at 55 legs runs 100%).

What survives all three caveats is the *shape* of the finding, and it is the important one:

> **The assignment engine is not the bottleneck.** Given a roster, the shipped passes already
> place legs about as well as the hand-finished board does — while breaking *fewer* hour rules
> (0 days over 15 h against state B's 2.18/day). The lever Build 3b is being built for is the
> one 01 §1 already named: **which drivers work at all, on which cars, with which splits.**

That is Pass A. It also means Pass A's constraint has a concrete measured value: **the
suggest+build baseline the optimizer may not do worse than is 16.1 farm-outs/day at 15.2
driver-days/day on these dates** — recomputed per date at run time, never hard-coded.

### 1.1 The runtime that constrains everything

One cold pipeline run, measured on real dates [measured, n=12 runs over 6 dates]:

| legs on the day | 83 | 102 | 114 | 125 | 155 | 186 |
|---|---:|---:|---:|---:|---:|---:|
| seconds | 3.3–4.2 | 6.9–8.1 | 2.8–3.9 | 5.0–5.1 | 10.1–12.0 | 11.3–15.3 |

**Median 6.0 s, P90 12.0 s, max 15.3 s.** Superlinear in legs. Every candidate plan the outer
loop scores costs one of these. That number, not the search algorithm, sets the design:
**a 60-evaluation search is a 12-minute job.** Every ticket below is written against it.

---

## 2. Ticket A — the Pass-A/Pass-B objective

### A1. The score, lexicographic (not a weighted soup)

01 §1 is binding: coverage is the number that goes up; conflicts and hours are **walls**.
So the score is a tuple compared left to right, never summed:

```
score(plan) = ( driver_days,          # ← Pass A minimises this
                farm_outs,            # ← subject to: ≤ baseline + epsilon
                farm_cost,            # ← Pass B minimises this first
                quality )             # ← then this
```

**Walls — a plan violating any of these is not scored, it is discarded.** Every one is
already computable from a `PipelineResult` plus the existing guards; none is new maths:

| Wall | Where it is measured | Threshold |
|---|---|---|
| Hard-infeasible turn pairs | `board_validation.turn_slack_minutes` + `pickup_policy.turn_band == 'critical'` | **0** |
| Driver-day over the hard ceiling | `scheduler.effective_span_hours` | **0 above `span_exception_max_hours` (15.0)** |
| Overnight rest breach | `rest_min_gap_minutes` (510), both sides, vs *actual* adjacent-day work | **0 new** (pre-existing breaches on the real board are not the plan's fault — count the delta) |
| Drivers per vehicle-date | `car_share.MAX_DRIVERS_PER_VEHICLE_DATE` | **≤ 2** |
| Co-driver share conflict | `car_share.sharers_conflict` (convention A — see §9.1) | **0** |
| Handoff band RED | `handoff_chain.handoff_band` | **0 proposed** (RED is shown, never suggested — 04 §3.2b) |

**`driver_days`** = the number of drivers holding ≥1 assigned leg in the plan. Not the number
rostered: a driver on the roster with nothing to do is a *correct* answer being left off, which
is precisely D-"available ≠ required".

**`farm_outs`** = `len(result.unassigned) − len(result.assignments)`, i.e. legs the plan leaves
for affiliates.

**`farm_cost`** = `Σ fleet_intel.affiliate_base_cost(leg)` over the farmed set — reuse the
shipped function (00 §B3 names it as worth reusing), do not invent a second cost model. Where
a per-leg cost is unavailable, fall back to `standby_mints.FARMOUT_PREMIUM_PER_LEG` (70.99)
and **say so in the UI**, per 04 §1 rule 3.

**`quality`** = a weighted sum, and the ONLY place weights are allowed, because these terms are
genuinely commensurable preferences rather than rules:

| Term | Definition | Default weight | Home |
|---|---|---:|---|
| span pressure | Σ over drivers of `max(0, effective_span_h − 13.5)` | 1.0 | `SchedulerSettings.opt_w_span` |
| fairness | population stdev of legs-per-working-driver | 1.0 | `SchedulerSettings.opt_w_fairness` |
| handoff risk | count of AMBER handoff bands (RED is a wall) | 2.0 | `SchedulerSettings.opt_w_handoff` |
| idle gaps | Σ of internal gaps above `idle_gap_threshold` (120), in hours | 0.5 | `SchedulerSettings.opt_w_gaps` |

Weights are **[assumed]** starting values, live-editable, and must be labelled as such in the
UI. They break ties inside Pass B only; they can never move a wall or outrank farm cost.

### A2. Pass A — the roster descent

```
R0        = the dispatcher's roster (drivers with a DVA row who are available)
baseline  = run_assignment_pipeline(R0)          # 1 evaluation
best      = R0
loop:
    rank the removable drivers in `best` by the CHEAP pre-screen (A3)
    for d in the top PASS_A_PROBE_WIDTH candidates:
        cand = run_assignment_pipeline(best \ {d})     # 1 evaluation each
    keep the cand with the fewest farm_outs (ties: lower quality term)
    accept it iff cand.farm_outs <= baseline.farm_outs + epsilon   AND no wall broken
    stop when nothing is acceptable, or PASS_A_MAX_EVALS is spent
```

Removal is one-directional (never re-add): the descent is greedy and monotone in driver-days,
which is what makes it bounded and explainable to a dispatcher — "these three came off, in
this order, and here is what each one cost."

### A3. The cheap pre-screen (this is what makes the runtime work)

A full evaluation per candidate is 6–15 s, so ranking candidates before spending one is the
whole game. **Reuse `fold_advisor.build_fold_out_proposals`** — it already answers "whose
entire day verifiably fits on the others", with a six-gate stack, on an already-built board,
for free. Its ranking becomes Pass A's probe order.

> **Prerequisite inside this ticket:** 00 §B3 records that `fold_advisor._simulate` and
> `rebalance_advisor._gate_receiver` are hand-maintained duplicates that have **already
> drifted** — rebalance carries a seventh `hollow` gate fold does not have, and their `idle`
> gates differ semantically. Extract one shared `gate_receiver()` and reconcile the drift
> **before** Pass A leans on fold's ranking. Same discipline as Build 3a's P1: a byte-identical
> gate on replayed dates first, and if reconciling changes a verdict, bring the discrepancy
> back rather than picking a side.

### A4. Pass B — pairing and splits at the chosen roster

At the roster Pass A settles on, the remaining levers are **which vehicle each driver takes**
and **where the share cuts fall**. The space is too large to enumerate, so bound it explicitly:

1. **Seed** with the existing Day Setup pairing (`day_setup.suggest_day_setup`) — it already
   solves tier feasibility and unit affinity; do not re-solve them.
2. **Targeted pairing swaps only.** Consider a swap of two drivers' units only when it changes
   a tier constraint or removes a co-driver conflict. Cap at `PASS_B_MAX_SWAPS` (default 6).
3. **Share cuts** come from the shipped Build-2 machinery: `standby_mints` for the mint
   proposals, `handoff_chain.handoff_band` for feasibility, `share_split_hour` for the cut.
   Pass B chooses *whether* to take each proposal, not how to compute it.
4. Score each variant with A1; keep the best; stop at `PASS_B_MAX_EVALS`.

### A5. Named constants (all new `SchedulerSettings` fields, all live-editable)

| Field | Default | Meaning |
|---|---:|---|
| `opt_enabled` | `False` | master switch; ships OFF |
| `opt_epsilon_farmouts` | `0` | the dial (§3) |
| `pass_a_probe_width` | `3` | full evaluations per descent step |
| `pass_a_max_evals` | `20` | hard evaluation budget for Pass A |
| `pass_b_max_swaps` | `6` | pairing swaps considered |
| `pass_b_max_evals` | `10` | hard evaluation budget for Pass B |
| `opt_runtime_budget_s` | `240` | wall-clock ceiling; the job stops and returns its best-so-far |
| `opt_w_span` / `opt_w_fairness` / `opt_w_handoff` / `opt_w_gaps` | 1.0 / 1.0 / 2.0 / 0.5 | quality weights **[assumed]** |

At the defaults the worst case is 1 + 20 + 10 = **31 evaluations**, which at the measured P90
of 12.0 s is **6.2 minutes**, inside the 240 s budget on a median day (6.0 s → 3.1 min) and
truncated by the budget on the heaviest. That truncation is intended and must be *visible*:
the result carries `budget_exhausted: true` and the panel says so.

---

## 3. Ticket B — the epsilon dial

04 §4 is explicit that the coverage/quality trade is **surfaced to the dispatcher, not hidden
in a weight**. So:

- One control in the Day Setup panel: **"Allow up to N more farm-outs to buy a better day"**,
  N ∈ {0, 1, 2, 3}, default **0**. Persists to `opt_epsilon_farmouts`.
- At N = 0 the optimizer may never worsen coverage against the same-date suggest+build
  baseline. This is the shipping default and the one the Gate-4 acceptance runs at.
- Whenever N > 0 changes the answer, the panel must name the trade in the founder's own terms:
  *"Leaving Marcus off farms 2 more legs (≈ $142) and takes 1.5 h off three other drivers."*
  Never a score, never a weight — legs and dollars and hours.
- The epsilon applies to `farm_outs` **only**. It can never buy a wall.

---

## 4. Ticket C — the surrogate-noise test (ship/no-ship for the roster ladder)

**04 §4 makes this a gate, not a nice-to-have: if between-roster-size score differences do not
exceed within-size jitter, the ladder is cut from v1 and the builder optimizes at the
dispatcher-chosen roster size.** Build it as `analysis/16_surrogate_noise.py`, offline.

**Method.** Pick one P50-demand and one P90-demand date from the current regime (derived from
the data, no literals — same technique as `analysis/13`/`14`). For each roster size
*k* ∈ {|R₀|, |R₀|−1, …, |R₀|−4|}: draw **M = 8** distinct rosters of size *k* from R₀
(deterministic selection — index-strided, never `random`, so the script is re-runnable), run
the pipeline on each, and record the A1 score.

- `within(k)` = P90 − P10 of the quality-and-farm score across the 8 same-size rosters.
- `between(k)` = |median score at *k*| − |median score at *k*−1|.

**Ship the ladder iff `between(k) > within(k)` for every adjacent pair, on BOTH days.**
Otherwise Pass A is cut from v1: the optimizer keeps the dispatcher's headcount and only runs
Pass B. Record the verdict in the script's output, not in a person's memory.

**Cost:** 2 days × 5 sizes × 8 rosters = 80 evaluations. At the measured 6.0 s median that is
~8 minutes; on a P90 day nearer 16. Acceptable for a one-off offline experiment; this never
runs in production.

---

## 5. Ticket D — runtime and the async job

**Never in the request cycle.** The gunicorn worker is a single sync worker with a 60 s
timeout, and the measured search is minutes (§1.1). The job uses the **existing background
pattern** — 00 §B3 records three daemon loops already running in-process
(`dispatching/samsara_scheduler.py` lock 737_202, `ghl_integration/scheduler.py` 737_201,
`drivers/wakeup_scheduler.py` 737_203), plus `reservations.utils._run_in_background`.
**04 §6 forbids a new periodic daemon**, so:

- The dispatcher clicks **"Build a plan"** in Day Setup → the endpoint validates, claims the
  job, and returns immediately.
- The work runs in a `_run_in_background` daemon thread. It **must close its DB connection on
  exit** — the 2026-07-18 outage post-mortem made that a standing rule for this pattern.
- One job per date at a time, claimed by a row-level flag so a double-click cannot double-run.
- The panel polls a small status endpoint and renders the result with a **computed-at
  timestamp** ("built 4:12 PM, from bookings as of 4:11 PM"). A plan older than
  `opt_stale_after_min` (default 120) renders greyed with "re-build".
- The job is **read-only** (§6). It never writes a Leg or a DVA row.
- `ROUTE_DISTANCE_INLINE_RESOLVER`: the job must not become a new billing surface. It performs
  no drive-time lookups on unknown routes beyond what the shipped pipeline already does; the
  zone chain is a static table (04 §3.2e). Confirm with a query-count assertion in the gate.

---

## 6. Ticket E — the write contract

**v1 is propose-only.** Applying a plan routes through `apply_day_setup` +
`auto_assign_drivers(apply=True)` exactly as a human's plan would. **No new write door, no
third copy of `set_leg_driver` semantics** (00 §B3 records that the auto-assign apply path
already re-implements it inline and has drifted — do not add a third).

The two known door gaps are **explicit non-goals for v1**, resolved before any v2 one-click
apply:

1. **`apply_day_setup` cannot delete a DVA row.** Pass A's whole output is "leave these drivers
   off", and the Apply door cannot express it. In v1 the panel *shows* the drivers to leave off
   and the dispatcher unticks them, exactly as today. The optimizer must not pretend otherwise:
   the card reads "untick Marcus and Dee, then Apply", not "Apply".
2. **No held-date branch.** On a held (sandbox) date the builder **refuses to apply and says
   why**. The no-leak invariant is never risked. Detection uses the existing
   `_active_draft_for_date` + `can_use_sandbox`; the refusal names the draft.

---

## 7. Ticket F — Gate 4, the acceptance harness

`analysis/17_build3_gate.py`, same technique as 13/14 (frozen snapshot → migrated throwaway
copy → Django → the production code path → verdicts re-derived from the raw side).

**On 10 replayed dates, at `opt_epsilon_farmouts = 0`, every one of these must hold** (01 Gate 4
/ 04 §5):

| # | Criterion | Threshold |
|---|---|---|
| 1 | In-house coverage vs the **hand-finished board** (state B, per date) | **≥**, never below |
| 2 | In-house coverage vs the **same-date suggest+build baseline** (§1) | **≥**, never below |
| 3 | Hard-infeasible turn pairs | **0** |
| 4 | Driver-days over 15.0 h | **0** |
| 5 | New rest-floor breaches vs the real board | **0** |
| 6 | Every 13.5–15.0 h exception | priced and visible in the payload |
| 7 | Drivers per vehicle-date | **≤ 2** |
| 8 | Handoff bands proposed | no RED |
| 9 | Driver-days used | **≤** the same-date baseline (the point of Pass A) |
| 10 | Wall-clock per date | ≤ `opt_runtime_budget_s`, or `budget_exhausted` flagged |

Criterion 1 is the founder's success test and criterion 9 is "available ≠ required"; a run that
passes 1 but not 9 has not built anything Build 3 was for.

**Also emit, as evidence rather than as a gate:** the per-date delta table (coverage,
driver-days, farm cost, span pressure) so the founder can judge D11 promotion on numbers.

---

## 8. Non-goals for 3b (04 §6, restated so scope cannot creep)

No auto-apply anywhere. No optimizer-initiated `Leg` or `DriverVehicleAssignment` writes. No
driver-facing surface of any kind. No affiliate/vendor selection changes. No capacity planning
(hire/buy — the D8 follow-on). No per-trip what-if simulator (D10). No new periodic daemon. No
GPS reactivation (D3). No changes to pricing, payments, or the sandbox publish flow. No change
to the `INLINE_RESOLVER` billed path, and no worsening of it.

---

## 9. Open founder decisions

None of these block writing code against §2–§7; all of them change what ships.

### 9.1 The co-driver convention — the P2 discrepancy, with numbers

Build 3a was asked to unify the co-driver gate "without changing any verdict — if true
unification would change a verdict anywhere, stop and bring me the discrepancy". It does change
verdicts. What was unified (one home, one overlap predicate, one occupancy construction, one
holders grouping, one ≤2 constant) is verdict-neutral and shipped. What was **not** unified is
the interval convention, and here is the size of it
[measured, `analysis/15_share_gate_divergence.py`, 35 shared unit-days that all really operated,
290 legs]:

| Convention | Where it bites | Rejects a leg of a day that really ran |
|---|---|---:|
| **A** engine — `[pickup − pad, clear + pad]`, overlap only, **hard** | the builder farms the leg | 23 / 290 (7.9%) |
| **B** manual warn — P75 occupancy, overlap + interleave + pickup-pad, **advisory** | an info row, never blocks | 23 / 290 (7.9%) |
| **C** mint — P50 occupancy, overlap + full one-sided separation, **hard** | kills a second-shift proposal | 20 / 290 (6.9%) |

**All three agree on 285 of 290 legs (98.3%).** A and B agree on every one of the 290. The
entire disagreement is 5 legs where C differs: **4 legs C allows that A and B reject, 1 leg C
rejects that A and B allow.**

*Caveat that matters:* A's clear time in this census is the founder's static planning model
(pickup + 45-min dwell + a flat drive), not Django's flight-aware `estimate_job_end_time` — so
"A ≡ B on all 290" is partly an artifact of that approximation and should not be read as proof
they can be merged for free.

**The ruling needed:** unify on one convention, or ratify all three as deliberate layering?
The case for ratifying is that the layering is real and defensible — a hard gate that removes
work from a build, a strict planning gate that protects the mint engine from booking one car in
two places (the +4.0 → +2.4 legs/day correction), and an advisory checker that must never block
a dispatcher. The case for unifying is that 5 legs is a small price for one rule. **Build 3a
did not choose; the three conventions are documented side by side in
`dispatching/car_share.py` with the worked disagreements.**

### 9.2 Three genuine inconsistencies found during 3a recon (each is a real behaviour change, so none was touched)

1. **`shift_advisor` uses a hardcoded 90-minute share buffer** (`ADVISOR_FREED_BUFFER_MIN`)
   while every other copy reads the live 120-minute `vehicle_share_pad_min`. Consequence: it
   can offer a freed unit for a shift the builder will then refuse to fill. Pointing it at the
   setting is a one-line fix that makes **fewer** proposals — your call.
2. **`shift_advisor` does not know the ≤2-drivers rule.** It can propose a third driver on a
   unit that already has two holders; `apply_day_setup` then rejects the accept with a 400. The
   proposal was never buildable.
3. **The engine's share gate silently switches OFF when a co-holder falls outside the caller's
   driver set.** `build_sharer_partners` filters to `driver_ids`; `board_validation.py:420`
   derives that set from **legs**, not the roster — so a rostered co-holder with no legs yet
   makes the gate return `{}` and the car can be double-booked. This one looks like a bug
   rather than a policy choice.

### 9.3 Two pre-existing view behaviours worth a decision (found in recon, not introduced, not changed)

1. **A leg that is both excluded and manually assigned is saved but never shown.** The manual
   merge does not check `excluded_set`, so in apply mode the leg is written while the preview
   strips it — the dispatcher never sees what was saved. A manual assignment naming an
   already-assigned leg also inflates `assigned`, so `remaining` can go negative.
2. **The gap-compaction pass is the only pass that receives no driver window at all** — neither
   `driver_windows` nor `capped_windows`. It re-derives windows internally from saved
   availability, so a modal-typed window is not enforced there. Four spellings of one dict
   across the passes (`driver_windows=`, `capped_windows=`, positional, absent) is the reason
   this went unnoticed; normalising them is a behaviour change and needs its own gate.

### 9.4 One cosmetic change 3a did make, for the record

The evict-pass log line now emits under `dispatching.assignment_pipeline` instead of
`dispatching.views` (the logger takes its module's name). Routing is unaffected; the text of
the log line changes. Flagged rather than worked around, because faking the old module name in
the new file would be worse.

---

## 10. Suggested ticket order

1. **F** (Gate 4 harness) — first, so every later ticket is measurable the day it lands.
2. **A3's prerequisite** (one shared `gate_receiver()`, drift reconciled, byte-identical gate).
3. **C** (surrogate-noise test) — it decides whether A2 ships at all. Do not build the ladder
   before knowing.
4. **A** (objective + Pass A/Pass B), then **B** (epsilon dial), then **D** (async job), then
   **E** (write contract + refusals).

Build 3a's discipline carried forward: **the gate script is written and the baseline captured
BEFORE the code changes**, the snapshot is frozen before any replay arithmetic, and a deviation
from this spec comes back for review rather than being decided mid-build.
