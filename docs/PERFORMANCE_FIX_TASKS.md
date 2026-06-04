# Performance Fix Tasks — Phased Implementation

Companion to `PERFORMANCE_AUDIT_AND_ACTION_PLAN.md`. Finding IDs (e.g. `DISP-03`) reference
that document's Section B. Severities are **post-verification**.

**Decisions baked into this plan (owner-confirmed):**
- **First pass = Phase 1 + Phase 2 only** (instrumentation + safe query wins + the `DEBUG`
  deploy-safety fix). Zero behavior change.
- **Do NOT increase Gunicorn worker count** until the in-process scheduler/background
  sender is a singleton (Phase 5). Single-worker bottleneck is documented, not changed.
- **Background architecture = harden daemon threads, no Celery.**
- **Protect payment / reservation / scheduling correctness above all.** Optimize structure
  and queries only; never change financial or assignment *results*.

Each task: ❑ todo. Mark done and add a row to `PERFORMANCE_FIX_CHANGELOG.md` as you go.

---

## Phase 1 — Instrumentation & Measurement (do first; safe)

- ✅ **Add per-request query-count logging.** Extend `reservations/middleware.py::SlowRequestMiddleware`
  to also log `len(connection.queries)` for slow requests **when `DEBUG` is on** (queries
  are only recorded under DEBUG). Keep the existing 500ms threshold. *(2026-06-04 — DEBUG-gated
  delta around `get_response`.)*
- ✅ **Document the local measurement workflow** — see "Local measurement workflow" in
  `PERFORMANCE_FIX_CHANGELOG.md`.
- ❑ **Baseline the worst pages** (record query count + wall time before any fix): conflict-task
  detail, capacity planner, schedule board, legs dashboard, agency-head dashboard, the three
  heavy admin changelists (Reservation / Driver / Payment), auto-assign apply.
  *(Instrumentation is in place — `PERF TEMP` checkpoints + middleware query count — but capturing
  the numbers requires a running prod-sized dataset; do this on Railway/staging before Phase 4.)*
- ✅ **Confirm the `perf` logger is wired to a handler** — already wired to a `console` handler in
  `settings.py` LOGGING (level INFO).

**Exit criteria:** you have before-numbers for each target page to compare against.

---

## Phase 2 — Safe Quick Wins (low risk, behavior-preserving) — FIRST IMPLEMENTATION PASS

### Deploy-safety
- ✅ **Env-gate `DEBUG`** — already committed in HEAD as `DEBUG = os.environ.get("DJANGO_DEBUG") == "1"`
  (working tree carries a local `DEBUG = True` dev override; leave it). Original spec below.
- ~~❑~~ **Env-gate `DEBUG`** (`business/settings.py:37`): `DEBUG = os.environ.get("DJANGO_DEBUG","False").strip().lower() in ("1","true","yes")`.
  Local devs add `DJANGO_DEBUG=true` to `.env` (`python-dotenv` already loaded). On Railway leave it unset/false.
  - Verify before shipping: `ALLOWED_HOSTS` already lists prod + localhost ✔; `whitenoise`
    serves static with `DEBUG=False` via `CompressedStaticFilesStorage` + `collectstatic` ✔
    (build.sh runs collectstatic); debug-toolbar block stays `if DEBUG`.
  - `CONN_MAX_AGE` — **already set** (`settings.py:151-157`), no change.

### Query / structure wins
- ✅ **DISP-01 / DISP-02** — `capacity_planner` now memoizes `get_effective_availability` per request
  (`_cp_get_eff`); eligible drivers were computed 2× (lines ~8575 & ~8843). `schedule_board` needs
  **none** — its two call sites (drivers-in-timeline vs not-in-timeline) are mutually exclusive, so
  each driver is already computed exactly once.
- ⏸️ **DISP-03 (Critical) — DEFERRED to Phase 4.** `driver` ∈ `Leg._EXPENSIVE_FIELDS`, so the per-leg
  `leg.save()` runs the FULL path (driver-pay auto-fill, cross-leg gratuity smear, revenue_share,
  status reset, profit/commission signals). A naive `bulk_update(['driver',...])` drops all of that →
  drivers unpaid. The "assignments identical" guard is insufficient. See changelog → Deferred.
