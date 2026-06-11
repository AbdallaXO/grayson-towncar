# Arrival Flight Analytics & Scheduling Efficiency — Analysis + Build Plan

> Status: PLANNED (not started). Read-only v1. Zero schema migrations through Phase 4.
> Scope: arrival flights + dispatch scheduling only — NOT fleet/vehicle capacity.

## Context

Grayson Towncar runs MCO/SFB airport-arrival pickups in Orlando. The founder wants to turn the
**arrival flight data we already collect** into analytics that help dispatchers build better
arrival schedules — flight timing patterns, airline behavior, dwell time, and risk warnings.

**The single most important finding:** the codebase is already far richer than "we only have
guest-entered flight numbers." It persists **real AeroAPI actual landing times**, **normalized
airline + flight number**, **timezone-aware datetimes**, **LegStatus operational timestamps**, a
**`RouteTimingMetric` dwell model**, and even **two unwired summary tables** (`DemandPattern`,
`DriverDailyCapacity`). So this is mostly a **wire-up + dashboard** effort, **not** a green-field
tracking build. **Zero schema migrations are required for the entire read-only v1.**

**Decisions locked with the founder:**
1. **v1 = read-only Flight Analytics dashboard.** Wiring warnings into the live planner is deferred to a later phase (highest blast radius, kept out of v1).
2. **Defer the one missing field** (driver→guest contact timestamp). Not needed for demand, early/late, or landing→pickup dwell. v1 stays zero-migration.
3. **Cover MCO + SFB** (`AIRPORT_TERMINALS = ("MCO Terminal", "SFB Terminal")`) with an airport filter on the page.

---

## PART 1 — Current arrival flight data structure (Q1 + Q6)

### Where flight data lives
Flight data is **not** on `Reservation` — it hangs off the **`Leg`** via a dedicated **`Flight`** model.

| Model | File:line | Role |
|---|---|---|
| `Reservation` | `reservations/models.py:61` | Only `trip_type` (`one_way`/`round_trip`) + `is_vip`. No flight fields. |
| `Leg` | `reservations/models.py:912` | `pickup_location`/`dropoff_location` (free-text CharField 255), `pickup_date` (DateField), `pickup_time` (TimeField), `driver`, `status`, `driver_assigned_at`, `confirmation_sms_sent_at`, `flight_information` (OneToOne→Flight, legacy), `controlling_flight` property (`:1972`). |
| `Flight` | `reservations/models.py:2150` | The real flight record (see below). |
| `LegFlight` | `reservations/models.py:2097` | Through-model for multi-flight legs; exactly one `is_controlling=True` per leg. |
| `LegStatus` | `reservations/models.py:2832` | **Operational timestamp history** — one row per status transition (`timestamp` indexed). |
| `RouteTimingMetric` | `reservations/models.py:2981` | Pre-aggregated dwell/drive/total percentiles per route+time bucket. |
| `DemandPattern` | `reservations/models.py:3214` | Per-date/hour leg counts by trip type. **Written, never populated.** |
| `DriverDailyCapacity` | `reservations/models.py:3128` | Per-driver/day rollup. **Written, never populated.** |

### The `Flight` model — what we actually store
- **Identity (normalized, clean):** `airline` = IATA code e.g. `DL` (`:2160`), `airline_display_name` = "Delta Airlines" (`:2161`), `flight_number` = digits-only e.g. `1691` (`:2165`), `flight_iata` e.g. `DL1691`, `flight_type` (`arrival`/`departure`), `origin`, `destination`, `status` (`En Route`/`Landed`/…).
- **Scheduled / estimated / ACTUAL times — all `DateTimeField`, all timezone-aware:**
  `scheduled_arrival_local` / `scheduled_gate_arrival_local`, `estimated_arrival_local` / `estimated_gate_arrival_local`, **`actual_arrival_local` (`:2200`, runway) / `actual_gate_arrival_local` (`:2204`, gate)**.
- **Gate/terminal/baggage:** `terminal`, `gate`, `baggage_claim`. `last_updated`.
- **Single source of truth:** `Flight.best_arrival_local()` (`:2225`) → priority chain *actual_gate → estimated_gate → actual_runway → estimated_runway → scheduled_gate → scheduled_runway*. Mirrored as `dispatching/analytics.py:best_flight_arrival_local()` (`:291`).

