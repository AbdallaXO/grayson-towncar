# Scheduler Automation — Resume / Status

**Last updated:** 2026-05-31. Read this first when resuming. Deeper detail is in the auto-memory
(`~/.claude/.../memory/project_scheduler_*.md`) and `docs/scheduler-automation/`.

---

## THE GOAL

One-button **auto-assign-all** that builds in-house driver schedules as well as (or better than) the
founder does by hand — so he stops building each driver's day one-by-one. Founder's objective, in
priority order (memory: `project_scheduler_objective`):

1. **Max in-house jobs** (coverage/revenue; farming is expensive: $70–230 vs in-house $25–50)
2. **Min empty deadhead** (build paid round-trips)
3. **Min gaps**

- Farming: shed **out-of-pattern** legs first (far from the Orlando hub = high opportunity cost); prefer
  **keeping returns / farming arrivals**, UNLESS the arrival needs a scarcer vehicle (van) than the return.
- **Match a driver to his own vehicle** (van driver → van jobs). On slow days, **use fewer drivers/cars**.
- "We are never late." Tight turns that work in reality must NOT be farmed as "impossible."

## SAFE TEST ENV (no prod risk)

Local SQLite = a **scrubbed copy of prod** at `content/db.sqlite3` (customers anonymized; driver names
real). `USE_PROD_RO` was removed from settings.py — local can't reach prod. Run plain `manage.py` (offline).

- **Local server:** `DJANGO_DEBUG=1 .venv/Scripts/python.exe manage.py runserver 127.0.0.1:8000 --noreload`
  → login `http://127.0.0.1:8000/users/login/` user **`localtest`** / pass **`Local2026!`** (local-only admin).
- Runs with `USE_LIVE_DISTANCE=True`. Test on dates with data (2026-05-31, 06-01, 05-09, 05-16).
- Analysis harnesses in `scratch/` (gitignored): `bench_quality.py` (human vs engine board), `audit_0601*.py`,
  `swap_pass_0601.py`, `check_examples*.py`, `repro_yovanny.py`, `test_swap_fn.py`.

---

## ✅ DONE THIS ARC — COMMITTED + MERGED TO MAIN + PUSHED (2026-05-31)

Merged to `main` as `c6e60767` (arc commit `d186179a` on top of guards `90ea341b`) and **pushed to
origin → Railway auto-deploys**, so this is now LIVE on prod. Merged cleanly alongside main's
inhouse-schedule editor / flex-label fixes (cbb6ee95, 3eba1d86, a417d43a, 4a2cef31) — full suite 169
pass on the merged tree, system check clean. Branch `phase3-feasibility-guards` is folded in.

**PROD FOLLOW-UP (cannot run locally — needs prod DB + LegStatus history):** run
`update_all_route_timing_metrics` on prod to recompute RouteTimingMetric under the corrected
single-source helpers (airport detector, flight anchor, flight-aware buckets, contributing-only
sample_count). Until then, route-timing reads use the old (pre-fix) cached metrics.

**A. Feasibility calibrated to reality** (`feasibility_guards.py` + `scheduler.py`):

- Deplaning grace 20→**15**; `required_turnaround` credits the FULL window even on short hops (no floor) →
  a driver already at MCO grabbing a deplaning arrival is no longer "impossible." `SAFETY_PAD_MIN=0`.
- **Intra-resort reposition** (`resolve_drive_minutes`): same location → 0; same-cluster hops
  (`INTRA_CLUSTER_LIVE_CATS` = Disney/Universal/Port) use **live road distance** not the blanket 20-min
  category average. Far/odd routes already use live distance (`LIVE_DISTANCE_UNKNOWN_CATS`).
- **Displayed buffer now matches the engine** (`_recalculate_timing_details`/`_capture_timing_details` use
  the same context-aware turnaround) — no more false "−3 min late" on feasible airport pickups.

