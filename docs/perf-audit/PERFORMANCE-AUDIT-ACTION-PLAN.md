# Performance Audit — Findings & Action Plan

**Date:** 2026-06-11 · **Status:** read-only exploration DONE, nothing measured/changed yet · **HEAD at time of audit:** `b94266c2`
**Goal:** eliminate site-wide lag for dispatchers/drivers while heavy operations (Auto Assign All) run. Measure first, fix with before/after numbers, prove non-blocking with a load test. **Nothing gets committed or deployed without explicit approval.**

---

## TL;DR

The site lags because **prod runs ONE synchronous gunicorn worker** (`railway.json:10`: `gunicorn business.wsgi --timeout 60`) — any slow request serializes ALL traffic. Auto-assign is CPU-bound pure Python (2–10s typical, **100s+ worst case** from swap searches), so while it runs, every driver poll and dispatcher page waits behind it. Three latent bugs were also found (below). The fix path: gthread workers (one-line railway.json change), a shared cache prerequisite for ≥2 workers, then measured scheduler optimizations.

---

## Bugs found during exploration (before any profiling)

| # | Bug | Where | Impact |
|---|-----|-------|--------|
| **A** | With the `sync` worker class, `--timeout 60` is a heartbeat **SIGKILL** — any request >60s is killed → 502. Long auto-assign runs are likely **already dying in prod**. | `railway.json:10` | Check Railway deploy logs for `[CRITICAL] WORKER TIMEOUT`. Switching to `gthread` fixes this independent of worker count. |
| **B** | Flight-refresh background job stores progress in **cache keys** (`flight_refresh_*`) polled by a status endpoint. With LocMemCache + 2 workers, the poll lands on the wrong worker (W−1)/W of the time → "task not found", broken progress UI. | `dispatching/views.py:4894–5222` | **Shared cache (Redis) is a HARD prerequisite for workers ≥ 2.** `settings.py:176–189` already switches to Redis if `REDIS_URL` is set — unknown whether it's set on Railway. |
| **C** | `locustfile.py:53` targets `/dispatching/legs/` but the real route is `/dispatching/legs-list/` — the legs load-test task has been hitting a **404**. Any past locust numbers for it are invalid. | `locustfile.py:53` vs `dispatching/urls.py:31` | Fix in the scratch copy used for load tests. |

---

## Architecture facts (verified, with file:line)

### Concurrency / deployment
- `railway.json:10` — 1 sync worker, 1 thread, `numReplicas: 1`, `--timeout 60`.
- Cache backend: **Redis if `REDIS_URL` env set, else LocMemCache** (`business/settings.py:176–189`). redis/celery packages installed but Celery is **unconfigured** (no celery.py, no CELERY_* settings — dead weight).
- DB: Postgres on Railway via dj_database_url, `conn_max_age=600`, health checks on. Sessions DB-backed. Static via whitenoise `CompressedStaticFilesStorage` (.br/.gz present). Media on S3.
- **All 3 background pollers are already multi-worker safe** via Postgres advisory locks: Samsara `737202` (3min, `dispatching/samsara_scheduler.py:33–45`), GHL `737201` (30min, runs AeroAPI auto_refresh_flights + batches), wakeup `737203` (60s). Each worker spawns the daemon threads; the lock elects one runner.
- `_run_in_background` (`reservations/utils.py:48`) = daemon thread, ~26 call sites (email/SMS/Meta/GHL). No new duplication risk with multiple workers (one request → one worker), but worker SIGKILL/OOM loses in-flight sends, and the wrapper lacks `close_old_connections()` (hardening follow-up).
- Module-level `_timing_cache`/`_timing_cache_agg` (`dispatching/scheduler.py:267–268`) — per-process; re-preloaded on every auto-assign request so staleness is bounded. Low risk.

