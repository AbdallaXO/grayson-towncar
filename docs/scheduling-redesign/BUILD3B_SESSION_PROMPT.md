# Build 3b kickoff — ready to paste into a fresh session

*(Copy everything below the line into a new chat. This is the last build in the
current project — the optimizer itself, from the approved tickets. D12 still
governs: no rushed build, this is the heart of the operations.)*

---

Continue the Grayson Towncar scheduling redesign — BUILD phase, **Build 3b**.
Branch: `scheduling-redesign/phase-1`.

Build 3a is SHIPPED (commit `cf6ed86a`): the pipeline extraction
(`dispatching/assignment_pipeline.py`, `run_assignment_pipeline(..., dva_rows=None)`,
byte-identical on 10 dates × 4 scenarios) and the one co-driver home
(`dispatching/car_share.py`, three conventions documented side by side,
deliberately not unified). `docs/scheduling-redesign/05_BUILD3B_TICKETS.md` is
**reviewed and approved**, including a founder addendum on how farm-out
selection already works (Ticket A, the note under `farm_cost`) and the §9.1
co-driver ruling. I approve proceeding to Build 3b.

**Since 05 was written, two things changed that you must pick up:**

1. **§9.1 is partially resolved, commit `c335a6be` (2026-08-24).** The engine's
   own car-share gate (`car_share.sharers_conflict`, convention A) now reads a
   **dedicated** `SchedulerSettings.engine_share_pad_min` (default 65), split
   from `vehicle_share_pad_min` (120, still used by the manual warning and the
   mint engine — unchanged). Ground-truthed against real operating history and
   verified against the real Django engine on a migrated throwaway copy before
   shipping. **05 §2 Ticket A's wall table row "Co-driver share conflict" now
   reads this new setting, not `vehicle_share_pad_min` — update any reference
   accordingly.** The layering-vs-unifying question in §9.1 itself is otherwise
   unchanged: still three conventions, documented side by side, not unified.
2. **Two small consistency fixes also shipped, commit `65da0fcd`**: `shift_advisor`'s
   freed-unit buffer now reads the live setting instead of a stale hardcoded
   90-minute constant; `board_validation.revalidate_moves_against_db`'s
   car-share partner scope now derives from the day's roster, not legs alone.
   Neither changes anything Build 3b touches, noted for completeness.

**The goal, in my words, binding:** the overall schedules must come out
BETTER — more efficient (fewest driver-days that cover the day well;
available ≠ required), more stable (0 hard conflicts, plans that survive
flight retimes), same or higher coverage (never below the hand-finished
board), and sustainable (13.5 h days by default, 15 h only as a priced,
visible exception). Coverage is the number that goes up; conflicts and hours
are walls, never trade goods.

**Two decisions that shape how you build this, not just what (D13/D14, 01
§1.1, 2026-08-24):**

- **D13 — do not design around live, day-of changes.** Flight delays, drivers
  falling behind, vehicle outages, mid-day new bookings and cancellations are
  explicitly a LATER tool's job (the "Day Manager"), not this one's. Build 3b
  plans a day *before it starts*, once, well. Anticipating live drift inside
  it is scope creep — don't.
- **D14 — v1 stays propose-only, but do not architect around that assumption.**
  Nothing should make turning on automatic application difficult later, once
  trusted (a founder call, made later, no metric gate). In practice: keep
  routing every write through the real doors (`apply_day_setup`,
  `auto_assign_drivers(apply=True)`) exactly as 05 Ticket E already specifies
  — don't invent a parallel path "for now." Two pre-existing gaps are known
  prerequisites for real automation and are NOT this session's job to fix:
  `auto_assign_drivers`'s apply path re-implementing `set_leg_driver` inline,
  and `apply_day_setup`'s inability to delete a DVA row. Leave both alone.

Before writing any code, read in full, in this order:

1. `docs/scheduling-redesign/05_BUILD3B_TICKETS.md` — the approved spec.
   Governs exactly; deviations come back for review, not judgment calls
   mid-build. §10 is the ticket order — follow it.
2. `docs/scheduling-redesign/05_SIMPLE_READ.md` — a plain-English companion;
   skim it to confirm your read of 05 matches the founder's actual intent,
   especially Section 2's farm-out note.
3. `docs/scheduling-redesign/04_PLANNER_AND_BUILD_PLAN.md` §4 and §1 (global
   rules) and §6 (non-goals — hard walls, re-read before writing anything).
4. `docs/scheduling-redesign/01_REVISED_SCOPE_AND_PLAN.md` — §1, the full
   decision ledger D1–D14, §A3.
