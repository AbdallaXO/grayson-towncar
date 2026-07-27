# Staffing — Phase 2: the Dispatcher-Facing View

_Status: **shipped** (my-week + who-with **and** the today timeline). Phase 1 (admin board) is shipped. The only remaining thread is the separate on-call self-log button (see bottom)._

## What shipped

Live at `/dispatching/my-schedule/` (route name `my_coverage`), gated `@login_required` + `_is_staff` — **any dispatcher, not superuser-only**. This is the confirmed teammate-visibility change: dispatchers now see each other's *scheduled hours*, but only through this calm, read-only lens (their own week + who overlaps them), never the admin coverage board.

- **Backend** — `ops/coverage.py::my_week(user, roster, today_dow)` (my cells + per-day overlapping coworkers with in/out, `arrives_after_me`/`leaves_before_me`, and plain `handoff_from`/`handoff_to`) and `my_today_timeline(user, roster, today_dow)` (my bar + coworkers' bars on one axis, `is_me` flagged). Both read the recurring weekday pattern from `StaffWeeklySchedule` via a shared `_pattern_by_dow` helper — **no risk/gap/thin/target fields are produced**. View: `ops/views.py::my_coverage` (also does a date-scoped `StaffOnCall` lookup for "on-call tonight", caller only).
- **Template** — `dispatching/templates/dispatching/my_coverage.html`, its own calm `mc-*` design system (warm neutral ground, gold accent, mono times, ☀/🌙 opener/closer). No red, no dims, no "understaffed/critical/gap" language anywhere.
- **Nav** — "My Schedule" link in `dispatcher_navbar.html`, non-superuser branch, next to Clock. Superusers keep the admin "Staffing" link.
- **Tests** — `ops/tests/test_coverage.py`: `MyWeekTests` + `MyCoverageViewTests` (overlap, handoffs, solo, opener/closer, off/no-schedule empty states, the non-superuser access change, on-call banner, and an assertion that no alarming vocabulary renders).

> **Local DB note:** the `my_coverage` view queries `StaffOnCall`; a dev DB that hasn't run `ops.0013_staffoncall` (and the other pending migrations) will 500. Run `migrate` locally before hitting the page.

## Original plan (kept for reference)

_Status: planned. Phase 1 (admin board) is shipped. Pick this up later._

## Where Phase 1 left off (what's already built)

The **admin** side is done and live at `/dispatching/staffing/` (superuser only):

- **Weekly Staffing Pattern board** — weekday-based (Mon–Sun, *not* specific dates), read straight from `StaffWeeklySchedule`. Two views behind a toggle:
  - **Table** — dispatchers × weekdays, hours per cell, ☀ opener / 🌙 closer marks, a per-weekday coverage cue.
  - **Timeline** — each weekday a 24h strip with lane-packed named bars, on-call band, opener/closer, and a red **dim** over any uncovered window.
- **Calm by design** — neutral palette, color only where a day actually runs thin (amber) or has an uncovered hole (red).
- **On-call** — marked per night on the Time Clock manage page (`StaffOnCall`), shown as "on-call o/n" on the board (overnight is the on-call window, never flagged as a gap).
- Backend: `ops/coverage.py::weekly_pattern(roster, today_dow)`; view `ops/views.py::staffing_board`; template `dispatching/templates/dispatching/staffing_board.html`. Roster from `ops/staff.py::office_staff_qs()`.

Key models (no changes needed for Phase 2): `StaffWeeklySchedule`, `StaffScheduleOverride`, `StaffOnCall`, `TimeClockShift`.

## The goal of Phase 2

A **dispatcher-facing** view so each staff member can see their own week and their teammates — the flip side of the admin board.

> **The #1 rule: this view is reassuring, never alarming.** A dispatcher working a quiet solo overnight should feel oriented, not scared. So the admin board's coverage-risk language does **not** carry over. **No red gaps, no "understaffed," no "you are alone" alarms, no risk colors.** Just "here's your week and who you're with."

### What it shows (framed positively)

- **My week** — the days and hours *I* work (from my `StaffWeeklySchedule`), with today highlighted.
- **Who I'm on with** — for each of my days, the coworkers whose shifts overlap mine, and when they arrive / leave.
- **Handoffs** — who I take over from (previous person out) and who I hand off to (next person in), so a shift change is obvious.
- **Who's on now** — a simple, calm "currently on" list (optional; reads open shifts / the schedule for the current moment).
- **On-call** — if I'm the on-call person tonight, show it plainly ("You're on-call tonight, 12–6 AM"). Informational, not a warning.

### What it deliberately does NOT show

- No coverage gaps, "thin," "critical," red dims, or headcount-vs-target.
- No "you'll be working alone from X to Y" alarms. (If solo time is worth surfacing at all, phrase it neutrally, e.g. "Sarah leaves at 6 PM" — let them infer, don't alarm.)
- No editing. Read-only. Schedule changes stay with admins.

## Design

Reuse the **calm weekday aesthetic** already built (the `sp-*` design system in `staffing_board.html`): warm neutral ground, mono times, gold accent, ☀/🌙 for open/close. Two easy shapes to consider:

1. **"My week" strip** — my 7 days with hours, today highlighted; under each day a small "with: Luis (7:30a–4p), Iris (from 6:30p)" line.
2. **"Today" focus** — a single clean timeline for today: my bar plus my coworkers' bars on the same axis, so overlaps/handoffs are visual. Calm bars, no dim/red.

Recommend starting with #1 (my week + who-with per day); add #2 (today timeline) if wanted. Both read from the same data as `weekly_pattern` — no risk computation, just the roster's overlapping shifts relative to the viewer.

## Backend

- New view `ops/views.py::my_coverage`, gated `@login_required` + `@user_passes_test(_is_staff)` (any dispatcher, **not** superuser-only). `_is_staff` = `is_staff OR is_superuser`.
- Reuse `office_staff_qs()` for the team and the existing weekday resolution. A small helper (e.g. `coverage.my_week(request.user, roster)`) can return: my cells + per-day the overlapping coworkers (name, in, out, is_before_me/after_me). **Do not** compute or return gap/thin/risk fields for this view.
- Scope to the caller: the view reads `request.user`'s own row; team info is just "who overlaps me," no per-person drill-down.
- No new models. No migration.

## Navigation & permissions

- Add a dispatcher nav link in `dispatcher_navbar.html` — the **`{% else %}` (non-superuser) branch**, next to the personal "Clock" link (e.g. "My Schedule" / "Who's On"). Superusers keep the admin "Staffing" link; dispatchers get this one.
- Route: `dispatching/urls.py` → `ops_views.my_coverage`.

## Product decision to confirm

- **Teammate visibility:** this shows dispatchers each other's hours. Today they can't see anyone else's schedule. That's the point of the feature, but confirm it's wanted before shipping (it's a real access change).

## Edge cases

- A dispatcher with no weekly schedule set → "No schedule set yet — ask your manager." (calm, not an error).
- Overnight shifts crossing midnight → show the coworker relationship correctly (someone coming on at 6:30 PM overlaps my day-shift tail).
- Someone marked on-call but also working a day shift → show both plainly.
- Keep it read-only; never surface admin-only coverage judgments here.

## Related open thread (separate, smaller)

- **"On-call" self-log button** — on-call staff are paid but *not hourly*, so they want to press **"On-call"** (instead of Clock In) to log that they took the night. This is the *actual*-side mirror of the on-call schedule — a lightweight log (a new small model, or a flagged `TimeClockShift` excluded from hourly totals), surfaced on the personal Time Clock page. Not built yet; decide the pay/logging shape with the founder first. Can ship independently of the dispatcher view.

## Rough build order

1. `coverage.my_week(user, roster)` helper (my cells + overlapping coworkers per day; no risk fields) + tests.
2. `my_coverage` view (`_is_staff`) + `my_coverage.html` (reuse `sp-*` styles; "my week + who-with").
3. Dispatcher nav link + route.
4. (Optional) a calm "today timeline" of my + coworkers' bars.
5. (Separate) the on-call self-log button.

---
_See also: memory `dispatch-staffing-coverage.md`. Admin board = `/dispatching/staffing/`; on-call marking = Time Clock manage page._
