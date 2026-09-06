# Build 3 kickoff — ready to paste into a fresh session

*(Copy everything below the line into a new chat. Build 3 is split in two on
purpose: 3a = the two prerequisites + the detailed tickets, then a hard stop;
3b = the optimizer itself, in its own session, from the approved tickets.
D12: no rushed build — this is the heart of the operations.)*

---

Continue the Grayson Towncar scheduling redesign — BUILD phase, **Build 3a**.
Branch: `scheduling-redesign/phase-1`.

Build 2 is SHIPPED and reviewed (commits `1afe33b0` / `6bc26122` / `50b1ee0f` /
`38cf0d17`, 2026-08-23): planned hours written on both rows of every applied
share + the ≤2-drivers-per-vehicle-date rule, green/amber/red handoff bands
from the zone chain, standby mint proposals from the shared engine (Gate 4:
10 replayed dates, 26 proposals, 0 rejected by analysis/10), span readouts +
the priced crunch exception, five live SchedulerSettings knobs. I approve
proceeding to Build 3a.

**The goal, in my words, binding:** the overall schedules must come out
BETTER — more efficient (fewest driver-days that cover the day well;
available ≠ required), more stable (0 hard conflicts, plans that survive
flight retimes), same or higher coverage (never below the hand-finished
board), and sustainable (13.5 h days by default, 15 h only as a priced,
visible exception). That is 01 §1's constrained maximisation — coverage is
the number that goes up; conflicts and hours are walls, never trade goods.

Before writing any code, read in full, in this order:

1. `docs/scheduling-redesign/04_PLANNER_AND_BUILD_PLAN.md` — §4 (Build 3)
   governs; §1 global rules apply; §6 non-goals bind. Deviations come back to
   me for review, never judgment calls mid-build.
2. `docs/scheduling-redesign/01_REVISED_SCOPE_AND_PLAN.md` — §1 (what the
   product is + the success test), §1.1 decision ledger D1–D12, §A3 (the
   Candidate-Plan Outer Loop architecture and why the pipeline extraction and
   the co-driver gate are hard prerequisites).
3. `docs/scheduling-redesign/03_STANDBY_AND_HANDOFF_MODEL.md` — the model the
   candidate evaluator must carry (pool §1, mints §2, chain + bands §3,
   volatility guard §3.3, hours §4).
4. `docs/scheduling-redesign/02_BENCHMARK_AND_EVIDENCE.md` §1 — state B is
   the scorecard every proposed plan is measured against; skim 00 §A6 (data
   rules) and 00 §B3 (`day_setup.py` payload contracts; the three background
   loops — the async-job home; the INLINE_RESOLVER billed path, still not to
   be worsened).

Build-2 carry-overs you build on (do not duplicate):

* `dispatching/standby_mints.py` — the pool rule + per-day mint engine,
  verified byte-identical with `analysis/10`. `dispatching/handoff_chain.py`
  — zone chain, `handoff_band()`, the 13-min flight-retime guard. These are
  the one version-controlled homes (04 §1 rule 4).
* `apply_day_setup` is the ONLY roster write door: planned hours on both rows
  of a share, ≤2 per vehicle-date, holder cross-checks, drift 409, never
  deletes. The builder's Apply routes through it and
  `auto_assign_drivers(apply=True)` exactly as a human's plan would.
* SchedulerSettings knobs (all live): `vehicle_share_pad_min` (120),
  `share_split_hour` (16), `handoff_gap_green_pct`/`handoff_gap_amber_floor_pct`
  (100/100), `mint_min_jobs_soft` (2), `span_exception_max_hours` (15.0).
* The gate technique: `analysis/12`/`analysis/13` — Django on a MIGRATED COPY
  of the snapshot (`RUN_SCHEDULERS_IN_WEB=0`, `ROUTE_DISTANCE_INLINE_RESOLVER=False`),
  raw-sqlite cross-checks via `analysis/_common.py`; set `GRAYSON_SNAPSHOT_DB`
  to a frozen copy — my dev server writes to the live local DB while you work
  and will silently move the derived horizon (it happened in Build 2).
