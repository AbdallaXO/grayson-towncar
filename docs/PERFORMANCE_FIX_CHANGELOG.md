# Performance Fix Changelog

Append-only log of performance changes. One row per fix.

> **Status: Phase 1 + Phase 2 first pass landed 2026-06-04** (safe, behavior-preserving
> query/structure wins + instrumentation). See `PERFORMANCE_AUDIT_AND_ACTION_PLAN.md` and
> `PERFORMANCE_FIX_TASKS.md`. Three items were re-scoped to Phase 4 because the "safe" framing
> didn't hold once the real side effects were read — see **Deferred** below.

> **Already done in HEAD before this pass** (verified, no action needed): DEBUG env-gated
> (`settings.py:37`, working-tree has a local `DEBUG=True` dev override); `perf` logger wired to a
> console handler (`settings.py` LOGGING); `CACHES` Redis branch + `CONN_MAX_AGE=600`;
> `Reservation.total_paid` already `@cached_property`; commission `post_save` already guards on
> `update_fields`/`COMMISSION_FIELDS`; `FlightAdmin.is_in_use` already annotated (Exists);
> `DriverAdmin` heavy callables already off the changelist (DRIV-11/12); `bulk_ready_totals` already
> used by the affiliate-payments page (AGENTS-08). `PERF TEMP` checkpoints already present in
> `dispatching/views.py`.

## How to use this log
For every fix, record:
- **Date** — ISO date the change merged.
- **File(s)** — paths touched.
- **Finding ID** — the audit finding(s) addressed (e.g. `DISP-03`).
- **Issue fixed** — one line.
- **Risk level** — Low / Medium / High (the fix's risk, not the bug's severity).
- **Test performed** — what you ran/checked (query-count before→after, manual steps, unit tests).
- **Notes** — surprises, follow-ups, rollback notes.

## Local measurement workflow

To capture query count + wall time for a page before/after a fix:

1. **Slow-request log (always on).** `SlowRequestMiddleware` logs any request > 500ms to the `perf`
   logger as `SLOW <method> <path> — <ms>ms, <N> queries`. The query count appears **only when
   `DEBUG` is on** (Django records `connection.queries` only under DEBUG). Set `DJANGO_DEBUG=1` in
   your local `.env` (`python-dotenv` is already loaded) and watch the console.
2. **Per-view query count in a shell** — exact and side-effect-free:
   ```python
   from django.db import connection
   from django.test.utils import CaptureQueriesContext
   from django.test import Client
   c = Client(); c.force_login(some_staff_user)
   with CaptureQueriesContext(connection) as ctx:
       c.get("/dispatching/legs/")
   print(len(ctx))            # query count
   # for q in ctx.captured_queries: print(q["sql"][:200])
   ```
3. **Debug Toolbar** — run with `ENABLE_DEBUG_TOOLBAR=1` (the `if DEBUG` toolbar block in settings)
   for an in-page SQL panel.

The `PERF TEMP` checkpoints already in `dispatching/views.py` emit wall-time splits to the `perf`
logger (INFO) for the dashboard / schedule build / auto-assign apply.

## Log