### auto_assign_drivers hot path (`dispatching/views.py:9914–10603`)
Pipeline + plausible cost ranking (to be confirmed by profiling):
1. **`suggest_assignments` main loop** (`scheduler.py:~1367–1689`) — O(legs × drivers) `check_feasibility` calls, each 2× `resolve_drive_minutes`. All in-memory after `preload_timing_cache()` (1 query for ~1400 RouteTimingMetric rows) → **CPU-bound, holds the GIL**.
2. **`recover_residuals_via_swaps`** (`scheduler.py:1713–1838`) → `find_swaps` (`swap_optimizer.py:239+`): iterative deepening, **budget = `SchedulerSettings` DB singleton (depth 5 / 5000ms / 5000 iters) PER farmed residual leg**. 20 residuals × 5s = 100s+. Budget is **wall-clock** → preview can be nondeterministic when it binds. Also rebuilds the full board per target.
3. **Advisors** (fold/rebalance/shift) — `deepcopy(proposed_schedules)` per candidate simulation (`fold_advisor.py:169`, `rebalance_advisor.py:211,301`).
4. **Preview deepcopy** (`views.py:10336`) + JSON serialization.
5. **O(legs²) chain precompute** (`scheduler.py:1234–1262`).
6. **Apply mode**: per-leg `Leg.save()` takes the FULL expensive path — `'driver'` ∈ `_EXPENSIVE_FIELDS` (`reservations/models.py:1446–1450`): pay autofill with per-leg sibling query (`models.py:1544–1546`), pay-clear, status reset + LegStatus insert, HistoricalLeg, AuditLog. ~140 saves on a busy day. **`bulk_update` is NOT a drop-in** — it silently skips payroll + audit side effects.
- Total queries in preview: only ~7–10 (legs fetch is well-prefetched). The cost is CPU, not DB.
- **Determinism:** with `USE_LIVE_DISTANCE=False` (default) output is deterministic for fixed date+payload — EXCEPT if the find_swaps wall-clock budget binds. Must verify with 3 identical runs before trusting before/after diffs.
- Existing replay harnesses to copy: `scratch/handoff_sweep_0516.py`, `scratch/sandbox_0613.py` (test Client, login `localtest/Local2026!`, modal payload construction, pristine/restore discipline).
- PERF instrumentation already exists: apply-loop timing (`views.py:10299–10326`), `Leg.save` timing at DEBUG level (`models.py:1452–1465`), SlowRequestMiddleware >500ms (`reservations/middleware.py:30–62`, perf logger → console).

### Views / I/O / static (mostly healthy)
- **Driver board poll** `/drivers/api/board-state/` (60s interval, `drivers/views.py:507`) — lightweight values_list + SHA1 fingerprint. ~3–4 queries. Good design; with N drivers the burst case (all tabs refocus at shift start) only matters on a single-threaded server.
- **Sync external I/O in request paths — only two**, both user-initiated buttons: `refresh_drive_time` (Google, 5s timeout, `drivers/views.py:1361`) and `refresh_flight_data` (AeroAPI, 10s, `dispatching/views.py:3908`). On a 1-worker server each click = site-wide stall up to the timeout. Everything else (Twilio, email, Web Push, GHL, Meta, Samsara) is already backgrounded — Samsara has ZERO request-path API calls (views read denormalized DB snapshots).
- Capacity planner: 60s cache `capacity_planner_{date}` + ~16 `cache.delete()` invalidation sites — with LocMemCache + 2 workers, invalidation misses the other worker (stale ≤60s). Template 5,718 lines; legs_filter 4,897 lines (paginated 20/page).
- **Index "gaps" are probably non-issues**: `dispatch_eta_evaluated_at` is never used in a queryset filter (zero grep hits). `Leg.vehicle_id` is an FK (Django auto-indexes FKs) and its only filter is the daily vehicle-profit report. Verify with `PRAGMA index_list`, expect to DROP both recommendations with evidence.

---

## Action plan