### Guest-entered vs tracked
**Both, reconciled.** Guest types airline + flight number (`reservations/forms.py:304` `FlightForm`); `Flight.save()` normalizes it (`:2238`). Then **AeroAPI overwrites with real tracked data** — `dispatching/aeroapi_service.py` maps `actual_in→actual_gate_arrival_local`, `actual_on→actual_arrival_local`, etc.; `ops/tasks.py:auto_refresh_flights` (`:1446`) refreshes today..+2 every cycle (days 3–7 on demand). **There is no validation that AeroAPI returned the flight the guest meant** — large mismatches are only *flagged* (`Leg.has_flight_time_mismatch()` `:1636`, ops mismatch scan).

### Can we isolate arrivals / separate from departures? — Yes, but only in Python
`Leg.get_trip_type()` (`:1577`) returns `arrival` / `return` / `cruise` / `other` by classifying pickup vs dropoff text through `dispatching/analytics.py:is_airport_location()` (`:120`, keyword matching — **no Location category field, no FK; `pickup_location` is free-text**). `arrival` = airport pickup + non-airport dropoff. **A clean DB-only filter (`pickup_location__category='Airport'`) is NOT possible today** — the predicate must run in Python after a `select_related`/`prefetch_related` fetch. This is the core data-shape constraint and it's why aggregation belongs offline, not in the request path.

### Data-quality verdict (Q6)
| Item | Verdict | Evidence |
|---|---|---|
| Flight number storage | **CLEAN** — `normalize_flight_number()` digits-only on save (`utils.py:221`) | "AA123"/"aa 123"/"AA-123" → "123" |
| Airline storage | **CLEAN for ORM data** — IATA via `normalize_airline()` (`utils.py:275`), 17 carriers hardcoded | `GROUP BY airline` gives clean buckets |
| Flight # / airline grouping | **CLEAN** for ORM-written rows | risk only on direct-SQL/import bypass |
| Timezone | **CLEAN** — `USE_TZ=True`, `TIME_ZONE="America/New_York"` (`settings.py:327`); flight times aware, pickup naive but correctly combined | `has_flight_time_mismatch` makes both naive before compare |
| Scheduled vs **actual** arrival | **RELIABLE** — actuals persisted, populated by auto-refresh | `actual_gate_arrival_local` |
| Duplicate flights | **POSSIBLE but mostly harmless** — no `(airline,flight_number,date)` unique constraint; each leg owns its own `Flight` row | recommend canonical lookup later, not v1 |
| Completed leg ↔ flight outcome | **JOINABLE** via `flight_information_id` | actuals null only if refresh never ran |

**Cleanup needed for analytics:** essentially none for ORM data. Only defense-in-depth: add `clean_*` to `FlightForm` + a one-time `normalize_flight_numbers` backfill for any legacy/bypass rows (Phase 2). **Do not** add an `Airline` FK model — zero analytics benefit now.

---

## PART 2 — What we can build NOW vs what's missing (Q2, Q3, Q4)

### Q2 Arrival demand — **ALL BUILD-NOW**
Source: `Flight.airline` + `flight_number` + `best_flight_arrival_local(flight)` as the demand clock (so a delayed 8 AM flight counts in its real landing hour, matching `leg_time_of_day_category` `:317`). Answerable today: busiest hours, busiest days-of-week, most-serviced airlines, most-common flight numbers, busiest 30/60-min windows, early-morning (4–7 AM) and late-night (10 PM–4 AM) patterns via `categorize_time_of_day` (`:235`) / `categorize_day_type` (`:268`).

### Q3 Early/late behavior — **BUILD-NOW for landed flights**
We **do** store actual landing time, so `delay = actual_gate_arrival_local − scheduled_gate_arrival_local` (negative = early). Answerable: avg early/late by flight number, by airline, by time-of-day, by day-of-week; predictability via std-dev / `iqr_filter` (`:36`) of signed delay per recurring flight. **Caveat to display:** only legs where an `actual_*` time exists (historical/landed); exclude future flights. The one genuine gap is **no `FlightStatusHistory` audit table** (each refresh overwrites) — so forecast-accuracy ("estimate at T-2h vs actual") is *not* buildable; single-point actual-vs-scheduled is.

### Q4 Dwell time — **partly BUILD-NOW, low-N**
| Dwell metric | Status | Path |
|---|---|---|
| Landing → actual pickup | **BUILD-NOW** (already coded) | `calculate_airport_dwell_time()` (`:471`): `best_flight_arrival_local` → `LegStatus('picked-up').timestamp` |
| Landing → trip start | **BUILD-NOW** (= picked-up) | same |
| Scheduled arrival → pickup | **BUILD-NOW** | `scheduled_gate_arrival_local` → `LegStatus('picked-up')` |
| Landing → driver-contact | **NEEDS-TRACKING (deferred)** | no field; `confirmation_sms_sent_at` is the *next-day* SMS, not en-route contact |
| (fallback) gate → completed | **BUILD-NOW** | `calculate_gate_to_completed_time()` (`:414`) |

