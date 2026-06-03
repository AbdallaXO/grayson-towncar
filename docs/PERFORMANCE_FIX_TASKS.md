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

- ❑ **Add per-request query-count logging.** Extend `reservations/middleware.py::SlowRequestMiddleware`
  to also log `len(connection.queries)` for slow requests **when `DEBUG` is on** (queries
  are only recorded under DEBUG). Keep the existing 500ms threshold.
- ❑ **Document the local measurement workflow** in the changelog/README: run with
  `ENABLE_DEBUG_TOOLBAR=1` for Debug Toolbar, or use a `CaptureQueriesContext(connection)`
  snippet in a shell/test to count queries for a specific view.
- ❑ **Baseline the worst pages** (record query count + wall time before any fix): conflict-task
  detail, capacity planner, schedule board, legs dashboard, agency-head dashboard, the three
  heavy admin changelists (Reservation / Driver / Payment), auto-assign apply.
- ❑ **Confirm the `perf` logger is wired to a handler** in production logging config so `SLOW`
  lines are actually captured on Railway.

**Exit criteria:** you have before-numbers for each target page to compare against.

---

## Phase 2 — Safe Quick Wins (low risk, behavior-preserving) — FIRST IMPLEMENTATION PASS

### Deploy-safety
- ❑ **Env-gate `DEBUG`** (`business/settings.py:37`): `DEBUG = os.environ.get("DJANGO_DEBUG","False").strip().lower() in ("1","true","yes")`.
  Local devs add `DJANGO_DEBUG=true` to `.env` (`python-dotenv` already loaded). On Railway leave it unset/false.
  - Verify before shipping: `ALLOWED_HOSTS` already lists prod + localhost ✔; `whitenoise`
    serves static with `DEBUG=False` via `CompressedStaticFilesStorage` + `collectstatic` ✔
    (build.sh runs collectstatic); debug-toolbar block stays `if DEBUG`.
  - `CONN_MAX_AGE` — **already set** (`settings.py:151-157`), no change.

### Query / structure wins
- ❑ **DISP-01 / DISP-02** — memoize `get_effective_availability(selected_date)` into a
  `{driver_id: eff}` dict computed once per request in `capacity_planner` and `schedule_board`
  (drivers are already `prefetch_related`'d, so this removes redundant *computation*, not queries).
- ❑ **DISP-03 (Critical)** — `auto_assign_drivers` apply loop (`dispatching/views.py:9959-9972`):
  collect modified legs, then `Leg.objects.bulk_update(legs, ["driver","driver_assigned_by","driver_assigned_at"], batch_size=500)`.
  **Correctness guard:** assignments must be identical to the pre-change run on the same date.
- ❑ **Admin `list_display` N+1** (all Low-risk):
  - ADMIN-01 `ReservationAdmin.payment_status_display` — annotate latest payment in `get_queryset`.
  - ADMIN-03 / ADMIN-14 `DriverPaymentAdmin` — `list_select_related = ('payment__driver','leg','leg__reservation')`.
  - ADMIN-04 `FlightAdmin.is_in_use` — keep the annotation, drop the fallback `.exists()`.
  - ADMIN-06 `mark_agents_for_payment` — `queryset.aggregate(Sum('unpaid_commissions'))` not a Python loop.
  - ADMIN-07 `preview_driver_payments` — prefetch unpaid legs for all selected drivers before the loop.
  - DRIV-11 / DRIV-12 — remove `unpaid_legs_display` / `recent_leg_history` from the changelist (link to detail instead).
- ❑ **`@cached_property` + prefetch** for `Leg.intermediate_stops / all_stops / additional_dropoffs /
  has_intermediate_stops / has_additional_dropoffs` (`reservations/models.py` ~1795-1830); add
  `prefetch_related('legstop_set')` in the views/templates that iterate legs.
- ❑ **RES-01** `Reservation.calculate_total_driver_payments` — `self.legs.aggregate(Sum('total_driver_pay'))`
  instead of Python `sum()`. **Guard:** result identical.
- ❑ **RES-03** `Reservation.recalculate_leg_revenue_shares` — `Leg.objects.bulk_update(...)` instead of
  per-leg `save()`. **Guard:** per-leg `revenue_share` identical to looped version.
- ❑ **OPS-06** `ops/views.py::task_queue_view` — paginate (first 100 + lane counts via aggregate;
  lazy-load the rest).
- ❑ **DISP-05 (analytics)** `calculate_route_timing_metrics` — default to a bounded window (e.g.
  last 90 days) when no date range is passed.
- ❑ **AGENTS-01 / AGENTS-08 / AGENTS-09** agency dashboards — `prefetch_related('reservations')` +
  `annotate(Count/Sum)`; reuse `users/eligibility.py::bulk_ready_totals` for AGENTS-08.
- ❑ **BOOKING-01 / RES-10** `reservation_form` — reduce to ≤2 saves; pass `update_fields` on the
  intermediate save so commission/profit signals short-circuit. **Guard:** re-confirm
  `reservations/signals.py` `COMMISSION_FIELDS` intersection logic makes this behavior-identical,
  and commission/profit totals are unchanged.

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