### Phase 0 — Isolated workbench (protects main tree + scrubbed DB)
```powershell
git -C C:\Users\14078\Desktop\grayson-towncar worktree add -b perf-audit-trials C:\Users\14078\Desktop\grayson-perf-audit b94266c2
Copy-Item C:\Users\14078\Desktop\grayson-towncar\content\db.sqlite3 C:\Users\14078\Desktop\grayson-perf-audit\content\db.sqlite3
Copy-Item C:\Users\14078\Desktop\grayson-towncar\.env C:\Users\14078\Desktop\grayson-perf-audit\.env
# use main repo's venv by absolute path (.venv gitignored); then in the worktree:
manage.py check ; manage.py test dispatching      # green baseline (~340 tests)
pip install locust                                 # venv only — NOT requirements.txt
```
Harness scripts live in `grayson-perf-audit\scratch\perf_audit\` (gitignored). Every measured run restores the DB from a pristine snapshot copy. Branch never pushed; each fix saved as `patches\T<n>.patch` via `git diff`, then tree reset.

### Phase 1 — Baseline measurements (before ANY fix)
1. **`perf_harness.py`** (copy plumbing from `sandbox_0613.py`): subcommands
   - `init --date 2026-05-09|2026-05-16` — blank the day's legs (`driver=None` + **null the 4 pay fields** or apply timing is unrepresentative), keep DVAs, freeze the modal payload to `fixtures\payload_<date>.json`, snapshot pristine DB. Also a `tight` fixture (roster minus 2 lowest-utilization drivers) to force residuals so swap/rescue stages actually exercise.
   - `determinism` — 3 stock previews must hash-match. If not (= swap wall-clock budget binding), pin `swap_time_limit_ms` high in BOTH baseline and candidate for identity comparisons; headline timings stay at true stock.
   - `preview` / `apply` ×3 — stage wall-times via **monkeypatched module attributes** (every stage fn is lazily imported inside views.py, so rebinding works with zero file edits): preload, build_driver_schedules, build_smart_schedule, suggest_assignments_clustered, recover_residuals_via_swaps (+ per-call find_swaps), rescue, trim, gap-compact, 3 advisors. `--profile` (cProfile top-40 + `print_callers('deepcopy')`), `--counters` (dup-key rates for check_feasibility / estimate_job_end_time), `--queries` (CaptureQueriesContext — works with DEBUG=0).
   - `gate` — **identity gate**: canonical `core.json` (assigned/remaining/total, per-driver sorted slot triples, sorted unassigned ids, span warnings, trim moves) + `advisor.json`; PASS = byte-identical hashes on both dates. **Apply gate**: canonical leg rows (excl. timestamps) + LegStatus/HistoricalLeg/AuditLog row-count deltas.
2. **`measure_views.py`** per-view sweep (in-process test Client, settings shim → second DB copy): dashboard, legs-list p1/deep, capacity-planner cold/warm, schedule-board, reservations-list, confirmations, `/drivers/`, board-state, weekly-schedule, driver POSTs — on 05-09 + 05-16 (busy) and 06-01 (quiet). Queries mode (DEBUG=0, normalize SQL shapes; **N+1 verdict = >3 same-shape queries differing by pk AND count scales busy-vs-quiet**) + timing mode (`execute_wrapper` splits db_ms vs template+python_ms; DEBUG=1 first-request delta = template parse probe).
3. **External-I/O spy** — patch `Session.request`/smtplib/Twilio transport to raise during every swept request; proves request paths clean. Measure the two refresh buttons by patching the call to `sleep(timeout)` and timing a parallel light request.
4. **`static_audit.py`** — assets actually loaded by dispatcher/driver pages only: encoding, size, Cache-Control, hashed names (DEBUG=0 + whitenoise). Marketing images out of scope.
5. **Load baseline** — `locustfile_perf.py` (copy; **fix legs-list URL**; add `DriverPollUser` ×20 @60s, `HeavyDispatcher` ×1 POSTing auto-assign preview 05-09, `LightDispatcher` ×4). Pre-flight: assert preview makes zero writes (no SQLite lock noise). Runs **A1** (no heavy) / **A2** (heavy) on `runserver --nothreading` (= prod 1-worker proxy; gunicorn doesn't run on Windows).

### Phase 2 — Fix trials (each isolated: measure → gate → `manage.py test dispatching` → save patch → reset)

| # | Trial | Gate | Notes |
|---|-------|------|-------|
| T1 | **find_swaps budget sweep** — mutate the cached `SchedulerSettings` singleton in-harness (zero code/DB change): depth {5,3,2} × time {5000,2000,1000,500}ms on the tight fixture, 3 runs/cell | Coverage frontier, **NOT identity** — this knob legitimately changes behavior | Deliverable = latency-vs-in-house-jobs trade-off table. Admin-tunable already; **founder decides** |
| T2 | **`estimate_job_end_time` memo** keyed on all mutable inputs, cleared inside `preload_timing_cache()`; add `check_feasibility` memo only if counters show ≥30% duplicate keys | Identity | Expected biggest safe win (greedy loop + board rebuilds) |
| T3 | **Chain precompute O(legs²) → binning** (per-category sorted lists + bisect, `scheduler.py:1234–1262`) | Identity + one-shot `old_map == new_map` assert | Only if profile shows ≥5% of wall time |
| T4 | **deepcopy → `clone_schedules()`** shallow-slot clone (`views.py:10336`, `fold_advisor.py:169`, `rebalance_advisor.py:211,301`); ScheduleSlot fields all immutable types — verify sims only mutate lists first | Identity (watch advisor JSON) | |
| T5 | **Apply path**: report per-save cost breakdown; document that `bulk_update`/sibling-prefetch are **UNSAFE** (skip payroll + audit side effects — proven via apply gate); safe sub-trial = one outer `transaction.atomic()` with per-leg savepoints (~140 commits → 1) | Apply gate | SQLite overstates the Postgres commit gain — report the transferable fact (N commits → 1) |
| — | **Index verdicts**: PRAGMA index_list + filter-site inventory + row counts; recommend an index only if (request-path filter) AND (>50k rows) AND (>50ms measured) — otherwise explicitly DROP the recommendation | Evidence table | Both candidates expected dropped |

**Stacked final** (passing patches only) → re-run identity + apply gates → **full `manage.py test`** (green except pre-existing `test_ghl_full` cp1252 + 1 expected skip).

### Phase 3 — Concurrency fix proposal + local proof
- Local runs **B1/B2** (threaded runserver, or `waitress-serve --threads=4` for higher fidelity) vs A1/A2. **Success = light-endpoint p95 in B2 ≤ ~1.5× B1 while heavy runs**; A2 should show light p95 ≈ heavy duration (head-of-line proof).
- **Proposed Railway rollout (each step reversible, founder-gated):**
  1. **Founder checks (no deploy):** is `REDIS_URL` set in Railway Variables? Memory metrics (steady + during an auto-assign). Postgres `SHOW max_connections;`. Grep deploy logs for `WORKER TIMEOUT` (confirms Bug A).
  2. **Deploy A** — `railway.json:10` only:
     `gunicorn business.wsgi --workers 1 --threads 4 --worker-class gthread --timeout 120 --graceful-timeout 90 --keep-alive 5 --access-logfile -`
     Zero cross-process semantics change at W=1; kills the 60s SIGKILL; partial unblocking (GIL: CPU-bound auto-assign still time-slices the other threads — light requests degrade ~2–5× during a heavy run instead of blocking 10–100s).
  3. **Shared cache** — set `REDIS_URL` (Railway Redis plugin; **zero code change**, settings already switch) or fallback DatabaseCache + `createcachetable`. Verify the planner cache tuple pickles cleanly.
  4. **Deploy B** — `--workers 2 --threads 4`: real isolation (heavy run saturates at most one process). Gates: memory <70% of plan limit (est. 350–650MB at W2×T4), flight-refresh progress works across workers, cross-worker planner invalidation works, exactly one lock-holder per poller in logs. **Never `--preload`** (kills the apps.py ready() scheduler threads); **defer `--max-requests`** (recycling kills in-flight `_run_in_background` sends).
- **Background-job evaluation:** keep auto-assign in-request after Deploy B. Convert to a **DB-row-backed job + UI polling** (new `AutoAssignJob` model mirroring the flight-refresh 202+poll pattern but with a DB row, NOT cache — Bug B) only if 2 weeks of perf logs show p95 >60s or Railway-edge timeouts. Celery = overkill (installed but unconfigured; would need a new service + broker).
- The two sync refresh buttons: keep synchronous once W≥2 (user-initiated, user waits for the answer); consider tightening AeroAPI timeout 10s→5s. Backgrounding only if click frequency proves high.

### Phase 4 — Report + teardown
Assemble the final `<performance_audit>` report (exec summary, 5 investigation areas with before/after tables, load-test proof A1/A2/B1/B2, Railway config changes, full change list, risks). Copy `scratch\perf_audit\` results + patches back to the main repo's `scratch\`; `git worktree remove` + delete the `perf-audit-trials` branch. Main tree, scrubbed DB, and git history end untouched.

---

## Verification protocol (applies to every fix)
1. Determinism check passes before any gate is trusted.
2. Per fix: identity/apply gate byte-identical on BOTH dates + dispatching suite green.
3. Stacked candidate: full suite green (modulo the 2 known pre-existing issues).
4. Load test proves light traffic unaffected by heavy runs in condition B.
5. Honest caveats in the report: SQLite query **times** don't transfer to Postgres (query **counts** do; CPU-bound scheduler times roughly do); Windows threaded-runserver is a **proxy** for gthread — final verification is Railway-only; T1 is a behavior trade-off knob, never reported as a free win.

## Founder/user action checklist (needed during execution, not blockers to start)
- [ ] Railway → Variables: is `REDIS_URL` set? (decides shared-cache path)
- [ ] Railway → Metrics: memory at steady state + during an auto-assign run
- [ ] Railway Postgres: `SHOW max_connections;`
- [ ] Railway deploy logs: grep `WORKER TIMEOUT` (confirms Bug A is live)