By airline / flight / time / day: all computable by grouping the per-leg dwell values. **Key caveat: `LegStatus` history is recent** — sparse on legacy legs (`analytics.py:477` says so; the local scrubbed DB has none), so dwell is **low-confidence until history accrues**. Every dwell figure must carry `sample_count` + a confidence badge (reuse the `get_route_timing_for_scheduler` thresholds `:1269`: ≥20 high / ≥10 medium / ≥5 low).

---

## PART 3 — Scheduling-efficiency features (Q5) — design now, wire later

These are *computed and shown* on the dashboard in v1; **integrating them into the live planner is the deferred Phase 5.** Each reuses an existing scheduler anchor so numbers never disagree with feasibility checks:

| Feature | Computation | Existing hook to reuse |
|---|---|---|
| Recommended driver-ready time | gate arrival − deplaning grace | `feasibility_guards.required_turnaround()` (`:96`) |
| Likely guest-ready time | gate arrival + p75 dwell for that route/time | `RouteTimingMetric.p75_airport_dwell_time`, `get_route_timing_for_scheduler()` (`:1269`) |
| "Often-early flight" warning | avg signed delay ≤ −15 min over ≥N samples | mirrors `flight_timing_flag()` 15/20-min thresholds (`:1751`) |
| "Unpredictable dwell" warning | wide dwell IQR or `sample_count` < floor | `iqr_filter` + `sample_count` |
| "When is the driver realistically free" | gate arrival + p75 dwell + p75 drive to dropoff category | `get_route_timing_for_scheduler(pickup_cat='MCO Terminal', dropoff_cat=…)` |

---

## PART 4 — The Flight Analytics page (Q7)

Mirror the existing `analytics_dashboard` end-to-end pattern: `@login_required` view → single bounded `list(queryset)` fetch with `select_related('flight_information','reservation','driver')` + `prefetch_related('status_history')` → keep MCO/SFB arrivals in Python → single aggregation pass → context → **server-rendered template** (div-bar charts + HTML tables, **no Chart.js** — matches `analytics_dashboard.html`).

- **URL:** `dispatching/urls.py` → `path("flight-analytics/", views.flight_analytics, name="flight_analytics")` (next to `analytics/` at `:37`). *(Confirmed no existing flight page.)*
- **View:** `flight_analytics(request)` in `dispatching/views.py` (clone `analytics_dashboard` `:8239`). GET filters: `days` (7/30/90/365), `airport` (MCO / SFB / both), `airline`, `min_samples`.
- **Template:** `dispatching/templates/dispatching/flight_analytics.html` extends `main.html`; reuse hourly-bar CSS + confidence badges from `analytics_dashboard.html`.

| # | Section | Shows | Why it helps dispatch | Data | Now? |
|---|---|---|---|---|---|
| 1 | Summary cards | total arrivals, avg signed delay, on-time %, avg dwell p50/p75, low-data warning | one-glance health | A+B+C | ✅ |
| 2 | Demand by hour / day | server-rendered bars | staffing peaks | A | ✅ |
| 3 | Airline breakdown | volume, avg delay, on-time %, recommended buffer | which carriers run hot | A+B+ §3 | ✅ |
| 4 | Recurring flight table | top flights by volume; avg delay; delay spread; predictability badge | pre-plan daily repeats | A+B | ✅ |
| 5 | Early/late leaderboard | top-20 most-early / most-late / most-unpredictable | risk spotting | B | ✅ landed-only |
| 6 | Dwell analytics | landing→pickup, sched→pickup by airline/time + sample_count | realistic turnaround | C | ✅ low-N; contact-row greyed |
| 7 | Readiness recommendations | per-flight driver-ready + guest-ready + risk flags | tighter, safer schedules | §3 | ✅ |

**Live vs cached:** **cache it.** The Python arrival predicate forces loading all flight legs each request — too heavy for the single 60-s sync gunicorn worker. Cache the computed context per `(days, airport, airline)` in LocMemCache/Redis (already configured both ways) with a short TTL (10–15 min) + the existing manual-invalidate idiom (`cache.delete(f"capacity_planner_{date}")`). **No Celery** — the 30-min in-process scheduler already runs offline work.

