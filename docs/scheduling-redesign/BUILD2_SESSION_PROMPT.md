# Ready-to-paste kickoff prompt — Build 2 session

*Paste everything inside the block below as the first message of a fresh chat when
ready to build. Written 2026-08-23, at the Build-1/Build-2 boundary.*

---

Continue the Grayson Towncar scheduling redesign — BUILD phase, **Build 2**.
Branch: `scheduling-redesign/phase-1`.

**Build 1 is SHIPPED and reviewed** (commits `6b96d116` / `d7021325` / `4997449e`,
2026-08-23): warn-only manual-assign validation (turn classes measured 93.6% / 79.2%
precision, gate passed), the signals.py phantom fix, `vehicle_share_pad_min` = 120 on
SchedulerSettings, the one-L 'canceled' exclusion. I approve proceeding to **Build 2**.

Before writing any code, read in full, in this order:
1. `docs/scheduling-redesign/04_PLANNER_AND_BUILD_PLAN.md` — **§3 (Build 2) governs;
   §1 global rules apply.** Follow it exactly. Deviations come back to me for review,
   never judgment calls mid-build.
2. `docs/scheduling-redesign/03_STANDBY_AND_HANDOFF_MODEL.md` — the operational model:
   the standby pool rule (§1), mint rules (§2), the handoff chain with its low/central/
   high components and the green/amber/red rule (§3), the volatility guard (§3.3 — a
   GREEN arrival-anchored handoff must survive the P75 retime, 13 min), hours structure (§4).
3. `docs/scheduling-redesign/01_REVISED_SCOPE_AND_PLAN.md` — scope + decision ledger
   D1–D12.
4. `docs/scheduling-redesign/02_BENCHMARK_AND_EVIDENCE.md` §3–4; skim 00 §A6 (data
   rules) and 00 §B3's `day_setup.py` section (payload contracts: `swaps` strings are
   regex-parsed, `hint` keeps its `N/M` substring, `vehicle_label` stays `"#NNN <type>"`,
   injected DOM must not match `.ds-row`/`.ds-check`/`.ds-veh`; **adding payload keys is
   safe, renaming is not**; the locked-row availability bypass; Apply never deletes).

**Build-1 carry-overs you build on (do not duplicate):**
- `dispatching/handoff_chain.py` exists with the occupancy lead/tail tables (00 §A3.5).
  **Build 2 adds the zone chain matrix and the green/amber/red rule there** — the one
  version-controlled home (04 §1 rule 4). Per-zone components: 03 §3.1 and
  `analysis/out/11_chain_matrix.csv`.
- `dispatching/assign_warnings.py` holds the pure co-driver share cores
  (`share_conflicts`, `build_share_entry`) shared with the replay scripts.
- SchedulerSettings already has `vehicle_share_pad_min` (120) and
  `manual_assign_warnings` (on); the tuning panel renders both (Timing & Buffer →
  Global) and coerces booleans to 1/0.
- `analysis/12_warn_precision.py` documents the replay technique (Django on a MIGRATED
  COPY of the snapshot; purge 09's django stub modules before `django.setup()`; no-op
  `connection.check_constraints` during migrate; `RUN_SCHEDULERS_IN_WEB=0`,
  `ROUTE_DISTANCE_INLINE_RESOLVER=False`).
- The local dev DB was repaired for snapshot pull-skew (placeholder customer 18286,
  two leg flight-links nulled); a fresh prod pull replaces all of it.

**Build 2 scope (04 §3, exactly):**
- **2a** Apply writes `planned_start_hour`/`planned_end_hour` on both DVA rows of a
  shared car (NULL on all 2,591 rows to date — writing the plan down IS the feature);
  server-side hard rule **≤2 drivers per vehicle-date**.
- **2b** Green/amber/red handoff feasibility per shared car from `handoff_chain.py`
  (03 §3.2), amber rendered with its required explicit plan, red shown never suggested;
  arrival-anchored handoffs apply the volatility guard. New payload keys per shared
  unit: `handoff_band`, `handoff_ready_at`, `handoff_reason` — existing keys untouched.
- **2c** Standby mint proposals: the adopted pool rule (03 §1 — active, zero legs, zero
  DVA, no time-off, rest 510 both sides against ACTUAL adjacent boards), **logic
  identical to `analysis/10`, extracted into a shared helper so script and product
  cannot drift**. Soft ≥2-job packing (D6), thin-shift flag ("thin — worth it?"), **no
  daily call-out cap**. Propose-only: nothing written until the dispatcher ticks and
  Applies through the existing door.
- **2d** Per-driver span readout vs 13.5 h; the priced crunch exception ("+1.5 h on
  [driver] keeps 2 legs in-house (≈ $142)"), capped 15.0 h hard, rendered as a choice.
- **2e** Touch only: the suggest payload (new keys), the Apply handler (planned-hours
  write + the ≤2 rule), the share-cut hour (hard-coded 15:00 → SchedulerSettings
  `share_split_hour`, default 16). Leave alone: `peak_concurrency`, `parkable_units`/
  `must_run`, the availability resolver hard gate, unit-affinity, pre-checked-vs-listed,
  locked rows, every existing payload key. No drive-time lookups on unknown routes (the
  zone chain is a static table — the INLINE_RESOLVER billed path must not be worsened).
- New SchedulerSettings fields (all live-editable): `share_split_hour` (16),
  `handoff_gap_green_pct`/`amber_floor` (from 03's central/low), `mint_min_jobs_soft`
  (2), `span_exception_max_hours` (15.0).

**Acceptance (Gate 4 for Build 2, 04 §5):** on 10 replayed dates the panel's proposals
reproduce `analysis/10`'s feasibility verdicts (no proposal 10 would reject);
browser-tested on a real date; release note shipped; planned-hours rows actually
written on a real applied share.

**One open decision from Build 1, confirm with me before shipping Build 2's UI:** the
shared-car warning classes currently all render as passive "info" (strict reading of
the 04 §1 rule-2 precision bar — no 09 truth set exists for them). Propose whether
`share_overlap`/`share_interleave` (physically impossible states) should be promoted
to "warning", with whatever evidence Build 2's replay work adds.

Standing rules, non-negotiable:
- Everything is additive — current dispatcher workflows unchanged except where 04 names
  the change. No auto-apply anywhere. Nothing driver-facing. Respect every non-goal in
  04 §6.
- Dispatcher-visible changes ship with a release note per CLAUDE.md; invisible ones
  commit with `Release-Note: none`.
- Any frontend/template work: read `docs/claude.md` first, and test in a real browser
  on a real date before calling it done.
- `content/db.sqlite3` is opened read-only for analysis, never written; production data
  rules per 00 §A6.
- Keep me in the loop: report outcomes plainly — if a test fails or a gate doesn't
  pass, say so with the output.

**STOP when Build 2 is done and verified, show me what shipped, and wait for my review
before touching Build 3.**