* Suggest-payload contracts: adding keys is safe, renaming is not; injected
  DOM never matches `.ds-row`/`.ds-check`/`.ds-veh` (Build 2 used a `dsx-`
  namespace); mint proposals attach to REAL DVA rows only, on built days only.

Build 3a scope (exactly this, nothing more):

* **P1 — extract the pipeline.** The five-pass build lives inside
  `auto_assign_drivers` (`dispatching/views.py`, ~line 12173 — RE-VERIFY every
  anchor by grep, lines have drifted since 04 was written; six
  unparameterised `build_driver_schedules` call sites in `scheduler.py`,
  ~ten view-level ones — re-enumerate them first). Extract
  `run_assignment_pipeline(legs, drivers, target_date, windows, locked, *,
  dva_rows=None)` returning `(assignments, warnings, moves)`;
  `auto_assign_drivers` becomes a caller. **Gate: byte-identical output on 10
  replayed dates before anything builds on it** — same evenly-spaced-dates
  technique as `analysis/13`; the comparison script is committed evidence.
* **P2 — ONE co-driver share gate.** Today the rule lives in ~three places:
  the engine's shared-car window/pad logic in `scheduler.py`,
  `assign_warnings.share_conflicts` (Build 1a), and
  `standby_mints.car_share_ok` (Build 2c). Unify on one function with one
  home, called by the engine, the warnings, and the replay scripts — without
  changing any verdict: the 12/13 gates and the full test suite must come out
  unchanged. If true unification would change a verdict anywhere, stop and
  bring me the discrepancy instead of picking a winner.
* **Tickets for 3b.** With P1/P2 landed, write
  `docs/scheduling-redesign/05_BUILD3B_TICKETS.md`: the Pass-A/Pass-B
  objective (driver-days minimized subject to farm-outs ≤ the suggest+build
  baseline; then farm cost, then span/fairness/handoff-risk quality terms),
  the epsilon dial surfaced to the dispatcher, the surrogate-noise test that
  decides whether the roster ladder ships in v1, the async-job runtime plan
  (existing background pattern, computed-at timestamp, never in the request
  cycle), the propose-only write contract with the two known door gaps as
  explicit non-goals (Apply cannot delete; held dates refuse with a reason),
  and the Gate-4 acceptance harness. Every threshold named, every config
  value given a home. **No optimizer code in this session.**

Acceptance (Build 3a): P1 byte-identical gate green on 10 dates; P2 unified
with zero verdict changes (12/13 re-run + full `dispatching` suite green);
tickets doc complete; `Release-Note: none` throughout (nothing here is
dispatcher-visible).

Standing rules, non-negotiable:

* Everything is additive — current dispatcher workflows unchanged. No
  auto-apply anywhere. Nothing driver-facing. Respect every non-goal in
  04 §6. No new write doors; no third copy of `set_leg_driver` semantics —
  the front door only.
* `content/db.sqlite3` is opened read-only for analysis, never written;
  production data rules per 00 §A6. I work in this same tree with a live dev
  server — freeze a snapshot copy before any replay arithmetic.
* Any frontend/template work: read `docs/claude.md` first (there should be
  none in 3a).
* Keep me in the loop: report outcomes plainly — if a gate fails, say so with
  the output.

STOP when Build 3a is done and verified, show me what shipped, and wait for
my review of the tickets before any 3b session starts.

---

*Held over from Build 2, confirm whenever ready (none of it blocks 3a):
(1) push/deploy of Build 2 — it is committed locally, not pushed, and 04 §4
wants Build 2 "live and reviewed" before 3b ships; (2) promote
`share_overlap`/`share_interleave` to "warning" (recommended; evidence: 2
fires on 32 real shared unit-days, both genuine anomalies; the 32 executed
handoffs re-band 23 green / 7 amber / 2 red); (3) the seven in-spec
interpretation calls listed in the Build-2 report.*
