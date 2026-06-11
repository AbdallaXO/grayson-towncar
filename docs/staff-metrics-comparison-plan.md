# Schedule-Aware Staff Comparison — Implementation Plan

> Status: planned, not yet implemented. Captures the design agreed with the founder on 2026-06-05.

## Context

The Staff Deep Dive page (`ops/views.py:staff_metrics_view` ~line 2083 → `dispatching/templates/dispatching/staff_metrics.html`)
aggregates each person over a **flat date range** with no awareness that staff work different days
and hours. Comparing a part-timer to a full-timer on raw totals is misleading — a full-timer always
"wins" on volume even if the part-timer is more productive per day worked.

The founder wants a **fair, schedule-aware side-by-side**: pick two (or more) staff, see them compared
**only on the weekdays they both work**, with metrics **normalized per working day and per scheduled
hour**. Example: Joseph and Luis both work Mon/Tue/Fri — show how they each do on exactly those days.

### Decisions confirmed with the founder
- **Working days = auto-detect from real activity + manual weekday toggle** (reality drives it, but he can
  force "just Mon/Tue/Fri"). `StaffWeeklySchedule` is only filled in for Joseph today, and his *set*
  schedule (Mon/Tue/Fri) already disagrees with his *actual* booking pattern (heavy Wednesdays) — so
  detection must come from observed activity, not the planned table.
- **Show both**: a per-shared-weekday table (Mon: A vs B, Tue: A vs B, …) **and** normalized averages.
- **Normalize by both day and hour** (per-hour shown where `StaffWeeklySchedule` hours exist, "—" otherwise).
- **Placement: a dedicated "Compare Staff" page** linked from the existing Deep Dive page.

## Data reality (local vs prod)

Only `Reservation.created_by` survived the local DB scrub; `StaffActivity` / `OperationalTask` /
`CommunicationAttempt` / `EmailLog` / `AuditLog` are empty or test-only locally. The comparison must
degrade gracefully: metrics with no data show 0 / "—", and **bookings + revenue are the only
locally-verifiable signals**. Joseph (user 840, 446 reservations) and Luis (user 639, 1140 reservations)
have rich real reservation history for testing. On prod, all signals populate.

---

## New files

### 1. `ops/staff_compare.py` — computation (keeps the already-huge views.py lean, unit-testable)

Constants (tunable): `WORKDAY_MIN_RATIO = 0.4`, `WORKDAY_MIN_DAYS = 2`.

- `get_dispatcher_uids()` — the office-staff allowlist (`is_staff` minus driver/travel-agent profiles),
  lifted verbatim from `staff_metrics_view` (`ops/views.py` ~lines 2133–2142). Reuse it in both views.
- `gather_daily_metrics(uids, range_start)` → `{uid: {local_date: {reservations, revenue, tasks, comms,
  emails, legs_modified, assigns}}}`. Built from the **same grouped queries** the existing view already
  uses (reservations by day, tasks resolved, comms, emails, legs history, audit `driver_assigned`) —
  scoped to the selected `uids`. Use `TruncDate` for the local (Eastern) date, weekday via Python
  `date.weekday()` (Mon=0..Sun=6, matching `StaffWeeklySchedule.day_of_week`).
