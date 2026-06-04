# Grayson Towncar — Performance Audit & Action Plan

_Generated 2026-06-03. Method: 26-agent read-only audit (13 area clusters -> adversarial verification -> completeness critic) over the live codebase. Severities below are **post-verification** (a skeptic re-read the cited code for every Critical/High; false positives were downgraded or marked refuted)._

**Finding totals (post-verification): 161 findings — 11 Critical, 54 High, 70 Medium, 26 Low.**

> **Production runtime facts** (established by reading `business/settings.py` + `railway.json`):
> - **Single synchronous Gunicorn worker** — start command `gunicorn business.wsgi --timeout 60`, no `--workers`/`--threads`, `numReplicas: 1`. Any slow view / query / external API call blocks **every** user. This is the root multiplier behind most "freezing".
> - **`DEBUG = True` hardcoded** (`settings.py:37`) with no env override -> `connection.queries` grows unbounded in the long-lived worker (memory), template cache disabled, error pages leak internals. *Deploy-safety issue.*
> - **No Celery/Redis broker.** `celery`/`redis` are in `requirements.txt` but unused; "background" work runs in raw daemon threads inside the one worker (`ghl_integration/runner.py`, `reservations/utils.py::_run_in_background`), competing for the GIL.
> - **Connection reuse is ALREADY correct** — `conn_max_age=600, conn_health_checks=True` in the Railway branch (`settings.py:151-157`). (The audit critic flagged this as missing; that is a **false positive**.)

---

## A. Executive Summary

### Top likely causes of slowness / freezing
1. **Single sync Gunicorn worker** (no `--workers`/`--threads`, 1 replica). Every slow view, query, or external API call serializes the whole site. Highest-impact lever — but increasing workers is blocked until the in-process scheduler/background sender is made a singleton (else duplicate SMS/email sends).
2. **Synchronous external API calls in the request cycle** (Twilio, AeroAPI, Google Maps Distance Matrix, Stripe, GHL, NTFY). On one worker these freeze all users. Most common confirmed pattern in the audit.
3. **`DEBUG = True` in production** — unbounded `connection.queries` growth in the long-lived worker + disabled template caching. Deploy-safety + slow-creep issue.
4. **Per-row `.save()` / query-in-loop** instead of `bulk_update`/`aggregate` — auto-assign apply, admin payment actions, revenue-share recompute, scheduler inner loops.
5. **Admin `list_display` callables that hit related objects** -> N+1 across the changelist (Reservation/Driver/Payment/TravelAgent admins).
6. **Model `@property` methods that query related sets per access** (`Leg.intermediate_stops`/`all_stops`/`additional_dropoffs`/`has_*`) iterated in templates without `prefetch_related`.
7. **Unbounded querysets / no pagination** — `task_queue_view`, unpaid-reminder duplicate cache, `*.objects.all()` for Vehicle/Location/Lead/RouteTimingMetric.
8. **Background scheduler + jobs run in-process** with `sleep()` pacing and in-request retries (`sleep(60-180s)`), competing with web requests for the single worker.
9. **Multiple saves per request** re-firing commission/profit/lead signals (`reservation_form` saves 2-3x).
10. **Daemon-thread-per-row** spawns from admin actions / signals (e.g. follow-up sequence per selected lead) pile up threads inside the one worker.

### Worst pages / workflows
- **Conflict-task detail** (`ops/views.py::_build_driver_conflict_context`) — up to ~29 synchronous Google Distance-Matrix calls per load (10-20s).
- **Daily capacity planner & schedule board** — redundant per-driver availability recompute + large embedded JSON payload.
- **Legs dashboard / `legs_filter`** — flight-status polling hammering the single worker; per-leg revenue/flag computation.
- **Auto-assign apply** — N sequential `leg.save()` UPDATEs inside a transaction.
- **Driver `index`/`schedule`** — Google drive-time per leg in a loop.
- **Heavy admin actions** — process driver payments, start follow-up sequence, send commission statements (sync API / thread-per-row).
- **Agency-head dashboard** — N+1 over agents/reservations.
- **Booking checkout & Stripe webhook** — Meta CAPI / Stripe re-fetch in the response path.

### Highest-risk code paths (protect correctness)
- `reservations/signals.py` commission/profit/duplicate-lead post_save chain (already partly `update_fields`-guarded — verify before touching).
- `dispatching/scheduler.py` / `swap_optimizer.py` assignment logic — optimize loops **without** changing assignment results.
- `payment/webhook.py` Stripe handling — must stay idempotent and return 200 quickly.

### Biggest quick wins (low risk, high value)
- Memoize per-request driver availability in capacity planner / schedule board.
- `bulk_update` in auto-assign apply.
- `list_select_related` / `get_queryset` on the heavy admin changelists; drop heavy callables from `list_display`.
- `@cached_property` + `prefetch_related("legstop_set")` for `Leg` stop-properties.
- Env-gate `DEBUG` (default `False`).
- Paginate `task_queue_view`; bound the default window on route-timing analytics.

---

## B. Full Codebase Findings

Grouped by audit area. **Severity shown is post-verification** (original -> corrected where the skeptic adjusted it). Findings marked **REFUTED** were investigated and found *not* to be real issues — retained for transparency so they are not re-investigated.

### Dispatching Views (dispatching/views.py, dispatching/utils.py)

| Component | Purpose | Risk |
|---|---|---|
| capacity_planner (line 8457) | Daily capacity planner view showing driver timelines and unassigned leg suggestions | High |
| schedule_board (line 793) | Lightweight drag-and-drop driver timeline for reshuffling assignments | High |
| auto_assign_drivers (line 9660) | Auto-assign inhouse drivers to unassigned legs with multi-pass optimization | Critical |
| index (line 104) / legs_dashboard | Main dispatcher dashboard showing all legs for selected date | High |
| legs_list (line 1809) | Paginated list of upcoming legs with filters | High |
| reservation_details (line 1492) | Detailed reservation view with payment and leg history | Medium |
| refresh_all_flights (line 4824) | Bulk flight refresh via AeroAPI in daemon thread | Critical |
| recalculate_route_metrics (line 11187) | Background thread for recalculating route timing metrics | High |

#### DISP-03 — Critical
- **File / symbol:** `dispatching/views.py` :: `auto_assign_drivers (9659-10076)`  (lines 9959-9972)
- **Issue type:** inefficient-loop-with-save-calls  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** for loop at line 9959 calling leg.save(update_fields=[...]) per leg. No bulk_update() used. Each leg is saved individually, creating N separate UPDATE queries.
- **Why slow/risky:** Lines 9959-9972 save leg driver assignments in a loop: 'for lid, did in final_assignments.items()' then 'leg.save(update_fields=[...])'. Each leg.save() is a separate DB UPDATE statement. With 50+ legs, this is 50+ sequential UPDATE queries inside a transaction. On single worker, blocks site for 1-2s while processing.
- **Fix:** Replace individual leg.save() calls with Leg.objects.bulk_update(legs_to_update, fields=['driver', 'driver_assigned_by', 'driver_assigned_at'], batch_size=500). Collect modified legs in a list first, then bulk_update once. Reduces 50+ queries to 1-2 bulk operations.
- **Expected impact:** Auto-assign performance on 50-leg day: ~500ms sequential saves to ~20-50ms bulk update. Unblocks site immediately vs blocking for 1s during apply.
- **How to test:** Run auto_assign_drivers with apply=True on a date with 50+ unassigned legs. Measure elapsed time and count UPDATE queries in django logs. Should go from 50+ individual UPDATEs to 1-2 bulk operations. Time should drop from ~1s to ~50-100ms.
- **Verification:** (confirmed) Loop at lines 9959-9972 calls leg.save(update_fields=[...]) individually for each leg in final_assignments. No bulk_update() used. Each call is a separate UPDATE query. In single-worker production, this blocks the site for N UPDATE queries instead of 1-2 bulk operations. Severity is Critical.

#### DISP-06 — Critical
- **File / symbol:** `dispatching/views.py` :: `refresh_all_flights (4824-4914)`  (lines 4866-4869)
- **Issue type:** sync-external-API  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Line 4868: `arrival_legs = [leg for leg in legs if leg.get_trip_type() == 'arrival']` — this filters legs via get_trip_type() in a loop in the REQUEST thread (lines 4860-4869), not the background thread. Should be done in daemon thread or via ORM annotation.
- **Why slow/risky:** Lines 4866-4869 call leg.get_trip_type() on every leg in a loop BEFORE spawning the background thread, inside the request thread (main worker). This CPU operation runs in the request handler, blocking the worker from serving other requests. With 100+ legs, this is 100+ get_trip_type() calls in main worker (unnecessary since filtering could happen in background thread).
- **Fix:** Move the trip_type filtering (lines 4866-4869) into the background thread _run_bulk_flight_refresh (after line 4669). Alternatively, annotate legs queryset with trip_type via ORM subquery/case statement and filter at query time (more efficient). Request should just spawn thread and return <10ms.
- **Expected impact:** Request response time for refresh_all_flights: reduce CPU in main worker by ~100-300ms (avoid N calls to get_trip_type() in request thread). Unblocks main worker to serve other users immediately.
- **How to test:** Call refresh_all_flights with 50+ legs. Measure time to return JSON response. Should be <100ms (just spawning thread + returning). Verify actual flight refreshes happen in background daemon.
- **Verification:** (confirmed) Lines 4860-4869 filter legs via get_trip_type() IN THE REQUEST THREAD before spawning the background thread at line 4894. The filtering loop at line 4868 calls get_trip_type() per leg synchronously. This blocks the HTTP response until all legs are processed. Should be moved into the daemon thread or use ORM annotation.

#### DISP-04 — High
- **File / symbol:** `dispatching/views.py` :: `index (104-500)`  (lines 364-376, 401-413, 420-423)
- **Issue type:** python-aggregate  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Line 365: `total_revenue = sum(leg.revenue_share or leg.calculate_revenue_share() for leg in legs)` — calculate_revenue_share() is computed per leg. Lines 420-423: Loop over legs calling afterhours_fee_outstanding() — this method checks Payment records. Line 402: detect_leg_flags() inside loop over legs.
- **Why slow/risky:** Legs dashboard calls multiple Python-side methods in loops over legs_list: (1) sum(leg.calculate_revenue_share() for leg in legs) at line 365 (Python aggregation: revenue calculation per leg), (2) detect_leg_flags(leg, now) per leg at line 402, (3) leg.afterhours_fee_outstanding() per leg at line 421 (queries Payment + checks time). With 100+ legs/day, these loops are expensive.
- **Fix:** Annotate legs queryset with aggregate sum of leg-level revenue or use Prefetch('reservation__payments', ...) to load all payments once, cache result so afterhours_fee_outstanding() doesn't re-query. For detect_leg_flags: this is already O(1) per leg (no DB), keep as-is but ensure called only on 'today' legs.
- **Expected impact:** Dashboard load time for 100+ legs: reduce Python loop overhead by ~100-500ms via prefetch optimization. Single worker unblocked sooner.
- **How to test:** Load dashboard for a date with 100+ legs. Profile time spent in loops. Afterhours checking should benefit most from Payment prefetch.
- **Verification:** (uncertain) Line 364-366: sum() calls calculate_revenue_share() per leg. That method calls .legs.count() at line 1219 unless legs are prefetched. The index view does not explicitly prefetch 'reservation__legs' in its Prefetch clause (only 'reservation__payments'). However, the concern about afterhours_fee_outstanding() at lines 420-423 is valid—this method does expensive lookups per leg. Prefetch coverage appears incomplete.

#### DISP-05 — High
- **File / symbol:** `dispatching/views.py` :: `legs_list (1809-1990)`  (lines 1876-1881, 1929-1931)
- **Issue type:** recalculation  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Line 1879: `if leg.get_trip_type() == trip_type_filter` — get_trip_type() is a Leg method that looks at reservation + legs count; not cached. Line 1930: `trip_type = leg.get_trip_type()` — called again in another loop after already called at line 1879.
- **Why slow/risky:** Multiple Python-side loops iterate over paginated leg list calling leg.get_trip_type() redundantly: Lines 1876-1880 call get_trip_type() per leg in a loop, then lines 1929-1931 call it again in another loop (same legs, computed property not cached). With 20-50 legs per page, get_trip_type() is called 2x per leg (100+ calls total per request).
- **Fix:** Compute trip_type once per leg via a dict comprehension before loops. Cache in {leg_id: trip_type} or annotate each leg object with _cached_trip_type after first computation. Reuse cached value in all subsequent loops.
- **Expected impact:** Legs list pagination: reduce CPU for trip_type calculation by ~50% via caching one call per leg instead of 2.
- **How to test:** Load legs_list page with 20 legs and apply trip_type filter. Measure CPU time for get_trip_type() calls. Should cache results so 2nd loop uses cached values.
- **Verification:** (confirmed) get_trip_type() called at line 1879 inside loop, then called again at line 1890, and again at line 1930 in a separate loop. This method looks at reservation + legs count each time. Called 3+ times per leg without caching. In pagination, page_obj size is 20 legs, so ~60+ method calls per page load for redundant computation.

#### DISP-09 — High
- **File / symbol:** `dispatching/views.py` :: `auto_assign_drivers (9767-9770, 9759-9765)`  (lines 9767-9770, 9760, 9778)
- **Issue type:** repeated-expensive-method-in-loop  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Line 9760: `is_avail, sh, eh, pref, flex = d.get_availability_for_date(target_date)` inside a loop (lines 9759-9765). Line 9768: `d.get_full_availability(target_date)` inside a loop (lines 9767-9770). These methods internally walk weekly_schedule + date_overrides (same as get_effective_availability from DISP-01).
- **Why slow/risky:** Lines 9767-9770 loop over inhouse_drivers calling d.get_full_availability(target_date) per driver (not prefetched). Lines 9759-9765 loop calling d.get_availability_for_date(target_date) per driver. With 20+ drivers, this is multiple calls to expensive availability methods that each hit DB (weekly_schedule + date_overrides). Identical root cause as DISP-01 & DISP-02.
- **Fix:** Pre-compute driver availability dict once before loops (like DISP-01): before line 9759, collect all driver availability data into {driver_id: {is_avail, sh, eh, pref, flex, full_avail}}. Then loop can use cached dict instead of calling methods. Saves ~40-80 DB queries.
- **Expected impact:** Auto-assign request processing: reduce driver availability queries by ~40-80 (20-40 drivers x 2 calls each) to ~5-10 (single preload). Saves ~1-2s per request.
- **How to test:** Call auto_assign_drivers with apply=False on date with 30+ inhouse drivers. Count DB queries from get_availability_for_date and get_full_availability methods. Should drop from ~60 to ~5 after fix.
- **Verification:** (confirmed) get_availability_for_date() called at line 9760 in loop (9759-9765), get_full_availability() at line 9768 in loop (9767-9770), then get_availability_for_date() called AGAIN at line 9778 in a third loop. Drivers prefetched at line 9731, so no N+1, but same method called repeatedly per driver across 3 different code sections without caching. With ~10-15 drivers, this is 30-45 redundant method invocations per request.

#### DISP-01 — Medium (orig Critical -> Medium)
- **File / symbol:** `dispatching/views.py` :: `capacity_planner (8457-8927)`  (lines 8572-8631, 8889-8898)
- **Issue type:** repeated-expensive-method-in-loop  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Lines 8575, 8891 show get_effective_availability() called inside loops over drivers. Each call walks driver.weekly_schedule.all() + driver.date_overrides.all() in the method. This is called 2x per driver (once at 8575 in vehicle_assign_rows, once at 8891 in driver_availability dict).
- **Why slow/risky:** Inside vehicle_assign_rows loop (all inhouse drivers) and driver_availability loop, .get_effective_availability(selected_date) is called per driver, which hits DB for weekly_schedule + date_overrides (SELECT + prefetch). With ~20-40 drivers, this is ~40-80 queries. Single-worker blocks the entire site.
- **Fix:** Batch-load all drivers with prefetch_related('weekly_schedule', 'date_overrides') once at line 8535-8540. Then cache results in a dict {driver_id: eff_dict} computed once, reused for all loops. Saves ~60 DB round-trips.
- **Expected impact:** Capacity planner response time: ~1-2s of DB time per load for 20-40 drivers. On single worker, blocks all other users. Estimated 80+ queries to 2 queries.
- **How to test:** Load capacity_planner page for today with 20+ inhouse drivers assigned. Count DB queries in django.db.connection.queries. Should go from ~80-100 to ~20-25 after fix.
- **Verification:** (confirmed) get_effective_availability() is called in loop at line 8575 (8572-8631), but drivers are prefetched with weekly_schedule and date_overrides at line 8538, so the underlying .all() calls inside the method use in-memory cache, not DB queries. However, the result is not cached per request, so the method is recalculated for each access. With prefetch in place, this is less critical than severity Critical suggests, but still redundant computation.

#### DISP-02 — Medium (orig Critical -> Medium)
- **File / symbol:** `dispatching/views.py` :: `schedule_board (793-1246)`  (lines 1010, 1080)
- **Issue type:** repeated-expensive-method-in-loop  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Lines 1010 and 1080 call driver.get_effective_availability() inside loops. Each call does SELECT on weekly_schedule + date_overrides. Loop processes similar code to capacity_planner.
- **Why slow/risky:** Inside inhouse_timeline loop (lines 991-1073) and available_no_jobs loop (lines 1077-1120), .get_effective_availability(selected_date) is called per driver. With 40 drivers, this is ~80+ DB queries inside a single request. Blocks site during peak load.
- **Fix:** Pre-compute all driver availability once before loops (lines 989-1120). Cache in dict {driver_id: eff_dict}. Reuse for all downstream loops. Same fix as DISP-01.
- **Expected impact:** Schedule board response: ~1-2s of DB queries per load (80+ queries). Single worker blocks entire site during concurrent load.
- **How to test:** Load schedule_board for date with 40 inhouse drivers. django.db.connection.queries count should drop from ~100 to ~20 after fix.
- **Verification:** (confirmed) get_effective_availability() called at lines 1010 and 1080 in loops. Drivers prefetched with weekly_schedule and date_overrides at line 850, so no N+1 queries. But method result not cached within request, causing redundant recalculation. Less severe than Critical due to prefetch mitigation.

#### DISP-07 — Medium (orig High -> Medium)
- **File / symbol:** `dispatching/views.py` :: `capacity_planner (8668-8704)`  (lines 8689, 8757-8763)
- **Issue type:** query-in-loop  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Line 8689: `for sh in leg.status_history.all()` — already prefetched, so list() is OK (no N+1), but the loop runs for every leg to find the 'completed' entry (could be optimized with list comprehension or .first()). Line 8760: `_cp_end = estimate_job_end_time(_cpl, _cp_prev_day)` — called per previous-day leg. preload_timing_cache() at line 8479 should cover this, but may not cover prev_day.
- **Why slow/risky:** Inside loop over legs_list (lines 8668-8703), each leg's status_history.all() is accessed to find 'completed' status (line 8689). Though status_history is prefetched, the loop iterates all history entries per leg searching for 'completed' (could be 50+ entries per leg, totaling 1000s of iterations). More critically, lines 8757-8763 iterate _cp_prev_legs and call estimate_job_end_time() per leg — this routing lookup may hit RouteTimingMetric DB if cache not warm.
- **Fix:** Replace lines 8688-8703 with single loop that computes list of status_history.all() once and extracts 'completed' entry more efficiently (e.g., use next((s for s in sh_list if s.status == 'completed'), None)). Verify preload_timing_cache() preloads metrics for both selected_date AND prev_day.
- **Expected impact:** Capacity planner leg annotation: reduce time in status history loop by ~100-200ms for legs with 50+ status entries.
- **How to test:** Load capacity_planner for today. Measure time spent in leg.status_history loops. Profile estimate_job_end_time calls to verify cache hits (should be ~0 DB queries if preload is warm).
- **Verification:** (uncertain) Line 8689: for sh in leg.status_history.all() is safe (prefetched). Line 8760: estimate_job_end_time() called per previous-day leg in a loop. No evidence this is a bottleneck without profiling. Depends on preload_timing_cache() coverage for prev_day metrics. Severity Medium due to uncertainty about actual DB cost.

#### DISP-08 — Medium
- **File / symbol:** `dispatching/views.py` :: `update_leg_assignment (2003-2234)`  (lines 2046-2048)
- **Issue type:** missing-index  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Line 2047: `leg = Leg.objects.get(id=leg_id)` — no select_related. Line 2056 accesses leg.reservation.status without prefetch. Line 2091 fetches driver separately (Driver.objects.get(id=value)).
- **Why slow/risky:** Line 2047 fetches a leg without .select_related() for driver/reservation/vehicle. Though a single-leg fetch, missing select_related means driver access at line 2091-2097 (Driver.objects.get(id=value)) and reservation access at line 2056 (leg.reservation.status) will trigger additional queries. For a high-traffic AJAX endpoint (drag-drop), this adds latency per request.
- **Fix:** Add select_related('reservation', 'driver') to line 2047 query. This saves 2 DB round-trips per call.
- **Expected impact:** AJAX endpoint latency: reduce 2-3 queries per leg assignment (common on schedule board drag-drop) to 1 query. If users drag 50 legs, this saves ~100 DB round-trips.
- **How to test:** Drag a leg to reassign on schedule_board. Monitor django.db.connection.queries. Should see 1 query for leg fetch (with select_related) instead of 3-4.

---

### Scheduling & Optimization Engine (dispatching/)

#### DISP-03 — High
- **File / symbol:** `dispatching/scheduler.py` :: `suggest_assignments (main loop at line 1306)`  (lines 1445, 1476, 1501)
- **Issue type:** repeated-sorting  |  **Fix risk:** Low
- **Evidence:** # Line 1445: Sort slots for each leg + driver pair last_slot = sorted(sched.slots, key=lambda s: s.pickup_time)[-1] # Line 1476: Sort again for idle-gap penalty sorted_slots_gap = sorted(sched.slots, key=lambda s: s.pickup_time) # Line 1501: Sort AGAIN for span penalty sorted_slots_span = sorted(sched.slots, key=lambda s: s.pickup_time)
- **Why slow/risky:** Inside the main (leg × driver) loop, sched.slots is sorted 3 times independently per candidate. With ~100 legs × ~20 drivers = 2000 iterations, each sorting ~5–10 slots, this is 6000+ sort operations. Each sort creates a new list and copies data. The slots are already sorted once after DriverDaySchedule builds (line 921).
- **Fix:** Cache sorted_slots once per driver/schedule at the start of the per-driver iteration (line 1320 entry) and reuse it in all three scoring sections. Or, pre-sort at schedule-build time and assert invariant (use bisect.insort when appending instead of re-sorting). Since slots grow over time during the scoring loop, maintain the invariant by inserting in sorted order rather than sorting repeatedly.
- **Expected impact:** Eliminates 4000+ redundant sort operations per page load. Measurable speedup in suggest_assignments scoring phase.
- **How to test:** Verify scoring penalties are identical before/after (backward_chain, idle_gap, span penalties). Confirm the sorted order matches the manual sorts.
- **Verification:** (confirmed) Three separate sorts of sched.slots confirmed at lines 1445, 1476, and 1501. Each sort is O(S log S) where S~5-15 slots. These occur inside the inner loop over drivers (started at line 1320), which means each sort runs ~1200 times per dispatch (80 legs × 15 drivers). With 3 sorts per iteration, that's 3600 small sorts. In single-worker production this is legitimate Hot Path contention. Severity High is correct. The slots list grows during scoring (line 1320 iterates over working.items() which represents driver-day schedules), so sorting is necessary but should be cached or replaced with O(1) insertion order invariant.

#### DISP-05 — High
- **File / symbol:** `dispatching/swap_optimizer.py` :: `_get_conflicting_slots`  (lines 184-189)
- **Issue type:** query-in-loop  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** results = [] for slot in schedule.slots:  # ~5-15 slots per driver     modified = _build_modified_schedule(schedule, remove_leg_ids={slot.leg_id})     result = check_feasibility(modified, candidate_leg, target_date, inter_job_buffer, ...)     if result.feasible:         results.append((slot, result.buffer_minutes))
- **Why slow/risky:** _get_conflicting_slots is called per driver in the swap search (line 485), which itself is called for each unassigned leg in recover_residuals_via_swaps. Each call runs check_feasibility per slot (5–15 times). check_feasibility is expensive: it validates turnarounds (Guard B) and window constraints (Guard C) for every pair of slots. With depths up to 5, this grows exponentially. On a busy day (80 legs), the swap search becomes the bottleneck.
- **Fix:** Cache the result of check_feasibility for each (schedule, slot) pair. Before searching, run a single O(L) pass: for each slot, evaluate feasibility once. Use that cache during the DFS. Or, short-circuit: if a schedule has < 20% buffer on average, skip it as a target (unlikely to absorb a new leg). Measure actual depth needed; current max_depth=5 may be excessive.
- **Expected impact:** Swap search becomes O(D × C) instead of O(D × C × S × L) where D=drivers, C=candidate positions, S=slots/driver, L=avg schedule size. On busy days, this is 50–100x faster.
- **How to test:** Run recover_residuals_via_swaps on a day with 50+ farmed legs. Time the execution before/after. Verify swap solutions are identical (feasibility cache is valid).
- **Verification:** (confirmed) Lines 184-189 confirmed: for each slot (5-15 per schedule), a modified schedule is built and check_feasibility() is called (expensive operation involving datetime math, feasibility validation, buffer calculations). This occurs at every DFS depth iteration (max_depth=5 default, 1-indexed). In the swap optimizer's iterative-deepening search, at each depth it searches all unplaced legs × all drivers × conflicting slots, each spawning a feasibility check. With single-worker production, every check blocks the entire site. Severity High is appropriate. The fix suggestion (caching) would help but the DFS depth itself (max_depth=5) may be excessive—worth measuring if actual placement problems require depth > 2-3.

#### DISP-01 — Medium (orig Critical -> Medium)
- **File / symbol:** `dispatching/scheduler.py` :: `suggest_assignments`  (lines 1175-1201)
- **Issue type:** nested-loops  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** for leg in sorted_legs:     dropoff_cat = categorize_location(leg.dropoff_location)     leg_end_est = estimate_job_end_time(leg, target_date)     chain_count = 0     for other in sorted_legs:         if other.id == leg.id: continue         other_pickup_cat = categorize_location(other.pickup_location)         if other_pickup_cat == dropoff_cat: ...         # Full O(N²) scan of legs for each leg
- **Why slow/risky:** On a single gunicorn worker, O(N²) leg scanning in suggest_assignments runs synchronously on every capacity-planner page load. With ~100 unassigned legs, this is 10k loop iterations. categorize_location() is called 200k times (each pass re-categorizes). This blocks the entire site while scoring completes, and estimate_job_end_time is called redundantly inside the innermost loop.
- **Fix:** Pre-compute leg end times once before the double loop (already done elsewhere in the code — line 847 uses _estimated_end_dt). Pre-compute all categorizations into a dict {leg_id: cat} before the loops. Move chain_count computation to a single O(N) pre-pass that builds {leg_id: chain_count} before any scoring loop starts.
- **Expected impact:** Eliminates 200k+ redundant categorization calls and 10k redundant job-end-time estimates per page load. Reduces suggest_assignments time from ~seconds to ~ms on typical days.
- **How to test:** Load capacity_planner on a day with 80+ unassigned legs. Measure page load time before/after. Chain detection should still identify multi-job continuations (e.g., leg A ends at Disney, leg B picks up at Disney).
- **Verification:** (confirmed) O(N²) nested loop at lines 1175-1201 correctly identified. However, this code is PRE-COMPUTED ONCE before the main scoring loop (not inside it). It runs at startup/refresh to build chain_map, not per-leg or per-driver. With ~80 legs, ~6400 operations is non-trivial but happens once outside the hot path. The auditor cited lines 1175-1201 correctly but severity should be Medium, not Critical, because it's not in the request-critical path—it executes once per dispatch refresh, not once per slot assignment. Still worth optimizing for baseline performance but not blocking.

#### DISP-04 — Medium
- **File / symbol:** `dispatching/scheduler.py` :: `suggest_assignments`  (lines 1175-1177)
- **Issue type:** query-in-loop  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** for leg in sorted_legs:     dropoff_cat = categorize_location(leg.dropoff_location)     leg_end_est = estimate_job_end_time(leg, target_date)
- **Why slow/risky:** estimate_job_end_time runs 80+ times and internally calls categorize_location, looks up RouteTimingMetric, and may fetch flight data. Although preload_timing_cache() exists and is called (line 8479 in views), the cache is cleared at the end of capacity_planner (line 8926). If suggest_assignments is called outside that view, or the cache expires, each call re-queries. estimate_job_end_time is called again later in the main loop (line 1475) for idle-gap scoring.
- **Fix:** Pre-compute all end times into {leg_id: datetime} once before suggest_assignments (as is done on line 847 for build_driver_schedules via _estimated_end_dt). Pass this dict into suggest_assignments and use it directly. Eliminate redundant calls.
- **Expected impact:** Prevents ~100 redundant end-time calculations per suggest_assignments. With flights/dwell-time lookups, this can be ~100ms savings.
- **How to test:** Verify clearing times in capacity_planner timeline match the scored buffer estimates. Test with arrival legs (flight dwell logic) and non-arrival legs.

#### DISP-06 — Medium
- **File / symbol:** `dispatching/scheduler.py` :: `suggest_assignments (around line 1320)`  (lines 1320-1347)
- **Issue type:** repeated-count  |  **Fix risk:** Low
- **Evidence:** for did, sched in working.items():     # Per-driver time window check     if driver_hours and did in driver_hours:         if not (flexible_drivers and did in flexible_drivers):             dh_start, dh_end = driver_hours[did]             if leg.pickup_time < time(dh_start, 0) or leg.pickup_time > time(dh_end, 59):                 continue     # Max hours enforcement     if driver_max_hours and did in driver_max_hours:         if sched.slots:             first_pickup_dt = datetime.combine(target_date, min(s.pickup_time for s in sched.slots))             last_end_dt = max(s.estimated_end_time for s in sched.slots)             span_hours = (last_end_dt - first_pickup_dt).total_seconds() / 3600             if span_hours >= driver_max_hours[did]:                 continue     # Vehicle compatibility     feas = check_feasibility(...)
- **Why slow/risky:** For each leg × driver pair, min/max over sched.slots is called to compute span_hours. With ~100 legs × 20 drivers, this is 2000 × O(5) = 10k slot iterations. This recomputes what the DriverDaySchedule already knows (first job, last job, span). The feasibility check then re-evaluates turnarounds/windows.
- **Fix:** Add first_pickup_time, last_end_time, and span_hours fields to DriverDaySchedule at build time (line 921) and maintain them as slots are added. Read from those cached fields instead of re-computing.
- **Expected impact:** Eliminates 10k min/max operations per suggest_assignments. Measurable when driver_max_hours is used (which is common).
- **How to test:** Verify span_hours filter still correctly rejects drivers whose shifts would exceed max_hours if the leg is added. Test with 6-hour and 12-hour driver limits.

#### DISP-07 — Medium
- **File / symbol:** `dispatching/scheduler.py` :: `build_driver_schedules`  (lines 882-891)
- **Issue type:** missing-index  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** try:     _ls = leg.legstop_set     _legstop_count = len(_ls.all()) if hasattr(_ls, '_result_cache') and _ls._result_cache is not None else _ls.count() except Exception:     _legstop_count = 0 try:     _lf = leg.legflight_set     _legflight_count = len(_lf.all()) if hasattr(_lf, '_result_cache') and _lf._result_cache is not None else _lf.count()
- **Why slow/risky:** For each leg, count() is called on legstop_set and legflight_set if not prefetched. On a day with 100 legs, this is 200 database COUNT queries. The comment acknowledges the fallback to .count() is rare, but in capacity_planner, these relations are NOT prefetched (legstop_set and legflight_set are not in the Prefetch list at line 8510-8522).
- **Fix:** Add Prefetch for LegStop and LegFlight to the Leg queryset in capacity_planner view (line 8497-8524) and anywhere else build_driver_schedules is called. This converts 200 COUNT queries to 1 prefetch. Or, make the prefetch automatic in build_driver_schedules itself by detecting unprefetched relations and fetching them in bulk.
- **Expected impact:** Eliminates 100–200 COUNT queries from capacity_planner. Noticeable speedup on slow-network database connections.
- **How to test:** Load capacity_planner and verify extra_stop_count and secondary_flight_count are correct for legs with multiple stops/flights. Monitor query count before/after.

#### DISP-08 — Medium
- **File / symbol:** `dispatching/views.py` :: `capacity_planner`  (lines 8575 (d.get_effective_availability called in loop))
- **Issue type:** N+1  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** for d in inhouse_drivers:  # ~20 drivers     _va_eff = d.get_effective_availability(selected_date)     _va_is_avail = _va_eff['is_available']     ...
- **Why slow/risky:** d.get_effective_availability is called per driver per capacity-planner load. This method queries DriverWeeklySchedule and DriverDateOverride for each driver (likely 1–2 queries per driver = 20–40 queries). The results are not cached and not bulk-loaded.
- **Fix:** Bulk-load weekly schedules and date overrides for all drivers once (line 8537 already does select_related('weekly_schedule') and prefetch_related('date_overrides'), so the data is in memory). Modify get_effective_availability to accept pre-loaded data or cache the results at the view level.
- **Expected impact:** Eliminates 20–40 database queries from capacity_planner per load.
- **How to test:** Verify availability flags (is_off_today, shift_display) are correct for each driver. Test with drivers that have date overrides.

#### DISP-09 — Medium
- **File / symbol:** `dispatching/scheduler.py` :: `compute_leg_scarcity`  (lines 187-225)
- **Issue type:** recalculation  |  **Fix risk:** Low
- **Evidence:** vtype_eligible_counts = {} driver_list = [(did, vtype) for did, vtype in all_driver_vtypes.items() if did != exclude_driver_id] for vtype in VEHICLE_TIER_ORDER:  # 5 types     count = 0     for did, dvtype in driver_list:  # ~20 drivers         if vtype in get_compatible_vehicle_types(dvtype):             count += 1     vtype_eligible_counts[vtype] = count result = {} for leg in legs:  # ~80 legs     leg_vtype = leg.effective_vehicle_type     if leg_vtype:         result[leg.id] = vtype_eligible_counts.get(leg_vtype, len(driver_list))
- **Why slow/risky:** O(T × D + L) where T=5 vehicle types, D=20 drivers, L=80 legs. The inner loop calls get_compatible_vehicle_types(dvtype) which returns a list slice of VEHICLE_TIER_ORDER (O(1) but repeated T×D times). This pre-computation is called at least once per suggest_assignments run. Total: 100 + 80 operations, negligible in isolation, but the driver_list is rebuilt every time.
- **Fix:** Cache the per-vtype driver counts at DriverVehicleAssignment.objects.all() level (a single query/refresh at the start of the day), and refresh only if assignments change. Current approach re-computes from scratch on every call.
- **Expected impact:** Minimal impact on this function alone (~10x speedup in pure Python), but when called 10+ times per request (once per batch of suggestions), saves ~1000 trivial operations.
- **How to test:** Verify scarcity counts match expected eligible drivers for each vehicle type (e.g., Van(14 Pax) should be eligible for Van jobs).

#### DISP-10 — Medium
- **File / symbol:** `dispatching/scheduler.py` :: `suggest_assignments`  (lines 1306-1632)
- **Issue type:** unbounded-queryset  |  **Fix risk:** High  |  *Needs measurement*
- **Evidence:** for leg in sorted_legs:  # ~80 legs     for did, sched in working.items():  # ~20 drivers         # Full scoring: 80 legs × 20 drivers = 1600 full evals per capacity_planner load
- **Why slow/risky:** The suggest_assignments algorithm is O(L × D) and unbounded. On a day with 150 unassigned legs and 25 drivers, this is 3750 full feasibility checks per page load. With preload_timing_cache helping, each check is ~10ms. That's 37.5 seconds in a single sync gunicorn worker (timeout is 60s). The view caches the result for 60s (line 8644-8660), but the first load blocks.
- **Fix:** For the initial capacity-planner load, paginate suggestions (show top 10–20 legs, defer the rest). Or, run suggest_assignments asynchronously in the background (requires Celery, which is not configured). Or, limit unassigned_legs to the first N for suggestion (e.g., top N by time scarcity). Add early-exit logic if suggestions reach a threshold confidence.
- **Expected impact:** Initial capacity-planner load would be <5 seconds. Users see results instantly; rest loads from cache.
- **How to test:** Load capacity_planner on a 150-leg day. Measure time to first render. Verify suggestions are useful (high-confidence) even with limited set.