- ✅ **Admin `list_display` N+1**:
  - ✅ ADMIN-01 `ReservationAdmin.payment_status_display` — now reads the prefetched `payments`
    (was per-row `.exists()` + `.order_by().first()`).
  - ✅ ADMIN-03 / ADMIN-14 — set `LegPaymentAdmin.list_select_related` (the model with `payment`/`leg`
    FKs; the audit's `DriverPaymentAdmin` label pointed at the wrong class — that one was already covered).
  - ✅ ADMIN-04 `FlightAdmin.is_in_use` — already annotated (Exists) in HEAD; no change.
  - ✅ ADMIN-06 `mark_agents_for_payment` — single `aggregate(Sum+Count)`.
  - ✅ ADMIN-07 `preview_driver_payments` — batch-fetch unpaid legs grouped by driver before the loop.
  - ✅ DRIV-11 / DRIV-12 — already off the changelist in HEAD; no change.
- ✅ **`@cached_property` + prefetch** for `Leg.intermediate_stops / all_stops / additional_dropoffs /
  has_intermediate_stops / has_additional_dropoffs`; `legstop_set` now prefetched in
  `get_filtered_legs_queryset` (fixes `legs_list` N+1). (`has_extra_stops` left as-is — out of scope,
  uses `.exists()`.)
- ✅ **RES-01** `Reservation.calculate_total_driver_payments` — single SQL aggregate mirroring the
  `total_driver_pay` property (can't `Sum()` a property directly). **Guard passed:** 483 reservations,
  0 mismatches vs old Python sum.
- ✅ **RES-03** `Reservation.recalculate_leg_revenue_shares` — one `bulk_update(['revenue_share',
  'profit_estimate'])`. **Guard passed:** per-leg values computed identically; verified on real
  multi-leg reservations (rolled-back txn).
- ⏸️ **OPS-06 — DEFERRED to Phase 4.** Lanes are partitioned in Python from related objects + JSON
  metadata and every tab needs an exact count; an aggregate rewrite risks wrong operational counts
  (not behavior-neutral). See changelog → Deferred.
- ✅ **DISP-05 (analytics)** `calculate_route_timing_metrics` — `legs_queryset is None` default now
  bounded to last 90 days. Safe: every in-repo caller passes its own queryset, so metric values (and
  scheduler estimates) are unchanged.
- ✅ **AGENTS-01 / AGENTS-09** — dashboards use the prefetch cache (`len(agent.reservations.all())`)
  and a single grouped count query instead of per-agent `.count()`. **AGENTS-08** already reuses
  `bulk_ready_totals` (affiliate-payments page) in HEAD.
- ⏸️ **BOOKING-01 / RES-10 — DEFERRED to Phase 4.** Reservation has 4 `post_save` receivers
  (incl. an audit-logger that writes per save) + a `pre_save` old-values fetch; cutting saves changes
  non-idempotent firing counts. Needs the signal-idempotency audit + tests. See changelog → Deferred.
- ✅ **(bonus, audit DISP-05 views)** `legs_list` now caches `get_trip_type()` once per leg
  (`_leg_trip_type`) — was called 3× per leg across three loops.

**Exit criteria:** each fixed page shows a measured query-count drop; correctness guards pass;
one changelog row per fix.

---

## Phase 3 — Database Indexes (migrations; justified + reversible)

For each: confirm the column names exist on the model, write a forward+reverse migration, run
`EXPLAIN (ANALYZE, BUFFERS)` before/after on a prod-sized dataset, note rollback = reverse migration.

- ❑ `Payment.stripe_checkout_id` → `db_index=True` (PAY-06; webhook lookup per Stripe event).
- ❑ `Leg(pickup_date)` (+ consider composite `(pickup_date, driver)`) — dispatch boards filter per-day.
- ❑ `Reservation(status, pickup_date)` — dashboards / capacity planner / commission aggregates.
- ❑ `OperationalTask(status)` — task-queue lanes + context-processor count.
- ❑ `RouteTimingMetric(route, ...)` — analytics filters (DISP-05 / critic gap).
- ❑ Lead dedup keys (`last_name`, last-10 phone, `pickup_date`) — `_build_duplicate_cache` / convergence.
- ❑ Verify `TimeClockShift` `idx_tcshift_open` partial index exists in migrations (code references it).

---

## Phase 4 — Heavy Workflow Optimization (needs testing; behavior-preserving)

- ❑ **OPS-02 / OPS-11 (Critical)** conflict-task drive times — request-local cache keyed by
  `(pickup, dropoff)`; prefer historical P75 first, fetch Google async. (≤29 sync calls → ~0 on the request.)
- ❑ **DISP-03 (scheduler) / DISP-05 (swap_optimizer)** — hoist repeated sorting/feasibility out of the
  per-leg loop, memoize per `(schedule, slot)`. **Guard:** assignment output identical.
- ❑ **RES-04 / RES-05** commission + duplicate-lead signals — batch convergence with `bulk_update`;
  only recalc on actual status change.
- ❑ **ADMIN-08 (Critical)** `process_driver_payments` — bulk-fetch legs, `bulk_create` payments.
- ❑ **ADMIN-09** `update_profit_calculations` admin action — batch / defer.
- ❑ **DISP-04 / DISP-07 / DISP-09** dashboard per-leg recompute — prefetch payments, compute trip_type once.

---

## Phase 5 — Background Job Cleanup (harden daemon threads; NO Celery)

- ❑ **Centralize** every fire-and-forget on `reservations/utils.py::_run_in_background()`; delete
  ad-hoc `threading.Thread(...)` spawns (dispatching/views, reservations/signals & admin, payment/views
  & webhook, users/emails, flight_verify_*).
- ❑ **BG-01 (Critical)** `sync_lead_to_ghl_and_send_sms` — remove in-request retry `sleep(60-180s)`;
  let scheduled `retry_failed_syncs()` handle retries.
- ❑ **BG-02 / BG-05** — drop `sleep(1)` between sends; batch via `ThreadPoolExecutor` or scheduled batch.
- ❑ **ADMIN-10 (Critical)** `start_follow_up_sequence_action` — enqueue ONE batch job, not a thread per lead.
- ❑ **Idempotency + locking** — wrap `batch_send_unsent_leads`, `process_follow_up_batch`,
  unpaid-reminders, escalations in a cache/DB lock so concurrent triggers cannot double-send;
  track per-record `sent` / `next_retry_at`.
- ❑ **Move confirmed sync external API calls off the request path** (deferred from Phase 2):
  DRIV-05/06/07, DISP-04/12, PAY-02, OPS-09, DISP-01/08 (flight verify). Use `_run_in_background()`
  with timeouts; never raise in the handler.

**Exit criteria:** no `sleep()`/retry in any request path; no thread-per-row; batches are
lock-guarded and idempotent. **This is the prerequisite for Phase 6 worker scaling.**

---

## Phase 6 — Bigger Architecture (only after Phase 5; owner sign-off)

- ❑ **Scale the web tier** — now that the scheduler is a singleton/locked, set Gunicorn
  `--workers N --threads M` (and revisit `numReplicas`). Validate with `locustfile.py` p95.
- ❑ **Move cache to Redis** — `REDIS_URL` branch already exists in `settings.py`; switching makes
  dedup/idempotency/availability caches shared across workers (required once workers > 1).
- ❑ **Cached summaries / reporting tables** — for the heaviest dashboards, cache computed
  aggregates (TTL) or maintain a small denormalized summary table, once measurement justifies it.
- ❑ (Optional, deferred) Revisit Celery only if daemon-thread volume outgrows the hardened model.

---

## Cross-cutting reusable fix patterns (apply once, fix many)
1. Model `@property` querying a related set per access → `@cached_property` + `prefetch_related`.
2. Sync external API in request cycle → `_run_in_background()` with timeout + logging.
3. Query-in-loop → batch-load before the loop, index by id in a dict.
4. Admin `list_display` N+1 → `get_queryset().select_related(...)` / `list_select_related` / `Prefetch`.
5. Per-row `.save()` in a loop → `bulk_update` / `bulk_create(batch_size=...)`.
6. Raw `threading.Thread()` → centralize on `_run_in_background()`; never thread-per-row.
7. Unbounded `.objects.all()` / no pagination → slice, `values_list`, paginate, or cache w/ TTL.