- `detect_working_weekdays(active_dates, range_start, today)` → `{weekday: {occurrences, active_count,
  worked}}`. `active_dates` = dates where the staffer had ANY activity. A weekday is **worked** when
  `active_count >= max(WORKDAY_MIN_DAYS, ceil(WORKDAY_MIN_RATIO * occurrences))` — filters one-off
  logins (e.g. Joseph's stray Sundays) while keeping regular days.
- `scheduled_hours(uid)` → `{weekday: hours}` from `StaffWeeklySchedule` (end − start; `is_working` only).
- `compute_comparison(uids, range_start, today, forced_weekdays=None)` → per-staff:
  `detected_weekdays`, `shared_weekdays` (intersection across all selected, or `forced_weekdays` when the
  toggle is used), and **restricted to shared weekdays**: totals per metric, `days_worked` (distinct
  active dates), `scheduled_hours_total` (Σ over shared worked weekdays of `hours[wd] × active_occurrences`),
  `per_day` averages (`total / days_worked`), `per_hour` averages (`total / scheduled_hours_total`, or
  `None`), and a `by_weekday` breakdown (`{weekday: {uid: {bookings_avg, revenue_avg, …}}}`, averaged
  per occurrence of that weekday).

### 2. `dispatching/templates/dispatching/staff_compare.html`

Extends `main.html`, includes `dispatching/dispatcher_navbar.html` (same shell as staff_metrics.html).
- **Picker**: GET form — checkbox list of dispatcher staff (2–4) + range buttons (14/30/60/90, default 30).
- **Weekday toggles**: Mon–Sun chips, shared/detected days pre-checked; submitting sets `?days=` to force
  the comparison set. Each person's detected working days rendered as small chips so the overlap is visible.
- **Fairness summary**: one column per staffer — per-working-day averages (bookings, revenue, tasks,
  comms, emails) + per-scheduled-hour (or "—"), with `days worked` and `scheduled hrs` shown as the denominators.
- **Per-weekday table**: rows = shared weekdays; a column group per staffer; cells = that day's
  avg-per-occurrence for the headline metrics (bookings + revenue). Metric selector defaults to bookings.
- Info note: website self-serve bookings aren't attributed to anyone (mirror existing page's caveat);
  empty metrics mean no recorded activity for that signal.

### 3. `ops/tests/test_staff_compare.py`

Follows `ops/tests/test_scheduling.py` patterns. Unit tests for `detect_working_weekdays` (threshold
boundaries) and `compute_comparison` (shared-day intersection, per-day/per-hour math, `forced_weekdays`
override). Integration test via Django test `Client`: build a couple of staff users with reservations on
known weekdays → GET the compare URL → assert 200, correct shared days, and normalized values.

## Modified files

- **`ops/views.py`** — add `staff_compare_view(request)` (decorators mirror `staff_metrics_view`:
  `@login_required(login_url="login")` + `@user_passes_test(_is_superuser, login_url="dashboard")`).
  Parses `users` (csv, validated ⊆ dispatcher allowlist, 2–4), `range` (default 30), `days` (csv ISO
  weekday override); calls `compute_comparison`; renders `staff_compare.html`. With <2 users selected,
  renders the picker only. Optionally swap the existing view's inline allowlist for the new
  `get_dispatcher_uids()` helper (light reuse, no behavior change).
- **`dispatching/urls.py`** — add `path("staff-metrics/compare/", ops_views.staff_compare_view,
  name="staff_compare")` **before** the `staff-metrics/<int:user_id>/` route (~line 432) so "compare"
  isn't swallowed by the int converter.
- **`dispatching/templates/dispatching/staff_metrics.html`** — add a "Compare staff" button in the header
  (~line 47) linking to the new page, plus a lightweight checkbox per roster card to pre-select people
  and "Compare selected".

## Verification

1. **Unit/integration tests**: `.venv/Scripts/python.exe manage.py test ops.tests.test_staff_compare`
   (+ full `manage.py test ops` for no regressions) and `manage.py check`.
2. **Manual** (real local data): run
   `DJANGO_DEBUG=1 .venv/Scripts/python.exe manage.py runserver 127.0.0.1:8000 --noreload`, log in as
   `localtest` / `Local2026!`, open `/dispatching/staff-metrics/compare/?users=840,639&range=90`. Expect:
   - Detected days — Joseph: Mon/Tue/Wed/Fri; Luis: Mon–Fri; shared pre-checked = Mon/Tue/Wed/Fri.
   - Unchecking Wed (→ `?days=0,1,4`) re-renders the comparison on just Mon/Tue/Fri.
   - Per-working-day bookings/revenue side-by-side; Joseph shows per-hour values (schedule set), Luis "—".
3. Confirm the "Compare staff" entry point works from the Staff Deep Dive page.

## Out of scope

No new migrations (reuses existing models). No changes to how activity is recorded. Per-hour stays blank
for staff without a `StaffWeeklySchedule` until the founder fills in their hours (intentional).