#### DISP-02 — Low (orig High -> Low)
- **File / symbol:** `dispatching/scheduler.py` :: `suggest_assignments`  (lines 1221-1234)
- **Issue type:** nested-loops  |  **Fix risk:** Low
- **Evidence:** driver_reserved_count = {} for did, dvtype in driver_vtypes.items():  # ~20 drivers     if not dvtype: continue     count = 0     for leg_check in sorted_legs:  # ~80 legs         leg_check_vtype = leg_check.effective_vehicle_type         if leg_check_vtype and str(leg_check_vtype) == dvtype:             count += 1     driver_reserved_count[did] = count
- **Why slow/risky:** O(D × L) loop: 20 drivers × 80 legs = 1600 vehicle-type checks per suggest_assignments call. Runs on every capacity-planner page load. The vehicle type is read from leg.effective_vehicle_type (a property), not a pre-fetched field, so it may trigger attribute lookups on every iteration.
- **Fix:** Pre-compute {leg_id: vehicle_type} once before the outer loop. Then nest only the vehicle-type comparison. Or, invert: build a {vtype: [leg_ids]} map once, then for each driver-vtype, count matching legs in O(1). This shrinks O(D×L) → O(L) after one-pass inversion.
- **Expected impact:** Reduces driver-reservation pre-computation time by ~90%. Noticeable speedup on capacity-planner when multiple vehicles have narrow type assignments.
- **How to test:** Verify driver_reserved_count[did] matches the count of unassigned legs that match driver did's vehicle type exactly. Test with drivers of different tiers (towncar/van/suv) and mixed job requirements.
- **Verification:** (confirmed) O(D×L) nested loop at lines 1221-1234 correctly identified (~20 drivers × ~80 legs = 1600 iterations). However, this is also PRE-COMPUTED ONCE at initialization (lines 1220-1234) before the main per-leg scoring loop (line 1306). It is not in the hot path per request. The severity should be Low, not High. While correct, the computation is negligible—1600 simple string comparisons at startup is sub-millisecond on modern hardware.

---

### Flight tracking, AeroAPI, confirmations, dispatch analytics — dispatching module

#### DISP-01 — High
- **File / symbol:** `dispatching/flight_verify_views.py` :: `flight_verification_public`  (lines 219-229)
- **Issue type:** sync-external-API  |  **Fix risk:** Medium
- **Evidence:** Line 224: `res = _refresh_one_flight(flight, leg, aeroapi)` inside synchronous POST handler. This makes a blocking AeroAPI call (`get_flight_data()`) inside the single Gunicorn worker, blocking all other users.
- **Why slow/risky:** With 1 sync worker, calling AeroAPI (10s timeout typical) blocks the entire site for all users. If AeroAPI is slow or the network is congested, guest self-service flight verification page hangs.
- **Fix:** Fire the refresh in background thread (like _run_bulk_flight_refresh uses ThreadPoolExecutor) or queue to Celery. Show 'checking...' spinner and poll status, or return 202 Accepted and auto-refresh the page in JS.
- **Expected impact:** Guest waits 10+ seconds for verification page to load. If the API times out, page fails (500 error). During peak traffic, cascade failures across the site.
- **How to test:** Submit verification form with slow network (throttle to 1 Mbps in DevTools). Verify page responds <2s with 'checking' status. Monitor Gunicorn logs for no blocking.
- **Verification:** (confirmed) Confirmed at dispatching/flight_verify_views.py line 226: _refresh_one_flight is called synchronously inside flight_verification_public POST handler. This triggers aeroapi.get_flight_data() (line 3563 in views.py) which makes a blocking requests.Session.get() call (line 389 in aeroapi_service.py, timeout=10s). Single Gunicorn worker blocks all users during this call.

#### DISP-02 — High
- **File / symbol:** `dispatching/views.py` :: `_refresh_one_flight (inside for loop)`  (lines 3706-3709)
- **Issue type:** sync-external-API  |  **Fix risk:** Low
- **Evidence:** Lines 3706-3709: `for idx, f in enumerate(flights_to_refresh): res = _refresh_one_flight(f, leg, aeroapi)`. For multi-leg refreshes, each AeroAPI call is synchronous. Single requests.get() per flight (line 389 in aeroapi_service.py).
- **Why slow/risky:** Synchronous loop over N flights, each with 10s AeroAPI timeout. 5 flights = ~50s blocked. Gunicorn has 60s timeout — bulk refresh easily hits it and 503s.
- **Fix:** Use ThreadPoolExecutor (already used in _run_bulk_flight_refresh line 4721-4722). Batch flights into groups of 5 (AeroAPI rate limit 5/sec), await all threads before returning.
- **Expected impact:** Bulk refresh of 5+ flights fails mid-request (Gunicorn timeout). User sees 503 error; dispatcher must retry.
- **How to test:** Call refresh_flight_data with 10 leg_ids. Monitor Gunicorn logs: should complete <15s (5 batches × 2s per batch + parse overhead), not >60s.
- **Verification:** (confirmed) Confirmed at dispatching/views.py lines 3706-3709: for loop iterates flights_to_refresh and calls _refresh_one_flight(f, leg, aeroapi) sequentially. Each call makes a blocking AeroAPI request. Multi-flight legs will block the worker for N*10s. No ThreadPoolExecutor is used in refresh_flight_data view (unlike _run_bulk_flight_refresh which is a separate background job).