---

## PART 5 — Backend implementation plan (Q8) + report examples (Q9)

### Models / fields / migrations
- **New models needed: none for v1.** Reuse `Flight`, `LegStatus`, `RouteTimingMetric`, and (Phase 1) wire the existing `DemandPattern`.
- **New fields needed: none for v1.** (Deferred: optional `'guest-notified'` `LegStatus` *choice* — a choices change, **no migration**; optional `FlightArrivalDailyMetric` summary table only if the live+cache pass proves too heavy.)
- **Indexes:** none required for v1 (heavy pass runs offline / cached). Useful later if a persisted `pickup_category` lands: index `(pickup_category, pickup_date)` and `Flight(airline, flight_number)`.

### Compute strategy (hard rule from CLAUDE.md: no full-table aggregation in the request path)
Hybrid, no Celery: **offline management command** rebuilds summaries on the existing 30-min scheduler / nightly; **dashboard view reads cached/pre-aggregated rows** or does one bounded single-pass + short cache. Discipline to copy: one shared aggregator function used by both command and view (the `_compute_bucket_metrics` `:692` pattern) so math never drifts.

### Report examples (Q9) — query shape, all Python group-by over the single fetched arrivals list
1. Top-20 earliest flights — group `(airline, flight_number)`, mean signed delay asc. ✅ landed-only
2. Airlines by avg delay / on-time % — group `airline`. ✅
3. Dwell by hour — group arrival-hour, p50/p75 of `calculate_airport_dwell_time`. ✅ low-N
4. Demand by hour / 5. by day-of-week — count. ✅
6. Most-unpredictable flights — IQR/std-dev of signed delay per `(airline, flight_number)`. ✅
7. Recommended buffer by airline / 8. by flight number — deplaning grace + p75 dwell + safety pad. ✅
9. Busiest 30-min arrival windows — bucket `best_flight_arrival_local`. ✅
10. Early-morning vs late-night mix — group `categorize_time_of_day`. ✅
11. Scheduled-arrival → actual-pickup gap by airline. ✅ where LegStatus exists
12. Landing → driver-contact by airline. ❌ NEEDS-TRACKING (deferred field)
13. On-time rate by day-type — % with |delay| ≤ 10 min. ✅

---

## PART 6 — Prioritized phased plan (Q10) — reflecting v1 = read-only dashboard

