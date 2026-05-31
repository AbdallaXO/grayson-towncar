# Phase 3 — Feasibility Guards (Build Summary)

**Branch:** `phase3-feasibility-guards`. **Build target:** local SQLite + synthetic fixtures for
logic; **all data validation read-only against prod-RO** (`readonly_local`, no writes). **No driver
records written.** New code is isolated in `dispatching/feasibility_guards.py`.

> ⚠ **All coverage/cost numbers under stubbed windows are PROVISIONAL** and must be re-measured once
> real per-driver windows are configured in production. Observed history (the stub source) is
> OPTIMISTIC on availability — it captures what a driver *did*, not their true hard limits.

## What was built

### Guard A — physical capacity — **REMOVED (not adopted)**
Initially built, then **removed** at the founder's direction: **booking-time validation already
enforces** party/luggage/car-seat limits against the booked vehicle type, so an assignment-time check
is redundant. Worse, it fired **false positives** off stale per-vehicle seat-count data — e.g. res 8331
was a *correct* 14-pax-van booking flagged only because the assigned `FleetVehicle`'s `rates.Vehicle`
capacity reads 5. `capacity_fit` / `load_all_driver_vehicle_caps` and all `vehicle_cap` wiring were
stripped from `check_feasibility`, both assignment paths, and the swap engine. **The shipped guards are
B (turnaround) + C (window) only.**

### Guard B — context-dependent turnaround (always on)
`check_feasibility` now computes required turnaround via `feasibility_guards.required_turnaround()`:
- airport-drop → airport **arrival** at the **same terminal**: ~0 reposition (deplaning is the slack);
- resort/other → airport arrival: `drive − DEPLANING_GRACE_MIN` (floored at 0);
- anything → non-arrival (incl. **Port Canaveral**): full category drive time;
- **+ `SAFETY_PAD_MIN` on every turnaround — now set to 0** (see "Post-build tuning"; the
  turnaround is therefore real drive time only, like before).
Replaces the old flat `inter_job_buffer` + airport `arrival_grace`. Applied to **both** the preceding
and following slot, so it governs the whole chain — and therefore also the swap optimizer (which calls
`check_feasibility`). `DEPLANING_GRACE_MIN` (default 20) is tunable.

### Guard C — per-driver window (stub-backed, hard)
`feasibility_guards.window_check()` enforces, when `driver_window` is supplied:
- **START**: pickup ≥ start hour (bypassed for `flexible` drivers — flexible on start);
- **END = CLEAR_BY**: the leg must **finish** by `end:00` (a clear exactly at `end:00` is OK).
  Config `END_HOUR_MODE` ∈ {`CLEAR_BY` (default), `LAST_PICKUP`};
- `flexible` drivers are **still bound by clear-by** (`FLEXIBLE_RESPECTS_CLEAR_BY=True`, flippable);
- **max_hours**: hard cap on day span (first pickup → last clear), enforced where set.
Wired into `check_feasibility` + both assignment paths (the single-driver builder previously had only a
soft +1h finish grace; auto-assign had no finish check — now unified).

Windows come from `get_effective_window(driver_id)`. While `USE_STUB_WINDOWS=True`, it returns the
**observed-history STUB** (`STUB_DRIVER_WINDOWS`, from `docs/driver_reality_report.md`, `flexible=False`
so it actually binds in testing). **Swapping in real configured windows is a one-line change**
(`USE_STUB_WINDOWS=False` → reads the live `DriverWeeklySchedule`-derived window the caller passes).

### Flagged config decisions (defaults now, trivially flippable)
| Flag | Default | Meaning |
|---|---|---|
| `END_HOUR_MODE` | `CLEAR_BY` | end_hour = must-finish-by (vs `LAST_PICKUP`) |
| `FLEXIBLE_RESPECTS_CLEAR_BY` | `True` | flexible on start, still bound by clear-by |
| `DEPLANING_GRACE_MIN` | `20` | deplaning slack on airport-arrival pickups |
| `SAFETY_PAD_MIN` | **`0`** | global turnaround pad — **set to 0** (dispatch monitors live); raise to re-add slack. See "Post-build tuning". |
| `USE_STUB_WINDOWS` | `True` | use observed-history stub until real windows configured |
| `USE_LIVE_DISTANCE` | `True` | live Google distance for unknown-location routes (else category table). See "Post-build tuning". |