**B. Flexible drivers + pins** (founder's "flag but do it"):

- `FLEXIBLE_RESPECTS_CLEAR_BY=False` — flexible drivers work AND finish anytime.
- `build_smart_schedule` obeys the dispatcher's From/Until + reads real flexibility; **pinned legs are
  advisory** (only a physical overlap drops them; window issues warn+keep).
- `get_effective_window` now **honors the real flexible flag even under the stub** (was hardcoded False) →
  flexible drivers usable for late jobs in auto-assign + swaps (fixed the 10:24 PM van being farmed).

**C. Auto pre-farm swap pass** (founder's idea):

- `scheduler.recover_residuals_via_swaps()` (flag `AUTO_PREFARM_SWAP_PASS=True`), wired into
  `auto_assign_drivers`: after the greedy build, each would-be-farmed leg is run through `find_swaps` to
  recover it in-house via cascade; manual assignments locked; guard-safe.

**D. Auto-assign modal is AUTHORITATIVE + per-driver Flexible toggle** (`views.py:auto_assign_drivers`
+ `daily_capacity_planner.html`):

- Fixed "Off doesn't work / puts all drivers" — the modal now drives availability: only drivers it
  lists work; ones marked Off (omitted from the payload) are EXCLUDED from the candidate pool.
- New **"Flexible" checkbox** column per driver. Unchecked → the Start/End you set are a **HARD window**
  (enforced). Checked → driver works **anytime** (window bypassed). Fixes "it ignores my times."
- New **"Build 1st" checkbox** column — marked drivers get their FULL day built first (seeded via
  `build_smart_schedule`, narrowest-window first) before the general assign, then everyone fills around
  them; seeded legs LOCKED from swap/gap passes. Fixes fixed-driver starvation (06-03: Yovanny 3→**7**
  legs, coverage unchanged 73/76). Payload `build_first:[ids]`; sent on both preview + apply.
  - The general assigner uses a SEPARATE `assign_board` (with seeded occupancy); `schedules` stays the
    pre-existing board so the preview (`proposed=deepcopy(schedules)` + final_assignments) doesn't render
    seeded legs twice (fixed the "15 legs for Yovanny" duplication). Validated via Django test Client: 0 dups.
- **Header UI**: the auto-assign preview now shows each driver's **vehicle (type · number)** + **on-duty
  hours** (first pickup→last clear), e.g. "Aftab — SUV · 009 — 8:00 AM–6:08 PM · 10h 8m"
  (`views.py` adds `vehicle`/`hours`; `daily_capacity_planner.html` renderAutoAssignPreview renders them).
- NOT yet: flexible-start STAGGERING by prior-night rest (a flexible driver still works fully anytime;
  interim control = leave them non-flexible + set a hard Start, or use Build 1st). See NEXT #4.

**E. Auto gap-compaction relocation pass** (founder's "give David the 6:15 Roberto holds; Roberto
just starts later" move — `scheduler.compact_gaps_via_relocation`, flag `AUTO_GAP_COMPACT_PASS=True`,
wired into `auto_assign_drivers` AFTER the pre-farm swap pass):

- After coverage is settled, relocate an ALREADY-COVERED leg from a donor to a driver with a big
  internal hole. Coverage preserved (a leg only changes driver, never farmed); manual/pinned locked;
  read-only (in-memory map); deterministic. "Never late" preserved (every insert re-runs `check_feasibility`).
- Accept rule = founder-calibrated: move L (donor D→receiver R) iff R can feasibly insert it AND
  `receiver_gap_healed − donor_gap_opened ≥ GAP_COMPACT_MIN_NET_GAIN` (a first/last job opens 0 donor
  gap → D just starts later/finishes earlier; a middle job only if the hole it opens on D < the hole it
  heals on R). Deadhead is NOT a gate (founder: "fill the hole, any deadhead") — only a tiebreak.
- Founder calibration (2026-06-02 review): **fill each driver's single BIGGEST hole only, then leave him
  alone**; **never strip a light donor** (`≤ GAP_COMPACT_PROTECT_DONOR_MAX_JOBS=3` jobs — protects the
  "give Steven more" intent); **prefer a tier-matched receiver** (use the scarce 14-pax van for a small
  job only when no smaller-vehicle driver has a hole for it). Each driver receives ≤1 relocation/run.

**Tried + REJECTED:** best-fit optional-fill (`BUILDER_BEST_FIT`, default **False**) — A/B showed it builds
deadhead-heavy days. Builder job-picking order is unchanged.

**VALIDATION (real boards):**

- **06-01 (slow): 65 → 67/67 in-house**, 0 impossible turns (10:24 van assigns directly via flex fix; 9:30
  recovered by swap pass).
- **05-09 (busy, live distance):** phantom "impossible" turns **18→6**, in-house coverage **90→96** (founder
  board 97), deadhead/leg **21.8→17.8**. No regression; busy days never capped. **32 guard tests pass.**
- **Gap-compaction (E), offline:** **06-02** reproduces the founder's exact move on its own — 1 relocation
  (Yovanny's 06:15 → David), David's 271m hole → 136m, Steven untouched. **06-01** 3 moves, **05-09** 2 moves;
  all dates: coverage preserved, **0 impossible turns**, idle + big-gap counts drop. **10 new gap tests + 32
  guard + drive-time/route-timing = 53 pass.** Harness: `scratch/gap_compact_0602.py` (env `DATE=`, `LIVE=1`).

---

## ▶ NEXT (recommended order)

3. **Consolidation** (fewer drivers/cars on slow days). Designed + adversarially reviewed (workflow
   `autoassign-consolidation-flex`). LOW-YIELD + safe (06-01 genuinely needs ~12-13 cars). Before building,
   apply review fixes: make slow-day first-job suppression **per-leg-feasibility-aware**; write missing
   helpers `make_slot`/`_count_spare_by_tier`. Flags designed: `consolidate_*`, `pack_bonus`, `fold_*`.
4. **Flexible-start staggering** (start late-finishers late by prior-night rest). Designed, but the review
   found the early-coverage guard is **vehicle-blind** (could strand an early _van_ pickup) — must be made
   vehicle-tier-aware; keep soft mode. Needs **prod data** (local 05-31 is unbuilt) for real hours.
5. **Full real windows:** eventually flip `USE_STUB_WINDOWS=False` AFTER cleaning driver data (deactivate
   `placeholder` id 6, fix ~9 OFF-marked-but-working drivers + pre-6AM starts), then re-benchmark.
6. ~~Review + commit this whole uncommitted arc, then deploy.~~ ✅ DONE 2026-05-31 (merged to
   `main` c6e60767, pushed → Railway). Remaining: run `update_all_route_timing_metrics` on prod.

## TUNABLE FLAGS

`scheduler.py`: `AUTO_PREFARM_SWAP_PASS=True`, `AUTO_GAP_COMPACT_PASS=True` (+ `GAP_COMPACT_MIN_GAP=120`,
`GAP_COMPACT_MIN_NET_GAIN=60`, `GAP_COMPACT_PROTECT_DONOR_MAX_JOBS=3`, `GAP_COMPACT_MAX_MOVES=25`),
`BUILDER_BEST_FIT=False`, `USE_LIVE_DISTANCE=True`.
`feasibility_guards.py`: `DEPLANING_GRACE_MIN=15`, `SAFETY_PAD_MIN=0`, `FLEXIBLE_RESPECTS_CLEAR_BY=False`,
`USE_STUB_WINDOWS=True` (start/end from observed-history stub; real flexible flag now honored).

## KEY FILES

- `dispatching/scheduler.py` — suggest_assignments_clustered, build_smart_schedule, check_feasibility,
  resolve_drive_minutes, recover_residuals_via_swaps, compact_gaps_via_relocation (+ `_max_internal_gap_minutes`).
- `dispatching/feasibility_guards.py` — required_turnaround, window_check, get_effective_window, config flags.
- `dispatching/swap_optimizer.py` — find_swaps. `dispatching/views.py:auto_assign_drivers (~8501)`,
  manual builder (`~9128`). `drivers/availability.py:resolve_effective_availability` (canonical windows).
- Analysis docs: `docs/scheduler-automation/`.