| Date | File(s) | Finding ID | Issue fixed | Risk | Test performed | Notes |
|------|---------|------------|-------------|------|----------------|-------|
| 2026-06-04 | reservations/middleware.py | Phase-1 | `SlowRequestMiddleware` logs per-request query count for slow reqs when `DEBUG` | Low | `manage.py check`; query count = delta around `get_response` | DEBUG-gated; no prod overhead |
| 2026-06-04 | reservations/admin.py | ADMIN-01 | `payment_status_display` reads prefetched `payments` instead of per-row `.exists()`+`.order_by().first()` | Low | `check` | ~2 queries/row → 0 (prefetch already present) |
| 2026-06-04 | drivers/admin.py | ADMIN-03/14 | `LegPaymentAdmin.list_select_related = (payment, payment__driver, payment__driver__profile, leg)` | Low | `check` | was no select_related → 2 queries/row |
| 2026-06-04 | drivers/admin.py | ADMIN-07 | `preview_driver_payments` batch-fetches unpaid completed legs in 1 query grouped by driver | Low | `check`; same filter/sum as before | N queries → 1; Python `total_driver_pay` sum unchanged |
| 2026-06-04 | users/admin.py | ADMIN-06 | `mark_agents_for_payment` uses one `aggregate(Sum+Count)` | Low | `check` | 3 queries (exists+sum+count) → 1 |
| 2026-06-04 | reservations/models.py, dispatching/utils.py | (stop props) | Leg `intermediate_stops`/`additional_dropoffs`/`all_stops`/`has_*` → `@cached_property`; `legstop_set` prefetched in `get_filtered_legs_queryset` | Low | `check`; no Python caller reads-then-mutates (templates only); `makemigrations` no-op | legs_list was N+1 on legstop_set |
| 2026-06-04 | reservations/models.py | RES-01 | `calculate_total_driver_payments` → single SQL aggregate mirroring `total_driver_pay` | Low | **483 reservations, 0 mismatches** vs old Python sum | all components 2-decimal → numerically identical |
| 2026-06-04 | reservations/models.py | RES-03 | `recalculate_leg_revenue_shares` → one `bulk_update(['revenue_share','profit_estimate'])` | Low | 5 multi-leg reservations in rolled-back txn: shares sum to total, profit set, no error | values computed identically; `bulk_update` bypasses signals like prior `.update()` |
| 2026-06-04 | dispatching/views.py | DISP-01 | `capacity_planner` memoizes `get_effective_availability` per request | Low | `check` | eligible drivers were computed 2× (lines 8575 & 8843); schedule_board needs none (call sites mutually exclusive) |
| 2026-06-04 | dispatching/views.py | DISP-05(views) | `legs_list` caches `get_trip_type()` once per leg | Low | `check` | was 3× per leg across 3 loops |
| 2026-06-04 | dispatching/analytics.py | DISP-05(analytics) | `calculate_route_timing_metrics` bounds the `legs_queryset is None` default to last 90 days | Low | `check`; sole caller passes its own queryset | defensive only — metric values & scheduler estimates unchanged |
| 2026-06-04 | users/views.py | AGENTS-01/09 | Agency dashboards: `len(agent.reservations.all())` via prefetch cache; `AgencyDetailView` reservation counts via one grouped query | Low | `check` | `.count()` per agent (N+1) → 0/1 |

## Deferred to Phase 4 (the Phase-2 "safe" framing did not survive reading the real code)

- **OPS-06 — `task_queue_view` pagination.** The five lanes are partitioned **in Python** from
  related objects and JSON `metadata` (`blocked_by.is_open`, `leg.pickup_date`,
  `metadata.earliest_pickup`), and every tab shows an **exact count**. Reducing to "first 100 +
  aggregate lane counts" requires replicating that logic in SQL and risks wrong operational counts —
  not a zero-behavior-change edit. Needs a tested re-architecture (light count queryset + full fetch
  for the active lane only).
- **DISP-03 — auto_assign apply → `bulk_update`.** `driver` is in `Leg._EXPENSIVE_FIELDS`, so the
  per-leg `leg.save()` runs the **full** path: driver-pay auto-fill (`calculate_driver_pay`),
  cross-leg gratuity smearing (reads `reservation.legs.all()`, order-dependent), `revenue_share`,
  status reset, and profit/commission `post_save` signals. A naive `bulk_update(['driver',...])`
  would silently drop all of that → drivers unpaid for auto-assigned legs. The audit's "assignments
  identical" guard is insufficient; the real guard must also assert pay + profit/commission are
  identical. Requires the Phase-4 batched-pay design.
- **BOOKING-01 / RES-10 — `reservation_form` ≤2 saves.** Reservation has 4 `post_save` receivers
  (`reservation_saved`, `update_agent_commission_data`, `auto_convert_lead_on_reservation`,
  `log_reservation_changes`) plus a `pre_save` old-values fetch. The audit-log handler writes per
  save; dropping a save changes firing counts of non-idempotent handlers. Behavior-neutral reduction
  needs the full signal-idempotency audit + tests the plan's own guard calls for.

<!--
Template row:
| 2026-06-04 | dispatching/views.py | DISP-03 | auto_assign per-leg save() -> bulk_update | Med | apply on 50-leg day, assignments identical, 1 UPDATE vs 50 | rollback = revert commit |
-->