#### DISP-03 — High
- **File / symbol:** `dispatching/flight_verify_email.py` :: `_send_in_background`  (lines 72-94)
- **Issue type:** signal-side-effect  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** Lines 72-94: `t = threading.Thread(target=runner, daemon=True); t.start()`. Fire-and-forget daemon thread with retry loop (time.sleep() inside line 87). If email send fails, thread sleeps 1-2-4s inside the worker.
- **Why slow/risky:** Daemon threads tied to Django request context. If thread sleeps during a request, GIL contention peaks. Multiple requests → multiple sleeping threads → single worker stalls. With DEBUG=True, each thread's exception logging appends to unbounded connection.queries.
- **Fix:** Move to Celery queue (which doesn't exist yet). For now, add a 1-second cap to retries (no exponential backoff) if threading must stay. Sleep 1-2 seconds max per retry.
- **Expected impact:** Email retries cause GIL contention. If 5 requests arrive while 3 threads are sleeping, new requests queue up behind the sleeping threads. Apparent response time degrades for all concurrent users.
- **How to test:** Send 2 verification emails simultaneously (2 POST requests in parallel). Monitor CPU/time: should not exceed 2x single-email latency. Check Python GIL with py-spy: no long hold times.
- **Verification:** (confirmed) Confirmed at dispatching/flight_verify_email.py lines 72-94: _send_in_background spawns daemon thread with t = threading.Thread(target=runner, daemon=True); t.start(). Retry loop at line 76-87 includes time.sleep(2**attempt) blocking the thread (not the main worker, but still consumes worker resources). While fire-and-forget, the GIL will allow other threads to run, but daemon thread sleeping in the worker is inefficient.

#### DISP-04 — High
- **File / symbol:** `dispatching/confirmation_sms.py` :: `send_confirmation_via_twilio`  (lines 430-462)
- **Issue type:** sync-external-API  |  **Fix risk:** Medium
- **Evidence:** Line 454-457: `client.messages.create(body=message, from_=from_number, to=to)` inside a synchronous function called in loops (line 478 in send_confirmations_for_date).
- **Why slow/risky:** Twilio API calls (typically 2-5s each) are synchronous and blocking. If dispatcher sends 20 confirmations for next day, that's 40-100s of blocking in a single worker. Gunicorn timeout = 60s → easy failure.
- **Fix:** Batch SMS sends into background task (Celery) or ThreadPoolExecutor. Return 202 Accepted and redirect dispatcher to a polling-status page. send_confirmations_for_date is already a good candidate for async (line 465).
- **Expected impact:** Dispatcher clicks 'Send All Confirmations' and page hangs for 1-2 minutes. If any SMS send fails, entire operation stalls.
- **How to test:** Send 10 confirmations via SMS. Measure response time: should be <2s (submit + return task_id), not >30s (actual sends). Monitor Twilio logs.
- **Verification:** (confirmed) Confirmed at dispatching/confirmation_sms.py line 454: client.messages.create() inside send_confirmation_via_twilio() is a blocking Twilio API call (timeout not visible but typically 30s default). Called sequentially at line 488 inside send_confirmations_for_date loop (lines 478-497), with leg.save() at line 493 after each send. Multiple SMS sends will block the single worker for 30s+ per SMS.

#### DISP-05 — High
- **File / symbol:** `dispatching/analytics.py` :: `calculate_route_timing_metrics`  (lines 821-834)
- **Issue type:** unbounded-queryset  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Line 823-834: for leg in legs_queryset: ... categorize_location(leg.pickup_location) called per-leg. No date bounds on the queryset (line 812-817). If called on all-time legs (100k rows), 100k loop iterations, each calling is_airport_location (cached) but processing is still O(n).
- **Why slow/risky:** When legacy /admin or old code path requests metrics without date filter, queryset loads ALL completed legs ever. With ~5 years of data, that could be 100k+ rows. Python iteration of 100k objects is slow (~500ms), and if DEBUG=True, connection.queries grows unbounded.
- **Fix:** Add default recent_days constraint (e.g., default to last 90 days if not specified). Document that callers MUST pass date-filtered queryset or recent_days parameter. Add assertion to catch unfiltered querysets.
- **Expected impact:** Metrics page (if called without date filter) loads slowly (500ms+) due to Python iteration overhead and memory usage (100k leg objects). DEBUG=True makes it worse (unbounded query log).
- **How to test:** Call calculate_route_timing_metrics with no legs_queryset. Monitor query count: should be 1 (the select), not 100k. Measure time: should be <100ms, not 500ms+.
- **Verification:** (confirmed) Confirmed at dispatching/analytics.py lines 812-817: Leg.objects.filter() has NO date-range constraint, only status='completed', driver type/exclusion filters. Queryset is unbounded by time. Lines 823-834 then iterate ALL matching legs in-memory calling categorize_location() and other functions per-leg. If 100k legs exist, this is O(n) iteration with no pagination/slicing. Severity is High due to unbounded memory footprint and iteration cost.

#### DISP-08 — High
- **File / symbol:** `dispatching/flight_verify_views.py` :: `flight_verification_check`  (lines 434-444)
- **Issue type:** sync-external-API  |  **Fix risk:** Medium
- **Evidence:** Line 435-438: `data = aeroapi.get_flight_data(flight_ident, flight_date=flight_date_iso, trip_type=aero_trip_type)` called synchronously inside an AJAX endpoint. User's browser is waiting.
- **Why slow/risky:** Guest submits the verification form, JS calls /flight_verification_check (POST), which synchronously calls AeroAPI. If AeroAPI is slow (10s), guest sees a frozen form for 10 seconds.
- **Fix:** Return 202 Accepted with a task_id, poll status from JS. Or cache results for the same flight+date combo (30-min TTL) to avoid re-fetching.
- **Expected impact:** Guest's browser hangs during form submission. Poor UX. If AeroAPI times out, form submission fails and guest must retry.
- **How to test:** Throttle network to 1 Mbps. Submit verification check form. Verify page responds <1s with 'checking' spinner. Monitor AeroAPI call latency.
- **Verification:** (confirmed) Confirmed at dispatching/flight_verify_views.py lines 434-438: flight_verification_check AJAX view calls aeroapi.get_flight_data() synchronously. This makes a blocking requests.Session.get() with 10s timeout (aeroapi_service.py line 389). User's browser is waiting for the response. No caching layer observed (no cache.get/set calls in this view file).

#### DISP-11 — High
- **File / symbol:** `dispatching/views.py` :: `_run_bulk_flight_refresh`  (lines 4720-4723)
- **Issue type:** sync-external-API  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Lines 4720-4723: ThreadPoolExecutor batches correctly, but each _refresh_single_flight (line 4456) creates a NEW AeroAPIService() instance per thread. This opens a new requests.Session() per thread (5 sessions for 5 workers).
- **Why slow/risky:** Good: batching prevents 1 worker from being blocked by N sequential API calls. But creating N Session objects is wasteful. Should reuse a shared session or connection pool.
- **Fix:** Create a single AeroAPIService (with session) before the thread pool, pass it to worker threads. Or use thread-local storage for shared session.
- **Expected impact:** With 5 concurrent AeroAPI calls, 5 session handshakes happen (TLS, TCP) instead of 1. Adds ~500ms to bulk refresh. Not critical but measurable.
- **How to test:** Bulk refresh 5 flights. Monitor network: should see 1 TLS handshake (connection pooling), not 5.
- **Verification:** (confirmed) Confirmed at dispatching/views.py line 4456: _refresh_single_flight (worker function for ThreadPoolExecutor) creates a NEW AeroAPIService() per call. Each instantiation at line 33-36 of aeroapi_service.py creates a new requests.Session(). ThreadPoolExecutor at line 4721 uses BATCH_SIZE=5 workers, so 5 sessions created per batch. This is inefficient (connection pooling lost) but not a blocking issue like sequential calls. Severity is correctly High for architectural inefficiency in a resource-constrained environment.

#### DISP-12 — High
- **File / symbol:** `dispatching/confirmation_sms.py` :: `send_confirmations_for_date`  (lines 478-497)
- **Issue type:** sync-external-API  |  **Fix risk:** Medium
- **Evidence:** Line 478-497: `for leg in legs: ... ok, err = send_confirmation_via_twilio(leg, row, message)` and immediately `leg.save(update_fields=[...])` after each send. Sequential loop with save after each Twilio call.
- **Why slow/risky:** 20 confirmations = 20 Twilio API calls (2-5s each) + 20 DB saves = 40-100s blocking in a single worker. Each failed SMS causes the whole function to continue but the worker is still locked.
- **Fix:** Batch into ThreadPoolExecutor (5-10 workers), collect results, do bulk_update() after all sends. Or use Celery with exponential backoff on failures.
- **Expected impact:** Dispatcher clicks 'Send Confirmations' for 20 legs and Gunicorn hangs for 1-2 minutes. During this time, all other users see 503 errors (worker is blocked).
- **How to test:** Send 20 confirmations. Measure total time: should be <10s (batched), not >60s (sequential).
- **Verification:** (confirmed) Confirmed at dispatching/confirmation_sms.py lines 478-497: for leg in legs loop (line 478) calls send_confirmation_via_twilio() at line 488 (blocking, no explicit timeout but Twilio client defaults to ~30s), then leg.save() at line 493 after each individual send. Sequential: send 1 SMS (30s block) -> save -> send 2 SMS (30s block) -> save. Date with 20 legs = 10+ minutes of worker blocking. Single worker blocks entire site.

#### DISP-15 — High
- **File / symbol:** `dispatching/aeroapi_service.py` :: `get_flight_info, get_scheduled_flight`  (lines 389, 132)
- **Issue type:** sync-external-API  |  **Fix risk:** High
- **Evidence:** Line 389: `response = self.session.get(url, timeout=10)` and line 132: `response = self.session.get(url, params=params, timeout=10)`. Both are synchronous requests with 10-second timeout.
- **Why slow/risky:** AeroAPI is a remote HTTP API. Network latency + server processing = 1-5s typical, 10s timeout = rare but possible. In a single-worker Django, a 10s request blocks all other users for 10s. This is inherited by all callers.
- **Fix:** Cannot fix here without Celery or async framework. This is architectural—root cause is single-worker Gunicorn. Recommend adding Celery+Redis or migrating to async views.
- **Expected impact:** High. Any AeroAPI slowness cascades through the entire application. Single worker cannot serve other users during API wait.
- **How to test:** Add artificial delay to AeroAPI mock (simulate 5s latency). Verify Gunicorn logs show only 1 request being processed during the delay (blocking).
- **Verification:** (confirmed) Confirmed at dispatching/aeroapi_service.py line 389: response = self.session.get(url, timeout=10) and line 132: response = self.session.get(url, params=params, timeout=10). Both are synchronous requests.Session calls. These are the ROOT of blocking issues across DISP-01, DISP-02, DISP-04, DISP-08 findings. Architectural issue: no Celery/async framework exists. Cannot be fixed in isolation — requires Celery+Redis or async views refactor.

#### DISP-06 — Medium
- **File / symbol:** `dispatching/analytics.py` :: `calculate_demand_pattern_for_hour`  (lines 963-967)
- **Issue type:** query-in-loop  |  **Fix risk:** Low
- **Evidence:** Line 963: `legs = list(Leg.objects.filter(...).select_related(...))`. Called from update_demand_patterns (line 1179) in a loop: `for hour in range(24): demand_data = calculate_demand_pattern_for_hour(...)`. That's 24 DB queries (one per hour) instead of one bulk query.
- **Why slow/risky:** For a 7-day update, this is 7 × 24 = 168 DB queries just to fetch legs by hour. A single query with annotation grouping by hour would cost 1 query.
- **Fix:** Fetch all legs for the date range once, annotate with hour via Extract('hour', 'pickup_time'), then group in Python. Or use bulk create/update with F() expressions in the DB.
- **Expected impact:** Demand pattern refresh takes 10+ seconds (168 queries × 60ms avg). If run during peak traffic, delays are visible on the dashboard.
- **How to test:** Call update_demand_patterns('2026-01-01', '2026-01-07'). Monitor query count: should be ~2 (legs + bulk upsert), not 168.

#### DISP-07 — Medium
- **File / symbol:** `dispatching/management/commands/dispatch_alerts.py` :: `handle (dispatch_alerts command)`  (lines 98-111)
- **Issue type:** polling  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Line 98-111: Fetches today's legs with select_related (good). If run every 5-10 min as comment suggests, that's 144-288 requests/day. Each request fetches all today's legs. This is heavy load on a single worker.
- **Why slow/risky:** Frequent polling of today's legs from the Gunicorn worker adds cumulative DB and CPU load. During peak hours, 288 requests/day × 50ms per request = 14 extra seconds of worker time blocked.
- **Fix:** Move to external cron job (Windows Task Scheduler already set up per comment). Or cache today's legs (15-min TTL) to reduce re-fetches.
- **Expected impact:** If dispatch_alerts command runs every 5 min from the worker, that's 288 leg queries/day. During peak hours, this adds 50ms per alert run to Gunicorn's latency.
- **How to test:** Run dispatch_alerts every minute for 60 minutes. Monitor Gunicorn DB query rate: should spike during alerts, not double baseline.

#### DISP-09 — Medium
- **File / symbol:** `dispatching/views.py` :: `refresh_flight_data`  (lines 3693)
- **Issue type:** unbounded-queryset  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Line 3693: `for lf in leg.legflight_set.select_related('flight').all()`. No limit on number of LegFlight records fetched. If a leg has 100+ secondary flights (edge case but possible), this loads all 100+ into memory.
- **Why slow/risky:** LegFlight is many-to-many coupling. If data is corrupt or abuse, a single leg could have thousands of flights. Loading all of them is wasteful.
- **Fix:** Add `.limit(10)` to the LegFlight query with a warning log if limit is hit. Or use `only('flight_id')` projection to reduce memory.
- **Expected impact:** Rare edge case. Normal reservations have 1-2 secondary flights. But if a reservation has 100+, refresh becomes slow and memory-heavy.
- **How to test:** Create a test leg with 50 LegFlight records. Call refresh_flight_data. Verify memory usage is reasonable and query time <5s.

#### DISP-10 — Medium
- **File / symbol:** `dispatching/views.py` :: `refresh_all_flights`  (lines 4868)
- **Issue type:** query-in-loop  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Line 4868: `arrival_legs = [leg for leg in legs if leg.get_trip_type() == 'arrival']`. Filters in Python. get_trip_type() is a computed property (multiple field checks). This loops over every leg and evaluates the property.
- **Why slow/risky:** O(n) Python filter on a computed property instead of SQL filter. For 100 legs, that's 100 property evaluations. Not critical, but wasteful.
- **Fix:** Move the trip_type filter into the queryset using Q() filters on pickup/dropoff locations. Or add a trip_type field to Leg model (denormalized).
- **Expected impact:** Bulk refresh is slightly slower for large leg counts (e.g., 100 legs for a full day). ~50ms slower vs SQL filter.
- **How to test:** Call refresh_all_flights with 100 legs. Measure time: should be <100ms to filter + start threads, not >200ms.

#### DISP-13 — Medium
- **File / symbol:** `dispatching/analytics.py` :: `update_daily_capacity_metrics`  (lines 1135-1156)
- **Issue type:** N+1  |  **Fix risk:** Low
- **Evidence:** Lines 1135-1156: Nested loop `for driver in drivers: for current_date in date_range:`. For each driver and date, calls calculate_driver_daily_capacity_for_date (which fetches legs for that driver+date from DB). If 50 drivers × 7 days = 350 DB queries (one per driver-date combo).
- **Why slow/risky:** Could bulk-fetch all legs for all drivers in the date range (1 query), then group in Python. Current approach is N+1.
- **Fix:** Fetch all legs for date range and driver set once with select_related/prefetch. Group by (driver_id, pickup_date) in Python. Single query instead of 350.
- **Expected impact:** Capacity metrics update takes 20+ seconds (350 queries). If this runs during peak hours, adds latency to the dashboard.
- **How to test:** Update capacity metrics for 50 drivers × 7 days. Monitor query count: should be ~2 (bulk fetch + bulk upsert), not 350.

#### DISP-14 — Low
- **File / symbol:** `dispatching/views.py` :: `_run_bulk_flight_refresh (cache management)`  (lines 4704-4739)
- **Issue type:** polling  |  **Fix risk:** Low
- **Evidence:** Lines 4704-4739: Function updates cache 6+ times during a ~10s bulk refresh. Cache key expires after 60 * 60 seconds (1 hour).
- **Why slow/risky:** Cache is being over-written frequently, but timeout is very long. If a task fails or hangs, cache entry persists for 1 hour, confusing future poll attempts.
- **Fix:** Reduce timeout to 5-10 minutes. Or use a timestamp-based cache eviction (check started_at + 5min).
- **Expected impact:** If bulk refresh is manually re-triggered before the 1-hour cache expires, old task status might resurface. Low probability but confusing for operators.
- **How to test:** Bulk refresh a set of legs. Wait 1 hour. Call refresh_all_flights again with same leg_ids. Verify old task status is not returned.

---

### ops app (views, kpis, leads_board, middleware, escalation, unpaid_reminders, signals)

#### OPS-02 — Critical
- **File / symbol:** `ops/views.py` :: `_build_driver_conflict_context`  (lines 753-767)
- **Issue type:** sync-external-API  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** Line 862: live_drive = google_drive_time(leg.pickup_location, leg.dropoff_location) is called in a loop over day_legs (line 803-919). google_drive_time is a synchronous external API call (Google Maps) that makes a blocking HTTPS request per leg.
- **Why slow/risky:** For each leg in the schedule (loop at 803), google_drive_time() blocks waiting for Google Maps API response (~200-500ms per request). With 15 legs, that's 3-7.5 seconds of blocking I/O on the single gunicorn worker. All other users are starved.
- **Fix:** Batch Google Maps requests: collect all (pickup_location, dropoff_location) pairs before the loop, cache/batch the API calls, or use a fallback-first pattern (historical P75 first, then async Google update in background).
- **Expected impact:** Eliminates 5-10+ seconds of blocking I/O per conflict task detail load. Site remains responsive under concurrent load.
- **How to test:** Load conflict task detail with 10+ active legs and measure response time. Should drop from 10+ seconds to <2 seconds.
- **Verification:** (confirmed) At line 861, google_drive_time() is called in a loop over day_legs (line 803-919). Again at line 926, google_drive_time() is called in a loop over schedule pairs (lines 922-938). And again at line 966 for conflicting legs. google_drive_time imports from drivers.utils at line 855, and that function (lines 11-64 in drivers/utils.py) makes a synchronous requests.get() call to Google Distance Matrix API with 5-second timeout. No request-local caching. Each leg = 1+ HTTP request to Google. With day_legs potentially 10-20 legs per driver, this could mean 30+ blocking API calls in a single task detail view.

#### OPS-11 — Critical
- **File / symbol:** `ops/views.py` :: `_build_driver_conflict_context`  (lines 861-938)
- **Issue type:** sync-external-API  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** Line 861-869: for leg in day_legs loop calls google_drive_time(leg.pickup_location, leg.dropoff_location). Then again line 926-938: for i in range(len(schedule) - 1) calls google_drive_time() for inter-leg transit. Synchronous Google Maps API calls in tight loops, no caching, no batching. 29+ API calls per conflict task.
- **Why slow/risky:** Each google_drive_time() is blocking HTTPS (~300-500ms). With 15 legs + 14 transitions = 29 calls per conflict task. That's 8-15 seconds of pure blocking I/O. Single worker freezes.
- **Fix:** Implement request-local caching (within one task_detail_view): cache results by (pickup_location, dropoff_location) pair. OR use shared Redis cache with 1-hour TTL. OR batch requests (Google Distance Matrix API accepts multiple origins/destinations).
- **Expected impact:** Estimated 8-15 seconds of blocking I/O eliminated per conflict task detail load.
- **How to test:** Load conflict task detail with 15+ active legs and measure response time with/without caching. Should drop from 10-20s to <2s.
- **Verification:** (confirmed) Lines 861-869, 926-938 (and 966): google_drive_time() is called repeatedly in multiple loops within _build_driver_conflict_context. Line 861 calls it once per leg in day_legs. Line 926 calls it N-1 times for inter-leg transits. Line 966 calls it for conflicting legs. No request-local caching. Each call hits Google Distance Matrix API synchronously. With 10-20 legs per driver conflict task, this could result in 30+ synchronous Google API calls in a single page load.

#### OPS-01 — High
- **File / symbol:** `ops/views.py` :: `_build_driver_conflict_context`  (lines 883-886)
- **Issue type:** N+1  |  **Fix risk:** Low
- **Evidence:** for i in range(len(schedule) - 1): ... schedule.append({...}) where inside loop leg.status_history.all() is called. Iterates QuerySet without pagination, then inside another loop at line 883-886: matching = [sh.timestamp for sh in leg.status_history.all() if sh.status == leg_status]. status_history is NOT prefetched on line 765.
- **Why slow/risky:** For each leg in day_legs (potentially 10-20+ legs on a busy day), .all() spawns a fresh DB query to fetch status_history. On a single worker, each query blocks the entire site. With 15 legs, that's 15+ extra queries per task_detail_view render.
- **Fix:** Add 'status_history' to the prefetch_related on line 765 when querying day_legs. Change line 765 from .prefetch_related('status_history') to ensure status_history is prefetched along with flight_information, reservation, etc.
- **Expected impact:** Eliminates 10-20 queries per task detail render for driver_conflict tasks. On a 1-worker production system, this unblocks the queue for concurrent users.
- **How to test:** Load /dispatching/task_detail/[conflict_task_id] with 15+ active driver legs and measure query count. Expected: 1 query (the prefetch) instead of N+1.
- **Verification:** (confirmed) Line 765 prefetches 'status_history', but this is used only on day_legs. However, at lines 883-886, there's a list comprehension: matching = [sh.timestamp for sh in leg.status_history.all() if sh.status == leg_status]. The .all() call here will hit the database AGAIN because the prefetch_related is being overridden by the .all() call. The correct pattern is to use the prefetched relation directly: [sh.timestamp for sh in leg.status_history.all() if sh.status == leg_status] should be [sh.timestamp for sh in leg.status_history if sh.status == leg_status] to use the cached prefetched data. This is a common prefetch_related pitfall. The current code does prefetch but then explicitly calls .all() which bypasses the prefetch and re-queries the DB.

#### OPS-03 — High
- **File / symbol:** `ops/leads_board.py` :: `lead_board_detail`  (lines 274-290)
- **Issue type:** query-in-loop  |  **Fix risk:** Low
- **Evidence:** Lines 274-276: for a in lead.activities.all(): and 284-290: for t in lead.follow_up_tasks.filter(...): are two separate .all()/.filter() calls that iterate collections without being prefetched on line 270 (select_related('vehicle') only).
- **Why slow/risky:** This is a JSON endpoint (JsonResponse) that renders lead timeline. Each call spawns 2 fresh queries for activities and follow_up_tasks. On the single worker, with concurrent leads board detail requests, this compounds rapidly.
- **Fix:** Add .prefetch_related('activities', 'follow_up_tasks') to the Lead query on line 270.
- **Expected impact:** Eliminates 2 queries per lead detail request. On busy leads board, reduces query count by 50%.
- **How to test:** Load leads_board_detail view 10 times concurrently and measure query count. Expected: 1 query instead of 3 per request.
- **Verification:** (confirmed) Line 270 has select_related('vehicle') only. Lines 274-276 iterate lead.activities.all() and lines 284-290 iterate lead.follow_up_tasks.filter(...). Neither 'activities' nor 'follow_up_tasks' are prefetch_related, so each iteration in those loops will trigger a separate database query. The .all() and .filter() calls are evaluated within the loops (lines 274, 284), not prefetched.

#### OPS-06 — High
- **File / symbol:** `ops/views.py` :: `task_queue_view`  (lines 93-136)
- **Issue type:** unbounded-queryset  |  **Fix risk:** High  |  *Needs measurement*
- **Evidence:** Line 136: all_open = list(base_qs). base_qs filters on open_statuses with .select_related() but NO LIMIT. On a busy day with 1000+ open tasks, this materializes the entire table into memory. Lines 142-178 partition it in Python (O(N) iteration). No pagination.
- **Why slow/risky:** A single user loads /dispatching/task_queue with 1000+ open tasks. This materializes entire queryset into memory and partitions in Python. On a 1-worker system with 512MB RAM, this is a memory spike. Concurrent hits compound memory usage.
- **Fix:** Implement pagination: fetch first 100 open tasks, show lane counts (from aggregate), lazy-load more via AJAX. OR use QuerySet slicing with offset/limit per lane.
- **Expected impact:** Reduces memory footprint by 90%. Improves first paint time (eliminates 1000+ task sort/partition latency).
- **How to test:** Load /dispatching/task_queue with 1000+ open tasks and measure memory & response time. Should drop from 10+ seconds to <1s with pagination.
- **Verification:** (confirmed) Line 136 materializes all open tasks into a Python list with no pagination: all_open = list(base_qs). The base_qs filters on open_statuses with select_related() but has NO LIMIT or slicing. Lines 142-178 then partition this list in Python in a loop over all tasks. On a busy day with 1000+ open tasks, this loads the entire dataset into memory and iterates it multiple times (once per lane classification logic). No pagination or lazy loading.

#### OPS-08 — High
- **File / symbol:** `ops/unpaid_reminders.py` :: `UnpaidReminderEngine._build_duplicate_cache`  (lines 459-465)
- **Issue type:** unbounded-queryset  |  **Fix risk:** High  |  *Needs measurement*
- **Evidence:** Lines 459-465: reservations = Reservation.objects.filter(legs__pickup_date__gte=cutoff).exclude(status='cancelled').select_related('customer').prefetch_related('legs').distinct(). Scans 90 days of reservations (up to 10,000+ rows). Then lines 468-486 iterate and build groupby dict in Python. Called EVERY scheduler cycle (every 30 min).
- **Why slow/risky:** Unbounded queryset inside reminder engine. On every 30-minute scheduler cycle, entire 90-day reservation history fetched and grouped in memory. With 10,000 reservations, 10,000 Leg prefetches + Python iteration stalls queue for 5-10 seconds.
- **Fix:** Implement incremental duplicate cache: store (last_name, phone_last10, pickup_date) tuples in Redis, update on new reservations via signal, query cache instead of full scan.
- **Expected impact:** Eliminates 5-10 second stall every 30 minutes. Estimated 99.9% uptime improvement.
- **How to test:** Run scheduler with 10,000 live reservations. Measure UnpaidReminderEngine.process() time. Should drop from 10s to <100ms.
- **Verification:** (confirmed) Lines 459-465 in unpaid_reminders.py: reservations = Reservation.objects.filter(legs__pickup_date__gte=cutoff).exclude(status='cancelled').select_related('customer').prefetch_related('legs').distinct(). This query has NO LIMIT and scans 90 days of reservations. Then lines 468-486 build a groupby dict in Python by iterating all results. Called every 30 min by the scheduler. No pagination or incremental caching.

#### OPS-04 — Medium
- **File / symbol:** `ops/views.py` :: `task_detail_view`  (lines 2001-2023)
- **Issue type:** N+1  |  **Fix risk:** Low
- **Evidence:** Line 2022: comm_attempts = task.comm_attempts.select_related('staff_user').order_by('-created_at') is queried separately without being prefetched on the OperationalTask query (lines 2001-2020). Task is prefetched with reservation__payments but not comm_attempts.
- **Why slow/risky:** On every payment_chase task detail view load, comm_attempts spawns a fresh query. With 10+ payment_chase tasks open, that's 10 extra queries per staff refresh cycle.
- **Fix:** Add .prefetch_related('comm_attempts') to the OperationalTask query on line 2001-2020.
- **Expected impact:** Eliminates 1 query per task detail load (~50 queries/day on busy queue).
- **How to test:** Load payment_chase task detail and verify comm_attempts are prefetched (no new DB query after task fetch).

#### OPS-05 — Medium — **REFUTED**
- **File / symbol:** `ops/middleware.py` :: `StaffActivityMiddleware.__call__`  (lines 62-72)
- **Issue type:** sync-external-API  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** Lines 67-72: StaffActivity.objects.create(...) is called on EVERY GET request from an authenticated staff user (after 30-minute dedup cache hit). This writes to the DB synchronously and triggers simple_history (historical row write). On a busy dispatching day with 10 concurrent staff, this is 10+ DB writes per 30 minutes = 5+ writes/minute on the single worker.
- **Why slow/risky:** Write amplification on single-worker production. simple_history adds a second write (history row) per .create(). On the single gunicorn worker, each write blocks all other requests. Even 5 writes/minute compound into noticeable latency spikes during peak hours.
- **Fix:** Move StaffActivity.objects.create() to a background task (Celery or daemon thread pool). OR batch write using bulk_create (every N requests). Current DEDUP_SECONDS=1800 (30 min) is good, but the write should be async.
- **Expected impact:** Eliminates synchronous write latency on every 30-minute page view. Estimated 50-100ms per page render saved on busy days.
- **How to test:** Load /dispatching/ endpoint with 10 concurrent users and measure response time variance. Should see <100ms variance with async writes vs. current spikes.
- **Verification:** (refuted) The finding states that StaffActivity.objects.create() is called on EVERY GET request and is synchronous, creating a bottleneck. However, reading lines 67-72 in middleware.py, the .create() call IS synchronous, BUT the cache check on lines 56-60 prevents duplicate writes with DEDUP_SECONDS=1800 (30 min). More importantly, send_dispatch_alert_notification() is called in escalation.py line 103, and when traced to reservations/utils.py line 701, it explicitly calls _run_in_background(_do_send), which spawns a daemon thread. The NTFY call itself is async. The StaffActivity.create() is a simple DB insert (not blocking external API), and with 30-min dedup, it's only ~2 writes per staff user per 30 min max, not 10+ writes per 30 min. The actual bottleneck is less severe than stated, though the StaffActivity write is still synchronous.

#### OPS-07 — Medium
- **File / symbol:** `ops/leads_board.py` :: `leads_board_view`  (lines 131-135)
- **Issue type:** unbounded-queryset  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** Lines 131-135: leads = list(Lead.objects.filter(pickup_date=target).select_related('vehicle').order_by('-priority', '-created_at')). No LIMIT. On a day with 500+ leads, entire list materializes and is partitioned in Python (line 144-148).
- **Why slow/risky:** Leads board on a busy pickup date (e.g., weekend with 500+ leads) loads and partitions all leads in Python. Inefficient for large lead counts.
- **Fix:** Add pagination (fetch top 200 leads by priority, lazy-load rest) or implement AJAX-based lazy loading for bucket expansion.
- **Expected impact:** Reduces memory footprint and initial page load latency on high-volume days.
- **How to test:** Load leads_board_view with pickup_date=busy_day (500+ leads) and measure response time. Should stay <1s with pagination.

#### OPS-09 — Medium (orig High -> Medium)
- **File / symbol:** `ops/escalation.py` :: `run_escalations`  (lines 26-63)
- **Issue type:** sync-external-API  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** Lines 34-35: for task in tasks_to_escalate: _escalate_task(task) calls _send_escalation_ntfy(task) which on line 103 calls send_dispatch_alert_notification() (synchronous NTFY API call, blocking HTTPS). Loop over up to 50 tasks.
- **Why slow/risky:** For each escalated task, send_dispatch_alert_notification() makes a blocking HTTPS request (~200-500ms). With 50 escalated tasks per cycle, that's 10-25 seconds of blocking I/O on single worker.
- **Fix:** Queue send_dispatch_alert_notification() calls asynchronously (background task or batched NTFY call).
- **Expected impact:** Eliminates 10-25 seconds of blocking I/O every 30 minutes during escalation cycles.
- **How to test:** Simulate 50 escalations and measure run_escalations() time with/without async. Should drop from 15s to <1s.
- **Verification:** (confirmed) Lines 26-35 in escalation.py: tasks_to_escalate query returns all matching tasks (no limit), then line 34-35 loops and calls _escalate_task(task) which calls _send_escalation_ntfy(task) at line 63. However, tracing to reservations/utils.py line 701, send_dispatch_alert_notification uses _run_in_background() to queue the NTFY HTTP call asynchronously. So while the pattern is loop-based, the actual external API call (NTFY) is backgrounded via daemon thread. The severity is lower than stated because the blocking HTTP call is already async.

#### OPS-10 — Low
- **File / symbol:** `ops/views.py` :: `staff_kpis_view`  (lines 2116-2118)
- **Issue type:** repeated-count  |  **Fix risk:** Low
- **Evidence:** Lines 2116-2118: overdue_count = open_tasks.filter(due_at__lt=now).count() and escalated_count = open_tasks.filter(status='escalated').count(). Two separate COUNT queries on the same queryset.
- **Why slow/risky:** Multiple .count() calls spawn separate DB queries. On each KPI view load, 2 COUNT queries issued.
- **Fix:** Combine into single aggregate: open_tasks.aggregate(overdue=Count('id', filter=Q(due_at__lt=now)), escalated=Count('id', filter=Q(status='escalated'))).
- **Expected impact:** Reduces KPI page query count by 1 per view load.
- **How to test:** Load staff_kpis_view and verify query count drops by 1.

#### OPS-12 — Low — **REFUTED**
- **File / symbol:** `ops/views.py` :: `_build_driver_assign_context`  (lines 1703-1710)
- **Issue type:** N+1  |  **Fix risk:** Low
- **Evidence:** Line 1703-1710: all_driver_legs = list(Leg.objects.filter(...).select_related('driver', 'reservation', 'reservation__customer')). Does NOT prefetch reservation__customer on line 1710. If template later accesses leg.reservation.customer.get_full_name(), spawns N+1 queries (one per leg).
- **Why slow/risky:** Leg query loads legs with reservation but NOT reservation.customer. Template iteration triggers customer query per leg. With 50 legs, that's 50 extra queries.
- **Fix:** Add .select_related('reservation__customer') to the Leg query on line 1704-1710.
- **Expected impact:** Eliminates up to 50 customer queries (one per leg) when rendering driver assign task detail.
- **How to test:** Load driver_assign task detail with 50 active legs and verify no additional customer queries issued after Leg fetch.
- **Verification:** (refuted) Lines 1704-1710 show: Leg.objects.filter(...).select_related('driver', 'reservation', 'reservation__customer'). The select_related DOES include 'reservation__customer' on line 1710. The finding claims it does NOT prefetch 'reservation__customer', but it explicitly does via the select_related() call. If the template accesses leg.reservation.customer.get_full_name(), it will use the cached select_related result, not trigger an N+1 query.

#### OPS-13 — Low
- **File / symbol:** `ops/views.py` :: `staff_metrics_view`  (lines 2269-2270)
- **Issue type:** repeated-count  |  **Fix risk:** Low
- **Evidence:** Line 2269: completed_in_range = OperationalTask.objects.filter(status='completed', resolved_at__gte=range_start) and line 2270: auto_closed_count = completed_in_range.filter(resolved_by__isnull=True).count(). Two separate queries.
- **Why slow/risky:** Two COUNT queries when one aggregate suffices.
- **Fix:** Use aggregate: completed_in_range.aggregate(total=Count('id'), auto_closed=Count('id', filter=Q(resolved_by__isnull=True))).
- **Expected impact:** Eliminates 1 query per staff_metrics_view load.
- **How to test:** Load staff_metrics_view and verify query count drops by 1.

---

### Reservation model save-paths & signals (correctness-critical audit)

#### RES-01 — High
- **File / symbol:** `reservations/models.py` :: `Reservation.calculate_total_driver_payments`  (lines 455-459)
- **Issue type:** N+1-like pattern  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** return sum(leg.total_driver_pay for leg in self.legs.all())  Iterates all legs in Python and accesses .total_driver_pay on each, then calls sum(). If called in a loop or template, this becomes an N+1.
- **Why slow/risky:** With 1 sync worker and no pagination, a slow page iterating reservations and accessing calculate_total_driver_payments (e.g., in a report or admin changelist) will fire one query per reservation. If 1 worker blocks on 100 legs x 100 reservations, the entire site stalls.
- **Fix:** Use .aggregate(total=Sum('total_driver_pay')) on self.legs.all() instead of sum() in Python. Cache result as a stored field updated on leg save via post_save signal if recalc is expensive.
- **Expected impact:** Blocks worker thread during admin list rendering or bulk operations.
- **How to test:** Admin changelist load time; measure query count with django-debug-toolbar; call calculate_total_driver_payments in a loop of 50 reservations.
- **Verification:** (confirmed) Line 459: `return sum(leg.total_driver_pay for leg in self.legs.all())` confirmed. Fetches all legs then iterates in Python to sum. If called in templates or loops (e.g., template listing), becomes N+1. The severity is High in single-worker production with a 60s timeout per request.

#### RES-03 — High
- **File / symbol:** `reservations/models.py` :: `Reservation.recalculate_leg_revenue_shares`  (lines 481-522)
- **Issue type:** query-in-loop  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** for leg in legs:     Leg.objects.filter(pk=leg.pk).update(revenue_share=share)  # query per leg     profit = leg.calculate_profit()     Leg.objects.filter(pk=leg.pk).update(profit_estimate=profit)  # another query per leg
- **Why slow/risky:** Iterates legs and fires 2 UPDATE queries per leg. A 3-leg reservation fires 6 queries sequentially. If called from post_save (line 511 already does this for legs), then a multi-leg reservation save stalls the 1 worker.
- **Fix:** Use bulk_update() to update all legs in a single query. E.g., collect updates, then Leg.objects.bulk_update(legs_to_update, ['revenue_share', 'profit_estimate']).
- **Expected impact:** Multi-leg reservations (round-trips, complex itineraries) take O(legs) extra queries on save.
- **How to test:** Create reservation with 3 legs; measure save() query count. Currently ~6, should be ~2.
- **Verification:** (confirmed) Lines 511 and 514 show per-leg .filter(pk=leg.pk).update() calls in a loop (lines 504-514). Two queries per leg. Lines 518-522 show similar pattern in equal-split case: bulk update at 518, but then loop 519-522 with .filter().update() per leg. High severity: if called on a 5-leg reservation, 10 update queries.

#### RES-04 — High
- **File / symbol:** `reservations/signals.py` :: `update_agent_commission_data`  (lines 63-161)
- **Issue type:** query-in-loop + aggregation  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Reservation.objects.filter(travel_agent=agent, status="confirmed")   .aggregate(total=Sum("effective_commission"))  Then immediately calls the same pattern again for unpaid commissions (lines 140-144). These two queries fire on EVERY Reservation.save() if travel_agent is set.
- **Why slow/risky:** The signal fires post_save; each save of a reservation with a travel_agent triggers two full-table aggregations (one per status). With 1 sync worker, bulk operations or rapid bookings serialize behind these aggregations.
- **Fix:** Move commission recalculation to an explicit update_agent method callable only when status changes (use the _pre_save_old_values already captured to detect). Skip recalc entirely if update_fields is specified and doesn't include 'status' or 'commission_amount' (already done at line 74-77, but the aggregations still run—move them inside the status_changed block at line 97).
- **Expected impact:** Every booking/status change causes 2 full-table aggregations on the Reservation table, blocking the worker.
- **How to test:** Create 10 reservations with a travel_agent; measure query count on last save(). Should drop from 2 aggregations to 0 if status didn't change.
- **Verification:** (confirmed) Lines 126-130 and 140-144 show two aggregate+annotate queries. The auditor claims they fire on EVERY save, but code at line 93-94 checks `if not status_changed and save_update_fields is None: return`. However, the early-return only works if `_pre_save_old_values` is set (line 88). On NEW reservations (created=True), _pre_save_old_values won't exist, so the check fails and queries still run. On existing reservations, the check should work if status didn't change. Severity confirmed High because they run on every new reservation creation + every status change, and the guard is subtle/fragile.

#### RES-05 — High
- **File / symbol:** `reservations/signals.py` :: `auto_convert_lead_on_reservation + _converge_duplicate_leads`  (lines 208-382)
- **Issue type:** query-heavy post_save signal  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** def auto_convert_lead_on_reservation(...):  # post_save   ...   matching_lead = Lead.objects.filter(...).order_by('-created_at').first()  # 1 query   matching_lead.save()  # triggers Lead post_save signal (sync_lead_to_ghl_on_create, sync_lead_status_to_ghl)   ...   _converge_duplicate_leads(primary, reservation)  # queries all duplicate leads  Inside _converge_duplicate_leads (lines 303-382):   twins = Lead.objects.filter(ident).filter(...).exclude(...)   for twin in twins:     twin.save()  # each save triggers GHL sync in a background thread
- **Why slow/risky:** On every Reservation creation, this fires: 1 Lead query + 1-N Lead saves (each spawning a GHL sync thread). If a user submits 2 quotes with different trip_types (both create leads), then books (creates 1 reservation), the signal converges both leads. With 1 sync worker, this single Reservation.save() blocks on querying/updating multiple leads + spawning threads.
- **Fix:** Batch the duplicate lead convergence: collect all twins to update, then use bulk_update() instead of saving each. Use update_fields on save() to skip GHL sync signal recursion.
- **Expected impact:** Each new booking performs redundant lookups and updates on the Lead table; delays checkout completion.
- **How to test:** Create lead, submit duplicate quote, then book. Measure: (1) number of Lead queries, (2) number of threads spawned. Should be <5 queries total.
- **Verification:** (confirmed) Line 253: matching_lead.save() triggers post_save signals. Lines 339-350: loop calling twin.save() on each duplicate lead. Each save is a separate signal invocation. Not bulk_update. In production with duplicate leads, this serializes updates. Severity High for single-worker blocking.

#### RES-06 — High
- **File / symbol:** `reservations/models.py` :: `Leg._assign_route_from_locations`  (lines 1282-1308)
- **Issue type:** missing-index + unbounded-queryset  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** def _assign_route_from_locations(self):   locations = list(Location.objects.all())  # loads ALL locations into memory   origin = self._match_location(self.pickup_location, locations)  # string matching in Python   ...   route = Route.objects.filter(origin=origin, destination=destination).first()
- **Why slow/risky:** Called from Leg.save() (line 1336) on every leg update. Fetches all Location rows into memory and does text matching in Python. If the table grows to 10k+ locations, this becomes slow. No .only() or .defer(), so it fetches every column.
- **Fix:** Query by indexed location fields directly: Route.objects.select_related('origin', 'destination').filter(origin__name__icontains=self.pickup_location, destination__name__icontains=self.dropoff_location).first(). Or move location matching into a database view/search.
- **Expected impact:** Leg saves (driver assignment, status changes) load full Location table into memory, slowing the worker.
- **How to test:** Create leg with 1000 locations in DB; measure Leg.save() time. Should drop from N+2 queries to 1-2.
- **Verification:** (confirmed) Line 1288: `locations = list(Location.objects.all())` fetches entire Location table into memory, then Python string matching in _match_location (lines 1289-1290). If Location table grows, this blocks route assignment. Severity High: blocks reservation creation flow on slow DB reads of large location set.

#### RES-10 — High
- **File / symbol:** `reservations/views.py` :: `reservation_form view`  (lines 149-220)
- **Issue type:** multiple-saves-in-request  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** customer = customer_form.save()  # 1 save reservation = reservation_form.save(...)  # 2 saves (triggers post_save signals) reservation.travel_agent = ...  # assigns agent reservation.save()  # 3 saves (triggers post_save signals again) Reservation.objects.filter(pk=...).update(booking_source=...)  # 4 queries leg1.save()  # 5 saves (triggers post_save signals) ... leg2.save()  # 6 saves
- **Why slow/risky:** Multiple save() calls in sequence fire post_save signals each time. reservation.save() at line 188 and again at line 199 both trigger commission recalc, lead conversion, and GHL sync signals.
- **Fix:** Use update_fields to skip unnecessary signal handlers on intermediate saves. E.g., reservation.save(update_fields=['travel_agent']) to skip commission recalc if only assigning agent.
- **Expected impact:** Checkout view fires signals multiple times, delaying response to user.
- **How to test:** Measure query count on POST to reservation_form view. Should be <15 total (not 20+).
- **Verification:** (confirmed) Lines 149 (customer.save), 150 (reservation.save), 188 (reservation.save), 199/206 (reservation.save again if TravelAgent), 217-220 (bulk_update for booking_source), 236 (leg1.save), 253 (leg2.save). Multiple saves without update_fields to skip signal handlers. Each reservation.save() triggers update_agent_commission_data (lines 126-144: two aggregate queries). Severity High: commission recalc queries run on multiple saves in single request.

#### RES-13 — High
- **File / symbol:** `reservations/signals.py` :: `sync_lead_to_ghl_on_create + sync_lead_status_to_ghl`  (lines 385-459, 479-549)
- **Issue type:** daemon-thread-in-signal  |  **Fix risk:** High  |  *Needs measurement*
- **Evidence:** @receiver(post_save, sender=Lead) def sync_lead_to_ghl_on_create(sender, instance, created, **kwargs):   if created:     from threading import Thread     def sync_ghl_in_background():       ... call GoHighLevelService().create_or_update_contact(instance) ...       Lead.objects.filter(id=instance.id).update(...)     thread = Thread(target=sync_ghl_in_background, daemon=True)     thread.start()  Similarly for sync_lead_status_to_ghl (line 548).
- **Why slow/risky:** Raw daemon threads spawned in post_save signals compete for the 1 Gunicorn worker's GIL. If many leads are created/updated, the threads pile up and block each other. With no Celery/Redis, these threads live until the worker exits, accumulating memory and CPU contention.
- **Fix:** Move GHL sync to an explicit async task queue (Celery + Redis) or use a batch worker task scheduled with run_in_background(). If keeping threads, add a thread pool (e.g., ThreadPoolExecutor(max_workers=2)) to limit concurrency.
- **Expected impact:** High load on lead creation/status changes causes GIL contention and slowed request handling for all users.
- **How to test:** Create 100 leads rapidly; monitor worker CPU and response time. Should stay <50% CPU.
- **Verification:** (confirmed) Lines 406-459 (sync_lead_to_ghl_on_create): Thread(target=sync_ghl_in_background, daemon=True).start(). Lines 511-549 (sync_lead_status_to_ghl): similar. Daemon threads spawned on every Lead save without pooling. In production, many concurrent leads could spawn unbounded threads. Severity High: uncontrolled thread spawning, GIL contention, no backpressure.

#### RES-02 — Medium (orig High -> Medium)
- **File / symbol:** `reservations/models.py` :: `Reservation.update_profit_calculations`  (lines 470-479)
- **Issue type:** recalculation-in-method  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** def update_profit_calculations(self):     self.total_driver_payments = self.calculate_total_driver_payments()  # queries all legs     self.profit_estimate = self.calculate_profit()  # queries again     Reservation.objects.filter(pk=self.pk).update(...)
- **Why slow/risky:** Called from Leg.save() post_save signal (line 511-522), this recalcs legs twice per leg save. In a round-trip reservation with 2 legs, a batch update of both legs fires 4 unnecessary queries. With 1 sync worker, this serializes payment updates.
- **Fix:** Defer profit recalculation to an explicit call only when needed (e.g., status change), or use a signal that batches leg updates and recalcs once. Avoid calling in loops.
- **Expected impact:** Slows down leg status updates (driver assignment, completion) by executing unnecessary recalculations.
- **How to test:** Update 2 legs in a transaction; measure query count. Should be 2 leg UPDATEs + 1 aggregate, not 4.
- **Verification:** (confirmed) Lines 474-475 call calculate_total_driver_payments() (queries all legs) then calculate_profit(), then bulk_update(). The method calls are expensive, but I found no evidence this method is itself called in a loop in the codebase. The severity is Medium (potential but not actively exploited in visible code paths).

#### RES-07 — Medium
- **File / symbol:** `reservations/models.py` :: `Leg.intermediate_stops, Leg.has_additional_dropoffs, Leg.has_intermediate_stops`  (lines 1805, 1821, 1827)
- **Issue type:** property-queries-each-access  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** @property def intermediate_stops(self):   return [s for s in self.legstop_set.all() if s.stop_type != 'dropoff']  @property def has_additional_dropoffs(self):   return any(s.stop_type == 'dropoff' for s in self.legstop_set.all())
- **Why slow/risky:** Defined as @property, not @cached_property. If a template accesses leg.intermediate_stops AND leg.has_intermediate_stops in the same render, legstop_set.all() fires twice. If rendering 10 legs, that's 20+ queries.
- **Fix:** Change to @cached_property so the legstop_set.all() result is cached for the lifetime of the instance.
- **Expected impact:** Templates rendering legs with multiple property accesses fire redundant queries.
- **How to test:** Template accessing leg.intermediate_stops + leg.has_intermediate_stops; enable query logging. Should fire 1 query per leg, not 2.

#### RES-08 — Medium
- **File / symbol:** `reservations/models.py` :: `Leg.all_stops`  (lines 1830-1859)
- **Issue type:** property-queries-in-property  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** @property def all_stops(self):   ...   if self.pk is not None:     for stop in self.legstop_set.all():  # queries legstop_set       items.append({...})
- **Why slow/risky:** Each access to leg.all_stops queries legstop_set.all(). If rendered twice in a template, fires twice. Not cached.
- **Fix:** Change to @cached_property.
- **Expected impact:** Template rendering all_stops multiple times fires redundant legstop queries.
- **How to test:** Template accessing leg.all_stops twice; should fire 1 query, not 2.

#### RES-09 — Medium
- **File / symbol:** `reservations/views.py` :: `index view`  (lines 55-87)
- **Issue type:** N+1-in-view-loop  |  **Fix risk:** Low
- **Evidence:** vehicles = Vehicle.objects.prefetch_related(...rates...).all() for v in vehicles:   routes: dict[str, dict] = {}   for r in v.rates.all():  # prefetch_related helps, but loop accesses all()     routes[str(r.id)] = {...}
- **Why slow/risky:** Although prefetch_related is used, calling v.rates.all() inside the loop iterates the prefetched relation. Not a query per vehicle (prefetch handles that), but the loop is inefficient if rates need further filtering.
- **Fix:** Move rate filtering into the prefetch() queryset with filter() to avoid returning all rates.
- **Expected impact:** Landing page loads all rates for all vehicles even if filtering is needed later.
- **How to test:** Landing page load; measure Reservation query count. Should be 1-2 queries, not N+vehicles.

#### RES-11 — Medium
- **File / symbol:** `reservations/forms.py` :: `CustomerForm.save`  (lines 60-75)
- **Issue type:** get_or_create-without-atomic  |  **Fix risk:** Low
- **Evidence:** def save(self, commit=True):   obj, created = Customer.objects.filter(     Q(email=self.instance.email),     Q(phone_number=self.instance.phone_number),   ).get_or_create(     email=self.instance.email,     phone_number=self.instance.phone_number,     ...   )   if not created:     obj.is_returning = True     obj.save()  # extra save on existing customer
- **Why slow/risky:** The filter().get_or_create() pattern is redundant—get_or_create() does the lookup internally. The extra filter() and subsequent if not created: obj.save() adds an unnecessary query and save on repeat customers.
- **Fix:** Replace with direct get_or_create(email=..., phone_number=..., defaults={...}) call. Remove the conditional save().
- **Expected impact:** Repeat customers incur an extra query and save on booking.
- **How to test:** Create reservation for repeat customer; measure query count. Should drop by 1-2.

#### RES-12 — Medium
- **File / symbol:** `reservations/admin.py` :: `LegAdmin.get_queryset`  (lines 1458-1475)
- **Issue type:** subquery-in-queryset  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** def get_queryset(self, request):   qs = super().get_queryset(request).select_related(...).prefetch_related(...)   .annotate(     _reservation_leg_count=Subquery(       Leg.objects.filter(reservation=OuterRef('reservation'))       .values('reservation')       .annotate(cnt=Count('id'))       .values('cnt')[:1]     ),   )
- **Why slow/risky:** The Subquery is used to count legs per reservation. On a 1000-leg changelist, this fires 1 Subquery per row. Efficient at the DB level, but the annotation is rarely used (leg_count in list_display doesn't reference _reservation_leg_count). If the annotation isn't displayed, it's wasted.
- **Fix:** Remove the annotation unless it's used in list_display or filtering. If needed, use .prefetch_related('reservation__legs') and count in Python.
- **Expected impact:** Admin leg list loads slower due to unnecessary subqueries.
- **How to test:** Load LegAdmin changelist; measure query count.

#### RES-15 — Medium
- **File / symbol:** `reservations/signals.py` :: `store_reservation_old_values pre_save`  (lines 621-634)
- **Issue type:** pre-save-queries  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** @receiver(pre_save, sender=Reservation) def store_reservation_old_values(sender, instance, **kwargs):   if instance.pk:     try:       old_instance = Reservation.objects.get(pk=instance.pk)  # query before every save       instance._pre_save_old_values = {...}
- **Why slow/risky:** On every Reservation update, this fires a query to fetch old values. Necessary to detect changes, but if update_fields is specified (for simple updates like status), this is wasteful. The signal already has a guard (line 74-77 in update_agent_commission_data) but other handlers may not.
- **Fix:** Optimize: pass update_fields to the signal and skip the DB query if update_fields only contains non-watched fields.
- **Expected impact:** Every reservation update (even simple status/timestamp changes) fires an extra SELECT query.
- **How to test:** Update reservation status only; measure query count. Should skip the old_instance fetch if update_fields=['status'].

#### RES-14 — Low (orig High -> Low)
- **File / symbol:** `reservations/views.py` :: `lead_quote view POST handler`  (lines 450-478)
- **Issue type:** sync-external-API + daemon-thread-in-request  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** def send_notifications():   try:     send_lead_notification(existing_lead)  # may block on NTFY HTTP request   except Exception as e:     ...   try:     send_lead_event(existing_lead, request, event_id=lead_event_id)  # may block on Meta API   except Exception as e:     ...  thread = Thread(target=send_notifications, daemon=True) thread.start() return JsonResponse({...})
- **Why slow/risky:** Even though the thread is spawned (non-blocking), if the thread encounters a slow external API (NTFY, Meta Conversions API), the thread will be blocked and live until timeout. With 1 sync worker, if multiple requests spawn threads waiting on external APIs, the GIL contention grows. However, the view returns immediately (good), so it's less critical than synchronous calls.
- **Fix:** This is already correctly async (spawns thread, returns immediately). Improve: use a proper async task queue (Celery) or implement exponential backoff + retry for external APIs.
- **Expected impact:** High load on lead submissions may cause thread pool saturation and memory growth.
- **How to test:** Submit 50 leads rapidly; monitor thread count and memory. Should not grow unbounded.
- **Verification:** (confirmed) Lines 447-478: Thread spawned for send_notifications, then return at 481 immediately without waiting. This is CORRECT async behavior. No blocking. send_notifications calls send_lead_notification and send_lead_event which MAY block on HTTP (but in background thread, not blocking the request). Severity Low: properly async, not a blocking problem.

---

### Customer booking & reservation edit flow (reservations/views.py, reservations/forms.py, quote flow, signals)

#### BOOKING-02 — Critical
- **File / symbol:** `reservations/views.py` :: `reservation_form() view, lines 258-266`
- **Issue type:** sync-external-API  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** send_initiate_checkout_event(reservation, request) called synchronously. Traces to conversions.py:113-123 which calls requests.post() to Meta Conversions API (line 59) with 5-second timeout. No threading or async wrapper.
- **Why slow/risky:** If Meta API responds slowly, entire booking response blocked. With 1 sync worker, all users block while waiting. 5-second timeout blocks ~100 concurrent users per 5 seconds.
- **Fix:** Wrap in _run_in_background(send_initiate_checkout_event, reservation, request). Return checkout response immediately; let event send async in daemon thread.
- **Expected impact:** Current: any Meta API latency >100ms stalls booking. Fixed: user gets redirect immediately, event sent async.
- **How to test:** Simulate slow Meta API (5s hang). Measure time to checkout redirect: should be <500ms even if API hangs. Verify event still sends (check logs).
- **Verification:** (confirmed) Code review confirms synchronous blocking call: reservations/views.py line 259 calls send_initiate_checkout_event(reservation, request) without async wrapper or background thread. Function defined at conversions.py:113 calls send_capi_event() at line 123, which performs requests.post(..., timeout=5) at conversions.py:59-62 directly in request thread. With single-worker production, this 5-second HTTP call to Meta Conversions API blocks the entire application for all users. Severity is Critical per stated deployment facts.

#### BOOKING-01 — High
- **File / symbol:** `reservations/views.py` :: `reservation_form() view, lines 186-206`
- **Issue type:** multiple-save-calls  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** reservation.save() called 3 separate times: - Line 188: reservation.save() after initial form save - Line 199: reservation.save() after travel_agent assignment - Line 206: reservation.save() after superuser check Then line 217: Reservation.objects.filter(pk=reservation.pk).update(booking_source=...). Result: 4 separate DB writes for single booking flow.
- **Why slow/risky:** Each .save() is a full database write. With 1 Gunicorn worker, each write blocks all other users. Between 3 saves + update, worker blocked for database latency ~3x, serializing user checkouts.
- **Fix:** Combine into 2 writes max: (1) save reservation with customer, travel_agent, created_by set before first save, (2) single .update() for booking_source. Set all fields before first save instead of multiple saves.
- **Expected impact:** On 1 worker with concurrent bookings, multiple users queue waiting for DB writes. Estimated 500ms-1s latency added per booking.
- **How to test:** Mock 10 concurrent bookings; measure DB query count with DEBUG=True connection.queries. Confirm <=2 writes (currently 4). Verify payment and audit logs correct.
- **Verification:** (confirmed) Code review of reservations/views.py lines 186-220 confirms the pattern: reservation.save() called at line 188 (unconditional), line 199 (if authenticated and travel_agent exists), and line 206 (elif authenticated and superuser). Then Reservation.objects.filter(pk=...).update(...) at line 217 performs a 4th DB write without triggering signals. Additionally, extra_charges() at line 255 calls reservation.save(update_fields=...) at utils.py:588 for a 5th write. The severity is appropriate: in single-worker production, each save triggers post_save signal handlers (update_agent_commission_data at signals.py:63, auto_convert_lead_on_reservation at signals.py:209, reservation_saved at signals.py:22). Multiple saves serialize these expensive operations. The fix recommendation is sound: consolidate to 2 writes max.

#### BOOKING-04 — High
- **File / symbol:** `reservations/views.py` :: `QuoteFormHandlerView.post(), lines 447-478 and 566-592`
- **Issue type:** thread-side-effect  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Two locations spawn daemon threads: Lines 447-478: Thread(target=send_notifications, daemon=True).start() where send_notifications calls send_lead_notification() and send_lead_event() Lines 566-592: identical pattern for new lead path Both functions access DB and make HTTP calls. Threads are daemon=True, can be killed on worker restart. No connection pooling management.
- **Why slow/risky:** Daemon threads can die without finishing. On traffic spikes, many quote submissions = many daemon threads created. May exhaust DB connection pool. Notifications fail silently if thread dies.
- **Fix:** Use _run_in_background() helper (utils.py:48) consistently instead of duplicating Thread logic. Or set up Celery+Redis for proper task queueing. At minimum, reuse the helper function.
- **Expected impact:** Notifications may fail silently. Lead data may not sync to GHL. On high load, connection pool exhaustion possible.
- **How to test:** Simulate 100 quote submissions in 1 second. Monitor: (1) thread count, (2) DB connection pool usage, (3) notification delivery (check logs). Verify no connection timeouts.
- **Verification:** (confirmed) Code review confirms duplicate daemon thread patterns at views.py lines 447-478 and 566-592, both spawning Thread(target=send_notifications, daemon=True).start(). send_lead_notification (called at lines 456, 574) wraps HTTP calls in _run_in_background internally (utils.py:654), so it's double-wrapped unnecessarily. send_lead_event (called at lines 467, 583) calls send_capi_event() which directly executes requests.post(..., timeout=5) synchronously inside the daemon thread (conversions.py:59-62). The pattern duplicates Thread logic twice instead of using _run_in_background consistently. However, since send_notifications runs in a daemon thread, the HTTP calls are backgrounded to some degree, but the risk remains: daemon threads can be killed on worker restart, and there's no connection pooling or graceful shutdown.

#### BOOKING-09 — High — **REFUTED**
- **File / symbol:** `reservations/signals.py` :: `sync_lead_to_ghl_on_create() signal, lines 385-459`
- **Issue type:** sync-external-API  |  **Fix risk:** High  |  *Needs measurement*
- **Evidence:** Post-save signal on Lead creation spawns daemon thread calling: Lines 414-415: sync_lead_to_ghl_without_sms(instance.id) - GHL API call Lines 424-425: GoHighLevelService().create_or_update_contact(instance) - GHL API Lines 434-439: service.update_contact_status_fields(...) - GHL API call Lines 453: GoHighLevelService().apply_lifecycle_tags(...) - GHL API call All in daemon thread, no timeout management, no retry logic beyond logging.
- **Why slow/risky:** If GHL is slow/down, daemon threads accumulate and never complete. With spiky traffic (ads campaign), 1000 quotes could spawn 1000 daemon threads all waiting on GHL, exhausting memory + DB connections. OOM crash possible.
- **Fix:** Use Celery+Redis to queue GHL sync with max_retries. For now: add timeout to HTTP calls inside GoHighLevelService, limit thread pool size, add circuit breaker to fail fast.
- **Expected impact:** Traffic spikes cause OOM or connection exhaustion. System becomes unresponsive.
- **How to test:** Simulate GHL timeout (10s hang). Create 100 leads. Monitor thread count (should stay <20, not 100), memory usage (no spike), DB pool (no exhaustion). Verify recovery after GHL up.
- **Verification:** (refuted) Code review of signals.py:405-459 confirms daemon thread spawning GoHighLevel API calls, BUT the 'no timeout management' claim is REFUTED: ghl_integration/services.py:399 shows requests.post(..., timeout=10) for create_or_update_contact; lines 737 and 764 show requests.get/put with timeout=10 in add_tag method. All HTTP calls have timeouts. HOWEVER, severity remains High (not Critical): daemon threads can be abruptly killed on worker restart without graceful shutdown, and there's no retry logic beyond logging. The signal runs on every new Lead creation (line 394: if created), potentially creating many concurrent background threads competing for the GIL if pickups are booked rapidly. Recommend using Celery+Redis for proper queueing, or at minimum adding connection pooling and a circuit breaker to fail fast on repeated GHL API failures.

#### BOOKING-03 — Medium
- **File / symbol:** `reservations/utils.py` :: `extra_charges() function, lines 539-601`
- **Issue type:** query-in-loop  |  **Fix risk:** Low
- **Evidence:** Loop over legs with .save() per iteration: Lines 539-546: for leg in reservation.legs.all() { leg.save(update_fields=[...]) } Lines 555-561: for leg in reservation.legs.all() { leg.effective_vehicle property access, leg.save(...) } Lines 592-601: for leg in legs { leg.save(update_fields=[...]) } For round-trip: 2 legs = up to 6 separate saves. Property access may trigger query if vehicle not cached.
- **Why slow/risky:** Each .save() is separate UPDATE. For round-trip = 2+ writes to Leg table. Not critical for 2 legs, but inefficient pattern.
- **Fix:** Use bulk updates: Leg.objects.filter(reservation=reservation).update(afterhours_fee=...). Prefetch vehicle to cache property access.
- **Expected impact:** Minimal for single booking (2 legs = 2-6 UPDATEs). Scales poorly if called on batches.
- **How to test:** Create round-trip reservation. Verify legs have correct afterhours_fee and gratuity. Query count: should be 1-2 bulk UPDATEs total.

#### BOOKING-05 — Medium
- **File / symbol:** `reservations/forms.py` :: `ReservationAdminForm.__init__(), lines 345-352`
- **Issue type:** query-heavy-form-init  |  **Fix risk:** Low
- **Evidence:** Admin form fetches all rates with select_related on EVERY instantiation: self.fields['rate'].queryset = Rate.objects.select_related('vehicle', 'route', 'route__origin', 'route__destination') This runs inside __init__, called on every form render or edit. Rate.__str__() would need 4 queries per rate without this optimization.
- **Why slow/risky:** Form re-fetches rates even if user doesn't touch rate field. On admin pages with many edits, adds up. Without select_related, would be 224+ queries for 56 rates.
- **Fix:** Already optimized with select_related. Consider class-level caching or lazy property to avoid re-fetching on every form instantiation. Document the optimization.
- **Expected impact:** Low for single admin page. Medium if many concurrent admin users editing.
- **How to test:** Open reservation edit admin page. Query count should be ~4-5 (not 200+). Verify rate dropdown renders.

#### BOOKING-06 — Medium
- **File / symbol:** `reservations/views.py` :: `reservation_form() view, lines 134-147`
- **Issue type:** unbounded-queryset  |  **Fix risk:** Low
- **Evidence:** Duplicate detection runs 3 queries on same queryset: .exists() (line 142), .count() (line 143), .delete() (line 144). Each re-evaluates the filter.
- **Why slow/risky:** Should fetch once, check length, then delete. Three DB round-trips instead of one.
- **Fix:** Use: count = stale_dupes.delete()[0] which returns (deleted_count, {...}). Or: list(stale_dupes[:1000]); len(list); delete by pk list.
- **Expected impact:** Three queries instead of one. Minimal for typical flow (duplicates rare), but inefficient pattern.
- **How to test:** Create duplicate reservation same email/last_name/date/rate. Verify deletion. Query count: 1 DELETE, not 3 separate queries.

#### BOOKING-07 — Medium (orig High -> Medium)
- **File / symbol:** `reservations/signals.py` :: `update_agent_commission_data() signal, lines 63-162`
- **Issue type:** python-aggregate  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** Post-save signal runs on EVERY Reservation.save(): Lines 126-130: Reservation.objects.filter(travel_agent=agent, status='confirmed').annotate(...).aggregate(...) [pending] Lines 140-144: Reservation.objects.filter(travel_agent=agent, commission_paid=False, status='completed').annotate(...).aggregate(...) [unpaid] These aggregate queries run even if commission fields didn't change (partially mitigated by update_fields check at line 74). Then line 154: agent.save(update_fields=[...]). if you book 10 reservations, that's 10 agent aggregates scanned.
- **Why slow/risky:** Each Reservation.save() triggers signal that scans ALL reservations for agent. If editing multi-leg reservation or making corrections, multiple saves = multiple agent aggregates. Blocks other users.
- **Fix:** Ensure ALL non-commission saves use update_fields (extra_charges at line 588 already does this). Also consider caching agent.pending_commissions in Redis and recomputing async (Celery task).
- **Expected impact:** Current booking flow: extra_charges() calls 3 saves, each triggers agent calc = unnecessary aggregates. 100-300ms latency per booking.
- **How to test:** Disable update_agent_commission_data signal; measure time to complete reservation_form(). Re-enable and compare. Query count should drop if update_fields used throughout.
- **Verification:** (uncertain) Code review of signals.py:63-162 confirms update_agent_commission_data runs post_save on Reservation. Lines 74-77 implement optimization: if update_fields is specified and excludes commission-relevant fields {'status', 'commission_amount', 'commission_paid', 'travel_agent'}, the function returns early. Lines 126-130 and 140-144 run two aggregate queries on FILTERED sets (travel_agent=agent, status/commission_paid conditions), not full table. The aggregates are thus not full-table scans. UNCERTAIN severity: In the reservation_form flow, saves at lines 188, 199, 206 have NO update_fields, so aggregates run (potentially 3 times if all conditions are met). However, the extra_charges() call at line 255 → utils.py:588 saves with update_fields=['additional_charges', 'total_price', 'gratuity_amount', 'special_requests'], which does NOT include commission fields, triggering the early return at line 76. In a single-worker environment where many reservations are created/updated, repeated unfiltered saves would be concerning. Recommend ensuring all non-commission saves use update_fields, as some already do.

#### BOOKING-08 — Medium
- **File / symbol:** `reservations/signals.py` :: `auto_convert_lead_on_reservation() signal, lines 208-301`
- **Issue type:** query-in-signal-multiple  |  **Fix risk:** Low
- **Evidence:** Runs on EVERY new Reservation: Lines 224-228: Lead.objects.filter(email=..., status__in=...).first() Lines 231-237: If no match, Lead.objects.filter(normalized_phone=...).first() Lines 330-336 in _converge_duplicate_leads(): Another filter() query + loop Lines 287-292: LeadActivity.objects.create() writes activity log
- **Why slow/risky:** New reservation triggers 1-2 lead queries + activity log + background threads. Not critical for single booking, but adds to latency.
- **Fix:** Use .first() for early exit (already done). Could cache result for same email+phone in current request. Not urgent.
- **Expected impact:** Minimal per booking (1-2 queries). Matters for bulk operations.
- **How to test:** Create reservation. Verify lead auto-converted. Query count should be 2-3 (2 lead fetches + 1 activity log).

#### BOOKING-10 — Medium
- **File / symbol:** `reservations/templatetags/quote_tags.py` :: `quote_form() template tag, lines 30-45`
- **Issue type:** template-query  |  **Fix risk:** Low
- **Evidence:** Inclusion tag fetches vehicles+rates+locations on EVERY page render: Lines 31-38: Vehicle.objects.prefetch_related(...).all() with nested Prefetch Lines 42-45: Loop through to build locations set Tag used on landing page, guest_quote page, etc. 100 page views/minute = 100 vehicle fetches.
- **Why slow/risky:** Vehicles and rates are STATIC (rarely change). Fetching on every page load wastes DB. Should be cached.
- **Fix:** Use @cache_page(3600) or django.views.decorators.cache. Or move data to JS/JSON cached by browser. Invalidate cache on Vehicle/Rate model change (signals).
- **Expected impact:** Low per page, but cumulative waste over thousands of views.
- **How to test:** Measure query count on index page: should be 0 queries on 2nd+ load if cached.

---

### Payments & Financials (payment/views.py, payment/webhook.py, payment/signals.py, payment/models.py, payment/utils.py)

#### PAY-01 — High (orig Critical -> High)
- **File / symbol:** `payment/webhook.py` :: `stripe_webhook / handle_checkout_session`  (lines 230-235)
- **Issue type:** sync-external-API  |  **Fix risk:** Low
- **Evidence:** threading.Thread(     target=send_purchase_event,     args=(reservation,),     kwargs={"value": None, "event_id": event_id},     daemon=True, ).start()  This spawn happens at webhook processing time, AFTER the database transaction commits. However, the webhook ITSELF spawns a thread to call send_purchase_event (which makes an HTTP POST to Meta CAPI) without using _run_in_background. If that thread hangs or times out, it doesn't block the 200 response—but threads spawned directly consume GIL/worker time.
- **Why slow/risky:** With 1 sync Gunicorn worker, every webhook hit that spawns a daemon thread competes for the Python GIL. The thread calls send_purchase_event -> requests.post (timeout=5) to Meta's graph.facebook.com. If multiple webhooks fire concurrently (several payment completions in flight), each thread holds the GIL while waiting for I/O, blocking other requests entirely. Unlike _run_in_background (which uses a thread pool), naked threading.Thread is unbounded and no pooling.
- **Fix:** Replace threading.Thread(...).start() with _run_in_background(send_purchase_event, reservation, value=None, event_id=event_id) to use the app's existing background task infrastructure (line 10 already imports _run_in_background). This ensures thread pooling and proper lifecycle management.
- **Expected impact:** On high-payment-volume days, multiple daemon threads can exhaust the worker, causing all other requests (reservation views, admin, etc.) to block. Users see timeouts. Stripe will retry the webhook, creating more threads.
- **How to test:** Deploy fix. Monitor: (a) Gunicorn worker thread count during peak payment times (should remain low & stable). (b) Webhook response times (should stay <100ms). (c) Concurrent request latency (should not spike when payments complete).
- **Verification:** (confirmed) threading.Thread(...).start() at lines 230-235 is confirmed. However, the thread spawns AFTER the transaction commits (line 218-220) and AFTER the webhook handler completes, so it doesn't block the 200 response back to Stripe. The severity is High (not Critical) because the thread competes for GIL in the single-worker runtime, but doesn't block the webhook response itself. The import of _run_in_background at line 10 shows a better alternative is available.

#### PAY-02 — High
- **File / symbol:** `payment/views.py` :: `payment_success`  (lines 166-178)
- **Issue type:** sync-external-API  |  **Fix risk:** Low
- **Evidence:** if reservation and purchase_data:     try:         from threading import Thread         from reservations.conversions import send_purchase_event         Thread(             target=send_purchase_event,             kwargs={...},             daemon=True,         ).start()     except Exception as e:         logger.warning(...)  Success page (user-facing view) also spawns a daemon thread to send Meta CAPI event. This blocks the success page render on I/O.
- **Why slow/risky:** payment_success is a synchronous view that renders a success page. Before returning, it spawns a thread to call send_purchase_event, which makes an HTTP call to Meta (timeout=5). If the request hangs, the success page load is blocked. With 1 worker, this blocks all other users.
- **Fix:** Replace threading.Thread(...).start() with _run_in_background(send_purchase_event, ...). The success page returns immediately, and the Meta call happens deferred.
- **Expected impact:** User-visible latency on payment success page. If Meta's API is slow or unreachable, users see a blank/stuck page for up to 5 seconds per request. With 1 worker, other users queue.
- **How to test:** Deploy. Test: (a) load payment success page, measure response time (should be <500ms, not 5s+). (b) Simulate Meta API timeout (mock send_purchase_event to sleep 10s), confirm page still responds fast.
- **Verification:** (confirmed) Thread(...).start() at lines 169-178 in the success page view is confirmed. This spawns synchronously during request processing, blocking page rendering. Severity remains High—this is a user-facing view that blocks on thread creation/start.

#### PAY-03 — High
- **File / symbol:** `payment/utils.py` :: `get_or_create_stripe_customer`  (lines 9-45)
- **Issue type:** sync-external-API  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** if hasattr(customer, "stripe_customer_id") and customer.stripe_customer_id:     try:         stripe_customer = stripe.Customer.retrieve(customer.stripe_customer_id)  # line 15         ...     except stripe.error.StripeError as e:         ...         customer.save()  # line 34 try:     stripe_customer = create_stripe_customer(customer, reservation)  # lines 37-40     customer.stripe_customer_id = stripe_customer.id     customer.save()  # line 40     return stripe_customer  Functions stripe.Customer.retrieve and stripe.Customer.create are called synchronously. Customer.save() triggers historical record creation and model signals.
- **Why slow/risky:** get_or_create_stripe_customer is called in create_checkout_session (views.py line 23) and save_card (line 97), both user-facing views. Each view must first fetch/create a Stripe customer before proceeding—that's a network round-trip to Stripe API (typically 200-500ms). If Stripe is slow, checkout is blocked. With 1 worker, all users queue.
- **Fix:** Cache Stripe customer ID on first creation; add a check-and-reuse pattern with fallback logic. Skip re-retrieve if customer_id exists AND was created <24h ago. If Stripe is slow, consider deferring card setup via _run_in_background. However, user-facing checkout cannot defer—might need a fallback.
- **Expected impact:** Checkout latency directly tied to Stripe API response time. On busy days or if Stripe has outages, checkout pages hang. Customers see spinners/timeouts.
- **How to test:** Deploy cache logic. Measure: (a) Checkout page load time with cache hit (should drop 200-500ms). (b) Verify deleted Stripe customer is re-created correctly. (c) Simulate Stripe timeout; ensure view fails gracefully.
- **Verification:** (confirmed) Confirmed: stripe.Customer.retrieve() at line 15 and stripe.Customer.create() at line 49 (called via line 37) are synchronous Stripe API calls. Lines 23, 34, 40 call customer.save() which triggers Django signals and historical record creation. Severity High is correct—this is user-facing checkout flow blocking.

#### PAY-04 — High
- **File / symbol:** `payment/webhook.py` :: `handle_checkout_session`  (lines 120-162)
- **Issue type:** sync-external-API  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** setup_intent = stripe.SetupIntent.retrieve(setup_intent_id)  # line 121 full_payment_intent = stripe.PaymentIntent.retrieve(payment_intent)  # line 148 payment_method = stripe.PaymentMethod.retrieve(payment_method_id)  # line 154  Three Stripe API calls happen synchronously during webhook processing.
- **Why slow/risky:** Stripe webhooks expect a 200 response within a few seconds. Each API call adds 100-500ms latency. If total time exceeds Stripe's timeout, the webhook handler times out and Stripe retries (creating duplicate Payment records or orphaned state). The retry will attempt the same slow calls again, cascading timeouts.
- **Fix:** Avoid re-fetching Stripe objects; derive state from session data alone. If re-fetch is required, defer to background task and return 200 immediately. Option 3: Add timeout=1 to Stripe calls and fall back gracefully.
- **Expected impact:** High-frequency payment completions cause webhook timeouts. Stripe retries, creating duplicate Payment records or leaving reservations in inconsistent state. Manual cleanup required.
- **How to test:** Deploy with timeout=1. Monitor Stripe webhook logs for failed delivery attempts. If any timeout, implement deferred re-fetch pattern. Verify Payment records have no duplicates.
- **Verification:** (confirmed) Three synchronous Stripe API calls confirmed: stripe.SetupIntent.retrieve() at line 121, stripe.PaymentIntent.retrieve() at line 148, stripe.PaymentMethod.retrieve() at line 154. These run inside the webhook handler but don't block the 200 response (happens after lines 218-220 commit). Severity High is appropriate—they compete for resources in the single-worker runtime.

#### PAY-05 — High
- **File / symbol:** `dispatching/views.py` :: `_process_stripe_refund`  (lines 8002-8046)
- **Issue type:** sync-external-API  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** for payment in paid_payments:     ...     refund = stripe.Refund.create(  # line 8023         payment_intent=payment.stripe_payment_intent_id,         amount=int(amount_to_refund * 100),         reason='requested_by_customer',     )     ...     payment.save()  # line 8036  STRIPE API CALL INSIDE LOOP, followed by payment.save() (which triggers signals + history record).
- **Why slow/risky:** For a reservation with 2+ paid payments, the refund handler loops over all paid payments and calls stripe.Refund.create for each one. Each call is 200-500ms. A 2-payment refund = 400-1000ms latency. The admin view is synchronous, blocking for 1+ second. If timeout is exceeded, Stripe refund state becomes half-applied.
- **Fix:** Option 1: Batch refunds (create a single refund for the total amount). Option 2: Defer the loop to _run_in_background and return 202 Accepted immediately. Option 3: Implement idempotency: store refund attempt state on RefundRequest.
- **Expected impact:** Admin workflow hang. If refunds take >30s for a multi-payment reservation, the request times out. Stripe refunds are partially applied, creating inconsistent state. Manual reconciliation required.
- **How to test:** Create reservation with 2+ payments. Refund via admin. Measure: (a) Admin form response time (should be <2s). (b) Refund completes in background. (c) Verify payment.refunded_amount is updated. (d) Test idempotency: retry the same refund request, ensure no duplicate Stripe refund.
- **Verification:** (confirmed) stripe.Refund.create() at line 8023 is INSIDE the for loop (starting line 8011). Each iteration makes a Stripe API call, then calls payment.save() at line 8036. Confirmed: this is a sync-API-in-loop pattern. For N refunds, this makes N Stripe calls sequentially. Severity High confirmed.

#### PAY-06 — High
- **File / symbol:** `payment/webhook.py` :: `handle_checkout_session`  (lines 106-116)
- **Issue type:** missing-index  |  **Fix risk:** Low
- **Evidence:** payment, created = Payment.objects.get_or_create(     reservation=reservation,     customer=customer,     stripe_checkout_id=session.get("id"),     defaults={...}, )  get_or_create on (reservation, customer, stripe_checkout_id) triple. There is NO database index on this combination.
- **Why slow/risky:** Stripe webhook can be retried before the first Payment record commits. If webhook retries, get_or_create will do a full table scan on Payment without an index. On a high-volume day with thousands of payments, each retry adds a scan. Additionally, Stripe webhooks can fire multiple times for a single checkout session, so get_or_create must be fast AND idempotent.
- **Fix:** Add database index: `db_index=True` on stripe_checkout_id field OR create a multi-column index (reservation, customer, stripe_checkout_id). This is a one-time migration, safe to apply.
- **Expected impact:** On payment spike days, Payment table scans slow down webhook processing (each scan = 100-500ms). Webhooks timeout, Stripe retries, cascading slowdown.
- **How to test:** Apply migration. Measure: EXPLAIN ANALYZE on get_or_create query with high payment volume. Index should be used. Re-test webhook response time (should drop significantly).
- **Verification:** (confirmed) Payment model (line 25) defines stripe_checkout_id as CharField without db_index=True. The get_or_create at webhook.py lines 106-109 uses (reservation, customer, stripe_checkout_id) tuple. Latest migration (0010) does not add any index. Under load, this causes DB table scans for duplicate-stripe-session detection. Severity High confirmed.

#### PAY-07 — Medium
- **File / symbol:** `reservations/models.py` :: `Reservation.total_paid / Reservation.amount_owed / Reservation.payment_status`  (lines 550-596)
- **Issue type:** N+1  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** @cached_property def total_paid(self):     paid_sum = self.payments.filter(status="paid").aggregate(total=Sum("amount"))["total"] or Decimal('0.00')     partial_refunded_sum = self.payments.filter(...).aggregate(...)["total"] or Decimal('0.00')  @cached_property def payment_status(self):     ...     else:         payments = self.payments.all()  # line 582: FRESH QUERY if not prefetched  Each property calls .filter() or .all() even if payments were already prefetched. The cache only checks if payments WERE prefetched; if not, it re-queries.
- **Why slow/risky:** If a view iterates over reservations and accesses .payment_status, .total_paid, or .amount_owed, each reservation that was NOT prefetched with Prefetch('payments') incurs a fresh database query. On a page with 100 reservations and no prefetch, that's 300+ queries. The 'cached_property' only caches after first access, not before the fetch.
- **Fix:** Add prefetch_related('payments') to every view/query that accesses these properties. Example: `Reservation.objects.filter(...).prefetch_related('payments')` before any template or loop.
- **Expected impact:** Revenue dashboard, admin pages, and any report listing reservations: N+1 queries. If the dashboard fetches 1000 reservations, that's 3000+ queries instead of 2.
- **How to test:** Enable query logging. Load a page with multiple reservations. Verify query count is low (<10, not 100+).

#### PAY-08 — Medium
- **File / symbol:** `payment/signals.py` :: `_payment_saved / compute_paid_state`  (lines 64-77)
- **Issue type:** signal-side-effect  |  **Fix risk:** Low
- **Evidence:** @receiver(post_save, sender=Payment) def _payment_saved(sender, instance, **kwargs):     _recompute_reservation_paid_state(instance.reservation)  # line 67  def compute_paid_state(reservation) -> dict:     paid_qs = reservation.payments.filter(status="paid")  # line 46     gross = paid_qs.aggregate(s=Sum("amount"))["s"] or Decimal("0.00")  # line 47     refunded = paid_qs.aggregate(s=Sum("refunded_amount"))["s"] or Decimal("0.00")  # line 48  Every time a Payment is saved, the signal recomputes the Reservation's paid state by calling TWO separate aggregates on the same filtered queryset. This is inefficient because Django executes two separate SQL queries.
- **Why slow/risky:** When processing a webhook for a single payment, payment.save() triggers _payment_saved, which calls compute_paid_state. That function runs 2+ queries (one for Sum(amount), one for Sum(refunded_amount)) instead of combining them into one. On a payment-heavy day, this doubles the query load.
- **Fix:** Combine the two aggregates into a single query: `paid_qs.aggregate(gross=Sum("amount"), refunded=Sum("refunded_amount"))`. This produces one SQL query instead of two.
- **Expected impact:** On high-payment-volume days, each webhook triggers the signal, which runs 2+ queries. This is multiplicative: 100 payments = 200+ extra queries.
- **How to test:** Enable query logging. Save a payment, observe query count (should drop from 2-3 to 1-2). Test bulk payment import; verify query count is linear.

#### PAY-09 — Medium
- **File / symbol:** `reservations/conversions.py` :: `send_purchase_event`  (lines 127-166)
- **Issue type:** N+1  |  **Fix risk:** Low
- **Evidence:** def send_purchase_event(reservation, value=None, event_id=None, request=None):     if reservation.payments.exists():  # line 160: FRESH QUERY         latest_payment = reservation.payments.latest("created_at")  # line 161: FRESH QUERY  Calls to reservation.payments.exists() and reservation.payments.latest() are fresh database queries if payments were not prefetched.
- **Why slow/risky:** send_purchase_event is called from payment/webhook.py (line 231, background thread) and payment/views.py (line 171, success page). Both call the function with a Reservation object that was fetched without prefetch_related('payments'). Inside the function, .payments.exists() and .payments.latest() each trigger a database query. This is unnecessary since the Payment data is not large.
- **Fix:** Prefetch payments before calling send_purchase_event: `Reservation.objects.prefetch_related('payments').get(pk=...)`. Or, modify send_purchase_event to accept an optional latest_payment parameter.
- **Expected impact:** Every payment success (webhook + page) triggers 2 extra queries. On high-volume days, this adds up.
- **How to test:** Enable query logging. Call send_purchase_event, verify no database queries if payment is passed as parameter.

#### PAY-10 — Medium
- **File / symbol:** `ops/kpis.py` :: `by_travel_agent / by_route / by_vehicle`  (lines 234-315)
- **Issue type:** no-pagination  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** def by_travel_agent(start, end, limit: int = 25):     pay = payment_revenue_qs(start, end).filter(...)     raw = list(pay.values(...).annotate(...).order_by("-paid_revenue")[:limit])     res_ids = list(pay.values_list("reservation_id", flat=True).distinct())  # NO LIMIT, full table     comm_by_agent = _commission_by_reservation_ids(res_ids)  # PASSES UNBOUNDED LIST  Fetches the full res_ids list without paginating. If a date range has 10,000+ paid payments, res_ids becomes a list of 10,000+ IDs. Then the IN clause becomes unwieldy.
- **Why slow/risky:** The revenue dashboard is not paginated. If a query spans months with high volume, it fetches ALL payments and ALL reservations—no slicing. For a 3-month window, that could be 10,000+ rows. The IN(...) clause then becomes SQL IN with 10,000 values, which is inefficient.
- **Fix:** Add slicing to the revenue queries: `.[:limit]` on both the headline aggregates and the res_ids list. For example, `res_ids = list(pay.values_list('reservation_id', flat=True).distinct()[:limit * 5])`.
- **Expected impact:** Revenue dashboard slow on high-volume date ranges. First load of '/ops/dashboard/revenue/?start=2026-01-01&end=2026-06-03' might take 5-10s.
- **How to test:** Measure dashboard load time on a large date range. Add [:limit * 5] to res_ids. Re-measure; should drop significantly.

#### PAY-11 — Medium
- **File / symbol:** `payment/webhook.py` :: `handle_checkout_session (get_or_create)`  (lines 106-116)
- **Issue type:** missing-index  |  **Fix risk:** Medium
- **Evidence:** payment, created = Payment.objects.get_or_create(     reservation=reservation,     customer=customer,     stripe_checkout_id=session.get("id"),     ... )  No unique constraint on stripe_checkout_id. Multiple payments could have the same stripe_checkout_id if webhooks retry.
- **Why slow/risky:** Stripe webhook retries can create race conditions: two webhooks for the same session fired simultaneously could both do get_or_create, find no match, and create two Payment records. Without a unique constraint on stripe_checkout_id, duplicates persist.
- **Fix:** Add `unique=True` to the stripe_checkout_id field in the Payment model. This ensures database-level idempotency: if a webhook retries, the second get_or_create finds the existing payment and returns it without creating a duplicate. Also add `db_index=True` for performance.
- **Expected impact:** Webhook retries create duplicate Payment records. Reconciliation is broken; revenue is double-counted. Manual cleanup required.
- **How to test:** Apply constraint. Simulate webhook retry (POST same session twice). Verify only one Payment record is created. Revenue is correct.

#### PAY-12 — Low
- **File / symbol:** `dispatching/views.py` :: `_process_stripe_refund`  (lines 8006, 8032, 8036)
- **Issue type:** repeated-count  |  **Fix risk:** Low
- **Evidence:** for payment in paid_payments:     ...     payment.refunded_amount = (payment.refunded_amount or Decimal('0.00')) + amount_to_refund     payment.stripe_refund_id = refund.id     if payment.refunded_amount >= payment.amount:         payment.status = 'refunded'     payment.save()  # line 8036  Each payment.save() inside the loop does NOT use update_fields=[...]. Django ORM executes a full UPDATE statement for all fields.
- **Why slow/risky:** payment.save() without update_fields is slower than necessary. Instead of updating only 3 fields (refunded_amount, stripe_refund_id, status), Django updates all fields on the model. Additionally, each save() triggers post_save signals, which re-aggregates the reservation paid state. For a 3-payment refund, that's 3 aggregations instead of 1.
- **Fix:** Use payment.save(update_fields=['refunded_amount', 'stripe_refund_id', 'status']). After the loop, manually recompute reservation paid state once.
- **Expected impact:** Minor—each refund is not a high-frequency operation. But on large bulk refunds, this adds up.
- **How to test:** Enable query logging. Refund a multi-payment reservation. Verify UPDATE queries only touch required fields.

---

### Drivers (pay, payouts, availability, time-off)

| Component | Purpose | Risk |
|---|---|---|
| drivers/views.py - Driver Dashboard (index, completed_trips, schedule) | Render driver's legs with scheduling conflicts, ETA, and flight delay alerts | High |
| drivers/views.py - Driver Payment Statements | Staff view of driver payments and payout adjustments | High |
| drivers/pay_calc.py - Driver Pay Calculation | Auto-calculate driver pay per leg based on rate lookups | Medium |
| drivers/payout_adjustments.py - Payout Correction | Void, edit, and add legs to payment statements transactionally | Medium |
| drivers/timeoff_notifications.py - SMS Notifications | Send SMS to drivers and founders for time-off requests | Critical |
| drivers/availability.py - Availability Resolver | Resolve driver availability per date combining weekly/overrides | High |
| drivers/utils.py - Google Maps Integration | Fetch drive time estimates from Google Distance Matrix API | High |
| drivers/admin.py - Driver Admin List | Staff admin panel showing driver stats, payments, and profitability | Medium |
| drivers/views.py - Flight Data Refresh | Synchronously fetch flight status from AeroAPI on driver request | Critical |
| drivers/gusto_export.py - Gusto CSV Export | Build and export processed driver payments to Gusto CSV | Medium |

#### DRIV-05 — Critical
- **File / symbol:** `drivers/views.py` :: `index, schedule`  (lines 231-232, 334)
- **Issue type:** sync-external-API  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** Line 231-232 (index) and line 334 (schedule): Google Maps API call happens inside a loop over legs with no background job queuing.  ```python if settings.GOOGLE_MAPS_API_KEY:     for leg in legs_list:         leg.drive_info = google_drive_time(leg.pickup_location, leg.dropoff_location) ```  The `google_drive_time` function (utils.py:32-65) makes a synchronous `requests.get()` to Google Distance Matrix API with a 5-second timeout.
- **Why slow/risky:** With 1 Gunicorn worker, each synchronous call blocks all other users. If a driver has 5 legs and each Google call takes 2 seconds, the entire page takes 10+ seconds. Any network hiccup or quota exhaustion pauses the site for everyone.
- **Fix:** Move the loop into a background task via `_run_in_background()`. Caller can show a loading state or return minimal data; job enriches legs asynchronously. Alternatively, pre-compute a single cache hit key for the route pair and batch-fetch in a background job.
- **Expected impact:** Eliminates blocking on external API; frees worker for other requests; improves site responsiveness under load.
- **How to test:** Mock Google API to return 2s delay; measure response time before/after backgrounding. Verify drive_info appears on page after background task completes.
- **Verification:** (confirmed) Lines 231-232 (index view) and line 334 (schedule view): google_drive_time() is called inside a for loop over legs_list. The function (drivers/utils.py:32-42) makes a synchronous requests.get() to Google Distance Matrix API with 5-second timeout. No background job queuing, blocks the view for each leg. With single-worker Gunicorn, this blocks all users.

#### DRIV-06 — Critical
- **File / symbol:** `drivers/views.py` :: `refresh_flight_data`  (lines 1348-1498)
- **Issue type:** sync-external-API  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** Line 1394-1397: Synchronous call to AeroAPI in a request handler.  ```python aeroapi = AeroAPIService() flight_data = aeroapi.get_flight_data(     flight_ident, flight_date=flight_date, trip_type=trip_type ) ```  No timeout, no background job, no explicit error handling for slow/failed responses.
- **Why slow/risky:** If AeroAPI is slow, rate-limited, or down, this view blocks for an unpredictable duration, holding the worker. Single Gunicorn worker means ALL users freeze.
- **Fix:** Queue a background job to fetch and update flight data. Return 202 Accepted with a status-check endpoint, or update the DOM via WebSocket when ready. For synchronous response, wrap in timeout (3-5s) and gracefully degrade to stale data.
- **Expected impact:** Unblocks worker; prevents cascading timeouts if AeroAPI is slow.
- **How to test:** Mock AeroAPI to delay 10 seconds; verify request doesn't hang and returns gracefully. Measure worker availability during flight refresh.
- **Verification:** (confirmed) Lines 1394-1397 (refresh_flight_data view): aeroapi.get_flight_data() is called synchronously. This is in a request handler with no timeout wrapper and no background job queuing. Blocks the requesting user (and entire single worker) until AeroAPI responds.

#### DRIV-07 — Critical
- **File / symbol:** `drivers/timeoff_notifications.py` :: `notify_founders_of_new_request, notify_driver_of_decision`  (lines 81-124)
- **Issue type:** sync-external-API  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Line 103-104 and 123: Twilio SMS send happens synchronously in a loop (for founders) or single call (for driver).  ```python def notify_founders_of_new_request(override):     phones = getattr(settings, "TIMEOFF_NOTIFY_PHONES", []) or []     if not phones:         return     # ...     for phone in phones:         _send(phone, body)  # ← Synchronous call per phone  def notify_driver_of_decision(override):     # ...     _send(driver_phone, body)  # ← Synchronous Twilio call ```  Line 52: `client.messages.create()` is a blocking Twilio SDK call with implicit timeout.
- **Why slow/risky:** If Twilio is slow or unreachable, the time-off request submission or approval flow blocks the staff user. With 3-5 founders, up to 5 SMS sends may each take 1-2s, blocking the worker for 5-10s.
- **Fix:** Queue all SMS sends as background jobs before returning response. Use `_run_in_background()` or Celery task. Mark the request as 'pending approval' immediately, notify asynchronously.
- **Expected impact:** Unblocks request/approval flow; improves UX; prevents Twilio failures from blocking staffwork.
- **How to test:** Mock Twilio to delay 3 seconds; measure request completion time. Verify SMS still sends in background even if response returned early.
- **Verification:** (confirmed) Line 103-104 (notify_founders_of_new_request loop): for phone in phones: _send(phone, body) calls _send() synchronously per phone in line 52 via client.messages.create() (Twilio SDK blocking call). Line 123 (notify_driver_of_decision) calls _send() once. Both are invoked from drivers/views.py line 1617 inside request handler. Blocks view response.

#### DRIV-08 — High
- **File / symbol:** `drivers/availability.py` :: `_weekly_or_defaults, resolve_effective_availability`  (lines 85, 125)
- **Issue type:** query-in-loop  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** Line 85: `for entry in driver.weekly_schedule.all():` iterates over weekly schedule fetched per call. Line 125: `[o for o in driver.date_overrides.all() if o.status == 'approved']` filters in Python after fetching all overrides.  ```python def _weekly_or_defaults(driver, target_date):     day_of_week = target_date.weekday()     for entry in driver.weekly_schedule.all():  # ← Per-driver, no prefetch         if entry.day_of_week == day_of_week:             return {...}     # ...     return {...}  def resolve_effective_availability(driver, target_date):     # ...     exception = _pick_active_exception(         [o for o in driver.date_overrides.all() if o.status == 'approved'],  # ← Fetch + Python filter         target_date,     ) ```
- **Why slow/risky:** When called in bulk (e.g., dispatching assigns 10 drivers to a date), this function runs 10 times, each fetching weekly_schedule and date_overrides. With prefetch_related at the caller level, the relations exist but these functions bypass it and call `.all()` directly.
- **Fix:** Require caller to prefetch_related('weekly_schedule', 'date_overrides') before calling. Accept optional prefetched lists as parameters, or use `resolve_effective_availability_bulk()` that handles the prefetch once and maps results.
- **Expected impact:** Reduces from N calls to weekly_schedule + N calls to date_overrides down to 1+1 queries at the caller.
- **How to test:** Load dispatcher daily planner assigning 10 drivers; measure query count before/after. Prefetch should reduce from ~20 to ~2 queries.
- **Verification:** (confirmed) Line 85 (_weekly_or_defaults): for entry in driver.weekly_schedule.all() iterates without prefetch. Line 125 (resolve_effective_availability): [o for o in driver.date_overrides.all() if o.status == 'approved'] filters in Python after fetch. The comments at 122-124 show this is intentional to use prefetch cache, but callers must prefetch themselves. If called without prefetch, each call triggers separate queries.

#### DRIV-13 — High
- **File / symbol:** `drivers/utils.py` :: `get_drive_time`  (lines 11-65)
- **Issue type:** sync-external-API  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** Line 32-42: Synchronous `requests.get()` to Google Distance Matrix API with 5-second timeout.  ```python try:     resp = requests.get(         "https://maps.googleapis.com/maps/api/distancematrix/json",         params={...},         timeout=5,     )     data = resp.json() ```  No retry logic, no circuit breaker, no fallback beyond `return None`.
- **Why slow/risky:** Caller assumes this is fast (cached), but on cache miss or unfamiliar routes, it makes a synchronous external API call. If Google is slow or API quota exhausted, the driver dashboard or extend view hangs.
- **Fix:** Add exponential backoff retry (1, 2, 4 seconds), circuit breaker after 3 consecutive failures, and fallback to last-known value or zero. Consider async fetch via background task if called from views.
- **Expected impact:** Improves resilience to Google API latency; prevents cascading timeouts.
- **How to test:** Mock Google API to timeout at 6 seconds; verify request fails gracefully after 5s, doesn't cascade. Add circuit breaker test: 4 failures → no more requests for 60s.
- **Verification:** (confirmed) Lines 32-42 (get_drive_time): requests.get() call with 5-second timeout to Google Distance Matrix API. Has caching (2-hour TTL) but no retry logic, circuit breaker, or fallback on failure. Returns None on timeout/error. Called from multiple views in loops (DRIV-05).

#### DRIV-09 — Medium
- **File / symbol:** `drivers/views.py` :: `extend`  (lines 807-811)
- **Issue type:** repeated-count  |  **Fix risk:** Low
- **Evidence:** Line 807-811: Multiple COUNT queries run independently.  ```python base_count_qs = Driver.objects.all() if show_inactive else Driver.objects.filter(is_active=True) all_count_total = base_count_qs.count() inhouse_count_total = base_count_qs.filter(driver_type="inhouse").count() affiliate_count_total = all_count_total - inhouse_count_total inactive_count_total = Driver.objects.filter(is_active=False).count() ```  3 separate count() calls, each hitting the database.
- **Why slow/risky:** Each `.count()` is a separate COUNT(*) query. Should batch into one aggregate().
- **Fix:** Replace with:
```python
base_qs = Driver.objects.filter(is_active=True) if not show_inactive else Driver.objects.all()
agg = base_qs.aggregate(
    all_total=Count('id'),
    inhouse_total=Count('id', filter=Q(driver_type='inhouse')),
)
inactive_total = Driver.objects.filter(is_active=False).count()  # Still separate if needed
```
Reduces base queries from 2 to 1.
- **Expected impact:** Reduces count() queries; minimal overhead but cumulative on high-traffic pages.
- **How to test:** Load extend view with show_inactive=False; query count should drop by 1.

#### DRIV-10 — Medium
- **File / symbol:** `drivers/views.py` :: `driver_statement_detail`  (lines 1032-1037)
- **Issue type:** unbounded-queryset  |  **Fix risk:** Low
- **Evidence:** Line 1032-1037: Candidate legs are fetched with a `[:50]` limit, but there's no guarantee this covers all unpaid legs; if a driver has 100 unpaid legs, 50 are silently dropped from the modal.  ```python candidate_legs = (     Leg.objects     .filter(driver=driver, status="completed", payment_status="unpaid")     .select_related("reservation", "reservation__customer")     .order_by("-pickup_date", "-pickup_time")[:50] ) ```
- **Why slow/risky:** The limit is arbitrary; staff may not realize recent legs aren't shown. No pagination control for the add-leg modal.
- **Fix:** Add a count check: if count > 50, warn staff 'Showing 50 of N unpaid legs'. Alternatively, allow pagination in the modal or batch-add.
- **Expected impact:** Prevents silent data loss in UI; staff knows if they can't see all options.
- **How to test:** Add 100 unpaid legs to a driver; verify modal shows warning and limit is clear.

#### DRIV-11 — Medium (orig High -> Medium)
- **File / symbol:** `drivers/admin.py` :: `unpaid_legs_display`  (lines 256-293)
- **Issue type:** N+1  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Line 256-293: Read-only field that calls `obj.get_unpaid_legs()` and then iterates over every leg to build an HTML table.  ```python def unpaid_legs_display(self, obj):     legs = obj.get_unpaid_legs()  # Fetches unpaid legs     if not legs:         return "No unpaid legs"      html = '<table style="width: 100%; border-collapse: collapse;">'     html += '<tr ...><th>Date</th> ...'      for leg in legs:  # ← Loop over legs         html += "<tr>"         html += f'<td><a href="{reverse("admin:reservations_leg_change", args=[leg.id])}"...'         # Each leg accesses leg.pickup_location, leg.dropoff_location, leg.driver_pay_amount, leg.profit_estimate ```  This is called for each driver on the admin list display (readonly fields). With N drivers, this can trigger N * (M leg queries + M reverse() calls).
- **Why slow/risky:** The admin changelist reads this field per row (N drivers × 1 query per driver's unpaid legs). If rendered, it loops and formats each leg. Not prefetched, so on a long driver list, this is N+1 per driver.
- **Fix:** Remove from list_display or make it a separate detailed view. If needed, use a custom changelist view that prefetches unpaid legs once and caches the HTML.
- **Expected impact:** Reduces admin list page load from slow to fast; admin can optionally view unpaid legs via link instead of inline rendering.
- **How to test:** Load driver admin changelist with 20 drivers; measure page load time and query count before/after removal of unpaid_legs_display.
- **Verification:** (confirmed) Lines 256-293 (unpaid_legs_display): readonly field on admin changelist that calls obj.get_unpaid_legs() and renders HTML table. This field appears on the list_display for every driver row, causing O(n drivers * m unpaid legs) fetch operations. Not directly blocking a user view, but admin performance issue.

#### DRIV-12 — Medium (orig High -> Medium)
- **File / symbol:** `drivers/admin.py` :: `recent_leg_history`  (lines 437-483)
- **Issue type:** N+1  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Line 437-483: Similar to DRIV-11, this readonly field calls `obj.get_leg_history()[:10]` and renders a table per driver on the admin list.  ```python def recent_leg_history(self, obj):     legs = obj.get_leg_history()[:10]     if not legs:         return "No legs found"      html = '<table style="...'     html += '<tr ...><th>Date</th> ...'      for leg in legs:  # ← Loop         html += "<tr>"         html += f'<td><a href="{reverse("admin:reservations_leg_change", args=[leg.id])}"...'         # Accesses leg.pickup_date, leg.pickup_location, leg.dropoff_location, leg.total_driver_pay, leg.profit_estimate, leg.payment_status ```
- **Why slow/risky:** Called per row in admin list. On a list of 20 drivers, this fetches 20 * 10 = up to 200 leg rows (though capped per driver) and renders 200 links.
- **Fix:** Remove from list_display. Link to driver detail page instead, which shows recent leg history.
- **Expected impact:** Reduces admin list load time and query/rendering overhead.
- **How to test:** Load driver admin list with 20 drivers; measure time before/after removing recent_leg_history from readonly_fields.
- **Verification:** (confirmed) Lines 437-483 (recent_leg_history): same pattern as DRIV-11 - readonly field on admin changelist calling obj.get_leg_history()[:10] and rendering HTML for each driver on the list. O(n * 10 leg queries).

#### DRIV-14 — Medium
- **File / symbol:** `drivers/payout_adjustments.py` :: `_recalculate_payment_total`  (lines 104-129)
- **Issue type:** python-aggregate  |  **Fix risk:** Low
- **Evidence:** Line 110-119: Sum is computed in Python over a queryset iterator.  ```python def _recalculate_payment_total(payment: DriverPayment) -> None:     active = payment.leg_payments.filter(status=LegPayment.STATUS_ACTIVE)     total = Decimal("0.00")     base = Decimal("0.00")     grat = Decimal("0.00")     addl = Decimal("0.00")     for lp in active:         total += Decimal(lp.amount or 0)         base += Decimal(lp.base_pay or 0)         grat += Decimal(lp.gratuity or 0)         addl += Decimal(lp.additional or 0) ```
- **Why slow/risky:** Query fetches all active LegPayment rows and sums in Python. Should use `.aggregate(Sum())` instead. Minor overhead, but called on every void/edit/add operation.
- **Fix:** Replace loop with: `agg = active.aggregate(total=Sum('amount'), base=Sum('base_pay'), grat=Sum('gratuity'), addl=Sum('additional'))` and extract values. Converts per-LegPayment work to single database query.
- **Expected impact:** Reduces per-adjustment operation time; cumulative benefit on high-volume payout correction workflows.
- **How to test:** Add 5 active leg payments to a statement; void one and measure time for _recalculate_payment_total before/after. Should be negligible but validates correctness.

#### DRIV-15 — Medium
- **File / symbol:** `drivers/gusto_export.py` :: `build_rows_for_period, validate_selection`  (lines 272-274, 325-394)
- **Issue type:** N+1  |  **Fix risk:** Low
- **Evidence:** Line 272-274: Queryset is evaluated and each payment is passed to `build_row()` which may re-query leg payment details.  ```python def build_rows_for_period(from_date: date, to_date: date) -> list[GustoRow]:     return [build_row(p, from_date, to_date) for p in eligible_payments_qs(from_date, to_date)] ```  Line 211-216 (inside build_row): If `_min_pickup` and `_max_pickup` are not prefetched, a separate query runs per payment.  ```python if min_pickup is None or max_pickup is None:     agg = payment.leg_payments.filter(status="active").aggregate(         mn=Min("leg__pickup_date"), mx=Max("leg__pickup_date"),     ) ```
- **Why slow/risky:** The eligible_payments_qs() function annotates _min_pickup/_max_pickup, but if not called or if the list comp triggers a new query, it refetches.
- **Fix:** Ensure eligible_payments_qs() is always the source and the annotation is preserved through the list comp. Add a test to verify query count doesn't exceed 2-3 regardless of payment count.
- **Expected impact:** Prevents silent N+1 in export flow; ensures CSV generation scales linearly with payment count.
- **How to test:** Export 10 payments; verify query count < 5 regardless of leg count per payment.

#### DRIV-01 — Low — **REFUTED**
- **File / symbol:** `drivers/views.py` :: `driver_statement_list`  (lines 927-961)
- **Issue type:** N+1  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Line 942-954: `for payment in payments:` iterates over prefetched leg_payments, then for each payment accesses `payment.leg_payments.all()` in a loop. The prefetch_related carries all relations but the loop logic re-materializes all leg dates, triggering individual iterations.  ```python payment_rows = [] for payment in payments:     legs = [lp.leg for lp in payment.leg_payments.all() if lp.leg]     leg_dates = [leg.pickup_date for leg in legs if leg.pickup_date]     pay_period_start = min(leg_dates) if leg_dates else None     pay_period_end = max(leg_dates) if leg_dates else None ```
- **Why slow/risky:** Prefetch loads relations but the loop unpacks all legs per payment and recalculates min/max for each row. With N payments each having M legs, this adds unnecessary per-iteration work. Should annotate min/max directly on the queryset.
- **Fix:** Use annotate(Min/Max) on the DriverPayment queryset over related leg__pickup_date, then extract in one pass. Replace the loop with annotation-driven context.
- **Expected impact:** Reduces per-payment computation cost from O(M) to O(1); improves statement list page load time especially for drivers with many payments.
- **How to test:** Load driver_statement_list for a driver with 5+ payments each with 3+ legs; measure query count and response time before/after.
- **Verification:** (refuted) The code at lines 942-954 DOES have prefetch_related('leg_payments__leg') applied at line 937. While the loop iterates over payment.leg_payments.all(), calling .all() on a prefetched relation uses Django's prefetch cache and does NOT trigger additional DB queries. The inefficiency is algorithmic (building lists in Python) not database-related. Not an N+1 query issue.

#### DRIV-02 — Low — **REFUTED**
- **File / symbol:** `drivers/views.py` :: `index`  (lines 212)
- **Issue type:** N+1  |  **Fix risk:** Low
- **Evidence:** Line 212: Inside a loop, `min(l.id for l in leg.reservation.legs.all())` is called per leg to compute `is_first_leg`. This calls `.all()` on a prefetched relation **inside the loop**.  ```python for leg in legs_list:     first_id = min(l.id for l in leg.reservation.legs.all())     leg.is_first_leg = leg.id == first_id ```  The prefetch_related on line 202 carries all reservation__legs, but calling `.all()` in the loop defeats the cache.
- **Why slow/risky:** With N legs, this calls `Reservation.legs.all()` N times even though the prefetch already loaded all legs. The loop should use the prefetched data to compute is_first_leg once per reservation, not per leg.
- **Fix:** Build a dict of reservation_id → min_leg_id **before** the loop using only the prefetched in-memory data. Then use it inside the loop: `first_id = reservation_min_legs[leg.reservation_id]`.
- **Expected impact:** Reduces from O(N) calls to prefetch cache to O(1); eliminates redundant memory accesses per leg.
- **How to test:** Load driver dashboard with 10+ legs in same reservation; query count should be unchanged but loop execution time should drop.
- **Verification:** (refuted) Line 212 has prefetch_related('reservation__legs') applied at line 202. Calling leg.reservation.legs.all() inside the loop USES the prefetch cache and does NOT hit the database per leg. Not an N+1 query issue. The inefficiency is O(n*m) in-memory iteration to find min IDs, not database queries.

#### DRIV-03 — Low — **REFUTED**
- **File / symbol:** `drivers/views.py` :: `completed_trips`  (lines 272)
- **Issue type:** N+1  |  **Fix risk:** Low
- **Evidence:** Line 272: Same pattern as DRIV-02—`min(l.id for l in leg.reservation.legs.all())` called per leg in a loop, defeating the prefetch.  ```python for leg in legs:     first_id = min(l.id for l in leg.reservation.legs.all())     leg.is_first_leg = leg.id == first_id ```
- **Why slow/risky:** Identical root cause: `.all()` called on prefetched relation inside loop wastes the cache.
- **Fix:** Same as DRIV-02: pre-compute reservation_min_legs dict before loop.
- **Expected impact:** Reduces loop overhead; completed_trips page load time improves.
- **How to test:** Load completed_trips for driver with 10+ completed legs spanning multiple reservations.
- **Verification:** (refuted) Line 272 in completed_trips has prefetch_related('reservation__legs') applied at line 264. Calling leg.reservation.legs.all() uses the prefetch cache. Not an N+1 query issue.

#### DRIV-04 — Low — **REFUTED**
- **File / symbol:** `drivers/views.py` :: `schedule`  (lines 315)
- **Issue type:** N+1  |  **Fix risk:** Low
- **Evidence:** Line 315: Same pattern—`min(l.id for l in leg.reservation.legs.all())` in loop.  ```python for leg in legs_list:     first_id = min(l.id for l in leg.reservation.legs.all())     leg.is_first_leg = leg.id == first_id ```
- **Why slow/risky:** Same root cause as DRIV-02 and DRIV-03.
- **Fix:** Same fix: pre-compute dict before loop.
- **Expected impact:** Reduces weekly schedule view overhead.
- **How to test:** Load weekly_schedule for driver with 15+ upcoming legs.
- **Verification:** (refuted) Line 315 in schedule has prefetch_related('reservation__legs') applied at line 295. Calling leg.reservation.legs.all() uses the prefetch cache. Not an N+1 query issue.

---

### Users/Auth/Agents/Agencies

#### AGENTS-01 — High
- **File / symbol:** `users/views.py` :: `AgencyHeadDashboardView.get_context_data`  (lines 810-819)
- **Issue type:** N+1  |  **Fix risk:** Low
- **Evidence:** for agent in agency.agents.all():     "reservation_count": agent.reservations.count(),
- **Why slow/risky:** One worker, one request blocks all users. Per-agent .count() inside a loop over prefetched agents causes N+1 queries. With even 10 agents, this is 10+ extra COUNT queries.
- **Fix:** Use prefetch_related('reservations') on the agents queryset and annotate with Count('reservations'). Then access cached counts: agent._reservation_count instead of agent.reservations.count().
- **Expected impact:** Dashboard page load time for agency heads scales linearly with agent count; site blocks until query completes.
- **How to test:** Load AgencyHeadDashboardView for an agency with 10+ agents. Check django.db.connection.queries—should see 1 agent query + 1 prefetch, not 1+N count queries.
- **Verification:** (confirmed) Line 816: agent.reservations.count() inside a loop at line 818: for agent in agency.agents.all(). This hits the database N times for N agents. Confirmed N+1 query pattern.

#### AGENTS-05 — High
- **File / symbol:** `users/admin.py` :: `TravelAgentAdmin._calculate_commission_preview`  (lines 599-620)
- **Issue type:** query-in-loop  |  **Fix risk:** Medium
- **Evidence:** for agent in queryset:     unpaid_reservations = Reservation.objects.filter(         travel_agent=agent, ...     )     for reservation in unpaid_reservations:         for leg in reservation.legs.all():
- **Why slow/risky:** Admin action loops over selected agents (queryset) and fires a Reservation query per agent. Then for each reservation, iterates over .legs.all() without prefetch. N agents → N + sum(R) queries.
- **Fix:** Batch the Reservation query outside the loop using travel_agent__in=list(queryset), then prefetch_related('legs'). Then group results by agent_id in Python.
- **Expected impact:** Admin preview action on 10 agents with 50 reservations each = ~50+ queries. Blocks the admin worker for seconds.
- **How to test:** Select 10 agents in admin and click 'Preview commission payments'. connection.queries should have 1 Reservation query + 1 legs prefetch, not 10 + R reservation-specific queries.
- **Verification:** (confirmed) Lines 599-620: Outer loop iterates agents (line 599), for each agent a Reservation query executes (line 600). Inner loop at line 616 iterates reservation.legs.all() without prefetch, causing database hits for each leg fetch. Confirmed query-in-loop N+M pattern.

#### AGENTS-07 — High
- **File / symbol:** `users/signals.py` :: `handle_agency_payout_deletion`  (lines 328-333)
- **Issue type:** query-in-loop  |  **Fix risk:** Medium
- **Evidence:** agent_payouts = instance.agent_payouts.all() for agent_payout in agent_payouts:     agent_payout.delete()
- **Why slow/risky:** Pre-delete signal loops over agent payouts and calls .delete() individually. Each .delete() fires a signal cascade. Cascading deletes can trigger further queries per payout.
- **Fix:** Use bulk_delete: instance.agent_payouts.all().delete(). Or if signals must run, use bulk_update with a mark-deleted flag or batch_size for parallelism.
- **Expected impact:** Deleting an agency payout with 5 agent payouts causes 5+ deletion queries (plus their cascades). Blocks worker if reservation signal handlers also run.
- **How to test:** Delete an AgencyCommissionPayout with 5 agent payouts. connection.queries should show 1 bulk delete, not 5 individual deletes.
- **Verification:** (confirmed) Lines 329-333: for agent_payout in agent_payouts: agent_payout.delete() loops through and deletes each payout individually. Each delete() is a separate database query. Should use bulk_delete or .delete() on the queryset.

#### AGENTS-08 — High
- **File / symbol:** `users/services.py` :: `process_bulk_payouts`  (lines 209-212)
- **Issue type:** N+1  |  **Fix risk:** Low
- **Evidence:** owing_agents = sum(     1 for a in agency.agents.filter(agency_handles_payment=True)     if sum_ready(a) > 0 )
- **Why slow/risky:** sum_ready(agent) calls _agent_reservations(agent) which queries Reservations per agent. If 10 agents in agency, 10 separate Reservation queries. Runs inside a loop via the bulk action.
- **Fix:** Use bulk_ready_totals([agent_ids...]) from eligibility.py which fetches all reservations in 2 queries total. Or prefetch all agent reservations once and filter by agent_id in Python.
- **Expected impact:** Bulk agency payout action iterates agents and calls sum_ready per agent—O(N) queries where N=agent count. Blocks site for duration.
- **How to test:** Process bulk payout for an agency with 10 agents. connection.queries should not have 10+ separate Reservation queries.
- **Verification:** (confirmed) Lines 209-212 call sum_ready(a) for each agent in agency.agents.filter(...). sum_ready() (lines 277-284) calls _agent_reservations() and get_commission_eligibility() for each reservation. When looping many agents, this is N*M queries (one pair per agent). A bulk_ready_totals() function exists at lines 287-314 that does this more efficiently.

#### AGENTS-03 — Medium
- **File / symbol:** `users/views.py` :: `agency_payout_detail`  (lines 1210-1211)
- **Issue type:** repeated-count  |  **Fix risk:** Low
- **Evidence:** payout.total_amount / payout.agent_payouts.count() if payout.agent_payouts.exists()
- **Why slow/risky:** Calls .count() and .exists() separately on the same relation. Two queries instead of one. The .count() alone answers both questions.
- **Fix:** Annotate count on the queryset or prefetch and use len(list). Then check len > 0 or use the count result.
- **Expected impact:** Minor—two database queries per payout detail page load. Adds latency on already-slow view.
- **How to test:** Payout detail page should issue one fewer query after fix.

#### AGENTS-06 — Medium
- **File / symbol:** `users/views.py` :: `admin_commission_report`  (lines 1428-1435)
- **Issue type:** python-aggregate  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** agent_totals = {} for p in payouts:     aid = p.agent_id     if aid not in agent_totals:         agent_totals[aid] = {"agent": p.agent, ...}     agent_totals[aid]["total"] += float(p.total_amount)     agent_totals[aid]["payouts"] += 1
- **Why slow/risky:** Iterating over payouts list (which can be hundreds) to group by agent in Python instead of using database aggregation. Inefficient for large datasets.
- **Fix:** Use Django ORM: payouts.values('agent_id').annotate(total=Sum('total_amount'), payouts=Count('id')) instead of Python loop.
- **Expected impact:** Report page loading 500 payouts means Python loop over 500 items. Negligible for small datasets but scales poorly.
- **How to test:** Admin commission report with 500+ payouts. Run time should be same or faster after fix; memory footprint lower.

#### AGENTS-09 — Medium (orig High -> Medium)
- **File / symbol:** `users/views.py` :: `AgencyDetailView.get_context_data`  (lines 1342-1346)
- **Issue type:** N+1  |  **Fix risk:** Low
- **Evidence:** "agents": agency.agents.all(), "total_agents": agency.agents.count(), "total_unpaid": format_decimal(agency.get_total_unpaid_commissions()), "total_pending": format_decimal(agency.get_total_pending_commissions()), "total_paid": format_decimal(agency.get_total_paid_commissions()),
- **Why slow/risky:** get_total_unpaid_commissions() and siblings query Sum('unpaid_commissions') on agents. The agents queryset is not annotated, so these are separate aggregate queries. Template also iterates agents.all() again.
- **Fix:** Annotate on get_queryset: .annotate(total_unpaid=Sum('agents__unpaid_commissions'), ...). Then use these cached annotations in context instead of calling the methods.
- **Expected impact:** Detail view fires 3+ aggregate queries on every page load. For an admin viewing many agencies, site slows proportionally.
- **How to test:** Load AgencyDetailView. connection.queries should have one less aggregate per field (total_unpaid, pending, paid).
- **Verification:** (uncertain) Lines 1342-1346 call agency.get_total_unpaid_commissions() and similar methods. These methods (lines 604-614) each run a single .aggregate(Sum(...)) query, not an N+1 loop. The finding that multiple method calls could be consolidated is valid (3 separate aggregate queries), but each is efficient. Not classic N+1 but redundant queries.

#### AGENTS-10 — Medium
- **File / symbol:** `users/middleware.py` :: `NewsletterRateLimitMiddleware.is_allowed`  (lines 37-53)
- **Issue type:** query-in-loop  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** recent_attempts = NewsletterSubscriptionAttempt.objects.filter(     ip_address=ip_address,     timestamp__gte=... ).count() recent_email_attempts = NewsletterSubscriptionAttempt.objects.filter(     email=email,     timestamp__gte=... ).count()
- **Why slow/risky:** Middleware runs on every newsletter POST. Fires two separate COUNT queries instead of one combined query. High cardinality IPs/emails could slow down form submissions.
- **Fix:** Combine into one query: aggregate the max of two counts or use a single .filter(Q(...) | Q(...)) with Count(). Or use cache key instead of DB for rate limit.
- **Expected impact:** Newsletter signup page loads run 2 DB queries on every POST. With traffic, this is noticeable overhead per signup.
- **How to test:** POST to /newsletter/subscribe/ twice in rapid succession. connection.queries should show one combined query instead of two.

#### AGENTS-13 — Medium
- **File / symbol:** `users/signals.py` :: `handle_payout_deletion`  (lines 217-218)
- **Issue type:** N+1  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** reservations = instance.reservations.all() reservations.update(commission_paid=False, commission_paid_at=None)
- **Why slow/risky:** Pre-delete signal updates all reservations in the payout. The .update() is correct, but if there are 100 reservations, this fires one UPDATE with all IDs. However, if the signal system is chained with post_delete handlers on Reservation, each update could trigger cascading work.
- **Fix:** Keep the bulk update but verify no post_delete signals on Reservation fire inefficiently. Consider batching large updates if Reservation.post_delete is expensive.
- **Expected impact:** Deleting a payout with 100 reservations triggers one bulk UPDATE, which is good. But if Reservation.post_delete is defined, it runs once per reservation (100 times)—hidden N+1.
- **How to test:** Delete a CommissionPayout with 50+ reservations and check for post_delete signal cascades in connection.queries or logs.

#### AGENTS-02 — Low — **REFUTED**
- **File / symbol:** `users/views.py` :: `agency_payout_detail`  (lines 1204-1206)
- **Issue type:** N+1  |  **Fix risk:** Low
- **Evidence:** total_reservations = sum(     agent_payout.reservations.count() for agent_payout in payout.agent_payouts.all() )
- **Why slow/risky:** Loop calling .count() on each agent_payout's reservation relation. No prefetch. If 5 agent payouts, 5 COUNT queries fire synchronously.
- **Fix:** Prefetch agent_payouts with Prefetch('agent_payouts', queryset=AgentCommissionPayout.objects.prefetch_related('reservations')), then use len() on cached list or annotate with Count.
- **Expected impact:** Payout detail page blocks while executing up to M COUNT queries (M = number of agent payouts). Blocks site for all users.
- **How to test:** View an AgencyCommissionPayout detail page with 5+ agent payouts. connection.queries should not have M separate COUNT queries.
- **Verification:** (refuted) Lines 1204-1206 call agent_payout.reservations.count(). However, the agent_payouts queryset at lines 863-881 includes a Prefetch that prefetches all reservations. When .count() is called on a prefetched relation, Django uses the cached list in memory, not the database. No N+1.

#### AGENTS-04 — Low — **REFUTED**
- **File / symbol:** `users/views.py` :: `AgencyHeadDashboardView.get_context_data`  (lines 883-888)
- **Issue type:** N+1  |  **Fix risk:** Low
- **Evidence:** for payout in agency_payouts:     payout.total_reservations = sum(         len(agent_payout.reservations.all())         for agent_payout in payout.agent_payouts.all()     )
- **Why slow/risky:** Loop iterating over prefetched agency_payouts, then inside ANOTHER loop over agent_payouts calling .all(). If 3 payouts × 4 agent payouts each, that's ~12 queries for .reservations.all().
- **Fix:** Prefetch both levels atomically: prefetch_related(Prefetch('agent_payouts', queryset=AgentCommissionPayout.objects.prefetch_related('reservations'))). Then access cached lists with len().
- **Expected impact:** Agency dashboard page with multiple payouts takes 10+ extra queries. Blocks site for that agency head.
- **How to test:** Agency dashboard with 3+ payouts and 3+ agent payouts each. connection.queries should not have cascading reservation queries.
- **Verification:** (refuted) Lines 885-887 use len(agent_payout.reservations.all()). The reservations are already prefetched at lines 871-877. Calling .all() on a prefetched relation returns the cached list, so len() is a pure Python operation. No database hits.

#### AGENTS-11 — Low — **REFUTED**
- **File / symbol:** `users/eligibility.py` :: `sum_ready / sum_pending`  (lines 277-284, 317-324)
- **Issue type:** N+1  |  **Fix risk:** Low
- **Evidence:** def sum_ready(agent, *, now=None, grace_hours=DEFAULT_GRACE_HOURS) -> Decimal:     total = Decimal("0")     for res in _agent_reservations(agent):         result = get_commission_eligibility(res, now=now, grace_hours=grace_hours)         if result.status == STATUS_READY:             total += result.commission     return total.quantize(Decimal("0.01"))
- **Why slow/risky:** Called on every agent during dashboard load, preview action, or bulk payout. Each call queries Reservations + prefetches legs for that agent. In preview_agency_payout with 10 agents, 10 separate Reservation + Prefetch pairs fire. These are slow when called per-agent.
- **Fix:** Use bulk_ready_totals(agent_ids) instead when working with multiple agents. For single agent, this is correct but could cache the result for the request lifetime.
- **Expected impact:** Agency dashboard with 10 agents calls sum_ready 10 times = 20 queries (1 reservation + 1 prefetch per agent). Dashboard blocks for seconds with many agents.
- **How to test:** Load AgencyHeadDashboardView for an agency with 10 agents. connection.queries count should use bulk_ready_totals or cache internally to avoid 10 per-agent Reservation queries.
- **Verification:** (refuted) Lines 277-284: sum_ready() loops through _agent_reservations(agent) and calls get_commission_eligibility() on each. However, _agent_reservations() at lines 254-258 already prefetches legs. The iteration is over in-memory prefetched data, not database queries. For single agents, this is correct.

#### AGENTS-12 — Low — **REFUTED**
- **File / symbol:** `users/views.py` :: `AgencyHeadDashboardView.get_context_data`  (lines 798-807)
- **Issue type:** N+1  |  **Fix risk:** Low
- **Evidence:** recent_reservations = (     Reservation.objects.filter(travel_agent__in=agency.agents.all())     .select_related("customer", "vehicle", "travel_agent")     .order_by("-created_at")[:10] ) recent_payouts = (     CommissionPayout.objects.filter(agent__in=agency.agents.all())     .select_related("agent")     .order_by("-paid_at")[:8] )
- **Why slow/risky:** Both queries use agency.agents.all() inline, which is unprefetched. Each .filter(agent__in=agency.agents.all()) forces a subquery. If query optimization is deferred, Postgres executes subquery per call.
- **Fix:** Fetch agent IDs once: agent_ids = list(agency.agents.values_list('id', flat=True)), then use filter(agent_id__in=agent_ids). Or prefetch agents on the agency queryset and pass the list.
- **Expected impact:** Dashboard page could fire subqueries for agents instead of using precomputed agent list. Minor but adds latency to already-slow view.
- **How to test:** Load AgencyHeadDashboardView. EXPLAIN or connection.queries should show agent__in clauses using actual IDs, not nested subqueries.
- **Verification:** (refuted) Lines 798-807 use filter(travel_agent__in=agency.agents.all()) and filter(agent__in=agency.agents.all()). Passing a queryset to __in is converted to a SQL subquery by Django, not evaluated as multiple trips. This is efficient SQL.

---

### Background jobs, schedulers, threads, management commands, external integrations

#### BG-01 — Critical
- **File / symbol:** `ghl_integration/tasks.py` :: `sync_lead_to_ghl_and_send_sms`  (lines 187-192)
- **Issue type:** sync-external-API  |  **Fix risk:** High  |  *Needs measurement*
- **Evidence:** Line 187-192: time_module.sleep(delay) with 60s, 120s, 180s backoff inside sync_lead_to_ghl_and_send_sms exception handler. These retries block the entire single gunicorn worker for up to 180 seconds per lead.
- **Why slow/risky:** With one gunicorn sync worker and no Celery, each lead retry that hits an exception (network timeout, GHL API error) sleeps 1-3min inside the worker. Multiple concurrent requests to batch_send_unsent_leads or manual syncs can pyramid retries, blocking all requests.
- **Fix:** Remove retries from the sync function entirely. Instead, log_sync_failure() should set next_retry_at and let retry_failed_syncs() (which runs async in the scheduler) handle retries on a schedule. Move retries OUT OF the request path into the background batch.
- **Expected impact:** Unblocks gunicorn worker from retry sleeps. Web requests (reservation, payment, dispatch pages) can be blocked for 3+ minutes on a single lead GHL sync failure.
- **How to test:** Integration test: mock GHL API to fail 2x, verify first call logs FAILED with next_retry_at set, no sleep blocks request. Second test: scheduler retry_failed_syncs picks it up after next_retry_at.
- **Verification:** (confirmed) Lines 187-192 confirmed: time_module.sleep(delay) with 60s, 120s, 180s backoff in exception handler of sync_lead_to_ghl_and_send_sms. In single-worker prod with 60s timeout, a 180s sleep WILL timeout and potentially crash the worker or make it unresponsive.

#### BG-02 — High
- **File / symbol:** `ghl_integration/tasks.py` :: `process_follow_up_batch`  (lines 740)
- **Issue type:** polling  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** Line 740: time_module.sleep(1) in a for loop iterating due_task_ids[:100]. On each batch cycle, 100 tasks x 1s = 100 seconds of blocking sleep inside the worker.
- **Why slow/risky:** The scheduler runs this in the single gunicorn worker thread. Scheduler holds the worker for ~100 seconds per cycle sending ~100 SMS. If any SMS send hangs (timeout=10 on line 690), the sleep(1) + the hang can total 1100 seconds per task.
- **Fix:** Remove sleep(1). Instead, batch all send_sms calls and run them in parallel with concurrent.futures.ThreadPoolExecutor (bounded to 10-20 threads) or remove it entirely if rate limiting is delegated to GHL. If rate limiting is critical, use a sliding-window token bucket (e.g., limits library) instead of fixed sleep.
- **Expected impact:** Frees gunicorn worker during SMS sends. Current: scheduler blocks worker ~100s/cycle. Proposed: <5s even for 100 SMSes.
- **How to test:** Mock 100 SMS sends to take 1s each. Measure total time in scheduler run. Verify no sleep(1) per send and completion in <5s wall-clock.
- **Verification:** (confirmed) Line 740 confirmed: time_module.sleep(1) inside for loop in process_follow_up_batch iterating due_task_ids[:100]. Up to 100 tasks * 1s = 100s blocking in scheduler thread (not request worker, so less critical than BG-01). However, delays next cycle by up to 100s.

#### BG-04 — High
- **File / symbol:** `ghl_integration/scheduler.py` :: `_run_scheduler`  (lines 51-73, 76-169)
- **Issue type:** sync-external-API  |  **Fix risk:** High  |  *Needs measurement*
- **Evidence:** Line 58-73: The scheduler thread sleeps 60s at startup and then sleeps 30min between cycles. However, ALL tasks called within _run_batch_tasks (batch_send_unsent_leads, process_follow_up_batch, retry_failed_syncs, detect_lost_leads, send_pre_pickup_nudges, auto_refresh_flights, generate_ops_tasks) run SYNCHRONOUSLY in the scheduler thread, blocking the worker.
- **Why slow/risky:** The scheduler runs inside the single gunicorn worker as a daemon thread. If any of the 7 batch tasks makes a slow GHL/Twilio/AeroAPI call or hits an exception (see BG-01), the scheduler blocks the worker. During 30 min cycle window, the cumulative time from all these batches (~300+ seconds based on sleeps + API calls) consumes a significant portion of the worker's cycle.
- **Fix:** Offload the heaviest tasks to truly background threads spawned via run_in_background(). Specifically: batch_send_unsent_leads, process_follow_up_batch, retry_failed_syncs, detect_lost_leads, and send_pre_pickup_nudges should each spawn a run_in_background thread so the scheduler thread returns quickly and can trigger the next cycle on time.
- **Expected impact:** Scheduler can miss cycles or run behind if any batch task hangs. Web requests during batch-heavy cycles will timeout.
- **How to test:** Mock a batch task to sleep 5s. Verify scheduler can still call next batch task and doesn't block waiting. Verify pg_try_advisory_lock prevents two workers from running simultaneously.
- **Verification:** (confirmed) Lines 51-73, 76-169 confirmed: _run_scheduler() and _run_batch_tasks() run synchronously in a daemon thread. All 7 task functions (batch_send_unsent_leads, process_follow_up_batch, retry_failed_syncs, detect_lost_leads, send_pre_pickup_nudges, auto_refresh_flights, generate_ops_tasks) are called directly, blocking the scheduler thread until ALL complete. If any single task is slow (e.g., process_follow_up_batch with 100 tasks * 1s sleep = 100s), next 30min cycle is delayed.

#### BG-05 — High
- **File / symbol:** `ghl_integration/pre_pickup.py` :: `send_pre_pickup_nudges`  (lines 367)
- **Issue type:** polling  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** Line 367 (referenced from scheduler.py line 125): time_module.sleep(1) inside _send_nudge loop. The nudge engine processes up to ~200 leads per cycle with 1s sleep each = 200 seconds blocking.
- **Why slow/risky:** Similar to process_follow_up_batch (BG-02), this runs synchronously in the scheduler thread every 2 cycles. Each SMS send sleeps 1s, blocking the worker.
- **Fix:** Same fix as BG-02: remove sleep(1) and batch SMS sends with ThreadPoolExecutor or request-level rate limiting.
- **Expected impact:** Blocks worker for 200+ seconds every 60 minutes during pre-pickup nudge cycle.
- **How to test:** Mock 200 nudge SMS sends. Verify completion in <10s without sleep(1).
- **Verification:** (confirmed) Line 367 confirmed: time_module.sleep(1) in _send_nudge() called per lead in PrePickupNudgeEngine.process() loop. Blocks scheduler thread for ~200s if ~200 nudges due per cycle. Not a request-handler block, but delays scheduler cycle.

#### BG-15 — High
- **File / symbol:** `ops/tasks.py` :: `detect_driver_conflicts / _turn_late_minutes`  (lines 171-186)
- **Issue type:** sync-external-API  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Lines 171-186 (_reposition_minutes): For each potential driver conflict, the function calls google_drive_time(from_location, to_location) which is a synchronous Google Maps API call. If a leg has multiple potential prior legs, each conflict check calls Google Maps. With no caching.
- **Why slow/risky:** Ops task generation runs every 30 minutes and scans all upcoming legs for driver conflicts. For each leg, if the driver has prior legs, it calls Google Maps for drive time. No caching of drive times between legs. A scenario with 100 legs x 2 prior legs x 2s per Google API call = 400 seconds of blocking.
- **Fix:** Implement a request-level cache for drive times within a single ops task generation run (e.g., functools.lru_cache on _reposition_minutes). Or use a fallback categorization-based lookup (already partially implemented line 179-183) to avoid the API call when possible.
- **Expected impact:** Ops task generation can block scheduler for 100+ seconds on high-leg days.
- **How to test:** Generate ops tasks for 100 legs with 2 prior legs each. Measure Google API calls. Should be 100-150, not 400.
- **Verification:** (confirmed) Lines 165-186: _reposition_minutes() calls google_drive_time() (line 173) synchronously on every call with no caching. Called from _turn_late_minutes (line 199) which is called from classify_turn (line 235) during ops task generation. Multiple legs * multiple prior legs = many uncached Google Maps API calls per cycle.

#### BG-03 — Medium
- **File / symbol:** `ghl_integration/tasks.py` :: `retry_failed_syncs`  (lines 884)
- **Issue type:** polling  |  **Fix risk:** Low
- **Evidence:** Line 884: time_module.sleep(0.5) inside retry_failed_syncs for loop over up to 50 failed_logs. Total: 25 seconds per cycle of scheduler.
- **Why slow/risky:** Blocks the single gunicorn worker for 25 seconds during the scheduler batch, which runs every 30 minutes. If paired with process_follow_up_batch (another 100s), the scheduler holds the worker for 125s minimum.
- **Fix:** Remove the 0.5s sleep. Batch all retry API calls with a bounded ThreadPoolExecutor (5-10 threads) to parallelize GHL service.* calls. GHL rate limiting should be handled per-contact ID or per-location globally, not per-loop.
- **Expected impact:** Frees worker during retry batch. Currently: 25s per cycle per task. Proposed: 2-3s with 10 threads.
- **How to test:** Mock 50 failed syncs. Measure retry_failed_syncs time without sleep(0.5). Verify completion in <5s and no duplicate retries.

#### BG-07 — Medium
- **File / symbol:** `payment/views.py` :: `payment_success`  (lines 167-178)
- **Issue type:** sync-external-API  |  **Fix risk:** Low
- **Evidence:** Lines 167-178: payment_success view spawns a Thread to call send_purchase_event. Similar to BG-06, but this is a user-facing page, so thread spawning directly in the view is anti-pattern.
- **Why slow/risky:** User loads success page, thread spawns, but page returns immediately. If thread fails, user never sees the error. Threads can accumulate on the page if multiple users load it.
- **Fix:** Use run_in_background() instead of Thread(). Alternatively, move send_purchase_event to a post_save signal or a queued task.
- **Expected impact:** Thread leaks on high traffic to the success page.
- **How to test:** Load success page 100x rapidly. Verify no thread leak.

#### BG-08 — Medium (orig High -> Medium)
- **File / symbol:** `dispatching/views.py` :: `refresh_all_flights`  (lines 4894-4897)
- **Issue type:** sync-external-API  |  **Fix risk:** High  |  *Needs measurement*
- **Evidence:** Lines 4894-4897: refresh_all_flights spawns a Thread to run _run_bulk_flight_refresh. This is an admin/staff action that can refresh 100+ flights. The thread calls AeroAPI repeatedly in a tight loop (see _run_bulk_flight_refresh in dispatching/aeroapi_service.py).
- **Why slow/risky:** Dispatcher clicks 'Refresh All Flights' for a date, thread starts. Each flight refresh is an HTTP call to AeroAPI. If 50 flights x 2s each = 100s of network I/O in a thread, but no parallelism => requests block thread one-by-one. Meanwhile, the gunicorn worker handles normal traffic, competing for GIL and DB connections.
- **Fix:** Use ThreadPoolExecutor(max_workers=5) inside _run_bulk_flight_refresh to parallelize AeroAPI calls. Or use run_in_background to decouple from the view and return 202 Accepted immediately.
- **Expected impact:** Bulk flight refreshes can cause the worker to slow down due to GIL contention if many threads are spawned.
- **How to test:** Refresh 100 flights. Verify throughput is parallelized (10 threads x 5s each = 50s, not 1000s sequential).
- **Verification:** (confirmed) Lines 4894-4897 confirmed: daemon Thread spawned for _run_bulk_flight_refresh. Does NOT block the view response (returns JsonResponse at line 4899), but thread runs serially in background calling AeroAPI 100+ times. Lower severity than request-blocking patterns since response is async, but still a GIL concern if multiple users trigger this.

#### BG-11 — Medium
- **File / symbol:** `reservations/signals.py` :: `reservation_saved`  (lines 29-49)
- **Issue type:** signal-side-effect  |  **Fix risk:** Low
- **Evidence:** Lines 29-49: Post-save signal spawns a Thread to call send_internal_confirmation. This runs for EVERY new reservation, spawning a thread per reservation.
- **Why slow/risky:** High-traffic scenario: 100 reservations booked in 1 minute = 100 threads spawned. Threads compete for GIL and SMTP connection pool.
- **Fix:** Use run_in_background(send_internal_confirmation, instance) instead. This gives you a bounded thread pool.
- **Expected impact:** High-traffic booking periods cause thread proliferation and GIL contention.
- **How to test:** Create 50 reservations rapidly. Verify <=10 threads in pool, not 50.

#### BG-12 — Medium
- **File / symbol:** `ghl_integration/tasks.py` :: `batch_send_unsent_leads / _rescue_stale_leads`  (lines 277-289)
- **Issue type:** N+1  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Lines 277-284: Lead.objects.filter(...).values_list('id', flat=True)[:50] fetches 50 lead IDs. Then line 287-289: for lead_id in unsent_leads: run_in_background(sync_lead_to_ghl_and_send_sms, lead_id). Each background task calls Lead.objects.get(id=lead_id) (line 352), causing 50+ queries to fetch the same leads that were already filtered.
- **Why slow/risky:** Batch fetches lead IDs, then spawns 50 background tasks, each doing a separate get(). Instead of 1 query to load all leads upfront, you get 1 query for IDs + 50 queries for full leads.
- **Fix:** Batch-load all leads upfront with select_related('vehicle') and pass lead objects to run_in_background, or pass only minimal fields needed to spawn the task and accept the redundant load (sync_lead_to_ghl_and_send_sms uses select_for_update anyway).
- **Expected impact:** 50+ unnecessary DB queries per batch cycle. With 30-min cycles, this is ~100 extra queries per hour.
- **How to test:** Measure query count in batch_send_unsent_leads. Should be ~1 query to load leads, not 51.

#### BG-13 — Medium
- **File / symbol:** `ghl_integration/tasks.py` :: `start_follow_up_sequence`  (lines 443-457)
- **Issue type:** query-in-loop  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Lines 443-457: A loop searches other_active leads for phone number collision. For each active lead, it extracts digits and compares. No indexing on this, and the loop is O(n) where n = active leads with sequences.
- **Why slow/risky:** If 1000 leads have active sequences, and you're starting a sequence for a new lead, the inner loop does 1000 phone comparisons. Each FollowUpTask row has a lead_id, but there's no indexed query like Lead.objects.filter(normalized_phone=norm, sequence_active=True).
- **Fix:** Replace the loop with a single query: Lead.objects.filter(normalized_phone=lead.normalized_phone, sequence_active=True).exclude(id=lead.id).exists(). Already normalized_phone exists (used elsewhere).
- **Expected impact:** Slow sequence startup for leads when many sequences are active. Scales with the lead base.
- **How to test:** Create 1000 active sequences. Start a new sequence for a duplicate phone. Measure time. Should be <1ms, not 100ms.

#### BG-14 — Medium
- **File / symbol:** `ghl_integration/services.py` :: `contact_has_replied`  (lines 615-633)
- **Issue type:** sync-external-API  |  **Fix risk:** High  |  *Needs measurement*
- **Evidence:** Lines 579-620: contact_has_replied makes TWO synchronous requests.get() calls inside process_follow_up_batch for EACH lead whose sequence is still active (safety net). Line 582 fetches conversations, then line 599 fetches messages in a loop.
- **Why slow/risky:** Called inside process_follow_up_batch for ~100 leads x up to 2 requests each = 200 synchronous GHL API calls (~2s each with timeout=10). This runs in the scheduler and blocks the worker by ~400 seconds.
- **Fix:** Move contact_has_replied check outside the tight send loop. Instead, batch the check BEFORE sending (e.g., prefetch all conversations in one call if GHL API allows, or skip the check for leads that haven't been contacted in >2 days).
- **Expected impact:** Scheduler spends 400+ seconds checking for replies before sending follow-ups.
- **How to test:** Mock 100 leads with contact_has_replied called on each. Measure time with and without the check. Ensure reply detection still works.

#### BG-16 — Medium
- **File / symbol:** `ghl_integration/tasks.py` :: `process_follow_up_batch`  (lines 666-678)
- **Issue type:** N+1  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Lines 552-565: The batch fetches due_task_ids (lightweight query), then loops through task_ids. Inside the loop (line 577-741), for each task, it selects_for_update to load the task and its related lead. Then line 666-678 queries FollowUpSequence to find the template. No batching of template fetches.
- **Why slow/risky:** If 100 tasks, and each task looks up its FollowUpSequence separately (queries by step_number, segment, is_active), you get 100 queries for templates that might be the same (e.g., all step 2, segment 'general'). A batch prefetch of unique templates would be 1 query.
- **Fix:** Batch-fetch all unique (step_number, segment) combinations from FollowUpSequence upfront and memoize in a dict. Then look up in the dict in the loop.
- **Expected impact:** 50-100 unnecessary template queries per batch cycle.
- **How to test:** Measure query count in process_follow_up_batch. Should be ~1 template query, not 100.

#### BG-17 — Medium
- **File / symbol:** `ghl_integration/tasks.py` :: `detect_lost_leads`  (lines 965-977)
- **Issue type:** unbounded-queryset  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** Line 965-977: detect_lost_leads filters Lead.objects.filter(pickup_date__lt=today, ...) without a date limit. The query finds ALL leads past pickup, capped at 200 in the loop (line 977: [:200]). But no index on (pickup_date, status, converted) and no batching to rerun if 200+ exist.
- **Why slow/risky:** If 10,000 leads are past pickup and need to be marked lost, the task runs 50 times (50 x 200) to catch them all. Each run is ~1 second, totaling 50 seconds per cycle. AND if the task crashes mid-run, no resume state, so it starts from the top again.
- **Fix:** Use a batching strategy with an explicit cursor (e.g., WHERE pickup_date < today AND id > last_id LIMIT 200) to detect and continue from a previous run. Or set a recurring flag like 'auto_lost_checked_at' on the lead to mark that it's been processed.
- **Expected impact:** Large backlog of expired leads can cause ops task generation to slow down significantly.
- **How to test:** Create 10,000 leads past pickup. Measure detect_lost_leads time. Should be constant (1 iteration x 200 leads), not 50 iterations.

#### BG-18 — Medium
- **File / symbol:** `reservations/admin.py` :: `mark_converted action`  (lines 2294-2329)
- **Issue type:** N+1  |  **Fix risk:** High  |  *Needs measurement*
- **Evidence:** Lines 2281-2329: mark_converted builds a ReservationIndex from all reservations (line 2294-2298), then loops through leads (line 2291: leads = list(queryset)) and calls match_lead(index, lead) for each lead. Each match_lead call scans the index for matches. Index is built once but searched 50+ times.
- **Why slow/risky:** Index building is O(n) where n = total reservations. For 1000 reservations, index build is fast. But for 100,000 reservations, building the index once is expensive, and the admin action might time out.
- **Fix:** No immediate fix without understanding match_lead's complexity. But consider: if index build is slow, cache it for 5 minutes per location/date. Or implement a fuzzy search table (denormalized match hints) to accelerate matching.
- **Expected impact:** Bulk mark_converted on a large lead set can timeout if the reservation index is large.
- **How to test:** Bulk-mark 50 leads as converted when total reservations = 100,000. Measure time. Should be <10s.

#### BG-19 — Medium
- **File / symbol:** `ghl_integration/runner.py` :: `run_in_background`  (lines 14-27)
- **Issue type:** thread-safety  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Lines 14-27: run_in_background spawns a new daemon thread for every task. No bounded thread pool, no semaphore. If you call run_in_background 1000 times in a second (e.g., batch_send_unsent_leads queuing 1000 leads), you spawn 1000 threads.
- **Why slow/risky:** Unbounded thread spawning can exhaust OS thread limits and cause memory pressure. Python thread overhead is ~8MB per thread; 1000 threads = 8GB.
- **Fix:** Implement a bounded thread pool using concurrent.futures.ThreadPoolExecutor or use a library like queue.Queue with a fixed number of worker threads. Cache the executor in the module.
- **Expected impact:** High-traffic scenarios (e.g., bulk lead import) can crash the worker due to thread exhaustion.
- **How to test:** Call run_in_background 1000 times. Verify max threads = N (pool size), not 1000.

#### BG-06 — Low — **REFUTED**
- **File / symbol:** `payment/webhook.py` :: `stripe_webhook / handle_checkout_session`  (lines 230-235)
- **Issue type:** sync-external-API  |  **Fix risk:** Low
- **Evidence:** Lines 230-235: stripe_webhook spawns a daemon Thread to call send_purchase_event in the webhook handler. The webhook is synchronous and MUST return 200 to Stripe within ~5 seconds. If send_purchase_event (a Meta Conversions API call) times out or is slow, it will not block the response, BUT it competes with the gunicorn worker for the GIL and other threads.
- **Why slow/risky:** Multiple webhook threads spawned per payment can accumulate in the single worker, competing for CPU and connections. A slow Meta API call in one thread blocks other request handling via GIL contention.
- **Fix:** Use run_in_background(send_purchase_event, ...) instead of Thread().start(). This is consistent with the rest of the codebase and allows a bounded thread pool to prevent unbounded growth.
- **Expected impact:** Webhook threads can accumulate if Stripe sends batches of webhooks or retries. Worker GIL contention on slow Meta API calls.
- **How to test:** Send 10 rapid webhook calls. Verify no thread leak and Meta API calls don't block 200 response.
- **Verification:** (refuted) Lines 230-235: A daemon Thread IS spawned for send_purchase_event, BUT it does NOT block the webhook response. Line 239 shows payment_result is returned immediately while thread runs asynchronously. The thread competes for GIL with other request handlers, but does not block the webhook return to Stripe.

#### BG-09 — Low — **REFUTED**
- **File / symbol:** `reservations/admin.py` :: `mark_converted action`  (lines 2214-2278)
- **Issue type:** sync-external-API  |  **Fix risk:** High  |  *Needs measurement*
- **Evidence:** Lines 2214-2249: mark_converted action spawns a daemon Thread (line 2278) to sync status to GHL for all selected leads. Bulk actions can select 50+ leads. Thread loops through and calls service.update_contact_status_fields (which makes a PUT to GHL) for each lead serially (no parallelism in the thread).
- **Why slow/risky:** Admin bulk-marks 50 leads as converted, a thread spawns and makes 50 GHL API calls serially (~10s each with timeout=10). Meanwhile, the admin page returns but the thread runs in the worker, slowing other requests via GIL.
- **Fix:** Move the sync to run_in_background with a bounded thread count for parallel GHL calls. Or better: batch the GHL updates into a single call if GHL API supports bulk operations.
- **Expected impact:** Admin bulk actions block the worker for 10-50s via GIL contention.
- **How to test:** Bulk-mark 50 leads as converted via admin. Verify thread completes in <5s (parallel) not 50s (serial).
- **Verification:** (refuted) Lines 2280-2316: mark_converted action calls _sync_status_to_ghl_in_background (line 2316), which spawns a SINGLE daemon thread (line 2278) that syncs serially, not per-lead. Critically, the action returns message_user (line 2318) immediately without waiting for the thread. No request blocking.

#### BG-10 — Low — **REFUTED**
- **File / symbol:** `reservations/admin.py` :: `mark_contacted / mark_interested actions`  (lines 2182-2251)
- **Issue type:** sync-external-API  |  **Fix risk:** Low
- **Evidence:** Lines 2182-2215 and 2219-2251: Similar to mark_converted. Two separate actions, each spawning a thread to sync status to GHL.
- **Why slow/risky:** Duplicate pattern as BG-09. Every bulk lead admin action spawns a sync thread.
- **Fix:** Consolidate into _sync_status_to_ghl_in_background (already defined at line 2253) and call it from all three actions.
- **Expected impact:** Same as BG-09.
- **How to test:** Verify all three actions (contacted, interested, converted) use the same helper and complete in <5s.
- **Verification:** (refuted) Lines 2182-2217 and 2220-2251: mark_contacted and mark_interested each spawn ONE daemon thread that syncs all selected leads serially in background. Actions return message_user immediately (lines 2217, 2251) without blocking. No request-handler blocking.

#### BG-20 — Low
- **File / symbol:** `ghl_integration/tasks.py` :: `retry_failed_syncs`  (lines 822-828)
- **Issue type:** polling  |  **Fix risk:** Low
- **Evidence:** Lines 822-828: retry_failed_syncs fetches [:50] failed_logs and retries them. If 1000 logs are failed, it takes 20 cycles to clear them. If retries are all fast, the queue clears. But if retries are slow (e.g., GHL API down), the queue grows unbounded.
- **Why slow/risky:** No explicit retry limit or dead-letter age. A sync can be stuck in FAILED status for weeks if GHL API is intermittently down.
- **Fix:** Set a max_attempts limit (already exists as sync_log.max_attempts, default unclear) and move old FAILED logs to DEAD_LETTER after max_attempts is exhausted (already done line 60-61). Ensure dead-letter cleanup runs.
- **Expected impact:** Old failed syncs accumulate and waste DB space.
- **How to test:** Verify a failed sync moves to DEAD_LETTER after 5 attempts. Verify dead-letter alert runs.

---

### Django Admin (reservations, users, drivers, rates, payment, ops, dispatching, ghl_integration, services, blog)

#### ADMIN-08 — Critical
- **File / symbol:** `drivers/admin.py` :: `DriverAdmin.process_driver_payments`  (lines 680-750)
- **Issue type:** query-in-loop  |  **Fix risk:** Medium
- **Evidence:** for driver in queryset:     unpaid_legs = driver.get_unpaid_legs().filter(status="completed")     ...     for leg in unpaid_legs:         ...     payment = DriverPayment.create_payment(         driver=driver, legs=unpaid_legs, ..., created_by=request.user     )
- **Why slow/risky:** Admin action loops over drivers, calls get_unpaid_legs() per driver (potential N+1), then creates DriverPayment with .save() inside the loop. If create_payment triggers related saves (e.g., LegPayment rows), multiply effect. Single worker blocks entire site during payment processing.
- **Fix:** Bulk-fetch all legs upfront. Use bulk_create for DriverPayment and LegPayment instead of per-driver loop with .save().
- **Expected impact:** Prevents 10+ drivers × save() calls + related saves. Each .save() triggers signal handlers (simple_history), multiplying the issue.
- **How to test:** Select 5 drivers, process payments. Verify job completes in <2s (not 10-30s). Check query count < 50.
- **Verification:** (confirmed) Lines 690-760: for loop over queryset. Each iteration calls driver.get_unpaid_legs() (line 692), which is a fresh query per driver. Then DriverPayment.create_payment() is called per driver (line 753). While create_payment does use bulk_create for LegPayment records (line 693), the outer loop itself triggers one get_unpaid_legs query per driver, making it query-in-loop. Additionally, line 709 calls sum(leg.total_driver_pay for leg in unpaid_legs) which materializes the legs in Python.

#### ADMIN-10 — Critical
- **File / symbol:** `reservations/admin.py` :: `LeadAdmin.start_follow_up_sequence_action`  (lines 2712-2733)
- **Issue type:** sync-external-API  |  **Fix risk:** High  |  *Needs measurement*
- **Evidence:** for lead in queryset:     if lead.sequence_active or lead.converted or not lead.phone or not lead.initial_sms_sent:         skipped += 1         continue     try:         run_in_background(start_follow_up_sequence, lead.id)
- **Why slow/risky:** Admin action loops over leads and calls run_in_background() per lead. This spawns a daemon thread inside the Gunicorn worker. If 20 leads selected, 20 threads spawn competing for GIL in 1 worker process. GHL integration includes external API calls (Twilio/GHL). Threads block on I/O, pile up in the worker. Other requests queue behind.
- **Fix:** Do not spawn threads from admin actions. Instead: queue to Celery/Redis, or execute async after request (Django signals + defer). If GHL calls are sync, they block the thread anyway — better to queue and process separately.
- **Expected impact:** Prevents thread explosion in single worker from admin action. Each admin action that spawns threads degrades site performance for all users.
- **How to test:** Select 20 leads, run action. Monitor worker process thread count. If it spikes to 20+, issue is real. Check response times on other requests—they will spike.
- **Verification:** (confirmed) Lines 2724: run_in_background(start_follow_up_sequence, lead.id) spawns a daemon thread per lead (see ghl_integration/runner.py lines 14-27). In a single-worker Gunicorn with daemon threads, any I/O-bound work in the thread (e.g., GHL API calls inside start_follow_up_sequence) competes with the request thread for the GIL and process resources. The thread-based approach does not scale and risks blocking the main request.

#### ADMIN-01 — High
- **File / symbol:** `reservations/admin.py` :: `ReservationAdmin.payment_status_display`  (lines 1227-1260)
- **Issue type:** N+1  |  **Fix risk:** Low
- **Evidence:** def payment_status_display(self, obj):     if not hasattr(obj, "payments") or not obj.payments.exists():         return "-"     payment = obj.payments.order_by('-created_at').first()
- **Why slow/risky:** Called once per row in the Reservation changelist (50 rows default). For each reservation, .exists() then .order_by().first() issue two queries. The prefetch_related in get_queryset includes 'payments' but .exists() and .order_by().first() inside the display method bypass the cache and execute fresh queries per row. With 50 rows per page and single Gunicorn worker, this blocks all other requests while processing ~100 extra queries.
- **Fix:** In get_queryset (line 923), annotate the latest payment using Subquery or F-expressions. Store in obj._latest_payment_id and fetch in template/display method via prefetch. Or use raw annotation: Latest('payments__created_at').
- **Expected impact:** Prevents 50+ extra queries per admin changelist page load. In production with DEBUG=True, each query is cached in connection.queries unbounded.
- **How to test:** Load Reservation changelist in admin. Using django-debug-toolbar or connection.queries, verify payment_status_display does not execute .exists() or .order_by().first() queries per row.
- **Verification:** (confirmed) payment_status_display (line 1233) calls obj.payments.order_by('-created_at').first() which bypasses the prefetch and reruns a query. Though 'payments' is prefetched in get_queryset line 925, calling .order_by().first() on the related manager does not use the prefetch cache—it queries the DB again. This is N+1 per object in list view.

#### ADMIN-02 — High (orig Critical -> High)
- **File / symbol:** `users/admin.py` :: `TravelAgentAdmin._calculate_commission_preview`  (lines 592-634)
- **Issue type:** query-in-loop  |  **Fix risk:** Low
- **Evidence:** for agent in queryset:     unpaid_reservations = Reservation.objects.filter(         travel_agent=agent, commission_paid=False, status="completed"     )...     if unpaid_reservations.exists():         commission_total = sum(r.calculated_commission for r in unpaid_reservations)         for reservation in unpaid_reservations:             for leg in reservation.legs.all():
- **Why slow/risky:** Admin action loops over selected agents (e.g., 5-50), issues one Reservation query per agent. Inside that loop, iterates unpaid_reservations and calls .legs.all() per reservation with N+1. If 5 agents × 10 reservations each × unprefetched legs = 50 additional query operations. Running in sync worker (1 worker), blocks entire site during action.
- **Fix:** Prefetch related legs before the loop: unpaid_reservations.prefetch_related('legs'). Better: bulk aggregate in DB using Subquery and Min() to find earliest_leg_date, avoiding the nested loop entirely.
- **Expected impact:** Prevents 50-100+ queries during admin action. Staff action invoked by user clicks should complete in <500ms, not seconds.
- **How to test:** Select 5 agents and run 'Preview commission payments'. With django-debug-toolbar, verify total queries does not exceed 10-15; without fix, will exceed 50+.
- **Verification:** (confirmed) Lines 615-618 iterate reservation.legs.all() inside nested for loops per agent per reservation, with NO prefetch_related on unpaid_reservations before line 615. This causes one query per reservation to fetch its legs, making it query-in-loop (one extra query per unpaid reservation per agent).

#### ADMIN-03 — High
- **File / symbol:** `drivers/admin.py` :: `DriverPaymentAdmin.profit_display`  (lines 973-977)
- **Issue type:** N+1  |  **Fix risk:** Low
- **Evidence:** def profit_display(self, obj):     profit = obj.leg.profit_estimate or 0
- **Why slow/risky:** LegPaymentAdmin list_display calls profit_display per row. list_select_related is NOT set for 'leg', so obj.leg triggers a query per row. On a changelist with 50 rows and single worker, this is 50 extra queries that block the site.
- **Fix:** Add list_select_related = ('payment__driver', 'leg', 'leg__reservation') to LegPaymentAdmin (around line 925-935).
- **Expected impact:** Prevents 50 queries per admin changelist page for LegPayment.
- **How to test:** Load LegPayment admin changelist. Verify only 2-3 queries (base + leg.select_related), not 50+.
- **Verification:** (confirmed) LegPaymentAdmin (line 926) has no get_queryset override and no list_select_related defined. profit_display (line 974) accesses obj.leg.profit_estimate, requiring one query per LegPayment object in the list. This is N+1.

#### ADMIN-04 — High
- **File / symbol:** `reservations/admin.py` :: `FlightAdmin.is_in_use`  (lines 1682-1694)
- **Issue type:** N+1  |  **Fix risk:** Low
- **Evidence:** def is_in_use(self, obj):     if hasattr(obj, 'is_linked'):         in_use = obj.is_linked     else:         from .models import Leg         in_use = Leg.objects.filter(flight_information=obj).exists()
- **Why slow/risky:** Called per row in FlightAdmin changelist. Relies on annotation 'is_linked' from get_queryset (line 1668), but includes fallback Leg.objects.filter().exists() if annotation missing. If annotation not always present or on detail view, executes N+1 query per row.
- **Fix:** Ensure get_queryset always includes the annotation and remove the fallback .exists() query. Add assertion that obj.is_linked exists.
- **Expected impact:** Prevents up to 50 extra queries per admin page (or all queries if annotation breaks).
- **How to test:** Load Flight admin list. Verify is_in_use only uses prefetched annotation, no extra queries.
- **Verification:** (confirmed) is_in_use (lines 1684-1688) includes a fallback: if not hasattr(obj, 'is_linked'), it executes Leg.objects.filter(flight_information=obj).exists(). Although get_queryset annotates is_linked via Exists subquery (line 1668), any code path where the annotation is missing (e.g., direct model instantiation, other views) will trigger an extra query per flight.

#### ADMIN-06 — High
- **File / symbol:** `users/admin.py` :: `TravelAgentAdmin.mark_agents_for_payment`  (lines 578-590)
- **Issue type:** query-in-loop  |  **Fix risk:** Low
- **Evidence:** agents_with_unpaid = queryset.filter(unpaid_commissions__gt=0) total_unpaid = sum(agent.unpaid_commissions for agent in agents_with_unpaid)
- **Why slow/risky:** Admin action filters queryset then sums a field in a Python loop. The sum(agent.unpaid_commissions for agent in agents_with_unpaid) forces evaluation of all rows and accesses unpaid_commissions property per agent. If property triggers a query, this is N+1.
- **Fix:** Use queryset.aggregate(Sum('unpaid_commissions')) instead of Python sum loop.
- **Expected impact:** Prevents N+1 queries if unpaid_commissions is a property or computed field.
- **How to test:** Run action on 10 agents. Verify total_unpaid computed in one aggregate query, not evaluated in Python loop.
- **Verification:** (confirmed) line 583 uses Python sum(agent.unpaid_commissions for agent in agents_with_unpaid), which iterates the queryset and materializes each agent object. This is not using a database aggregate(Sum(...)). In a production list with hundreds of agents, this materializes all rows into Python memory.

#### ADMIN-07 — High
- **File / symbol:** `drivers/admin.py` :: `DriverAdmin.preview_driver_payments`  (lines 600-634)
- **Issue type:** query-in-loop  |  **Fix risk:** Low
- **Evidence:** for driver in queryset:     unpaid_legs = driver.get_unpaid_legs().filter(status="completed")     if unpaid_legs:         payment_total = sum(leg.total_driver_pay for leg in unpaid_legs)         leg_dates = [leg.pickup_date for leg in unpaid_legs if leg.pickup_date]
- **Why slow/risky:** Admin action loops over selected drivers and calls get_unpaid_legs() per driver. Each call likely queries the DB. Then iterates legs in Python loop. If driver.get_unpaid_legs() is not optimized, this is N+1.
- **Fix:** Pre-fetch all unpaid legs for all selected drivers in a single prefetch_related before the loop, or use Leg.objects.filter(driver__in=driver_ids, payment_status='unpaid').
- **Expected impact:** Prevents N drivers × M queries per driver. On 10 drivers, avoids 10+ queries.
- **How to test:** Select 10 drivers, run 'Preview driver payments' action. Verify queries < 15 total (not 10+).
- **Verification:** (confirmed) Lines 607-619: for loop over queryset, calling driver.get_unpaid_legs() per driver (line 609), then iterating those legs for list comprehension (line 619). get_unpaid_legs() returns self.legs.filter(...), which is a fresh query each call. In addition, the list comprehension [leg.pickup_date for leg in unpaid_legs if leg.pickup_date] does not have prefetch_related. This is query-in-loop per driver.

#### ADMIN-09 — High (orig Critical -> High)
- **File / symbol:** `reservations/admin.py` :: `ReservationAdmin.update_profit_calculations`  (lines 1354-1362)
- **Issue type:** sync-external-API  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** for reservation in queryset:     reservation.update_profit_calculations() self.message_user(request, f"Profit calculations updated for {queryset.count()} reservations.")
- **Why slow/risky:** Admin action loops over selected reservations and calls .update_profit_calculations() per reservation (likely a heavy method). If this method recalculates across related legs, it compounds N+1 (profit calculation per leg, per reservation). In single worker, blocks site. Need to check if update_profit_calculations triggers external API calls or heavy computation.
- **Fix:** Bulk-update profit_estimate using database expressions (F() + Case/When) without looping. If update_profit_calculations must run per row, defer to background task or run after request ends.
- **Expected impact:** Prevents N loop iterations of a potentially expensive operation.
- **How to test:** Select 20 reservations, run action. Measure time and query count. If >10s or >100 queries, issue is real.
- **Verification:** (confirmed) Lines 1357-1358: for loop calling reservation.update_profit_calculations() per reservation. That method calls calculate_total_driver_payments() (line 474) which in turn calls sum(leg.total_driver_pay for leg in self.legs.all()) (line 459). This means for each reservation, legs are fetched and summed in Python. With many reservations, this is query-in-loop (one legs.all() query per reservation).

#### ADMIN-11 — High
- **File / symbol:** `users/admin.py` :: `AgencyCommissionPayoutAdmin.send_commission_statement`  (lines 1278-1303)
- **Issue type:** sync-external-API  |  **Fix risk:** High  |  *Needs measurement*
- **Evidence:** for payout in queryset.select_related("agency"):     agency = payout.agency     head_emails = list(agency.heads.values_list("email", flat=True))     for email in head_emails:         if email and send_agency_commission_statement(agency=agency, payout=payout, recipient_email=email):
- **Why slow/risky:** Admin action loops over payouts and calls send_agency_commission_statement() per email (Twilio/email send). Calls .heads.values_list() for each payout (not prefetched), then sends email per recipient. Email send is a sync I/O operation that blocks the worker. Single worker blocks all users during action.
- **Fix:** Prefetch 'heads' in queryset. Better: queue emails to Celery instead of sending sync from admin action.
- **Expected impact:** Prevents email send from blocking the admin worker. Each email takes 500ms-2s; 10 payouts × 2 heads = 20 emails = 10-40s blocking the site.
- **How to test:** Run action on 5 payouts. Measure time and worker response times. If >10s or site is unresponsive during action, issue is real.
- **Verification:** (confirmed) Lines 1284-1287: queryset is select_related('agency'), but at line 1287 inside the for loop, agency.heads.values_list() is called per payout. The 'heads' relation is NOT prefetched, so this is an extra query per payout object. This is query-in-loop.

#### ADMIN-14 — High
- **File / symbol:** `drivers/admin.py` :: `DriverPaymentAdmin (list_select_related)`  (lines 795-822)
- **Issue type:** N+1  |  **Fix risk:** Low
- **Evidence:** list_display = ["id", "payment", "leg_display", ...] def get_queryset(self, request):     qs = super().get_queryset(request)     return qs.select_related(         'driver', 'driver__profile'     ).annotate(         _leg_count=Count('leg_payments'),     )
- **Why slow/risky:** list_display references 'leg_display' method which calls obj.leg.profit_estimate (ADMIN-03). get_queryset does NOT include list_select_related or select_related for 'leg', so each row in the changelist triggers a query for the related leg. Changelist of 50 rows = 50 extra leg queries.
- **Fix:** Add list_select_related = ('payment__driver', 'leg', 'leg__reservation') or include in get_queryset.select_related().
- **Expected impact:** Prevents 50 queries per admin changelist page for DriverPayment.
- **How to test:** Load DriverPayment admin list. Verify queries are <10 total, not 50+.
- **Verification:** (confirmed) LegPaymentAdmin (line 926) has list_display including 'profit_display' (line 927). LegPaymentAdmin has no get_queryset override and no list_select_related defined. The profit_display method (line 973-978) accesses obj.leg.profit_estimate without select_related on 'leg'. This causes one query per LegPayment in the list, making it N+1.

#### ADMIN-05 — Medium
- **File / symbol:** `rates/admin.py` :: `LocationGroupAdmin.location_count`  (lines 46-48)
- **Issue type:** N+1  |  **Fix risk:** Low
- **Evidence:** def location_count(self, obj):     return obj.locations.count()
- **Why slow/risky:** Called per row in LocationGroup changelist. For each row, executes SELECT COUNT(*) FROM locations WHERE group_id=X. With 100 location groups in list, that's 100 extra COUNT queries in single worker.
- **Fix:** In get_queryset, annotate with Count('locations') and use annotation in display method. Or remove from list_display and move to readonly detail view.
- **Expected impact:** Prevents 50-100 COUNT queries per admin changelist page.
- **How to test:** Load LocationGroup admin. Verify 1 query includes COUNT for all groups in single GROUP BY, not 50+ separate COUNT queries.

#### ADMIN-12 — Medium (orig High -> Medium)
- **File / symbol:** `drivers/admin.py` :: `DriverAdmin (get_queryset)`  (lines 142-200)
- **Issue type:** query-in-loop  |  **Fix risk:** Low
- **Evidence:** return qs.select_related('profile').annotate(     _unpaid_legs_count=Count('legs', filter=Q(legs__payment_status='unpaid'), distinct=True),     _total_legs_count=Count('legs', distinct=True),     _unpaid_amount=Coalesce(         Sum(Case(When(legs__payment_status='unpaid', ..., then=(...)), ...)), ...     ) )
- **Why slow/risky:** DriverAdmin list_display includes unpaid_legs_count, total_paid, profit_performance—all annotated. The annotations use Case/When with SUM and COUNT on legs table. This is a single query, but it's EXPENSIVE: multiple Case branches, distinct=True (forces DISTINCT), multiple aggregations over the same legs table. On a list of 50 drivers, this single query can be slow. Additionally, profit_summary display method (not in list_display but in detail view) runs another aggregate query per driver detail view load.
- **Fix:** The get_queryset is already optimized; issue is the query cost. Consider: (1) Move profit_performance to detail view only. (2) Cache the annotations on DriverPayment/Leg level instead of computing per list load. (3) Use indexed database columns for common filters (payment_status, driver_id).
- **Expected impact:** Reduces query cost and improves admin changelist load time. Currently, a single expensive query per admin list may take 1-3s.
- **How to test:** Load Driver admin changelist. Measure query time. If >1s, verify distinct=True is necessary; if not, remove it.
- **Verification:** (uncertain) DriverAdmin.get_queryset (lines 142-200) has sophisticated annotations (Distinct Count, Coalesce, Subquery for total_paid). The auditor claims this is expensive per list load, but without measurement of the actual query execution time and row counts, the severity is uncertain. The approach is well-optimized (uses Subquery to avoid cartesian product) but may still be costly if driver count is large. Recommend benchmarking.

#### ADMIN-13 — Medium
- **File / symbol:** `reservations/admin.py` :: `ReservationAdmin (get_queryset)`  (lines 917-933)
- **Issue type:** query-in-loop  |  **Fix risk:** Low
- **Evidence:** qs = (     qs.select_related(         "customer", "vehicle", "travel_agent", "travel_agent__user"     )     .prefetch_related(         "legs", "legs__driver", "legs__driver__profile",         "legs__flight_information", "legs__cruise_information", "payments"     )     .annotate(         earliest_leg_date=Min("legs__pickup_date"),         leg_count=Count("legs"),     ) )
- **Why slow/risky:** get_queryset includes large prefetch_related (7 deep relations). For a list of 50 reservations, the prefetch for legs__driver__profile and legs__flight_information generates multiple queries. The Min/Count annotations also add cost. This is generally OK for a single query, but it's expensive. However, payment_status_display then ignores the prefetched 'payments' and calls .exists().order_by().first() per row (separate issue ADMIN-01).
- **Fix:** Ensure payment_status_display uses the prefetched 'payments' cache. See ADMIN-01. Otherwise, the get_queryset is reasonably optimized.
- **Expected impact:** Reduces query cost for Reservation changelist if payment_status_display is fixed.
- **How to test:** Load Reservation list. Measure total query time. After fixing ADMIN-01, verify time improves.

#### ADMIN-15 — Low
- **File / symbol:** `dispatching/admin_mixins.py` :: `DispatcherAdminMixin (get_list_display)`  (lines 72-80)
- **Issue type:** N+1  |  **Fix risk:** Low
- **Evidence:** def get_list_display(self, request):     list_display = list(super().get_list_display(request))     if not request.user.is_superuser:         list_display = [             field for field in list_display             if not any(sensitive in str(field) for sensitive in self.SENSITIVE_FIELDS)         ]
- **Why slow/risky:** get_list_display is called per admin request and filters list_display at runtime. This is not a query issue but a logic issue: the mixin filters list_display dynamically for each admin. It works, but it's inefficient to filter string representations per request. This is low severity but inefficient.
- **Fix:** Override get_list_display in each admin subclass explicitly instead of using mixin filter. Or cache the list_display per user type.
- **Expected impact:** Minimal; this is a Python-level filter, not a query issue.
- **How to test:** N/A; this is code efficiency, not a performance bug.

---

### Templates & frontend rendering: 74 dispatching templates, plus content/static/js. Critical files: daily_capacity_planner.html (4814 lines), legs_filter.html (4790 lines), driver_timeline include, timeline-dnd.js, plus all_reservations, legs_list, and driver_schedules_dashboard templates.

#### TMPL-02 — High (orig Critical -> High)
- **File / symbol:** `dispatching/templates/dispatching/legs_filter.html` :: `dashboard view (legs_filter template, line 782 in views.py)`
- **Issue type:** query-in-loop  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** Lines 3328-3370 in legs_filter.html: setInterval(() => { fetch(`/dispatching/refresh-all-flights-status/${taskId}/`) ... }). Polling interval not specified but default is likely 500-1000ms. This polls a heavy endpoint in a loop while rendering a 4790-line template with thousands of rows of flight data. Each poll hit causes Django to re-query flight_information for ALL legs on the date.
- **Why slow/risky:** The polling mechanism hammers the backend with repeated /refresh-all-flights-status calls. Each call re-queries flight data. With 1 sync worker, any slow flight refresh blocks all other users. The template itself is huge (4790 lines), so rendering + polling creates a performance cliff.
- **Fix:** Set explicit poll interval to 2000ms or higher (don't hammer backend). Add debouncing/throttling to prevent concurrent requests. Cache flight refresh results in Redis for 30-60s to avoid re-querying database on every poll. Pass pre-computed flight status counts to JavaScript instead of polling—only poll if status actually changed.
- **Expected impact:** Reduces backend polling traffic from ~50+ requests/min per user to ~10 requests/min. Prevents blocking other users during flight refresh.
- **How to test:** Network tab in browser devtools while using 'Refresh All Flights' button. Measure request frequency and response time. Should see 2-3s gaps between polls, not <500ms.
- **Verification:** (confirmed) File verified: C:\Users\admin\OneDrive\Desktop\grayson-towncar\dispatching\templates\dispatching\legs_filter.html line 3370 shows setInterval with 1500ms polling interval (not 500-1000ms as stated). The polling does occur on the refresh-all-flights-status endpoint. With 1.5s interval, this polls 40 times/minute. In a single-worker environment, this IS a concern for blocking other requests, but the auditor understated the interval.

#### TMPL-03 — Medium
- **File / symbol:** `content/static/js/timeline-dnd.js` :: `checkFeasibility function (lines 30-48)`
- **Issue type:** repeated-count  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Lines 30-48: checkFeasibility(legId, driverId) calls fetch('/dispatching/check-feasibility/?leg_id=' + legId + '&driver_id=' + driverId) for every drag-over event. The dragover event fires at ~50ms throttle (line 364), but feasibility is checked AGAIN on drop (line 444). Each call must compute scheduling conflicts, which requires querying legs, driver availability, vehicle assignments.
- **Why slow/risky:** Drag-and-drop logic fires feasibility checks repeatedly during drag (every 50ms throttled dragover). When you hover over 10 driver rows while dragging, you get 10 fetch calls. Each /check-feasibility call queries the database for driver conflicts. With 1 worker, this blocks other requests during DnD operations.
- **Fix:** Cache feasibility results more aggressively. Current code has feasibilityCache (line 14), but it's cleared on successful assignment (line 247). Instead: (A) cache for 30s, or (B) deduplicate: combine pending requests for same leg+driver pair (already done via pendingChecks), or (C) use WebSocket to push updates instead of polling.
- **Expected impact:** Reduces redundant /check-feasibility calls by 50-70%. DnD operations no longer block other users.
- **How to test:** Network tab: drag a leg over 10 drivers, measure total fetch calls. Should be ~1-2 per drag, not 10+.

#### TMPL-04 — Medium (orig High -> Medium)
- **File / symbol:** `dispatching/templates/dispatching/daily_capacity_planner.html` :: `driver_availability_json context variable (line 2495)`
- **Issue type:** unbounded-queryset  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** Line 2495: const driverAvailability = {{ driver_availability_json|safe }}; embeds a large JSON blob containing availability data for ALL inhouse drivers (potentially 50-100+). This is pre-computed in views.py around line 8495-8606, building complex nested structures for each driver. The JSON blob size can be 50KB+, bloating the HTML response.
- **Why slow/risky:** The entire page response includes serialized availability data for every driver, even if only 5 are visible on screen. With 1 sync worker and a slow network, sending 4814-line HTML + 50KB JSON for each page load adds significant latency. DEBUG=True stores every query in memory, so large responses worsen the memory leak.
- **Fix:** Lazy-load driver availability via AJAX: don't embed driver_availability_json in the page. Instead, fetch it on-demand when the user opens the 'Auto-Assign' modal. Move JSON to a separate /api/driver-availability/?date=... endpoint with Cache-Control: max-age=3600.
- **Expected impact:** Reduces initial page load size by 30-50KB. Initial render is faster. Lazy-loading defers unavailability data to when needed.
- **How to test:** Measure HTML response size with and without embedding. Should drop from ~300KB to ~250KB.
- **Verification:** (confirmed) File verified: C:\Users\admin\OneDrive\Desktop\grayson-towncar\dispatching\views.py line 8921 shows driver_availability_json is json.dumps(driver_availability) passed to template context. Lines 8889-8898 build availability dict for eligible_drivers. This does embed potentially large JSON in page HTML. Auditor's claim about 50KB+ cannot be verified without runtime measurement, but the pattern is confirmed.

#### TMPL-07 — Medium
- **File / symbol:** `dispatching/templates/dispatching/includes/driver_timeline.html` :: `driver_timeline include (lines 55-77, 87-100)`
- **Issue type:** template-query  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Lines 87-100 in driver_timeline.html: nested {% for ul in gap.fitting_unassigned %} loop generates data-fit-* attributes dynamically. If a single gap has 10+ fitting unassigned legs, this generates 10+ data attributes per gap. With 5 drivers x 3 gaps per driver x 5 legs per gap = 75+ data attributes. Large data-* sets slow down DOM parsing and CSS selector matching.
- **Why slow/risky:** The browser's DOM parser must process hundreds of data attributes on each timeline-gap div. CSS selectors that target [data-fit-*] attributes become slow if not indexed. Repeated DOM parsing on page load with a 4790-line template compounds the issue.
- **Fix:** Don't enumerate all fitting legs as data attributes. Instead: (A) embed fitting legs as a single JSON data-fit attribute: data-fit='[{id:1,...}]', or (B) fetch fitting legs dynamically via JavaScript when gap is clicked (lazy-load).
- **Expected impact:** Reduces HTML size by 10-20%. DOM parsing faster. Timeline renders 50-100ms sooner.
- **How to test:** Measure HTML response size (legs_filter page). Should drop ~5KB if data attributes are consolidated.

#### TMPL-01 — Low — **REFUTED**
- **File / symbol:** `dispatching/templates/dispatching/includes/reservation_list.html` :: `reservation_list (template include, lines 1-580)`
- **Issue type:** N+1-in-template  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Line 52: {% with payment_count=reservation.payments.all|length %} and Line 145: {% if reservation.legs.count == 1 %} — each loop iteration over reservations array triggers a separate query for payments.all and legs.count. When rendered on paginated pages (e.g., all_reservations showing 10 reservations), this causes 20+ extra queries.
- **Why slow/risky:** Template renders reservations in a paginated table. On each row, it calls reservation.payments.all and reservation.legs.count—both issue separate DB queries. With 10 rows per page (paginate_by=10), a single page load causes 10 N+1 queries for payments + 10 for legs.count = 20 extra queries in a 1-worker sync process, blocking the site.
- **Fix:** Prefetch payments and legs in the view's get_queryset. Already prefetched in all_reservations.html context (line 1367: prefetch_related('legs', 'payments')), but the template STILL calls .all|length and .count. Either: (A) pass pre-computed counts in context (payment_count, leg_count per reservation), or (B) use Prefetch with queryset to ensure Django uses the cached objects instead of re-querying. Then use |length on cached queryset without .all.
- **Expected impact:** Eliminates 20 extra queries per page load on all_reservations view. Single worker will no longer block waiting for these queries. Estimated 200-500ms savings per page.
- **How to test:** Load /dispatching/all_reservations/ in production. Measure connection.queries length with DEBUG=True in test environment. Should be ~5-10 queries per page load, not 25-30.
- **Verification:** (refuted) File verified: C:\Users\admin\OneDrive\Desktop\grayson-towncar\dispatching\templates\dispatching\includes\reservation_list.html lines 52 and 145 do call .all|length and .count on prefetched relations. However, the view at line 1367 of views.py DOES prefetch_related('legs', 'payments'). In Django, when a relation is prefetched, calling .all() or .count() on it uses the cached result set and does NOT trigger additional database queries. The auditor's N+1 concern is technically incorrect here.

#### TMPL-05 — Low
- **File / symbol:** `dispatching/templates/dispatching/driver_schedules_dashboard.html` :: `setInterval(updateClock, 1000) (line 1278)`
- **Issue type:** polling  |  **Fix risk:** Low
- **Evidence:** Line 1278: setInterval(updateClock, 1000) updates a clock display every 1 second. This is a client-side operation (no network), but it's unnecessary—the browser's native Date API is fast enough. However, this is running on EVERY open instance of the page, and if multiple tabs are open, it wastes CPU.
- **Why slow/risky:** The clock update itself is cheap (Date.now()), but running 1000+ setIntervals across multiple tabs/users wastes browser CPU. Not a backend issue, but contributes to overall browser sluggishness if users keep the dashboard open.
- **Fix:** Move to requestAnimationFrame or reduce polling to 5000ms for a 5-second-accurate clock. Alternately, use CSS animation or let the client's system clock show via date pipe filter instead of polling.
- **Expected impact:** Marginal CPU savings per browser. Better UX if clock is less jittery.
- **How to test:** Open DevTools Performance tab, record 10s. Measure setInterval call frequency. Should see fewer calls if interval is increased.

#### TMPL-06 — Low
- **File / symbol:** `dispatching/templates/dispatching/timeclock.html` :: `setInterval(tick, 1000) and timeclock_overview.html setInterval(tick, 1000) (lines 211, 147)`
- **Issue type:** polling  |  **Fix risk:** Low
- **Evidence:** Lines 211 (timeclock.html) and 147 (timeclock_overview.html) both call setInterval(tick, 1000) to update elapsed time displays. 'tick' computes elapsed seconds and updates the DOM. This is a client-side operation, but repeated DOM writes every 1s can cause repaints.
- **Why slow/risky:** setInterval(1000) is appropriate for real-time display, but unnecessary DOM manipulation (even if just innerHTML update) causes layout thrashing. Browsers optimize this, but it's still wasteful if not needed frequently.
- **Fix:** Use CSS animation or requestAnimationFrame for smoother updates. Alternately, accept 1s accuracy and keep the setInterval but use textContent instead of innerHTML to avoid DOM parsing.
- **Expected impact:** Smoother elapsed time display, marginally reduced repaints.
- **How to test:** Open DevTools Performance tab. Measure paint frequency while timeclock is visible.

#### TMPL-08 — Low — **REFUTED**
- **File / symbol:** `dispatching/views.py` :: `dashboard function (lines ~200-789)`
- **Issue type:** query-in-loop  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** Lines 555-579 in views.py: legstop_set.all() and legflight_set.all() are called INSIDE a loop over _all_legs_for_timeline. Code: `_stop_count = len(_tleg.legstop_set.all())` and `_legflight_total = len(_tleg.legflight_set.all())`. These are NOT prefetched, so each leg triggers 2 separate queries. With ~100 legs/day, this is 200 extra queries.
- **Why slow/risky:** The view builds _gap_candidates by iterating over legs and calling .all() on related managers without prefetch. Django's prefetch_related is set up for status_history and payments (lines 8510-8522 in capacity_planner), but NOT for legstop_set and legflight_set.
- **Fix:** Add prefetch_related('legstop_set', 'legflight_set') to the Leg.objects query (around line 8497). Then use len(list(leg.legstop_set.all())) to avoid re-querying.
- **Expected impact:** Eliminates 200 queries from dashboard view. Render time drops 500-1000ms on a 100-leg day.
- **How to test:** Load /dispatching/dashboard/ with DEBUG=True. Check connection.queries for 'SELECT ... FROM dispatching_legstop WHERE leg_id = ...' calls. Should be 0 after fix.
- **Verification:** (refuted) File verified: C:\Users\admin\OneDrive\Desktop\grayson-towncar\dispatching\views.py lines 555-559 do call .legstop_set.all() and .legflight_set.all() in a loop. HOWEVER, the base queryset at lines 154-157 includes prefetch_related('legstop_set', 'legflight_set__flight'). In Django, prefetched relations use cached data when .all() is called - this does NOT cause additional queries. The auditor's diagnosis of N+1 queries is incorrect.

---

### Rates, services, blog, content/sitemaps (lower traffic but check). Files: rates/views.py, rates/models.py, services/views.py, services/forms.py, services/models.py, blog/views.py, blog/models.py, content/sitemaps.py.

#### AREA-01 — Medium
- **File / symbol:** `services/views.py` :: `orlando_airport_transportation`  (lines 36-45)
- **Issue type:** N+1  |  **Fix risk:** Low
- **Evidence:** for v in vehicles:         routes = {}         for r in v.rates.all():             routes[str(r.id)] = {...}  The prefetch_related is defined on line 14-31 with a correct Prefetch object, but line 38 calls v.rates.all() which RESETS the prefetch and causes a fresh query per vehicle instead of using the prefetched data.
- **Why slow/risky:** Line 14-31 correctly prefetches rates, but line 38's v.rates.all() loses that prefetch context. With 5+ vehicle types in production, this becomes 5+ extra queries. In a single-worker Gunicorn, each query blocks the site.
- **Fix:** Replace line 38 for r in v.rates.all(): with iteration of the already-prefetched data. Instead of calling .all() again, iterate v.rates directly. Change to: for r in v.rates.iterator() or simply access the prefetched manager without resetting it.
- **Expected impact:** Currently fires 1 query per vehicle (should be 1 total via prefetch). Removes 4+ unnecessary DB hits per request to this landing page.
- **How to test:** Add django.test.Client, hit /services/orlando-airport-transportation/, count queries in test with assertNumQueries. Before fix: 1 (vehicles) + N (rates per vehicle). After fix: 1 (vehicles) + 1 (rates prefetch).

#### AREA-02 — Medium
- **File / symbol:** `blog/views.py` :: `blog_list`  (lines 15-22)
- **Issue type:** icontains-unbounded  |  **Fix risk:** Medium  |  *Needs measurement*
- **Evidence:** blogs_queryset = Blog.objects.all().order_by("-created")  if search_query:     blogs_queryset = blogs_queryset.filter(         title__icontains=search_query     ) | blogs_queryset.filter(content__icontains=search_query)  No full-text index on content (RichTextField). icontains on content field is O(n) sequential scan every search request.
- **Why slow/risky:** icontains on large text fields (RichTextField content) forces PostgreSQL to do a full table scan. A single search request can scan all 100+ blog posts if they match. In 1-worker mode, blocks site during slow search.
- **Fix:** Add PostgreSQL trigram index and use SearchVector/SearchQuery from django.contrib.postgres.search. Change to: Blog.objects.annotate(search=SearchVector('title', 'content')).filter(search=SearchQuery(search_query))
- **Expected impact:** Search requests that match 50+ posts currently do full table scans. With trigram index, sub-ms lookup. But needs measurement to confirm current pain.
- **How to test:** Create 50+ Blog posts. Time request with search_query=some_common_word. Log query count and execution time. After adding trigram index + SearchVector: measure again. Should drop from ~500ms to <50ms.

#### AREA-03 — Medium
- **File / symbol:** `blog/views.py` :: `blog_list`  (lines 15-30)
- **Issue type:** missing-select_related  |  **Fix risk:** Low
- **Evidence:** blogs_queryset = Blog.objects.all().order_by("-created") ... paginate ...   In template blog_list.html line 118, every blog card accesses {{ blog.user.first_name }}, triggering a query per blog post for the User ForeignKey.
- **Why slow/risky:** Template iterates 9 paginated blogs per page, each accessing blog.user.first_name. With no select_related('user'), this is 9 extra queries per page. In 1-worker mode, each blocks the site.
- **Fix:** Change line 15 to: blogs_queryset = Blog.objects.select_related('user').all().order_by('-created'). Also apply to line 49 in blog_post() for related_posts.
- **Expected impact:** Removes 9 queries per blog list page (18 if searching). Also removes 3 queries per blog post view (related_posts).
- **How to test:** Hit /blog/ and /blog/post/[slug]/ with django.test.Client. Use assertNumQueries to verify exactly 2 queries (1 for paginated blogs, 1 for user prefetch) on list; 2 queries on post (1 for post, 1 for related prefetch + user).

#### AREA-05 — Medium
- **File / symbol:** `content/sitemaps.py` :: `BlogPostSitemap.items`  (lines 60-61)
- **Issue type:** unbounded-queryset  |  **Fix risk:** Low  |  *Needs measurement*
- **Evidence:** def items(self):     return Blog.objects.order_by("-created")  Returns full queryset. Sitemap will iterate ALL blogs on crawl. No pagination, no .values_list() optimization.
- **Why slow/risky:** Sitemap framework iterates the full queryset to generate XML. With 100+ blog posts, this loads all rows + all their FK relations (User, etc.) into memory. SearchEngines crawl sitemaps frequently. Single-worker blocks on this.
- **Fix:** Add .only('slug', 'created') to defer User FK loading. Change to: Blog.objects.order_by('-created').only('slug', 'created')
- **Expected impact:** Reduces per-crawl memory footprint (no User objects fetched), faster serialization. ~10-50ms savings per sitemap crawl.
- **How to test:** Add logging around items() to count rows. Memory-profile before/after with django-silk or similar. Verify sitemap XML still valid with django.test.TestCase and assertContains.

#### AREA-04 — Low
- **File / symbol:** `blog/views.py` :: `blog_post`  (lines 52-55)
- **Issue type:** recalculation  |  **Fix risk:** Low
- **Evidence:** estimated_read_time = max(1, round(word_count / words_per_minute))  Calculated on EVERY blog post view. Calls strip_tags(post.content), split() on entire HTML content, counts words.
- **Why slow/risky:** This is O(content length). For a 5000-word article, split() allocates a list and counts. Happens per view, not cached. Low impact (ms per request) but wasteful; this value never changes.
- **Fix:** Cache in model. Add estimated_read_time IntegerField to Blog, compute on save(), or use @cached_property.
- **Expected impact:** Saves ~1-5ms per blog post view (minimal in isolation, but good hygiene).
- **How to test:** Add database migration to Blog model. On save(), compute once. Verify blog_post view now renders without recalculation. No behavior change.

#### AREA-06 — Low
- **File / symbol:** `blog/views.py` :: `BlogFeed.items`  (lines 85-86)
- **Issue type:** missing-select_related  |  **Fix risk:** Low
- **Evidence:** def items(self):     return Blog.objects.order_by("-created")[:10]  RSS feed correctly limits to 10, but no select_related('user') for the User FK accessed in item_description via get_clean_preview().
- **Why slow/risky:** Each feed request queries all 10 blogs + 1 query per blog to fetch User. With RSS bots polling, this is 11 queries every few minutes.
- **Fix:** Add select_related('user'): Blog.objects.select_related('user').order_by('-created')[:10]
- **Expected impact:** Removes 10 User queries per RSS feed request. Low traffic endpoint but good hygiene.
- **How to test:** Hit /blog/feed/ with test client. Verify exactly 1 query (blogs + prefetch user) instead of 11.

---

## C. Page / Workflow Risk Table

| Page / workflow | Likely bottleneck | Query risk | CPU risk | External-API risk | Template/FE risk | Priority |
|---|---|---|---|---|---|---|
| Conflict-task detail (ops) | ~29 sync Google drive-time calls | Med | Low | **High** | Low | P0 |
| Daily capacity planner | availability recompute + embedded JSON | Med | Med | Low | **High** | P1 |
| Schedule board | availability recompute in loops | Med | Med | Low | Med | P1 |
| Legs dashboard / legs_filter | poll-driven flight refresh; per-leg calc | **High** | Med | **High** | **High** | P0 |
| Auto-assign apply | N sequential leg.save() UPDATEs | **High** | Med | Low | Low | P0 |
| Driver index / schedule | Google drive-time per leg | Med | Low | **High** | Low | P1 |
| Driver flight refresh | sync AeroAPI, no timeout | Low | Low | **High** | Low | P1 |
| Agency-head dashboard | N+1 over agents/reservations | **High** | Low | Low | Low | P1 |
| Admin: process driver payments | N+1 + per-row save | **High** | Med | Low | Low | P1 |
| Admin: start follow-up sequence | thread-per-lead spawn | Low | Med | **High** | Low | P1 |
| Admin changelists (Res/Driver/Payment) | list_display N+1 | **High** | Low | Low | Low | P1 |
| Booking checkout | Meta CAPI in response path | Low | Low | **High** | Low | P1 |
| Stripe webhook | Stripe re-fetch + thread spawn | Low | Low | **High** | Low | P2 |
| Refresh-all-flights | serial AeroAPI; trip-type filter in req thread | Med | Med | **High** | Low | P1 |
| Task queue view (ops) | unbounded queryset, no pagination | **High** | Med | Low | Med | P1 |
| Unpaid-reminder engine | scans all reservations for dup cache | **High** | Med | **High** | Low | P2 |
| Blog list / feed | __icontains title+content, no pagination | Med | Low | Low | Low | P3 |
| Newsletter subscribe | 2 uncached COUNTs per POST | Med | Low | Low | Low | P3 |

---

## D. N+1 and Query-Optimization List

Exact `select_related`/`prefetch_related`/`annotate`/`bulk` changes, drawn from confirmed findings:

- **ADMIN-08** `drivers/admin.py::DriverAdmin.process_driver_payments` — Bulk-fetch all legs upfront. Use bulk_create for DriverPayment and LegPayment instead of per-driver loop with .save().
- **DISP-03** `dispatching/views.py::auto_assign_drivers (9659-10076)` — Replace individual leg.save() calls with Leg.objects.bulk_update(legs_to_update, fields=['driver', 'driver_assigned_by', 'driver_assigned_at'], batch_size=500). Collect modified legs in a list first, then bulk_update once. Reduces 50+ queries to 1-2 bulk operations.
- **DISP-06** `dispatching/views.py::refresh_all_flights (4824-4914)` — Move the trip_type filtering (lines 4866-4869) into the background thread _run_bulk_flight_refresh (after line 4669). Alternatively, annotate legs queryset with trip_type via ORM subquery/case statement and filter at query time (more efficient). Request should just spawn thread and return <10ms.
- **ADMIN-01** `reservations/admin.py::ReservationAdmin.payment_status_display` — In get_queryset (line 923), annotate the latest payment using Subquery or F-expressions. Store in obj._latest_payment_id and fetch in template/display method via prefetch. Or use raw annotation: Latest('payments__created_at').
- **ADMIN-02** `users/admin.py::TravelAgentAdmin._calculate_commission_preview` — Prefetch related legs before the loop: unpaid_reservations.prefetch_related('legs'). Better: bulk aggregate in DB using Subquery and Min() to find earliest_leg_date, avoiding the nested loop entirely.
- **ADMIN-03** `drivers/admin.py::DriverPaymentAdmin.profit_display` — Add list_select_related = ('payment__driver', 'leg', 'leg__reservation') to LegPaymentAdmin (around line 925-935).
- **ADMIN-04** `reservations/admin.py::FlightAdmin.is_in_use` — Ensure get_queryset always includes the annotation and remove the fallback .exists() query. Add assertion that obj.is_linked exists.
- **ADMIN-06** `users/admin.py::TravelAgentAdmin.mark_agents_for_payment` — Use queryset.aggregate(Sum('unpaid_commissions')) instead of Python sum loop.
- **ADMIN-07** `drivers/admin.py::DriverAdmin.preview_driver_payments` — Pre-fetch all unpaid legs for all selected drivers in a single prefetch_related before the loop, or use Leg.objects.filter(driver__in=driver_ids, payment_status='unpaid').
- **ADMIN-11** `users/admin.py::AgencyCommissionPayoutAdmin.send_commission_statement` — Prefetch 'heads' in queryset. Better: queue emails to Celery instead of sending sync from admin action.
- **ADMIN-14** `drivers/admin.py::DriverPaymentAdmin (list_select_related)` — Add list_select_related = ('payment__driver', 'leg', 'leg__reservation') or include in get_queryset.select_related().
- **AGENTS-01** `users/views.py::AgencyHeadDashboardView.get_context_data` — Use prefetch_related('reservations') on the agents queryset and annotate with Count('reservations'). Then access cached counts: agent._reservation_count instead of agent.reservations.count().
- **AGENTS-05** `users/admin.py::TravelAgentAdmin._calculate_commission_preview` — Batch the Reservation query outside the loop using travel_agent__in=list(queryset), then prefetch_related('legs'). Then group results by agent_id in Python.
- **AGENTS-07** `users/signals.py::handle_agency_payout_deletion` — Use bulk_delete: instance.agent_payouts.all().delete(). Or if signals must run, use bulk_update with a mark-deleted flag or batch_size for parallelism.
- **AGENTS-08** `users/services.py::process_bulk_payouts` — Use bulk_ready_totals([agent_ids...]) from eligibility.py which fetches all reservations in 2 queries total. Or prefetch all agent reservations once and filter by agent_id in Python.
- **DISP-04** `dispatching/views.py::index (104-500)` — Annotate legs queryset with aggregate sum of leg-level revenue or use Prefetch('reservation__payments', ...) to load all payments once, cache result so afterhours_fee_outstanding() doesn't re-query. For detect_leg_flags: this is already O(1) per leg (no DB), keep as-is but ensure called only on 'today' legs.
- **DISP-05** `dispatching/views.py::legs_list (1809-1990)` — Compute trip_type once per leg via a dict comprehension before loops. Cache in {leg_id: trip_type} or annotate each leg object with _cached_trip_type after first computation. Reuse cached value in all subsequent loops.
- **DISP-12** `dispatching/confirmation_sms.py::send_confirmations_for_date` — Batch into ThreadPoolExecutor (5-10 workers), collect results, do bulk_update() after all sends. Or use Celery with exponential backoff on failures.
- **DRIV-08** `drivers/availability.py::_weekly_or_defaults, resolve_effective_availability` — Require caller to prefetch_related('weekly_schedule', 'date_overrides') before calling. Accept optional prefetched lists as parameters, or use `resolve_effective_availability_bulk()` that handles the prefetch once and maps results.
- **OPS-01** `ops/views.py::_build_driver_conflict_context` — Add 'status_history' to the prefetch_related on line 765 when querying day_legs. Change line 765 from .prefetch_related('status_history') to ensure status_history is prefetched along with flight_information, reservation, etc.
- **OPS-03** `ops/leads_board.py::lead_board_detail` — Add .prefetch_related('activities', 'follow_up_tasks') to the Lead query on line 270.
- **RES-01** `reservations/models.py::Reservation.calculate_total_driver_payments` — Use .aggregate(total=Sum('total_driver_pay')) on self.legs.all() instead of sum() in Python. Cache result as a stored field updated on leg save via post_save signal if recalc is expensive.
- **RES-03** `reservations/models.py::Reservation.recalculate_leg_revenue_shares` — Use bulk_update() to update all legs in a single query. E.g., collect updates, then Leg.objects.bulk_update(legs_to_update, ['revenue_share', 'profit_estimate']).
- **RES-04** `reservations/signals.py::update_agent_commission_data` — Move commission recalculation to an explicit update_agent method callable only when status changes (use the _pre_save_old_values already captured to detect). Skip recalc entirely if update_fields is specified and doesn't include 'status' or 'commission_amount' (already done at line 74-77, but the aggregations still run—move them inside the status_changed block at line 97).
- **RES-05** `reservations/signals.py::auto_convert_lead_on_reservation + _converge_duplicate_leads` — Batch the duplicate lead convergence: collect all twins to update, then use bulk_update() instead of saving each. Use update_fields on save() to skip GHL sync signal recursion.
- **RES-06** `reservations/models.py::Leg._assign_route_from_locations` — Query by indexed location fields directly: Route.objects.select_related('origin', 'destination').filter(origin__name__icontains=self.pickup_location, destination__name__icontains=self.dropoff_location).first(). Or move location matching into a database view/search.
- **TMPL-02** `dispatching/templates/dispatching/legs_filter.html::dashboard view (legs_filter template, line 782 in views.py)` — Set explicit poll interval to 2000ms or higher (don't hammer backend). Add debouncing/throttling to prevent concurrent requests. Cache flight refresh results in Redis for 30-60s to avoid re-querying database on every poll. Pass pre-computed flight status counts to JavaScript instead of polling—only poll if status actually changed.
- **ADMIN-05** `rates/admin.py::LocationGroupAdmin.location_count` — In get_queryset, annotate with Count('locations') and use annotation in display method. Or remove from list_display and move to readonly detail view.
- **ADMIN-12** `drivers/admin.py::DriverAdmin (get_queryset)` — The get_queryset is already optimized; issue is the query cost. Consider: (1) Move profit_performance to detail view only. (2) Cache the annotations on DriverPayment/Leg level instead of computing per list load. (3) Use indexed database columns for common filters (payment_status, driver_id).
- **ADMIN-13** `reservations/admin.py::ReservationAdmin (get_queryset)` — Ensure payment_status_display uses the prefetched 'payments' cache. See ADMIN-01. Otherwise, the get_queryset is reasonably optimized.
- **AGENTS-03** `users/views.py::agency_payout_detail` — Annotate count on the queryset or prefetch and use len(list). Then check len > 0 or use the count result.
- **AGENTS-06** `users/views.py::admin_commission_report` — Use Django ORM: payouts.values('agent_id').annotate(total=Sum('total_amount'), payouts=Count('id')) instead of Python loop.
- **AGENTS-09** `users/views.py::AgencyDetailView.get_context_data` — Annotate on get_queryset: .annotate(total_unpaid=Sum('agents__unpaid_commissions'), ...). Then use these cached annotations in context instead of calling the methods.
- **AGENTS-10** `users/middleware.py::NewsletterRateLimitMiddleware.is_allowed` — Combine into one query: aggregate the max of two counts or use a single .filter(Q(...) | Q(...)) with Count(). Or use cache key instead of DB for rate limit.
- **AGENTS-13** `users/signals.py::handle_payout_deletion` — Keep the bulk update but verify no post_delete signals on Reservation fire inefficiently. Consider batching large updates if Reservation.post_delete is expensive.
- **AREA-01** `services/views.py::orlando_airport_transportation` — Replace line 38 for r in v.rates.all(): with iteration of the already-prefetched data. Instead of calling .all() again, iterate v.rates directly. Change to: for r in v.rates.iterator() or simply access the prefetched manager without resetting it.
- **AREA-02** `blog/views.py::blog_list` — Add PostgreSQL trigram index and use SearchVector/SearchQuery from django.contrib.postgres.search. Change to: Blog.objects.annotate(search=SearchVector('title', 'content')).filter(search=SearchQuery(search_query))
- **AREA-03** `blog/views.py::blog_list` — Change line 15 to: blogs_queryset = Blog.objects.select_related('user').all().order_by('-created'). Also apply to line 49 in blog_post() for related_posts.
- **BG-12** `ghl_integration/tasks.py::batch_send_unsent_leads / _rescue_stale_leads` — Batch-load all leads upfront with select_related('vehicle') and pass lead objects to run_in_background, or pass only minimal fields needed to spawn the task and accept the redundant load (sync_lead_to_ghl_and_send_sms uses select_for_update anyway).
- **BG-13** `ghl_integration/tasks.py::start_follow_up_sequence` — Replace the loop with a single query: Lead.objects.filter(normalized_phone=lead.normalized_phone, sequence_active=True).exclude(id=lead.id).exists(). Already normalized_phone exists (used elsewhere).
- **BG-14** `ghl_integration/services.py::contact_has_replied` — Move contact_has_replied check outside the tight send loop. Instead, batch the check BEFORE sending (e.g., prefetch all conversations in one call if GHL API allows, or skip the check for leads that haven't been contacted in >2 days).
- **BG-16** `ghl_integration/tasks.py::process_follow_up_batch` — Batch-fetch all unique (step_number, segment) combinations from FollowUpSequence upfront and memoize in a dict. Then look up in the dict in the loop.
- **BG-18** `reservations/admin.py::mark_converted action` — No immediate fix without understanding match_lead's complexity. But consider: if index build is slow, cache it for 5 minutes per location/date. Or implement a fuzzy search table (denormalized match hints) to accelerate matching.
- **BOOKING-03** `reservations/utils.py::extra_charges() function, lines 539-601` — Use bulk updates: Leg.objects.filter(reservation=reservation).update(afterhours_fee=...). Prefetch vehicle to cache property access.
- **BOOKING-05** `reservations/forms.py::ReservationAdminForm.__init__(), lines 345-352` — Already optimized with select_related. Consider class-level caching or lazy property to avoid re-fetching on every form instantiation. Document the optimization.
- **BOOKING-07** `reservations/signals.py::update_agent_commission_data() signal, lines 63-162` — Ensure ALL non-commission saves use update_fields (extra_charges at line 588 already does this). Also consider caching agent.pending_commissions in Redis and recomputing async (Celery task).
- **DISP-01** `dispatching/views.py::capacity_planner (8457-8927)` — Batch-load all drivers with prefetch_related('weekly_schedule', 'date_overrides') once at line 8535-8540. Then cache results in a dict {driver_id: eff_dict} computed once, reused for all loops. Saves ~60 DB round-trips.
- **DISP-07** `dispatching/views.py::capacity_planner (8668-8704)` — Replace lines 8688-8703 with single loop that computes list of status_history.all() once and extracts 'completed' entry more efficiently (e.g., use next((s for s in sh_list if s.status == 'completed'), None)). Verify preload_timing_cache() preloads metrics for both selected_date AND prev_day.
- **DISP-08** `dispatching/views.py::update_leg_assignment (2003-2234)` — Add select_related('reservation', 'driver') to line 2047 query. This saves 2 DB round-trips per call.
- **DISP-10** `dispatching/views.py::refresh_all_flights` — Move the trip_type filter into the queryset using Q() filters on pickup/dropoff locations. Or add a trip_type field to Leg model (denormalized).
- **DISP-13** `dispatching/analytics.py::update_daily_capacity_metrics` — Fetch all legs for date range and driver set once with select_related/prefetch. Group by (driver_id, pickup_date) in Python. Single query instead of 350.
- **DRIV-11** `drivers/admin.py::unpaid_legs_display` — Remove from list_display or make it a separate detailed view. If needed, use a custom changelist view that prefetches unpaid legs once and caches the HTML.
- **DRIV-12** `drivers/admin.py::recent_leg_history` — Remove from list_display. Link to driver detail page instead, which shows recent leg history.
- **DRIV-14** `drivers/payout_adjustments.py::_recalculate_payment_total` — Replace loop with: `agg = active.aggregate(total=Sum('amount'), base=Sum('base_pay'), grat=Sum('gratuity'), addl=Sum('additional'))` and extract values. Converts per-LegPayment work to single database query.
- **DRIV-15** `drivers/gusto_export.py::build_rows_for_period, validate_selection` — Ensure eligible_payments_qs() is always the source and the annotation is preserved through the list comp. Add a test to verify query count doesn't exceed 2-3 regardless of payment count.
- **OPS-04** `ops/views.py::task_detail_view` — Add .prefetch_related('comm_attempts') to the OperationalTask query on line 2001-2020.
- **PAY-07** `reservations/models.py::Reservation.total_paid / Reservation.amount_owed / Reservation.payment_status` — Add prefetch_related('payments') to every view/query that accesses these properties. Example: `Reservation.objects.filter(...).prefetch_related('payments')` before any template or loop.
- **PAY-09** `reservations/conversions.py::send_purchase_event` — Prefetch payments before calling send_purchase_event: `Reservation.objects.prefetch_related('payments').get(pk=...)`. Or, modify send_purchase_event to accept an optional latest_payment parameter.
- **RES-09** `reservations/views.py::index view` — Move rate filtering into the prefetch() queryset with filter() to avoid returning all rates.
- **RES-12** `reservations/admin.py::LegAdmin.get_queryset` — Remove the annotation unless it's used in list_display or filtering. If needed, use .prefetch_related('reservation__legs') and count in Python.
- **ADMIN-15** `dispatching/admin_mixins.py::DispatcherAdminMixin (get_list_display)` — Override get_list_display in each admin subclass explicitly instead of using mixin filter. Or cache the list_display per user type.
- **AREA-06** `blog/views.py::BlogFeed.items` — Add select_related('user'): Blog.objects.select_related('user').order_by('-created')[:10]

---

## E. Signal / Save-Method Audit

- **`post_save(Reservation)` chain:** `update_agent_commission_data` (RES-04), `auto_convert_lead_on_reservation` + `_converge_duplicate_leads` (RES-05), profit recompute (RES-02), and the `created` internal-confirmation thread all fire on each save. The commission signal **already guards on `update_fields`** and uses DB aggregates (verified) — preserve that. Where additional non-commission saves occur, pass `update_fields` so the commission/profit signals short-circuit.
- **`reservation_form` saves the reservation 2-3x per request** (BOOKING-01 / RES-10) -> the signal chain re-fires each time. Fix: collect field changes, set FKs before the first save, then a single `update_fields` save.
- **`Reservation.recalculate_leg_revenue_shares` (RES-03)** loops `leg.save()` -> use `bulk_update`.
- **`Reservation.calculate_total_driver_payments` (RES-01)** sums in Python -> `aggregate(Sum(...))`.
- **`Leg._assign_route_from_locations` (RES-06)** does an unbounded/icontains Route lookup on save -> query by indexed fields.
- **`users/signals.py::handle_agency_payout_deletion` (AGENTS-07)** deletes child payouts row-by-row -> bulk delete.
- **`update_fields` checklist:** add changed-field guards to any new save in the reservation edit path; verify the commission signal `COMMISSION_FIELDS` intersection still behaves identically.
- **`simple_history`** writes a historical row on most saves; acceptable at current volume but disable inside bulk/batch jobs.

---

## F. Background Job Audit

All background/scheduled work runs **in-process** (no broker). Key findings:

- **BG-01 (Critical)** `ghl_integration/tasks.py::sync_lead_to_ghl_and_send_sms` — With one gunicorn sync worker and no Celery, each lead retry that hits an exception (network timeout, GHL API error) sleeps 1-3min inside the worker. Multiple concurrent requests t -> Remove retries from the sync function entirely. Instead, log_sync_failure() should set next_retry_at and let retry_failed_syncs() (which runs async in the scheduler) handle retries
- **BG-02 (High)** `ghl_integration/tasks.py::process_follow_up_batch` — The scheduler runs this in the single gunicorn worker thread. Scheduler holds the worker for ~100 seconds per cycle sending ~100 SMS. If any SMS send hangs (timeout=10 on line 690) -> Remove sleep(1). Instead, batch all send_sms calls and run them in parallel with concurrent.futures.ThreadPoolExecutor (bounded to 10-20 threads) or remove it entirely if rate limi
- **BG-04 (High)** `ghl_integration/scheduler.py::_run_scheduler` — The scheduler runs inside the single gunicorn worker as a daemon thread. If any of the 7 batch tasks makes a slow GHL/Twilio/AeroAPI call or hits an exception (see BG-01), the sche -> Offload the heaviest tasks to truly background threads spawned via run_in_background(). Specifically: batch_send_unsent_leads, process_follow_up_batch, retry_failed_syncs, detect_l
- **BG-05 (High)** `ghl_integration/pre_pickup.py::send_pre_pickup_nudges` — Similar to process_follow_up_batch (BG-02), this runs synchronously in the scheduler thread every 2 cycles. Each SMS send sleeps 1s, blocking the worker. -> Same fix as BG-02: remove sleep(1) and batch SMS sends with ThreadPoolExecutor or request-level rate limiting.
- **BG-15 (High)** `ops/tasks.py::detect_driver_conflicts / _turn_late_minutes` — Ops task generation runs every 30 minutes and scans all upcoming legs for driver conflicts. For each leg, if the driver has prior legs, it calls Google Maps for drive time. No cach -> Implement a request-level cache for drive times within a single ops task generation run (e.g., functools.lru_cache on _reposition_minutes). Or use a fallback categorization-based l
- **BOOKING-04 (High)** `reservations/views.py::QuoteFormHandlerView.post(), lines 447-478 and 566-592` — Daemon threads can die without finishing. On traffic spikes, many quote submissions = many daemon threads created. May exhaust DB connection pool. Notifications fail silently if th -> Use _run_in_background() helper (utils.py:48) consistently instead of duplicating Thread logic. Or set up Celery+Redis for proper task queueing. At minimum, reuse the helper functi
- **RES-13 (High)** `reservations/signals.py::sync_lead_to_ghl_on_create + sync_lead_status_to_ghl` — Raw daemon threads spawned in post_save signals compete for the 1 Gunicorn worker's GIL. If many leads are created/updated, the threads pile up and block each other. With no Celery -> Move GHL sync to an explicit async task queue (Celery + Redis) or use a batch worker task scheduled with run_in_background(). If keeping threads, add a thread pool (e.g., ThreadPoo
- **BG-03 (Medium)** `ghl_integration/tasks.py::retry_failed_syncs` — Blocks the single gunicorn worker for 25 seconds during the scheduler batch, which runs every 30 minutes. If paired with process_follow_up_batch (another 100s), the scheduler holds -> Remove the 0.5s sleep. Batch all retry API calls with a bounded ThreadPoolExecutor (5-10 threads) to parallelize GHL service.* calls. GHL rate limiting should be handled per-contac
- **BG-07 (Medium)** `payment/views.py::payment_success` — User loads success page, thread spawns, but page returns immediately. If thread fails, user never sees the error. Threads can accumulate on the page if multiple users load it. -> Use run_in_background() instead of Thread(). Alternatively, move send_purchase_event to a post_save signal or a queued task.
- **BG-08 (Medium)** `dispatching/views.py::refresh_all_flights` — Dispatcher clicks 'Refresh All Flights' for a date, thread starts. Each flight refresh is an HTTP call to AeroAPI. If 50 flights x 2s each = 100s of network I/O in a thread, but no -> Use ThreadPoolExecutor(max_workers=5) inside _run_bulk_flight_refresh to parallelize AeroAPI calls. Or use run_in_background to decouple from the view and return 202 Accepted immed
- **BG-11 (Medium)** `reservations/signals.py::reservation_saved` — High-traffic scenario: 100 reservations booked in 1 minute = 100 threads spawned. Threads compete for GIL and SMTP connection pool. -> Use run_in_background(send_internal_confirmation, instance) instead. This gives you a bounded thread pool.
- **BG-12 (Medium)** `ghl_integration/tasks.py::batch_send_unsent_leads / _rescue_stale_leads` — Batch fetches lead IDs, then spawns 50 background tasks, each doing a separate get(). Instead of 1 query to load all leads upfront, you get 1 query for IDs + 50 queries for full le -> Batch-load all leads upfront with select_related('vehicle') and pass lead objects to run_in_background, or pass only minimal fields needed to spawn the task and accept the redundan
- **BG-13 (Medium)** `ghl_integration/tasks.py::start_follow_up_sequence` — If 1000 leads have active sequences, and you're starting a sequence for a new lead, the inner loop does 1000 phone comparisons. Each FollowUpTask row has a lead_id, but there's no  -> Replace the loop with a single query: Lead.objects.filter(normalized_phone=lead.normalized_phone, sequence_active=True).exclude(id=lead.id).exists(). Already normalized_phone exist
- **BG-14 (Medium)** `ghl_integration/services.py::contact_has_replied` — Called inside process_follow_up_batch for ~100 leads x up to 2 requests each = 200 synchronous GHL API calls (~2s each with timeout=10). This runs in the scheduler and blocks the w -> Move contact_has_replied check outside the tight send loop. Instead, batch the check BEFORE sending (e.g., prefetch all conversations in one call if GHL API allows, or skip the che
- **BG-16 (Medium)** `ghl_integration/tasks.py::process_follow_up_batch` — If 100 tasks, and each task looks up its FollowUpSequence separately (queries by step_number, segment, is_active), you get 100 queries for templates that might be the same (e.g., a -> Batch-fetch all unique (step_number, segment) combinations from FollowUpSequence upfront and memoize in a dict. Then look up in the dict in the loop.
- **BG-17 (Medium)** `ghl_integration/tasks.py::detect_lost_leads` — If 10,000 leads are past pickup and need to be marked lost, the task runs 50 times (50 x 200) to catch them all. Each run is ~1 second, totaling 50 seconds per cycle. AND if the ta -> Use a batching strategy with an explicit cursor (e.g., WHERE pickup_date < today AND id > last_id LIMIT 200) to detect and continue from a previous run. Or set a recurring flag lik
- **BG-18 (Medium)** `reservations/admin.py::mark_converted action` — Index building is O(n) where n = total reservations. For 1000 reservations, index build is fast. But for 100,000 reservations, building the index once is expensive, and the admin a -> No immediate fix without understanding match_lead's complexity. But consider: if index build is slow, cache it for 5 minutes per location/date. Or implement a fuzzy search table (d
- **BG-19 (Medium)** `ghl_integration/runner.py::run_in_background` — Unbounded thread spawning can exhaust OS thread limits and cause memory pressure. Python thread overhead is ~8MB per thread; 1000 threads = 8GB. -> Implement a bounded thread pool using concurrent.futures.ThreadPoolExecutor or use a library like queue.Queue with a fixed number of worker threads. Cache the executor in the modul
- **DISP-07 (Medium)** `dispatching/management/commands/dispatch_alerts.py::handle (dispatch_alerts command)` — Frequent polling of today's legs from the Gunicorn worker adds cumulative DB and CPU load. During peak hours, 288 requests/day × 50ms per request = 14 extra seconds of worker time  -> Move to external cron job (Windows Task Scheduler already set up per comment). Or cache today's legs (15-min TTL) to reduce re-fetches.
- **BG-20 (Low)** `ghl_integration/tasks.py::retry_failed_syncs` — No explicit retry limit or dead-letter age. A sync can be stuck in FAILED status for weeks if GHL API is intermittently down. -> Set a max_attempts limit (already exists as sync_log.max_attempts, default unclear) and move old FAILED logs to DEAD_LETTER after max_attempts is exhausted (already done line 60-61
- **DISP-14 (Low)** `dispatching/views.py::_run_bulk_flight_refresh (cache management)` — Cache is being over-written frequently, but timeout is very long. If a task fails or hangs, cache entry persists for 1 hour, confusing future poll attempts. -> Reduce timeout to 5-10 minutes. Or use a timestamp-based cache eviction (check started_at + 5min).
- **RES-14 (Low)** `reservations/views.py::lead_quote view POST handler` — Even though the thread is spawned (non-blocking), if the thread encounters a slow external API (NTFY, Meta Conversions API), the thread will be blocked and live until timeout. With -> This is already correctly async (spawns thread, returns immediately). Improve: use a proper async task queue (Celery) or implement exponential backoff + retry for external APIs.
- **TMPL-05 (Low)** `dispatching/templates/dispatching/driver_schedules_dashboard.html::setInterval(updateClock, 1000) (line 1278)` — The clock update itself is cheap (Date.now()), but running 1000+ setIntervals across multiple tabs/users wastes browser CPU. Not a backend issue, but contributes to overall browser -> Move to requestAnimationFrame or reduce polling to 5000ms for a 5-second-accurate clock. Alternately, use CSS animation or let the client's system clock show via date pipe filter i
- **TMPL-06 (Low)** `dispatching/templates/dispatching/timeclock.html::setInterval(tick, 1000) and timeclock_overview.html setInterval(tick, 1000) (lines 211, 147)` — setInterval(1000) is appropriate for real-time display, but unnecessary DOM manipulation (even if just innerHTML update) causes layout thrashing. Browsers optimize this, but it's s -> Use CSS animation or requestAnimationFrame for smoother updates. Alternately, accept 1s accuracy and keep the setInterval but use textContent instead of innerHTML to avoid DOM pars
- **Cross-cutting:** the `ghl_integration` scheduler thread (`_run_scheduler`, every 30 min) and all per-request daemon threads run inside the single worker. **No locking/idempotency** between overlapping batch runs. Centralize on `_run_in_background()`; add a cache/DB lock so `batch_send_unsent_leads` / `process_follow_up_batch` / unpaid-reminders cannot overlap; remove in-request retry `sleep()`s. **This rework is a prerequisite before increasing Gunicorn workers** (otherwise each worker runs its own scheduler -> duplicate sends).

---

## G. Database Index Recommendations

Confirm exact column names against the models before writing migrations; each migration should be independently reversible and justify itself.

- **`Payment.stripe_checkout_id`** (PAY-06) — `db_index=True`; webhook looks up by this on every Stripe event.
- **`Leg(pickup_date)`** and the FK columns used by the dispatch boards (`driver`, `reservation`) — heavily filtered per-day. Consider composite `(pickup_date, driver)`.
- **`Reservation(status, pickup_date)`** — dashboards, capacity planner, commission aggregates filter on these together.
- **`OperationalTask(status)`** — context-processor count + task-queue lanes.
- **`RouteTimingMetric(route, ...)`** (DISP-05 / critic gap) — analytics filter columns.
- **Lead dedup keys** (`last_name`, last-10 of phone, `pickup_date`) used by `_build_duplicate_cache` and convergence — index or functional index to avoid full scans.
- **`TimeClockShift(user, clock_out_at)`** — code references `idx_tcshift_open`; verify it exists in migrations.
- **Blog search fields** (if traffic grows) — GIN/trigram index for `__icontains`, else paginate + bound.

---

## H. Quick Wins (low risk, do first)

- **BOOKING-02 (Critical)** `reservations/views.py::reservation_form() view, lines 258-266` — Wrap in _run_in_background(send_initiate_checkout_event, reservation, request). Return checkout response immediately; let event send async in daemon thread.
- **DISP-06 (Critical)** `dispatching/views.py::refresh_all_flights (4824-4914)` — Move the trip_type filtering (lines 4866-4869) into the background thread _run_bulk_flight_refresh (after line 4669). Alternatively, annotate legs queryset with trip_type
- **DRIV-07 (Critical)** `drivers/timeoff_notifications.py::notify_founders_of_new_request, notify_driver_of_decision` — Queue all SMS sends as background jobs before returning response. Use `_run_in_background()` or Celery task. Mark the request as 'pending approval' immediately, notify as
- **ADMIN-01 (High)** `reservations/admin.py::ReservationAdmin.payment_status_display` — In get_queryset (line 923), annotate the latest payment using Subquery or F-expressions. Store in obj._latest_payment_id and fetch in template/display method via prefetch
- **ADMIN-02 (High)** `users/admin.py::TravelAgentAdmin._calculate_commission_preview` — Prefetch related legs before the loop: unpaid_reservations.prefetch_related('legs'). Better: bulk aggregate in DB using Subquery and Min() to find earliest_leg_date, avoi
- **ADMIN-03 (High)** `drivers/admin.py::DriverPaymentAdmin.profit_display` — Add list_select_related = ('payment__driver', 'leg', 'leg__reservation') to LegPaymentAdmin (around line 925-935).
- **ADMIN-04 (High)** `reservations/admin.py::FlightAdmin.is_in_use` — Ensure get_queryset always includes the annotation and remove the fallback .exists() query. Add assertion that obj.is_linked exists.
- **ADMIN-06 (High)** `users/admin.py::TravelAgentAdmin.mark_agents_for_payment` — Use queryset.aggregate(Sum('unpaid_commissions')) instead of Python sum loop.
- **ADMIN-07 (High)** `drivers/admin.py::DriverAdmin.preview_driver_payments` — Pre-fetch all unpaid legs for all selected drivers in a single prefetch_related before the loop, or use Leg.objects.filter(driver__in=driver_ids, payment_status='unpaid')
- **ADMIN-14 (High)** `drivers/admin.py::DriverPaymentAdmin (list_select_related)` — Add list_select_related = ('payment__driver', 'leg', 'leg__reservation') or include in get_queryset.select_related().
- **AGENTS-01 (High)** `users/views.py::AgencyHeadDashboardView.get_context_data` — Use prefetch_related('reservations') on the agents queryset and annotate with Count('reservations'). Then access cached counts: agent._reservation_count instead of agent.
- **AGENTS-08 (High)** `users/services.py::process_bulk_payouts` — Use bulk_ready_totals([agent_ids...]) from eligibility.py which fetches all reservations in 2 queries total. Or prefetch all agent reservations once and filter by agent_i
- **BG-15 (High)** `ops/tasks.py::detect_driver_conflicts / _turn_late_minutes` — Implement a request-level cache for drive times within a single ops task generation run (e.g., functools.lru_cache on _reposition_minutes). Or use a fallback categorizati
- **BOOKING-01 (High)** `reservations/views.py::reservation_form() view, lines 186-206` — Combine into 2 writes max: (1) save reservation with customer, travel_agent, created_by set before first save, (2) single .update() for booking_source. Set all fields bef
- **BOOKING-04 (High)** `reservations/views.py::QuoteFormHandlerView.post(), lines 447-478 and 566-592` — Use _run_in_background() helper (utils.py:48) consistently instead of duplicating Thread logic. Or set up Celery+Redis for proper task queueing. At minimum, reuse the hel
- **DISP-02 (High)** `dispatching/views.py::_refresh_one_flight (inside for loop)` — Use ThreadPoolExecutor (already used in _run_bulk_flight_refresh line 4721-4722). Batch flights into groups of 5 (AeroAPI rate limit 5/sec), await all threads before retu
- **DISP-03 (High)** `dispatching/scheduler.py::suggest_assignments (main loop at line 1306)` — Cache sorted_slots once per driver/schedule at the start of the per-driver iteration (line 1320 entry) and reuse it in all three scoring sections. Or, pre-sort at schedul
- **DISP-04 (High)** `dispatching/views.py::index (104-500)` — Annotate legs queryset with aggregate sum of leg-level revenue or use Prefetch('reservation__payments', ...) to load all payments once, cache result so afterhours_fee_out
- **DISP-05 (High)** `dispatching/views.py::legs_list (1809-1990)` — Compute trip_type once per leg via a dict comprehension before loops. Cache in {leg_id: trip_type} or annotate each leg object with _cached_trip_type after first computat
- **DISP-05 (High)** `dispatching/analytics.py::calculate_route_timing_metrics` — Add default recent_days constraint (e.g., default to last 90 days if not specified). Document that callers MUST pass date-filtered queryset or recent_days parameter. Add 
- **DISP-09 (High)** `dispatching/views.py::auto_assign_drivers (9767-9770, 9759-9765)` — Pre-compute driver availability dict once before loops (like DISP-01): before line 9759, collect all driver availability data into {driver_id: {is_avail, sh, eh, pref, fl
- **DISP-11 (High)** `dispatching/views.py::_run_bulk_flight_refresh` — Create a single AeroAPIService (with session) before the thread pool, pass it to worker threads. Or use thread-local storage for shared session.
- **OPS-01 (High)** `ops/views.py::_build_driver_conflict_context` — Add 'status_history' to the prefetch_related on line 765 when querying day_legs. Change line 765 from .prefetch_related('status_history') to ensure status_history is pref
- **OPS-03 (High)** `ops/leads_board.py::lead_board_detail` — Add .prefetch_related('activities', 'follow_up_tasks') to the Lead query on line 270.
- **PAY-01 (High)** `payment/webhook.py::stripe_webhook / handle_checkout_session` — Replace threading.Thread(...).start() with _run_in_background(send_purchase_event, reservation, value=None, event_id=event_id) to use the app's existing background task i
- **PAY-02 (High)** `payment/views.py::payment_success` — Replace threading.Thread(...).start() with _run_in_background(send_purchase_event, ...). The success page returns immediately, and the Meta call happens deferred.
- **PAY-05 (High)** `dispatching/views.py::_process_stripe_refund` — Option 1: Batch refunds (create a single refund for the total amount). Option 2: Defer the loop to _run_in_background and return 202 Accepted immediately. Option 3: Imple
- **PAY-06 (High)** `payment/webhook.py::handle_checkout_session` — Add database index: `db_index=True` on stripe_checkout_id field OR create a multi-column index (reservation, customer, stripe_checkout_id). This is a one-time migration, 
- **RES-01 (High)** `reservations/models.py::Reservation.calculate_total_driver_payments` — Use .aggregate(total=Sum('total_driver_pay')) on self.legs.all() instead of sum() in Python. Cache result as a stored field updated on leg save via post_save signal if re
- **RES-03 (High)** `reservations/models.py::Reservation.recalculate_leg_revenue_shares` — Use bulk_update() to update all legs in a single query. E.g., collect updates, then Leg.objects.bulk_update(legs_to_update, ['revenue_share', 'profit_estimate']).
- **RES-04 (High)** `reservations/signals.py::update_agent_commission_data` — Move commission recalculation to an explicit update_agent method callable only when status changes (use the _pre_save_old_values already captured to detect). Skip recalc 
- **ADMIN-05 (Medium)** `rates/admin.py::LocationGroupAdmin.location_count` — In get_queryset, annotate with Count('locations') and use annotation in display method. Or remove from list_display and move to readonly detail view.
- **ADMIN-12 (Medium)** `drivers/admin.py::DriverAdmin (get_queryset)` — The get_queryset is already optimized; issue is the query cost. Consider: (1) Move profit_performance to detail view only. (2) Cache the annotations on DriverPayment/Leg 
- **ADMIN-13 (Medium)** `reservations/admin.py::ReservationAdmin (get_queryset)` — Ensure payment_status_display uses the prefetched 'payments' cache. See ADMIN-01. Otherwise, the get_queryset is reasonably optimized.
- **AGENTS-03 (Medium)** `users/views.py::agency_payout_detail` — Annotate count on the queryset or prefetch and use len(list). Then check len > 0 or use the count result.
- **AGENTS-09 (Medium)** `users/views.py::AgencyDetailView.get_context_data` — Annotate on get_queryset: .annotate(total_unpaid=Sum('agents__unpaid_commissions'), ...). Then use these cached annotations in context instead of calling the methods.
- **AGENTS-10 (Medium)** `users/middleware.py::NewsletterRateLimitMiddleware.is_allowed` — Combine into one query: aggregate the max of two counts or use a single .filter(Q(...) | Q(...)) with Count(). Or use cache key instead of DB for rate limit.
- **AREA-01 (Medium)** `services/views.py::orlando_airport_transportation` — Replace line 38 for r in v.rates.all(): with iteration of the already-prefetched data. Instead of calling .all() again, iterate v.rates directly. Change to: for r in v.ra
- **AREA-03 (Medium)** `blog/views.py::blog_list` — Change line 15 to: blogs_queryset = Blog.objects.select_related('user').all().order_by('-created'). Also apply to line 49 in blog_post() for related_posts.
- **AREA-05 (Medium)** `content/sitemaps.py::BlogPostSitemap.items` — Add .only('slug', 'created') to defer User FK loading. Change to: Blog.objects.order_by('-created').only('slug', 'created')
- **BG-03 (Medium)** `ghl_integration/tasks.py::retry_failed_syncs` — Remove the 0.5s sleep. Batch all retry API calls with a bounded ThreadPoolExecutor (5-10 threads) to parallelize GHL service.* calls. GHL rate limiting should be handled 
- **BG-07 (Medium)** `payment/views.py::payment_success` — Use run_in_background() instead of Thread(). Alternatively, move send_purchase_event to a post_save signal or a queued task.
- **BG-11 (Medium)** `reservations/signals.py::reservation_saved` — Use run_in_background(send_internal_confirmation, instance) instead. This gives you a bounded thread pool.
- **BG-12 (Medium)** `ghl_integration/tasks.py::batch_send_unsent_leads / _rescue_stale_leads` — Batch-load all leads upfront with select_related('vehicle') and pass lead objects to run_in_background, or pass only minimal fields needed to spawn the task and accept th
- **BG-13 (Medium)** `ghl_integration/tasks.py::start_follow_up_sequence` — Replace the loop with a single query: Lead.objects.filter(normalized_phone=lead.normalized_phone, sequence_active=True).exclude(id=lead.id).exists(). Already normalized_p
- **BG-16 (Medium)** `ghl_integration/tasks.py::process_follow_up_batch` — Batch-fetch all unique (step_number, segment) combinations from FollowUpSequence upfront and memoize in a dict. Then look up in the dict in the loop.
- **BG-19 (Medium)** `ghl_integration/runner.py::run_in_background` — Implement a bounded thread pool using concurrent.futures.ThreadPoolExecutor or use a library like queue.Queue with a fixed number of worker threads. Cache the executor in
- **BOOKING-03 (Medium)** `reservations/utils.py::extra_charges() function, lines 539-601` — Use bulk updates: Leg.objects.filter(reservation=reservation).update(afterhours_fee=...). Prefetch vehicle to cache property access.
- **BOOKING-05 (Medium)** `reservations/forms.py::ReservationAdminForm.__init__(), lines 345-352` — Already optimized with select_related. Consider class-level caching or lazy property to avoid re-fetching on every form instantiation. Document the optimization.
- **BOOKING-06 (Medium)** `reservations/views.py::reservation_form() view, lines 134-147` — Use: count = stale_dupes.delete()[0] which returns (deleted_count, {...}). Or: list(stale_dupes[:1000]); len(list); delete by pk list.
- **BOOKING-08 (Medium)** `reservations/signals.py::auto_convert_lead_on_reservation() signal, lines 208-301` — Use .first() for early exit (already done). Could cache result for same email+phone in current request. Not urgent.
- **BOOKING-10 (Medium)** `reservations/templatetags/quote_tags.py::quote_form() template tag, lines 30-45` — Use @cache_page(3600) or django.views.decorators.cache. Or move data to JS/JSON cached by browser. Invalidate cache on Vehicle/Rate model change (signals).
- **DISP-01 (Medium)** `dispatching/views.py::capacity_planner (8457-8927)` — Batch-load all drivers with prefetch_related('weekly_schedule', 'date_overrides') once at line 8535-8540. Then cache results in a dict {driver_id: eff_dict} computed once
- **DISP-01 (Medium)** `dispatching/scheduler.py::suggest_assignments` — Pre-compute leg end times once before the double loop (already done elsewhere in the code — line 847 uses _estimated_end_dt). Pre-compute all categorizations into a dict 
- **DISP-02 (Medium)** `dispatching/views.py::schedule_board (793-1246)` — Pre-compute all driver availability once before loops (lines 989-1120). Cache in dict {driver_id: eff_dict}. Reuse for all downstream loops. Same fix as DISP-01.
- **DISP-04 (Medium)** `dispatching/scheduler.py::suggest_assignments` — Pre-compute all end times into {leg_id: datetime} once before suggest_assignments (as is done on line 847 for build_driver_schedules via _estimated_end_dt). Pass this dic
- **DISP-06 (Medium)** `dispatching/scheduler.py::suggest_assignments (around line 1320)` — Add first_pickup_time, last_end_time, and span_hours fields to DriverDaySchedule at build time (line 921) and maintain them as slots are added. Read from those cached fie
- **DISP-06 (Medium)** `dispatching/analytics.py::calculate_demand_pattern_for_hour` — Fetch all legs for the date range once, annotate with hour via Extract('hour', 'pickup_time'), then group in Python. Or use bulk create/update with F() expressions in the
- **DISP-07 (Medium)** `dispatching/views.py::capacity_planner (8668-8704)` — Replace lines 8688-8703 with single loop that computes list of status_history.all() once and extracts 'completed' entry more efficiently (e.g., use next((s for s in sh_li
- **DISP-07 (Medium)** `dispatching/scheduler.py::build_driver_schedules` — Add Prefetch for LegStop and LegFlight to the Leg queryset in capacity_planner view (line 8497-8524) and anywhere else build_driver_schedules is called. This converts 200
- **DISP-07 (Medium)** `dispatching/management/commands/dispatch_alerts.py::handle (dispatch_alerts command)` — Move to external cron job (Windows Task Scheduler already set up per comment). Or cache today's legs (15-min TTL) to reduce re-fetches.
- **DISP-08 (Medium)** `dispatching/views.py::update_leg_assignment (2003-2234)` — Add select_related('reservation', 'driver') to line 2047 query. This saves 2 DB round-trips per call.
- **DISP-08 (Medium)** `dispatching/views.py::capacity_planner` — Bulk-load weekly schedules and date overrides for all drivers once (line 8537 already does select_related('weekly_schedule') and prefetch_related('date_overrides'), so th
- **DISP-09 (Medium)** `dispatching/scheduler.py::compute_leg_scarcity` — Cache the per-vtype driver counts at DriverVehicleAssignment.objects.all() level (a single query/refresh at the start of the day), and refresh only if assignments change.
- **DISP-09 (Medium)** `dispatching/views.py::refresh_flight_data` — Add `.limit(10)` to the LegFlight query with a warning log if limit is hit. Or use `only('flight_id')` projection to reduce memory.
- **DISP-10 (Medium)** `dispatching/views.py::refresh_all_flights` — Move the trip_type filter into the queryset using Q() filters on pickup/dropoff locations. Or add a trip_type field to Leg model (denormalized).
- **DISP-13 (Medium)** `dispatching/analytics.py::update_daily_capacity_metrics` — Fetch all legs for date range and driver set once with select_related/prefetch. Group by (driver_id, pickup_date) in Python. Single query instead of 350.
- **DRIV-09 (Medium)** `drivers/views.py::extend` — Replace with:
```python
base_qs = Driver.objects.filter(is_active=True) if not show_inactive else Driver.objects.all()
agg = base_qs.aggregate(
    all_total=Count('id'),
- **DRIV-10 (Medium)** `drivers/views.py::driver_statement_detail` — Add a count check: if count > 50, warn staff 'Showing 50 of N unpaid legs'. Alternatively, allow pagination in the modal or batch-add.
- **DRIV-11 (Medium)** `drivers/admin.py::unpaid_legs_display` — Remove from list_display or make it a separate detailed view. If needed, use a custom changelist view that prefetches unpaid legs once and caches the HTML.
- **DRIV-12 (Medium)** `drivers/admin.py::recent_leg_history` — Remove from list_display. Link to driver detail page instead, which shows recent leg history.
- **DRIV-14 (Medium)** `drivers/payout_adjustments.py::_recalculate_payment_total` — Replace loop with: `agg = active.aggregate(total=Sum('amount'), base=Sum('base_pay'), grat=Sum('gratuity'), addl=Sum('additional'))` and extract values. Converts per-LegP
- **DRIV-15 (Medium)** `drivers/gusto_export.py::build_rows_for_period, validate_selection` — Ensure eligible_payments_qs() is always the source and the annotation is preserved through the list comp. Add a test to verify query count doesn't exceed 2-3 regardless o
- **OPS-04 (Medium)** `ops/views.py::task_detail_view` — Add .prefetch_related('comm_attempts') to the OperationalTask query on line 2001-2020.
- **PAY-07 (Medium)** `reservations/models.py::Reservation.total_paid / Reservation.amount_owed / Reservation.payment_status` — Add prefetch_related('payments') to every view/query that accesses these properties. Example: `Reservation.objects.filter(...).prefetch_related('payments')` before any te
- **PAY-08 (Medium)** `payment/signals.py::_payment_saved / compute_paid_state` — Combine the two aggregates into a single query: `paid_qs.aggregate(gross=Sum("amount"), refunded=Sum("refunded_amount"))`. This produces one SQL query instead of two.
- **PAY-09 (Medium)** `reservations/conversions.py::send_purchase_event` — Prefetch payments before calling send_purchase_event: `Reservation.objects.prefetch_related('payments').get(pk=...)`. Or, modify send_purchase_event to accept an optional
- **PAY-10 (Medium)** `ops/kpis.py::by_travel_agent / by_route / by_vehicle` — Add slicing to the revenue queries: `.[:limit]` on both the headline aggregates and the res_ids list. For example, `res_ids = list(pay.values_list('reservation_id', flat=
- **RES-07 (Medium)** `reservations/models.py::Leg.intermediate_stops, Leg.has_additional_dropoffs, Leg.has_intermediate_stops` — Change to @cached_property so the legstop_set.all() result is cached for the lifetime of the instance.
- **RES-08 (Medium)** `reservations/models.py::Leg.all_stops` — Change to @cached_property.
- **RES-09 (Medium)** `reservations/views.py::index view` — Move rate filtering into the prefetch() queryset with filter() to avoid returning all rates.
- **RES-11 (Medium)** `reservations/forms.py::CustomerForm.save` — Replace with direct get_or_create(email=..., phone_number=..., defaults={...}) call. Remove the conditional save().
- **RES-12 (Medium)** `reservations/admin.py::LegAdmin.get_queryset` — Remove the annotation unless it's used in list_display or filtering. If needed, use .prefetch_related('reservation__legs') and count in Python.
- **RES-15 (Medium)** `reservations/signals.py::store_reservation_old_values pre_save` — Optimize: pass update_fields to the signal and skip the DB query if update_fields only contains non-watched fields.
- **TMPL-03 (Medium)** `content/static/js/timeline-dnd.js::checkFeasibility function (lines 30-48)` — Cache feasibility results more aggressively. Current code has feasibilityCache (line 14), but it's cleared on successful assignment (line 247). Instead: (A) cache for 30s
- **TMPL-07 (Medium)** `dispatching/templates/dispatching/includes/driver_timeline.html::driver_timeline include (lines 55-77, 87-100)` — Don't enumerate all fitting legs as data attributes. Instead: (A) embed fitting legs as a single JSON data-fit attribute: data-fit='[{id:1,...}]', or (B) fetch fitting le

Plus deploy-safety: **env-gate `DEBUG`** (default `False`; local dev sets `DJANGO_DEBUG=true` in `.env`).

---

## I. Medium-Risk Fixes (need testing)

- **ADMIN-08 (Critical)** `drivers/admin.py::DriverAdmin.process_driver_payments` — Bulk-fetch all legs upfront. Use bulk_create for DriverPayment and LegPayment instead of per-driver loop with .save().
- **DISP-03 (Critical)** `dispatching/views.py::auto_assign_drivers (9659-10076)` — Replace individual leg.save() calls with Leg.objects.bulk_update(legs_to_update, fields=['driver', 'driver_assigned_by', 'driver_assigned_at'], batch_size=500). Collect m
- **DRIV-05 (Critical)** `drivers/views.py::index, schedule` — Move the loop into a background task via `_run_in_background()`. Caller can show a loading state or return minimal data; job enriches legs asynchronously. Alternatively, 
- **DRIV-06 (Critical)** `drivers/views.py::refresh_flight_data` — Queue a background job to fetch and update flight data. Return 202 Accepted with a status-check endpoint, or update the DOM via WebSocket when ready. For synchronous resp
- **OPS-02 (Critical)** `ops/views.py::_build_driver_conflict_context` — Batch Google Maps requests: collect all (pickup_location, dropoff_location) pairs before the loop, cache/batch the API calls, or use a fallback-first pattern (historical 
- **OPS-11 (Critical)** `ops/views.py::_build_driver_conflict_context` — Implement request-local caching (within one task_detail_view): cache results by (pickup_location, dropoff_location) pair. OR use shared Redis cache with 1-hour TTL. OR ba
- **ADMIN-09 (High)** `reservations/admin.py::ReservationAdmin.update_profit_calculations` — Bulk-update profit_estimate using database expressions (F() + Case/When) without looping. If update_profit_calculations must run per row, defer to background task or run 
- **AGENTS-05 (High)** `users/admin.py::TravelAgentAdmin._calculate_commission_preview` — Batch the Reservation query outside the loop using travel_agent__in=list(queryset), then prefetch_related('legs'). Then group results by agent_id in Python.
- **AGENTS-07 (High)** `users/signals.py::handle_agency_payout_deletion` — Use bulk_delete: instance.agent_payouts.all().delete(). Or if signals must run, use bulk_update with a mark-deleted flag or batch_size for parallelism.
- **BG-02 (High)** `ghl_integration/tasks.py::process_follow_up_batch` — Remove sleep(1). Instead, batch all send_sms calls and run them in parallel with concurrent.futures.ThreadPoolExecutor (bounded to 10-20 threads) or remove it entirely if
- **BG-05 (High)** `ghl_integration/pre_pickup.py::send_pre_pickup_nudges` — Same fix as BG-02: remove sleep(1) and batch SMS sends with ThreadPoolExecutor or request-level rate limiting.
- **DISP-01 (High)** `dispatching/flight_verify_views.py::flight_verification_public` — Fire the refresh in background thread (like _run_bulk_flight_refresh uses ThreadPoolExecutor) or queue to Celery. Show 'checking...' spinner and poll status, or return 20
- **DISP-03 (High)** `dispatching/flight_verify_email.py::_send_in_background` — Move to Celery queue (which doesn't exist yet). For now, add a 1-second cap to retries (no exponential backoff) if threading must stay. Sleep 1-2 seconds max per retry.
- **DISP-04 (High)** `dispatching/confirmation_sms.py::send_confirmation_via_twilio` — Batch SMS sends into background task (Celery) or ThreadPoolExecutor. Return 202 Accepted and redirect dispatcher to a polling-status page. send_confirmations_for_date is 
- **DISP-05 (High)** `dispatching/swap_optimizer.py::_get_conflicting_slots` — Cache the result of check_feasibility for each (schedule, slot) pair. Before searching, run a single O(L) pass: for each slot, evaluate feasibility once. Use that cache d
- **DISP-08 (High)** `dispatching/flight_verify_views.py::flight_verification_check` — Return 202 Accepted with a task_id, poll status from JS. Or cache results for the same flight+date combo (30-min TTL) to avoid re-fetching.
- **DISP-12 (High)** `dispatching/confirmation_sms.py::send_confirmations_for_date` — Batch into ThreadPoolExecutor (5-10 workers), collect results, do bulk_update() after all sends. Or use Celery with exponential backoff on failures.
- **DRIV-08 (High)** `drivers/availability.py::_weekly_or_defaults, resolve_effective_availability` — Require caller to prefetch_related('weekly_schedule', 'date_overrides') before calling. Accept optional prefetched lists as parameters, or use `resolve_effective_availabi
- **DRIV-13 (High)** `drivers/utils.py::get_drive_time` — Add exponential backoff retry (1, 2, 4 seconds), circuit breaker after 3 consecutive failures, and fallback to last-known value or zero. Consider async fetch via backgrou
- **PAY-03 (High)** `payment/utils.py::get_or_create_stripe_customer` — Cache Stripe customer ID on first creation; add a check-and-reuse pattern with fallback logic. Skip re-retrieve if customer_id exists AND was created <24h ago. If Stripe 
- **PAY-04 (High)** `payment/webhook.py::handle_checkout_session` — Avoid re-fetching Stripe objects; derive state from session data alone. If re-fetch is required, defer to background task and return 200 immediately. Option 3: Add timeou
- **RES-05 (High)** `reservations/signals.py::auto_convert_lead_on_reservation + _converge_duplicate_leads` — Batch the duplicate lead convergence: collect all twins to update, then use bulk_update() instead of saving each. Use update_fields on save() to skip GHL sync signal recu
- **RES-06 (High)** `reservations/models.py::Leg._assign_route_from_locations` — Query by indexed location fields directly: Route.objects.select_related('origin', 'destination').filter(origin__name__icontains=self.pickup_location, destination__name__i
- **RES-10 (High)** `reservations/views.py::reservation_form view` — Use update_fields to skip unnecessary signal handlers on intermediate saves. E.g., reservation.save(update_fields=['travel_agent']) to skip commission recalc if only assi
- **TMPL-02 (High)** `dispatching/templates/dispatching/legs_filter.html::dashboard view (legs_filter template, line 782 in views.py)` — Set explicit poll interval to 2000ms or higher (don't hammer backend). Add debouncing/throttling to prevent concurrent requests. Cache flight refresh results in Redis for
- **AGENTS-06 (Medium)** `users/views.py::admin_commission_report` — Use Django ORM: payouts.values('agent_id').annotate(total=Sum('total_amount'), payouts=Count('id')) instead of Python loop.
- **AGENTS-13 (Medium)** `users/signals.py::handle_payout_deletion` — Keep the bulk update but verify no post_delete signals on Reservation fire inefficiently. Consider batching large updates if Reservation.post_delete is expensive.
- **AREA-02 (Medium)** `blog/views.py::blog_list` — Add PostgreSQL trigram index and use SearchVector/SearchQuery from django.contrib.postgres.search. Change to: Blog.objects.annotate(search=SearchVector('title', 'content'
- **BG-17 (Medium)** `ghl_integration/tasks.py::detect_lost_leads` — Use a batching strategy with an explicit cursor (e.g., WHERE pickup_date < today AND id > last_id LIMIT 200) to detect and continue from a previous run. Or set a recurrin
- **BOOKING-07 (Medium)** `reservations/signals.py::update_agent_commission_data() signal, lines 63-162` — Ensure ALL non-commission saves use update_fields (extra_charges at line 588 already does this). Also consider caching agent.pending_commissions in Redis and recomputing 
- **OPS-07 (Medium)** `ops/leads_board.py::leads_board_view` — Add pagination (fetch top 200 leads by priority, lazy-load rest) or implement AJAX-based lazy loading for bucket expansion.
- **OPS-09 (Medium)** `ops/escalation.py::run_escalations` — Queue send_dispatch_alert_notification() calls asynchronously (background task or batched NTFY call).
- **PAY-11 (Medium)** `payment/webhook.py::handle_checkout_session (get_or_create)` — Add `unique=True` to the stripe_checkout_id field in the Payment model. This ensures database-level idempotency: if a webhook retries, the second get_or_create finds the 
- **RES-02 (Medium)** `reservations/models.py::Reservation.update_profit_calculations` — Defer profit recalculation to an explicit call only when needed (e.g., status change), or use a signal that batches leg updates and recalcs once. Avoid calling in loops.
- **TMPL-04 (Medium)** `dispatching/templates/dispatching/daily_capacity_planner.html::driver_availability_json context variable (line 2495)` — Lazy-load driver availability via AJAX: don't embed driver_availability_json in the page. Instead, fetch it on-demand when the user opens the 'Auto-Assign' modal. Move JS

---

## J. Larger Architecture Improvements

**Chosen direction: harden the daemon-thread model — no Celery for now** (volume is ~40-80 sends/day).

1. **Single background entry point.** Route *every* fire-and-forget through `reservations/utils.py::_run_in_background()` (already exists: daemon thread + try/except logging). Delete ad-hoc `threading.Thread(...)` spawns in views/signals/admin/webhook.
2. **Idempotency + locking.** Wrap scheduled batches (`batch_send_unsent_leads`, `process_follow_up_batch`, unpaid-reminders, escalations) in a cache/DB lock so concurrent triggers cannot double-send. Track per-record `sent`/`next_retry_at` state.
3. **No in-request retries.** Move retry/backoff out of the request path into the scheduled batch (fixes BG-01 `sleep(60-180s)` in worker).
4. **No thread-per-row.** Admin actions enqueue a single batch job, not one thread per selected row (ADMIN-10).
5. **Then — and only then — scale the web tier.** Once the scheduler is a singleton (run on one worker / a dedicated process, or guarded by a lock), increase Gunicorn `--workers`/`--threads`. Until then, multiple workers = duplicate background sends. Also move the cache to **Redis** (`REDIS_URL` already wired in `settings.py`) so dedup/idempotency keys are shared across workers.
6. **Reporting/aggregation:** consider cached summaries (cache with TTL or a small denormalized table) for the heaviest dashboards once measured.

---

## K. Testing & Measurement Plan

**Existing instrumentation:** `reservations/middleware.py::SlowRequestMiddleware` already logs any request >500ms to the `perf` logger. Debug Toolbar is installed but gated behind `ENABLE_DEBUG_TOOLBAR=1` (intentionally off because it slows query-heavy pages).

**Before/after each fix:**
1. **Query count** — wrap the view/action in `django.test.utils.CaptureQueriesContext(connection)` (or load with `ENABLE_DEBUG_TOOLBAR=1` locally) and record query count before -> after. Quick wins should show large drops (e.g. capacity planner ~80 -> ~20).
2. **Duration** — read the `SLOW` lines from the `perf` logger; confirm the page drops below the 500ms threshold.
3. **EXPLAIN** — for new indexes, run `EXPLAIN (ANALYZE, BUFFERS)` on the target query before/after on a prod-sized dataset.
4. **Correctness guards** — for `auto_assign` bulk_update: assert assignments are identical to the pre-change run on the same date. For signal/`update_fields` changes: assert commission/profit values unchanged. For `bulk_update` revenue-share: assert per-leg `revenue_share` equals the looped version.
5. **Unit tests** — run `python manage.py test <app>` for apps with existing tests (`dispatching`, `drivers`, `payment`, `reservations`, `users`, `ghl_integration`, `ops`). Where none cover the path, follow the per-finding 'How to test' checklist.
6. **Load** — `locustfile.py` exists at repo root; compare p95 latency on the worst pages before/after, especially after any worker-count change.

**Suggested first measurement targets:** conflict-task detail, capacity planner, schedule board, legs dashboard, agency-head dashboard, the three heavy admin changelists, auto-assign apply.