### Phase 1 — Demand analytics on existing data  ·  Difficulty: Low  ·  zero migration
- **Build:** wire the already-written `update_demand_patterns` (`analytics.py:1170`) to a new `dispatching/management/commands/update_demand_patterns.py` (mirror `reservations/management/commands/normalize_airlines.py` structure). Stand up the `flight_analytics` view + template showing demand by hour / day-of-week / airline / flight number (Sections 1–4, demand parts), filtered to MCO+SFB arrivals.
- **Why:** immediate value from data that's already clean; no schema risk.
- **Files:** `analytics.py:1170` (reuse), `reservations/models.py:3214` `DemandPattern` (reuse), new command, new view in `dispatching/views.py` + URL `dispatching/urls.py:37`, new template extending `main.html`.
- **Risks:** demand counts depend on `get_trip_type()` keyword accuracy (cruise "terminal" false-positives already handled by shared `is_airport_location`).
- **Test:** run command on local dates **2026-05-31 / 06-01**; assert `DemandPattern` rows reconcile vs a raw `Leg.objects.filter(pickup_date=…)` count. No-DB `SimpleTestCase` for hour/day bucketing (follow `dispatching/tests_route_timing.py` — local DB can't create a test DB).

### Phase 2 — Data cleanup / normalization  ·  Difficulty: Low  ·  zero migration
- **Build:** add `clean_flight_number()`/`clean_airline()` to `FlightForm` (`forms.py:304`); add `normalize_flight_numbers.py` backfill (`--dry-run` default, change-only saves); add a malformed-ident monitor (`get_flight_ident()` returns `None`).
- **Why:** guarantees airline grouping + early/late joins are trustworthy; closes the non-ORM bypass gap.
- **Test:** existing `reservations/tests.py` for `Flight.save()` normalization; backfill `--dry-run` then eyeball the changeset.

### Phase 3 — Tracking verification (mostly audit)  ·  Difficulty: Low–Med  ·  zero migration
- **Build:** confirm `actual_*` populate from AeroAPI (`aeroapi_service.py`, `ops/tasks.py:_apply_flight_update :1563`) and that `on-location`/`picked-up`/`completed` `LegStatus` rows write on driver status changes. Document the sparse-historical-`LegStatus` caveat. (Guest-contact field intentionally **deferred** per decision.)
- **Why:** ensures dwell/early-late raw material has coverage; surfaces low-N honestly.
- **Risks:** legacy legs lack history → dwell null for old data (document, don't backfill — unreconstructable).
- **Test:** drive a leg through statuses in dev, assert timestamped rows; verify `best_arrival_local()` chain on a refreshed live flight.

### Phase 4 — Early/late + dwell dashboard sections  ·  Difficulty: Med  ·  zero migration
- **Build:** add Sections 5–7 (early/late leaderboard, dwell analytics, readiness recommendations) to `flight_analytics`. Reuse `best_flight_arrival_local`, `calculate_airport_dwell_time`, `categorize_*`, `iqr_filter`; read `RouteTimingMetric` where possible, live-compute flight-specific slices with the bounded single-pass + short cache.
- **Why:** turns tracked data into operator insight (which flights/airlines/hours run hot; dwell vs the 45-min default).
- **Risks:** worker timeout if aggregation creeps into request path — enforce offline/cache rule; sparse dwell → confidence badges.
- **Test:** no-DB tests mirroring `tests_route_timing.py`; confirm dashboard renders within the 60-s budget on 2026-05-31..06-01.

### Phase 5 — Scheduling recommendations + planner warnings  ·  Difficulty: Med–High  ·  **DEFERRED (post-v1)**
- **Build (later):** feed early/late + dwell percentiles into the planner — "landing ≥20 min early/late" (`flight_timing_flag` `:1751`) and dwell-vs-default warnings in `feasibility_guards`/`scheduler`; suggest pickup adjustments when `has_flight_time_mismatch` exceeds threshold.
- **Why:** closes the loop so analytics drive dispatch, not just reports.
- **Risks:** highest blast radius — touches live auto-assign. Gate behind confidence (≥5 samples); keep warnings **advisory, never auto-move pickups**.
- **Test:** no-DB `tests_feasibility_guards.py` / `tests_gap_compaction.py`; simulate a delayed-flight leg and assert the warning fires without altering booked `pickup_time`.

---

## Critical files (reuse — do not rewrite)
- `dispatching/analytics.py` — `best_flight_arrival_local`:291, `leg_time_of_day_category`:317, `calculate_airport_dwell_time`:471, `calculate_drive_time`:537, `categorize_*`:235/268, `iqr_filter`:36, `_compute_bucket_metrics`:692, `update_demand_patterns`:1170, `update_all_route_timing_metrics`:1031, `get_route_timing_for_scheduler`:1269.
- `reservations/models.py` — `Flight`:2150 + `save()` normalization:2238 + `best_arrival_local`:2225, `Leg.get_trip_type`:1577 / timing helpers:1636–1820, `LegStatus`:2832, `RouteTimingMetric`:2981, `DemandPattern`:3214.
- `reservations/utils.py` — `normalize_flight_number`:221, `extract_airline_from_flight_number`:240, `normalize_airline`:275.
- `dispatching/views.py` — `analytics_dashboard`:8239 (clone target); `dispatching/urls.py`:37; `dispatching/templates/dispatching/analytics_dashboard.html` (layout/CSS to mirror).
- `dispatching/feasibility_guards.py` — `required_turnaround`:96, `is_airport_arrival`:121 (Phase 5).
- `dispatching/aeroapi_service.py` + `ops/tasks.py` — `auto_refresh_flights`:1446, `_apply_flight_update`:1563 (Phase 3 verification).

## Verification (end-to-end)
1. **Phase 1 command:** `python manage.py update_demand_patterns --start 2026-05-31 --end 2026-06-01` → row counts reconcile vs raw `Leg` counts.
2. **Page smoke test:** run the local server (`DJANGO_DEBUG=1 … runserver --noreload`, login `localtest`/`Local2026!`), open `/flight-analytics/?days=30&airport=both`, confirm it renders inside the 60-s budget and demand bars match a hand count for a known date.
3. **No-DB unit tests** for new pure helpers (bucketing, delay sign, buffer math), following the `tests_route_timing.py` style (the scrubbed local DB can't create a test DB and has no `LegStatus` history).
4. **Cache check:** second page load served from cache (no full re-aggregation).
5. **Honesty checks:** early/late excludes flights with no `actual_*`; every dwell cell shows `sample_count` + confidence; the landing→contact row renders greyed as NEEDS-TRACKING.