5. `docs/scheduling-redesign/03_STANDBY_AND_HANDOFF_MODEL.md` and
   `02_BENCHMARK_AND_EVIDENCE.md` §1 (state B is the scorecard); skim 00 §A6
   and §B3.

Build 2/3a carry-overs you build on (do not duplicate):

* `dispatching/assignment_pipeline.py` — `run_assignment_pipeline(legs,
  drivers, target_date, windows, locked, *, dva_rows=None)`. `dva_rows` is
  the whole mechanism: thread an UNSAVED, hypothetical roster through it to
  evaluate a candidate plan without writing anything.
* `dispatching/car_share.py` — the one home for all three co-driver
  conventions. Convention A (`sharers_conflict`) is what 05's wall table
  means by "co-driver share conflict" — reads `engine_share_pad_min` now.
* `dispatching/standby_mints.py` (pool rule + mint engine) and
  `dispatching/handoff_chain.py` (zone chain, `handoff_band()`, the 13-min
  flight-retime guard) — Pass B's share-cut machinery, per 05 §2 A4.
* `apply_day_setup` — the only roster write door. Never deletes; cannot
  express "leave this driver off" (05 §6 non-goal 1, known, do not fix here).
* The gate technique: `analysis/12`/`13`/`14` — Django on a MIGRATED COPY of
  the snapshot (`RUN_SCHEDULERS_IN_WEB=0`, `ROUTE_DISTANCE_INLINE_RESOLVER=False`),
  raw-sqlite cross-checks via `analysis/_common.py`. Freeze a snapshot copy
  before any replay arithmetic — my dev server writes to the live local DB
  while you work.

Build 3b scope — follow **05 §10's order exactly**, reporting back at each
numbered stop before continuing:

1. **Ticket F first** — the Gate 4 acceptance harness (`analysis/17_build3_gate.py`),
   so every later ticket is measurable the day it lands. Report the harness
   is running (it has nothing to gate yet — that's expected).
2. **A3's prerequisite** — extract one shared `gate_receiver()` from
   `fold_advisor._simulate` / `rebalance_advisor._gate_receiver`, reconciling
   their drift (00 §B3: rebalance carries a seventh `hollow` gate fold
   lacks; their `idle` gates differ semantically). Byte-identical gate on
   replayed dates first. **If reconciling changes a verdict anywhere, stop
   and bring me the discrepancy — do not pick a side.**
3. **Ticket C — the surrogate-noise test.** `analysis/16_surrogate_noise.py`,
   offline, per 05 §4's exact method. **This decides whether Pass A (the
   roster ladder) ships in v1 at all. STOP after this and report the verdict
   before writing Ticket A** — if it fails, Ticket A gets rescoped to
   "optimize pairing and splits at the dispatcher's chosen headcount" and I
   need to know that before you build the fuller version.
4. **Ticket A** (the objective + Pass A/Pass B) — respecting Ticket C's
   verdict — **then B** (epsilon dial) **then D** (async job) **then E**
   (write contract + refusals), in that order.

Acceptance (Build 3b): Ticket F's gate is green on all 10 criteria (05 §7) at
`opt_epsilon_farmouts = 0`; criterion 1 (coverage ≥ the hand-finished board)
and criterion 9 (driver-days ≤ the same-date baseline) both hold — a run that
passes 1 but not 9 has not built what this was for; full `dispatching` suite
green; `opt_enabled` ships `False` — the feature exists but is OFF until I
turn it on.

Standing rules, non-negotiable:

* Everything is additive. No auto-apply anywhere in v1 — Ticket E is
  propose-only, full stop, regardless of D14's forward-looking framing above.
  Nothing driver-facing. Respect every non-goal in 04 §6 and 05 §8.
* No new write doors; no third copy of `set_leg_driver` semantics.
* `content/db.sqlite3` opened read-only for analysis, never written.
* Any frontend/template work: read `docs/claude.md` first; test in a real
  browser on a real date before calling anything done.
* Dispatcher-visible changes ship with a release note per CLAUDE.md
  (`docs/release-notes/README.md` for voice); this build likely qualifies —
  a new "Build a plan" surface in Day Setup.
* Keep me in the loop: report outcomes plainly at each numbered stop above —
  if a gate fails, say so with the actual output, don't paper over it.

STOP when Build 3b is fully gated green, show me what shipped and the Gate-4
evidence table, and wait for my review before considering promotion (D11 —
my call, made later, no metric gate) or touching anything in D13/D14's scope.