## Validation

- **26 unit tests** (`dispatching/tests_feasibility_guards.py` + `tests_swap_guards.py`) — all pass
  (Guard B turnaround rules, Guard C window incl. clear-by edges / flexible / after-midnight / max-hours,
  swap-path window block, execute_swap abort). Capacity tests removed with Guard A.
- **Read-only prod-RO validation** over the 5 Phase-2 days (`scratch/phase3_coverage_recheck.py`):
  - **Guard C (stub + CLEAR_BY)** flags a handful of real-board clears past window (mostly post-23:00;
    `mesfin`'s 19:00 stub catches 21:03/23:07; H3 fix catches after-midnight clears). Low volume because
    stub end hours are loose (expected).
  - **Coverage effect (PROVISIONAL)** — guarded auto-build (B+C, no capacity) vs Phase-2 no-guard:

    | Day | No-guard | Guarded (B+C) | Δ | (was, with Guard A) |
    |---|---|---|---|---|
    | 04-23 | 83 | 73 | −10 | 72 |
    | 05-09 | 95 | 82 | −13 | 82 |
    | 03-29 | 88 | 77 | −11 | 77 |
    | 05-21 | 83 | 74 | −9 | 73 |
    | 05-02 | 101 | 91 | −10 | 91 |

    Removing Guard A added back only **0–1 legs/day** (+2 total) — it was near-zero-impact in the build.
    The −9…−13/day is almost entirely **Guard B** (the +10 turnaround pad rejecting over-packed chains)
    plus a few **Guard C** window clears. The −coverage shrinks once real (likely wider/earlier) windows
    replace the loose stub. **PROVISIONAL.**

## Adversarial review — findings & fixes

A 4-reviewer adversarial review of the diff ran (correctness / regression / spec). Verdict: **core
guard math is correct and the read-only/no-write invariant holds; keep the diff**, but gate unattended
operation and the production cutover on the HIGH items. Status:

| # | Severity | Finding | Status |
|---|---|---|---|
| H2 | HIGH | `get_effective_window()` called with no `configured=` → flipping `USE_STUB_WINDOWS=False` would silently **disable** Guard C instead of switching to real windows. | **FIXED** — both paths now build & pass a `configured` window (from `driver_hours`/`max_hours`/`flexible` in auto-assign; from `start_hour`/`end_hour` in the builder). Test `test_stub_false_uses_configured`. |
| H3 | HIGH | Clear-by failed **open** for after-midnight clears (`window_check` compared bare `.hour`, so a 00:30 next-day clear evaded a 23:00 clear-by). *(`estimate_job_end_time` itself rolls the date correctly — reviewer overstated that part.)* | **FIXED** — `window_check` takes `target_date` and compares an absolute clear-by datetime. Test `test_clear_after_midnight_fails`; validated catching real 00:44/01:03/00:52/01:10 clears. |
| H1 | HIGH | **Swap optimizer** (`find_swaps`) + `execute_swap` enforced Guard B (turnaround) but **not** the per-driver window, and `execute_swap` persisted with no re-validation. | **FIXED.** `find_swaps` now builds per-driver windows once and passes `driver_window` into **every** `check_feasibility` (direct placement, displacement via `_get_conflicting_slots`, and the diagnostic), so no swap that creates an out-of-window placement is produced. `execute_swap` now re-runs full feasibility (B+C) on the resulting board (`_revalidate_swap_feasibility`) **inside the transaction** and rolls back (409, writes nothing) if any touched leg would be infeasible. Tests `tests_swap_guards.py`. *(Originally also wired Guard A capacity; removed — see "Guard A removed" above.)* |
| M1 | MED | UI "why" text (`_recalculate_timing_details` / `_capture_timing_details`) still uses the old flat buffer, so displayed spare-minutes diverge from the new decision. | Deferred — display-only; route through `required_turnaround` before rollout. |
| M2 | MED | Manual drag-drop endpoint (`check_driver_feasibility`) applies Guard B but not A/C → disagrees with auto-assign. | Deferred — pass caps/window there (or comment as Guard-B-only) before rollout. |
| L1–L5 | LOW | per-driver cap loader efficiency; loose stub `max_hours`; per-tier (not per-VIN) capacity wording; same-terminal drive computed-then-discarded; future-caller cap hardening. | Noted; no correctness impact. |

**26 unit tests pass** after the fixes (22 guard-logic + 4 swap-guard/abort; capacity tests removed with Guard A).

## H1 closed — swaps now enforce all guards (read-only validation)

The swap optimizer is the component being automated to replace manual Pass 2, so it must enforce the
guards, not just turnaround:
- **`find_swaps` (Guard C window):** per-driver windows built once and threaded into every
  `check_feasibility` call (direct, displacement, diagnostic). Validated read-only on prod (05-02,
  capacity removed), recoverable swaps over the State-A farmed legs:
  - Phase-2 unguarded (old flat turnaround) = **22**
  - **B-only** (new context turnaround) = **14** → the +10 turnaround pad rejects 8 of the old swaps
  - **B+C** (add the window) = **13** → **window (Guard C) alone rejects just 1 more**
  - i.e. the earlier suspect "9" decomposes to ~8 turnaround + 1 window; **capacity contributed 0**
    independent swap rejections (removing it left 05-02 at 13). 0 errors.
- **`execute_swap` (re-validation):** before persisting, `_revalidate_swap_feasibility` rebuilds the
  resulting board and re-runs B+C on every leg each receiving driver would hold; any infeasibility
  raises inside `transaction.atomic()` → **409, nothing written**. Tested both the abort path and the
  feasible path proceeding to the save loop.

## Scope notes / follow-ups (not done)
- M1 (UI "why" text uses old buffer) and M2 (drag-drop endpoint Guard-B-only) — fix before rollout.
- `placeholder` (driver id 6) should be deactivated; the 9 OFF-all-week "active" drivers and the
  pre-6 AM start hours need real configuration (see `driver_reality_report.md`) before trusting numbers.
- Real per-driver windows must replace the stub (`USE_STUB_WINDOWS=False`) and the provisional
  coverage/margin numbers re-measured.

## Post-build tuning (founder direction, this session — uncommitted)

**1. Turnaround safety pad → 0 (`SAFETY_PAD_MIN = 0`).** Dispatch monitors/adjusts jobs live, so
the engine should allow tight back-to-back chains rather than reject them. With the pad at 0 the
turnaround reverts to **real drive time only** (it still *warns* on <15-min turns and still hard-rejects
true overlaps). Net: the only behavioral guard that now *reduces* coverage is the per-driver window
(Guard C) — small (the 05-02 swap test showed the window alone rejects ~1). **The −9…−13/day coverage
table above reflected the now-removed +10 pad and no longer applies; turnaround behaves like before.**

**2. Live drive times for unknown routes (`USE_LIVE_DISTANCE = True`).** The category table only knows
Orlando landmarks and guessed ~35 min for anything else — so far rides (Tampa, Fort Lauderdale) and odd
residential addresses were badly wrong. New `scheduler.resolve_drive_minutes()` routes through the
**existing** Google Distance Matrix helper (`drivers/utils.get_drive_time`, traffic-aware, **2h-cached**,
fails safe) on the **raw addresses** whenever an endpoint is an unrecognized bucket
(`LIVE_DISTANCE_UNKNOWN_CATS = {Other, Residential, Other Hotel}`); known landmarks keep the instant
table estimate. Wired into every per-route drive computation (`estimate_job_end_time`, both
`check_feasibility` repositions, `get_clearing_breakdown`, the timing-detail displays). Read-only
prod spot-check (8 real far-city legs):

| Real ride | Old table | **Live** |
|---|---|---|
| Orlando resort → Tampa cruise port | 35 | **73** |
| Tampa arena → Universal | 35 | **89** |
| Disney → Fort Lauderdale (Port Everglades) | 38 | **186** |
| Disney → Fort Lauderdale Marriott | 29 | **195** |
| Lakeland airport ↔ Disney | ~40 | ~48–50 |

5 new unit tests (`dispatching/tests_drive_time.py`); **32 tests total pass.** *(Note: the Maps helper
uses `departure_time=now` traffic; for far rides distance dominates so this is a large net improvement —
scheduling against the leg's own departure time is a possible future refinement.)*

*No writes to production. Window numbers provisional under stub windows — re-measure after real windows are set.*
