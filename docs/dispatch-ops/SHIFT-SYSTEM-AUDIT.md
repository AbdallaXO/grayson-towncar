# Dispatch Shift System Audit

**Date:** 2026-09-12 · **Branch:** `main` · **Scope:** read-only architecture audit, no code changed.

This report audits the existing Grayson Towncar Django codebase against a proposed Dispatch Shift
Operating System (Open/Close checklists, exceptions with owners, a Floor Card, lanes with a board
owner, a board handoff, and a Dispatch Lead view). It records what exists, what is reusable, what is
missing, and which business rules are still undefined.

**Evidence standard.** Every claim below names a file and a symbol. Where something was searched for
and not found, it is marked **NOT FOUND IN CODEBASE**. Where behaviour could not be determined from
the code alone, it is marked **UNCERTAIN — requires runtime/business confirmation**. Where more than
one implementation of the same idea exists, all are named and the authoritative one is identified.

A plain-language companion to this report (for non-engineers) is published separately as the
*Dispatch Shift System Briefing*.

---

## 1. Executive Summary

### How much can be built from existing architecture

**The four proposed system-verified checks are all computable today.** Unassigned legs, unconfirmed
chauffeurs, unreviewed flight alerts and open conflict/tight-turn tasks each have a persisted,
indexed source. **Which of them belongs on which shift is a separate question** — see the scoping
note below: the task-count row is measurably too noisy for an opening gate and has been moved to
Close only. No new fact needs to be recorded to display any of the four counts — only a new
screen that queries them for a chosen date and a shared definition of each count (today the
definitions are duplicated with small divergences across ~12 call sites, see §3.2).

**The floor-presence half of the Floor Card is solid.** `ops.TimeClockShift` gives a reliable,
DB-constrained answer to "who is clocked in right now" and "who is on break", already queried by two
superuser pages.

**Roughly a third of the proposal has no existing counterpart at all:** the checklists themselves,
exception/handoff notes with an owner, lanes and Lane A, a Dispatch Lead permission tier, the Lead's
7-day view, and any way for the app to reach the team's WhatsApp group. All of it is new build; none
of it has a partial implementation to extend.

### Biggest reusable components

| Component | Where | Used for |
|---|---|---|
| `TimeClockShift` / `TimeClockBreak` + `ops/services.py` state machine | `ops/models.py:413,652`, `ops/services.py:206-452` | Who is on the floor, who is on break, Lane A succession input |
| `OperationalTask` + the 30-min scanners | `ops/models.py:17`, `ops/tasks.py:579` | Conflict/tight-turn counts, queue depth, oldest-item age, task ownership |
| Board leg queryset + `driver IS NULL` | `dispatching/views.py:150,991`, `dispatching/utils.py:10` | Unassigned counts today/tomorrow |
| `Leg.status` ladder + `LegStatus` history | `reservations/constants.py:21`, `reservations/models.py:3580` | Chauffeur-confirmation counts, and telling a chauffeur's press from a dispatcher's |
| Flight task + ack fields | `ops/tasks.py:620`, `dispatching/views.py:7233,7283` | The only flight states with a real "reviewed" flag |
| `office_staff_qs()` + `resolve_staff_schedule()` | `ops/staff.py:21`, `ops/scheduling.py:108` | Roster, planned hours, opener/closer duty, office-vs-WFH |
| `SchedulerSettings` singleton + Tuning JSON API | `dispatching/models.py:10`, `dispatching/views.py:15969` | The established operator-editable configuration pattern |
| `staff_kpis_view` / `staff_metrics_view` range pattern + `ops/kpis.py:79` | `ops/views.py:2944,2226` | The shape of the Lead's 7-day supervisory view |
| `LegKeoi` watch flags | `reservations/models.py:3639` | "Watch items" in the board handoff, without a parallel model |
| `StaffActivity` action ledger | `ops/models.py:317` | Lightweight shift events without a second audit table |

### Biggest missing components

1. **Checklist state.** No model, view, template or test for an office-staff checklist exists.
   `SOPS/dispatcher-operations-handbook.md:83,95,105` describes shift-start, during-shift and
   shift-handoff procedures in prose; **nothing in code enforces or records any of it.**
2. **Exception / handoff notes with an owner.** NOT FOUND IN CODEBASE. The nearest structures are
   per-object (`Leg.private_notes`, `LegKeoi.description`, `OperationalTask.resolution_notes`) — none
   is per-shift or per-day.
3. **Lanes and Lane A.** NOT FOUND IN CODEBASE. "Lane" in this codebase means a UI row
   (`ops/coverage.py:496 _lane_pack`) or a task-queue tab (`ops/views.py:152`).
4. **A Lead role.** The app has exactly two tiers: `is_staff` and `is_superuser`
   (`ops/views.py:70,74`). No Group usage, no `is_dispatcher`, no supervisor concept.
5. **Any visibility into RingCentral, Gmail inbox, or the GHL conversation inbox.** See §7.
6. **Any outbound message channel for office staff.** The team's channel is **WhatsApp**, and there
   is no WhatsApp integration of any kind. NOT FOUND IN CODEBASE (no Google Chat, Slack or Discord
   either). ntfy was removed 2026-07-18 and its callers now invoke no-op stubs
   (`reservations/utils.py:643,658,887`). See §7.7 — the recommended Phase 1 answer is a
   copy-to-clipboard summary a dispatcher pastes into WhatsApp, not a bot.

### Biggest architectural risk

**Creating a second source of truth.** Three specific failure modes, in order of likelihood:

1. **Storing "who is working" on a lane/shift row** instead of deriving it from `TimeClockShift`.
   The moment a lane row says Bryan is on the floor and the clock says he left, the Floor Card is
   lying, and it is the one surface whose whole value is being believed.
2. **Snapshotting the four system counts as the truth** rather than computing them at read time.
   The counts must be live; a snapshot is legitimate only as a *historical record* attached to a
   completed checklist.
3. **Copying conflicts/tasks into checklist rows.** `OperationalTask` already has ownership,
   resolution and auto-close. An exception note must *reference* a task, never restate it.

A fourth, lower-probability risk: **crossing the scheduling boundary.** The scheduling redesign is
propose-only by construction, with a single write door (`dispatching/assignment.py:126
set_leg_driver`) and a `pre_save` tripwire that raises `SandboxLeakError` on any other live
`Leg.driver` write (`dispatching/assignment.py:171`). The Shift System must be a pure reader of legs.
See §13.

### Recommended smallest first release

**Open/Close checklists + exceptions + the Lead's 7-day view + a pasteable WhatsApp summary.** Four
new tables, one settings row, three new pages, one permission group. No board changes, no lanes, no
Floor Card, no external integration. The Lead view is included because it is a read query over tables
this phase already writes — deferring it saves nothing and costs the Lead visibility during exactly
the period when adoption is being established. Detail in §17.

### Scoping decision: conflict tasks are a Close check, not an Open check

**Founder direction, 2026-09-12:** the open conflict / tight-turn task count is **removed from Open
Shift** and kept only on Close (looking at tomorrow). The stated reason — the task queue is large and
is worked late in the shift, not at 7:15 AM — is supported by the project's own measurement.

`docs/scheduling-redesign/06_DAY_MANAGER.md` §0.2 (run `analysis/25_scanner_outcomes.py`, 2026-09-05)
measured `driver_conflict` + `tight_turn` filing per active day:

| Month | Tasks/day | % of the day's legs flagged | % of closes that bought nothing |
|---|---:|---:|---:|
| 2026-05 | 13.1 | 10.5% | 70.1% |
| 2026-06 | 37.0 | 23.7% | 73.1% |
| 2026-07 | 37.3 | 25.7% | 69.4% |
| 2026-08 (1–21) | **70.9** | **33.9%** | **65.9%** |

A third of every day's trips now raise an alarm, and two thirds of those alarms close without anyone
moving anything. A gate row reading "47 open conflict tasks" at 7:15 AM would be a wall the floor
learns to wave through — the exact failure the conflict flags themselves had before the 2026-08-27
self-clearing fix.

**Honest caveat:** that snapshot predates five tuning commits aimed squarely at this noise
(`2c36aada`, `c04489f8`, `083a7d0a`, `2419c414`, `076dfe8e`, 2026-08-09 → 08-27), so today's real
number may be materially lower. It has not been re-measured. Re-running `analysis/25` on a current
snapshot would settle it, and is worth doing before any decision to put the row back.

**What replaces it at Open:** nothing. Conflicts still show as red flags on the board the opener is
already working, and still generate tasks. The checklist simply stops counting them at the gate. A
narrower alternative — only critical conflicts on trips leaving this morning — is recorded as policy
decision 24 and is deliberately not built in Phase 1.

---

## 2. Current Architecture Map

### 2.1 Django apps

`business/settings.py:63-104`. Admin skin is **jazzmin** (`"jazzmin"` first in `NATIVE_APPS`).
`django_celery_beat` and `simple_history` are installed; `django-unfold` and `django-environ` are in
`requirements.txt` but **not** in `INSTALLED_APPS` / never imported.

| App | Role in this audit |
|---|---|
| `ops` | Office-staff layer: tasks, time clock, staff schedules, coverage, staff metrics. **The natural home for the Shift System.** |
| `dispatching` | Board, planner, scheduling engines, advisors, flights, KEOI, and **all URL routing + templates for `ops`**. |
| `reservations` | `Reservation`, `Leg`, `LegStatus`, `LegKeoi`, `AuditLog`, sandbox drafts, snapshots. |
| `drivers` | Chauffeurs, chauffeur schedules, driver portal, fleet, push, SMS, wake-up ladder. |
| `ghl_integration` | GoHighLevel + **the 30-minute background scheduler thread that runs the ops scanners**. |
| `users`, `payment`, `rates`, `services`, `blog` | Profiles/agents, Stripe, pricing, marketing. |

**Routing convention (important for any new page):** `ops` has **no `urls.py`**. Every ops view is
mounted in `dispatching/urls.py:539-573`; ops templates live in
`dispatching/templates/dispatching/`; nav links are added to
`dispatching/templates/dispatching/dispatcher_navbar.html` (an `{% include %}`, not block
inheritance). A new Shift System page follows that same path.

### 2.2 Models relevant to the Shift System

**`ops/models.py`**

| Model | Line | Notes |
|---|---|---|
| `OperationalTask` | 17 | 9 `TaskType`s, 6 `Status`es, 4 priorities, `assigned_to`, `resolved_by`, `metadata` JSON, 6 indexes |
| `CommunicationAttempt` | 263 | Contact log; **`task` FK is required** — no task-free contact log exists |
| `StaffActivity` | 317 | Behavioural ledger: page views + task actions; extensible `ActionType` |
| `EmailLog` | 369 | Every outbound email, `sent_by` |
| `TimeClockShift` | 413 | Open shift = `clock_out_at IS NULL`; `uniq_open_shift_per_user` constraint |
| `TimeClockRequest` | 571 | Off-schedule clock-in approval grant (valid 120 min) |
| `TimeClockBreak` | 652 | Open break = `break_end_at IS NULL`; `uniq_open_break_per_shift` |
| `STAFF_ROLE_CHOICES` | 703 | `opener` / `mid` / `closer` / `both` |
| `WORK_LOCATION_CHOICES` | 717 | `office` / `remote`, blank = untracked |
| `StaffWeeklySchedule` | 726 | Recurring hours per weekday, `role`, `location`; unique `(user, day_of_week)` |
| `StaffScheduleOverride` | 784 | One-off/range exception + time-off approval workflow |
| `StaffOnCall` | 926 | Planned overnight cover, default 00:00–06:00, unique `(user, date)` |
| `StaffExtraShift` | 973 | Split-shift second window (recurring or one-off) |

**`reservations/models.py`**

| Model | Line | Notes |
|---|---|---|
| `Reservation` | 65 | `status` defaults to `"confirmed"` (`reservations/constants.py:14`) — a *booking* status |
| `Leg` | 925 | `driver` FK:1062, `status`:1069, `operator_accepted_at`:1092, `driver_assigned_by/at`:1246/1254, `status_changed_by/at`:1262/1270, `confirmation_sms_sent_at`:1275, `pickup_time_changed_at`:1325, `pickup_change_ack_at`:1345 |
| `AuditLog` | 3475 | `model_name`/`object_id`/`action`/`field_name`/`old`/`new`/`user`/`ip` |
| `LegStatus` | 3580 | Per-tap status history with `updated_by` |
| `LegKeoi` | 3639 | "Keep Eye On It" watch flag, one active per leg, with `created_by`/`updated_by`/`closed_by` |
| `ScheduleSnapshot` / `ScheduleDraft` / `DraftAssignment` / `ScheduleDraftEvent` | 3767 / 3816 / 3915 / 3948 | Sandbox scheduling (hold → build → review → publish → notify) |
| `LegClientMessage` | 4356 | Chauffeur→guest text handoff record |

**`dispatching/models.py`**

| Model | Line | Notes |
|---|---|---|
| `SchedulerSettings` | 10 | ~85-field singleton (pk=1) with `get_settings()`:270 module-cached; the operator-tunable pattern |
| `FlightRefreshTask` | 320 | Bulk-refresh progress; a worked example of *why* DB beats LocMemCache under 3 workers |
| `ChauffeurExceptionDismissal` | 350 | "Handled" marker on the Chauffeur KPI exceptions list — not flight-related |
| `DayPlan` | 393 | Day-Builder job ledger; *"never writes a Leg or a DriverVehicleAssignment row"* |
| `AdvisorEvent` | 426 | Recovery Advisor ledger (what was shown, applied, and what happened) |
| `DispatchEtaSample` | 604 | GPS/ETA measurement ledger |

**`drivers/models.py`**: `Driver`:11 (`profile` OneToOne→`auth.User`, `driver_type` inhouse/affiliate,
`portal_role` driver/operator, `is_active`), `DriverWeeklySchedule`:469, `DriverDateOverride`:547,
`DriverVehicleAssignment`:1076, `DriverPushSubscription`:1099, `DriverWakeupCheck`:1123.

**`users/models.py`**: `UserProfile`:11 — `phone_number`, `is_driver`, `is_travel_agent`. That is the
entire role vocabulary outside `auth.User.is_staff` / `is_superuser`.

### 2.3 Services and engines

| File | What it is |
|---|---|
| `ops/services.py` | `create_task`:22 (dedup + 2 h cooldown), `close_task`:102, `log_communication`:160, and the **only** time-clock mutators (`get_open_shift`:206 … `admin_delete_break`:682) |
| `ops/scheduling.py` | `resolve_staff_schedule`:108, `extra_shifts_on`:208, `clock_in_schedule_check`:230, `schedule_vs_actual`:314 |
| `ops/coverage.py` | Staffing-board rendering: `weekly_pattern`:718, `dated_range`:845, `my_week`:1099, `day_view_actual`:1226, `_resolve_duty`:544 |
| `ops/staff.py` | `office_staff_qs()`:21 — the canonical office roster |
| `ops/tasks.py` | The scanners; `generate_ops_tasks`:579 |
| `ops/playbooks.py` | Code-config resolution ladders per task type (`PLAYBOOKS`:95) |
| `ops/kpis.py` | `resolve_range(start, end, days)`:79 — reusable date-range normaliser |
| `dispatching/assignment.py` | `set_leg_driver`:126 — the single write door for who drives; tripwire:171 |
| `dispatching/utils.py` | `get_filtered_legs_queryset`:10, `detect_leg_flags`:698 |
| `dispatching/pickup_policy.py` | The shared time constants (`ARRIVAL_MEET_GRACE_MIN=10`:46, `PAX_READY_MIN=15`:52, `OVERDUE_STALE_MIN=45`:81, `TURN_TIGHT_SLACK_MIN=15`:87) and `pickup_deadline`:174, `turn_band`:271 |
| `dispatching/board_validation.py` | `turn_slack_minutes`:52 — the board's gap-chip arithmetic |
| `dispatching/conflict_advisor.py` | Recovery Advisor engine, 2,943 lines, strictly read-only |
| `dispatching/day_setup.py` / `day_planner.py` | Roster+vehicle suggester and Day-Builder, both propose-only |
| `dispatching/handoff_chain.py` | **Chauffeur-to-chauffeur vehicle handoff** tables (`handoff_band`:184) — not a work handoff |

### 2.4 Views, templates and nav

| Surface | View | Template | URL name |
|---|---|---|---|
| Legs dashboard (primary board) | `dispatching/views.py:115 index` | `legs_filter.html` (5,715 lines) | `dashboard` |
| Schedule board (drag-drop timeline) | `dispatching/views.py:947 schedule_board` | `schedule_board.html` | `schedule_board` |
| Capacity planner / Day Setup | `dispatching/views.py:12183 capacity_planner` | `daily_capacity_planner.html` | `capacity_planner` |
| Task queue | `ops/views.py:80 task_queue_view` | `task_queue.html` | `task_queue` |
| Task detail | `ops/views.py:2111 task_detail_view` | `task_detail.html`, `conflict_task_detail.html`, `payment_task_detail.html` | `task_detail` |
| Personal time clock | `ops/views.py:4520 timeclock_view` | `timeclock.html` | `timeclock` |
| Manage time clock (superuser) | `ops/views.py:4807 timeclock_manage` | `timeclock_manage.html` | `timeclock_manage` |
| Time-clock overview (superuser) | `ops/views.py:4634 timeclock_overview` | `timeclock_overview.html` | `timeclock_overview` |
| Staffing board (superuser) | `ops/views.py:5305 staffing_board` | `staffing_board.html` | `staffing_board` |
| My Schedule (dispatcher) | `ops/views.py:5654 my_coverage` | `my_coverage.html` | `my_coverage` |
| Staff metrics / KPIs (superuser) | `ops/views.py:2226 / 2944` | `staff_metrics.html`, `staff_kpis.html` | `staff_metrics`, `staff_kpis` |
| Admin tasks hub (superuser) | `ops/views.py:4088 admin_tasks_view` | `admin_tasks.html` | `admin_tasks` |
| Confirmations (guest SMS) | `dispatching/views.py:6511 confirmations_view` | `confirmations.html` | `confirmations` |

Navbar badges come from context processors registered at `business/settings.py:154-158`:
`ops/context_processors.py:15 pending_task_count` (60 s cache), `:30 critical_disruption_count`
(cache-read-only), `:44 timeclock_status` (uncached, per-user), `drivers/context_processors.py:9
pending_timeoff_count` (60 s cache).

### 2.5 Background jobs

**No Celery.** `django_celery_beat` is installed but there is no `celery.py`, no `CELERY_*` setting,
no `@shared_task`, no `.delay()` (confirmed by grep; `dispatching/fleet_sync.py:9-11` says so
explicitly). Three daemon threads, each with a Postgres advisory lock so only one gunicorn worker
executes per cycle:

| Thread | File | Interval | Lock | Runs |
|---|---|---|---|---|
| GHL / flights / ops tasks | `ghl_integration/scheduler.py:51` | 30 min | `737_201` | GHL batches, `ops.tasks.auto_refresh_flights`, `overnight_confirm_sweep`, `advisor_events.fill_outcomes`, **`ops.tasks.generate_ops_tasks`** (`:207`) |
| Samsara GPS/ETA sweep | `dispatching/samsara_scheduler.py` | 180 s | `737_202` | Vehicle positions, `sweep_eta`, advisor sweep |
| Driver wake-up | `drivers/wakeup_scheduler.py` | 60 s | `737_203` | Early-morning ladder (gated off by `WAKEUP_CHECKS_ENABLED`) |

Started from each app's `ready()` when `RUN_SCHEDULERS_IN_WEB != "0"`
(`ghl_integration/apps.py:49`, `dispatching/apps.py:50`, `drivers/apps.py:44`). An alternative
dedicated-process host exists: `dispatching/management/commands/run_schedulers.py`.

`railway.json` declares **only a web service** (`gunicorn --workers 3 --threads 4`,
`--max-requests 1500`) with no cron block and no worker service, and `RUN_SCHEDULERS_IN_WEB` is
unset (default `"1"`) — so in production **the schedulers run inside the web workers**.
UNCERTAIN — requires runtime/business confirmation: whether a separate Railway worker service has
since been added outside this repo.

Consequence worth noting for any new periodic work: `--max-requests 1500` recycles workers, which
resets the per-process `_cycle_count` (`ghl_integration/scheduler.py:24`). Anything that must run
"nightly" has to be idempotent by data, not by cycle count — the pattern
`dispatching/advisor_events.fill_outcomes` already follows (`scheduler.py:180-196`).

### 2.6 Caching and request budgets

`business/settings.py:199-214`: Redis when `REDIS_URL` is set, otherwise **per-process LocMemCache**.
`reservations/middleware.py:79` caps DB statement time at 30 s; `:33` logs any request over 500 ms to
the `perf` logger. Relevant cache keys: `capacity_planner_{date}` (60 s, invalidated at 18 sites),
`ra_cards_v{n}_{date}_{fp}` (120 s), `ra_crit_count` (300 s), `ops_pending_task_count` (60 s).

---

## 3. Current Dispatch Workflow as the Code Implements It

This section describes only what the application does today.

### 3.1 A day on the board

A dispatcher opens `/dispatching/` (`views.py:115 index`), which renders every leg for
`?date=` (default `timezone.localdate()`) into `legs_filter.html`. The same date drives
`schedule_board` (drag-drop timeline, in-house and affiliate views sharing one Unassigned lane) and
`capacity_planner`. Pages are **fully server-rendered and do not auto-refresh**: the only pollers on
a board page are the Recovery Advisor rail (60 s, superuser-only) and the bulk-flight-refresh status
poll. Every mutation is a `fetch()` followed by `window.location.reload()`.

Assignment happens by drag-drop (`content/static/js/timeline-dnd.js`) or the row dropdown, both
POSTing to `dispatching/views.py:2877 update_leg_assignment`, which routes through
`dispatching/assignment.py:126 set_leg_driver` and returns advisory warnings from
`dispatching/assign_warnings.py:243 compute_manual_assign_warnings`. Warnings never block.

### 3.2 What "unassigned" means in code

The board's leg set, identically in all three date pages
(`views.py:150-152`, `:991`, `:12224`):

```python
Leg.objects.filter(pickup_date=selected_date)
   .exclude(reservation__status='cancelled')
   .exclude(status='cancelled')
```

"Unassigned" is then a **Python** test on `driver` being falsy — `views.py:212` (dashboard filter),
`:1535` (the Unassigned lane), `:443-448` (the coverage counters), `:12420` (planner). The ORM-level
version lives in `dispatching/utils.py:654` (`driver__isnull=True`) and in the scanner:

```python
# ops/tasks.py:1180 _scan_unassigned_legs
Leg.objects.filter(pickup_date=today, driver__isnull=True, pickup_time__gte=local_now_time)
   .exclude(status__in=["completed","cancelled"])
   .exclude(reservation__status="cancelled")
```

**Competing definitions, all live.** They differ in three ways:

| Variant | Excludes completed? | Drops past pickup times? | Excludes cancelled reservations? |
|---|---|---|---|
| Board / dashboard (`views.py:150`) | No | No | Yes |
| `get_filtered_legs_queryset` (`utils.py:10`) | No | No | Yes |
| `_scan_unassigned_legs` (`ops/tasks.py:1180`) | Yes | Yes | Yes |
| `dispatch_alerts` command (`dispatch_alerts.py:99`) | Only `completed` | No | **No** |

There are **no custom managers or querysets** on `Leg`, `Reservation` or `OperationalTask`
(NOT FOUND IN CODEBASE) — every call site re-writes the filter. The authoritative one for
dispatcher-facing counts is the board's, because it is what a dispatcher sees when they click
through. See §9 for the recommendation to introduce one shared read-only helper.

A leg held by an affiliate is **assigned** (`Driver.driver_type == "affiliate"`), rendered on the
affiliate board and counted as `driver_coverage["affiliate"]` (`views.py:443`).

On a day held by a sandbox draft, `_apply_draft_overlay` re-points each leg's in-memory driver to the
proposed driver before any of this runs (`views.py:206`, `:1108`, `:12266`), so "unassigned" on a
held day means *the draft says unassigned*.

### 3.3 What "confirmed" means in code

`reservations/constants.py:21 DRIVER_STATUS`:
`in-progress` (default) → `confirmed` → `on-the-way` → `on-location` → `picked-up` → `completed`,
plus `cancelled`.

A chauffeur confirms by moving a leg off `in-progress`. Three write paths:

1. `drivers/views.py:912 accept_job` — the **Accept Job** button, allowed only from `in-progress`;
   writes `Leg.status="confirmed"` and a `LegStatus` row with `updated_by=request.user`.
2. `drivers/views.py:579 update_leg_status` — the driver-app status dropdown.
3. `drivers/views.py:704 _advance_status_for_text` — tapping a guest-text chip advances the status
   forward only (never backwards, never to `completed`).

**A dispatcher can also set it** — `dispatching/views.py:3020` (board dropdown, stamps
`status_changed_by`) and `:9894 bulk_update_leg_status`. Both write `LegStatus` rows, so a
chauffeur's own press is distinguishable from a dispatcher's by `LegStatus.updated_by`.

**Reassignment destroys confirmation.** `reservations/models.py:1911-1941` — when `driver` changes
and the status is not terminal, status resets to `in-progress`, `status_changed_by` is stamped from
`leg._status_change_user`, and a `LegStatus` row is written at `:2121` (`'Auto-reset: driver
unassigned'`). Confirmation is therefore **per leg, per driver** — never per reservation and never
per driver-per-day.

**Affiliates do not use this at all.** An operator accepts a farm-out in their own portal, recorded
as `Leg.operator_accepted_at` (`reservations/models.py:1092`), cleared when the leg changes hands
(`:1926-1946`).

The board already computes the rule but never renders the words:

```python
# dispatching/utils.py:741 detect_leg_flags
if leg.status == 'in-progress':
    flags.append({'level': 'warning' if minutes_until_pickup > 60 else 'danger',
                  'icon': 'bi-question-circle', 'text': 'Not confirmed yet'})
```

`detect_leg_flags` is computed in `views.py:148` for the row tint; the reason text is rendered by no
template (also noted in `docs/dispatch_audit.md`). Its only other consumer is
`dispatching/management/commands/dispatch_alerts.py:131`, which pushes to the **removed** ntfy
channel and therefore sends nothing.

**There is no view listing chauffeurs who have not confirmed** for any date. NOT FOUND IN CODEBASE.
The Confirmations page (`views.py:6511`) is **guest** SMS, keyed on `Leg.confirmation_sms_sent_at`;
grep for `driver|chauffeur` in `confirmations.html` returns zero hits.

### 3.4 Flights

`ops/tasks.py:1796 auto_refresh_flights` runs on every 30-minute cycle for today, and for +1/+2 days
every 8th cycle (`_get_refresh_date_ranges`:1893). Days 3–7 are on demand only. It calls
`dispatching/aeroapi_service.py AeroAPIService` (`AEROAPI_KEY`).

Four different "alert" definitions coexist:

| Definition | Where | Threshold | Has a reviewed state? |
|---|---|---|---|
| `Leg.has_flight_time_mismatch` | `reservations/models.py:2283` | 30 min (scanners); 5 min for chain re-check (`ops/tasks.py:50`) | Via the task it raises — **yes** |
| `Leg.flight_timing_flag` | `reservations/models.py:2398` | alert ≥20 min; watch = early 15–20 min | No — recomputed per render |
| `Leg.flight_disruption_flag` | `reservations/models.py:2469` | `status` contains "cancel"/"divert" | No |
| `flight_refresh_review.classify_refresh_row` | `dispatching/flight_refresh_review.py:148` | 5 / 30 / 120 min buckets | Only inside one refresh run |

The **only** persistent reviewed states are:

* Closing the `FLIGHT_VERIFICATION` task — `dispatching/views.py:7233 dismiss_flight_review`
  ("Mark Reviewed"), setting `status=completed`, `resolved_at`, `resolved_by`, `resolution_notes`.
* Acking a pickup move — `Leg.pickup_change_ack_at` via `dispatching/views.py:7283
  acknowledge_time_change`, driving `Leg.has_unacked_time_change` (`reservations/models.py:1394`).

The task queue **cannot be filtered by leg pickup date** (`ops/views.py:80` supports lane, type,
`q`, `overdue` only), so "unreviewed flight alerts for date X" is a new query over existing data.

### 3.5 Conflicts and tight turns

Three engines, all live:

| Engine | File | Persists? | Threshold | Surface |
|---|---|---|---|---|
| **A. Ops scanner** | `ops/tasks.py:260 classify_turn`, `:483 detect_driver_conflicts`, `:984 _scan_driver_overlaps` | **Yes** — `OperationalTask` + `LegKeoi` | `TIGHT_TURN_RED_AFTER_MIN = 10` (`:71`) | Red conflict badge on board rows; task queue |
| **B. Board chips** | `dispatching/board_validation.py:52 turn_slack_minutes` + `pickup_policy.py:271 turn_band` | No | `TURN_TIGHT_SLACK_MIN = 15`, `<0` critical | Timeline gap chips |
| **C. Recovery Advisor** | `dispatching/conflict_advisor.py:828 _turn_severity` | Cards no; `AdvisorEvent` ledger yes | Degradation-aware | Rail on the dashboard, **superuser-only** (`advisor_views.py:69`) |

A ↔ B were realigned on 2026-09-05 after a three-day drift
(`docs/release-notes/2026-09-05-board-chips-tell-the-truth.md`). C never calls A
(`conflict_advisor.py:86-90`) and creates/closes nothing.

Scanner behaviour, precisely:

* In-house drivers only; affiliate legs raise nothing.
* Today: consecutive-pair walk per driver, legs whose pickup time has passed excluded
  (`ops/tasks.py:1009`).
* `> 10 min` late → `DRIVER_CONFLICT` at CRITICAL + a system `LegKeoi` flag
  (`_raise_conflict_keoi`:347). `1–10 min` → `TIGHT_TURN` at MEDIUM.
* Future dates: only via `_handle_future_driver_conflict`:804, which fires when a flight moves
  ≥ `CHAIN_RECHECK_THRESHOLD` (5 min). **There is no routine "scan tomorrow's board" pass.**
* Self-clearing: `_auto_close_resolved_tasks`:1427 plus `ops/signals.py:98` on reassignment;
  `reconcile_conflict_keois`:410 takes system-raised flags down, but never a flag a human has touched
  (`created_by IS NULL AND updated_by IS NULL`).

### 3.6 The task queue

`ops/views.py:80 task_queue_view` partitions one queryset into five lanes — Unclaimed, Mine, Others,
Waiting (snoozed or blocked), Future Blockers (open task on a future-dated leg) — plus Completed
Today and a "Next Up" anchor. Ordering is `["priority", "due_at"]`. Claiming
(`ops/views.py:330 task_claim`) sets `assigned_to`, `assigned_at`, `status=in_progress` and writes a
`StaffActivity(TASK_CLAIMED)` row. Contact attempts are logged on a task via
`ops/views.py:614 task_log_comm` → `ops/services.py:160 log_communication`.

Auto-escalation is **disabled** (`ops/tasks.py:604`), and `ops/escalation.py` referenced by
`ops/OPS_SYSTEM.md:100` and `systems_audit.md` is **NOT FOUND IN CODEBASE**.

### 3.7 The office-staff day

Clock in/out and breaks: `ops/views.py:4589 timeclock_action` → `ops/services.py`. Off-schedule
punches file a `TimeClockRequest` instead of starting the clock (`clock_in_or_request`:261); the page
polls `request_status` every 60 s. Superusers see a live roster at `timeclock_manage`:4807 and
`timeclock_overview`:4634, both of which call `auto_close_stale_shifts()` lazily on page load.

Planned hours, duties, time off and on-call are managed on `staffing_board`:5305; dispatchers see
their own week at `my_coverage`:5654.

**The handbook's procedures are prose only.** `SOPS/dispatcher-operations-handbook.md` lines 83
(shift-start), 95 (during-shift), 105 (shift-handoff) describe exactly the workflow this project
wants to formalise, including "assign unresolved tasks to the next dispatcher or leave owner + due
time", "confirm tonight's on-call person", and "end any open break and clock out". **Nothing in code
checks any of it.** Clocking out with open assigned tasks is neither blocked nor warned; clocking out
while on break silently auto-closes the break (`ops/services.py:409`).

---

## 4. Existing Building Blocks

Each entry: what it does, where it lives, how reliable it is, how the Shift System reuses it.

### 4.1 Time clock — `ops/models.py:413` + `ops/services.py:194-691`

**What it does.** One clock-in→clock-out span per row, with child unpaid breaks. `State` derives to
`clocked_out` / `clocked_in` / `on_break` (`models.py:534`).

**Reliability: high.** Two partial unique constraints make double-punching impossible at the DB
level (`uniq_open_shift_per_user`:505, `uniq_open_break_per_shift`:675), and both `IntegrityError`
races are caught in the service layer (`services.py:235`, `:436`). The state machine is centralised —
the module docstring states these are the only mutators — and is covered by
`ops/tests/test_timeclock.py` (double punch, break-before-clock-in, midnight-spanning, DST,
auto-close cap, the full approval flow).

**Caveats:** stale-shift auto-close is lazy (§6.3); there is no max-shift or break-length rule
(NOT FOUND IN CODEBASE); nothing records location on the punch.

**Reuse.** `get_open_shift(user)` and `TimeClockShift.objects.filter(clock_out_at__isnull=True)` are
the Floor Card's presence source and the Lane A succession input (`clock_in_at` ordering).

### 4.2 Office roster — `ops/staff.py:21 office_staff_qs()`

Active `is_staff` users minus anyone flagged `is_driver` or `is_travel_agent` on `UserProfile`.
Superusers are deliberately included (docstring: a founder who works dispatch shifts is a legitimate
row). **Reliability: good, with a scope question** — any future non-dispatcher staff account appears
unless filtered (→ policy decision 1). Note two staff-metrics views inline a *different* variant
without `is_active` so deactivated staff stay in historical reports (`ops/views.py:2282`, `:3030`).

### 4.3 Staff schedule resolver — `ops/scheduling.py:108 resolve_staff_schedule()`

Resolution priority: single-date approved override → range override (latest `updated_at`) → weekly
row → `kind="none"`. Only `status="approved"` overrides count, so a pending time-off request changes
nothing. Returns `is_working`, `start_time`, `end_time`, `kind`, `role`, `location`,
`location_flipped`, `time_off`. Split shifts come from `extra_shifts_on()`:208.

**Reliability: high** (`ops/tests/test_scheduling.py` covers precedence, WFH flips, and the overnight
window). **Reuse:** the office/WFH chip on the Floor Card, the opener duty that seeds Lane A, and the
"is this exception owner even working tomorrow?" warning.

### 4.4 Coverage / staffing board — `ops/coverage.py`

`weekly_pattern`:718 (dateless recurring week), `dated_range`:845 (real dates with time off, covering
shifts, on-call), `my_week`:1099 / `day_view_actual`:1226 (dispatcher-facing, deliberately risk-free),
`_resolve_duty`:544 (assigned opener/closer wins; otherwise earliest-in/latest-out).

**Reliability: good but planned-only** — the module docstring states it reads no `TimeClockShift`.
Note also that the tiered-target risk engine (`day_coverage`:139, `week_coverage`:333, `_cell`:305)
is exercised **only by `ops/tests/test_coverage.py`** — no view calls it. The live board renders
`weekly_pattern` / `dated_range`.

**Reuse:** read `_resolve_duty`'s opener for Lane A's starting owner. Do not reuse the coverage risk
maths for the Floor Card — it answers a different question (is the *plan* adequate) than the Floor
Card (who is *actually* here).

### 4.5 Operational tasks — `ops/models.py:17` + `ops/services.py:22` + `ops/tasks.py:579`

**What it does.** A single work queue with ownership, priority, due/snooze/escalate times, related
object FKs, dedup and a 2-hour re-creation cooldown, plus self-closing scanners.

**Reliability: high for counting, medium for completeness.** Counting open tasks by type and date is
exact and indexed (`idx_ops_type_status`, `idx_ops_leg_dedup`). Completeness caveats: conflicts are
today-only except for flight-triggered future re-checks; affiliates are excluded; escalation is off.

**Reuse.** Two of the four system rows (conflicts/tight turns; flight alerts) are literally task
counts. Queue depth and oldest-item age come from the same table. Exceptions should **reference**
tasks by FK rather than copy their text.

### 4.6 Watch flags (KEOI) — `reservations/models.py:3639`

One active flag per leg, categorised, with `operational_status`
(`needs_attention` / `being_monitored` / `backup_arranged`) and full actor trail. System-raised flags
auto-clear; the moment a dispatcher edits one it becomes theirs and nothing clears it but them
(`ops/tasks.py:410` + `docs/release-notes/2026-08-27-conflict-flags-you-can-trust.md`).

**Reuse.** This is the natural, already-trusted source for the handoff's "watch items" section. No
parallel model needed.

### 4.7 Staff activity ledger — `ops/models.py:317`

Page views (written by `ops/middleware.py:18`, deduped 30 min) plus task actions and
`FLIGHT_MATCHED`. Extensible by adding `ActionType` choices.

**Reliability: good for "what did someone do", poor as a presence signal** — `staff_metrics_view` and
`staff_kpis_view` derive "active hours" from page views and **never read the time clock**
(`ops/views.py:3372` uses `IDLE_THRESHOLD_SEC = 1800`).

**Reuse.** Add action types for shift events (`SHIFT_OPENED`, `CHECK_CONFIRMED`, `LANE_TAKEN`) so
accountability lands in the existing ledger instead of a second audit table. Checklist *state* still
needs its own rows — a ledger cannot answer "is the row currently green".

### 4.8 Configuration pattern — `dispatching/models.py:10 SchedulerSettings`

Singleton `pk=1` with `get_settings()`:270 memoised in a module global, `clear_cache()`:280,
`to_dict()`/`get_defaults()` driving a generic JSON API (`dispatching/views.py:15969`, `:15984`),
edited from the planner's Tuning panel. Holds feature flags (`opt_enabled`:218,
`manual_assign_warnings`:174).

**Caveat to carry forward:** the module-global cache is never invalidated across workers, so a save
is visible only to the worker that handled it until restart. A Shift-System settings row should read
per request (one indexed row) or use `django.core.cache` with a short TTL — not a module global.

**The established three-tier convention:** env var with an inert default (secrets/deploy toggles) →
singleton row edited from a custom page (operator-tunable numbers) → module `UPPER_SNAKE` constants
with a source comment (algorithm constants).

### 4.9 Supervisory-view pattern — `ops/views.py:2944 staff_kpis_view`, `:2226 staff_metrics_view`

Plain Django view + querystring range (`?range=N` rolling, clamped 1–90; `?from=&to=` custom;
`?date=` single) + full-page template, with prior-period deltas. `ops/kpis.py:79 resolve_range`
normalises the range. No htmx, no DRF, no admin changelist.

**Reuse.** The Lead's 7-day view is this pattern with a different query.

### 4.10 Permission precedent — `user.has_perm(...)`

Two per-object permissions already exist and are granted per user or via a group in Django admin:
`reservations.remove_keoi` (`dispatching/keoi_views.py:189`) and
`reservations.use_schedule_sandbox` (`dispatching/assignment.py:60 can_use_sandbox`). This is the
template for the Lead tier (§12.3).

### 4.11 Background execution and notification plumbing

`reservations/utils.py:48 _run_in_background` — daemon thread that closes its DB connections on exit;
used for every off-request side effect. `payment/webhook.py:27 stripe_webhook` and
`ghl_integration/views.py:76 ghl_webhook` are the two inbound-webhook precedents
(`@csrf_exempt`; Stripe verifies its signature, GHL does **not** — see §12.5).

**There is no generic outbound webhook helper.** NOT FOUND IN CODEBASE — every integration calls
`requests` directly with a 10 s timeout.

---

## 5. System-Verified Count Feasibility Matrix

Classification: **AVAILABLE NOW** (queryable as-is) · **DERIVABLE** (existing data, new
query/aggregation) · **INTEGRATION REQUIRED** · **NOT CURRENTLY PRACTICAL**.

| Check | Existing source | Exact model(s) | Relevant fields | Query possible today? | Missing piece | Confidence |
|---|---|---|---|---|---|---|
| **Today's unassigned legs** | Board Unassigned lane; `_scan_unassigned_legs` | `reservations.Leg`; `reservations.Reservation` | `Leg.pickup_date`, `Leg.driver_id IS NULL`, `Leg.status`, `Reservation.status` | **AVAILABLE NOW** — `dispatching/views.py:150` + `driver_id IS NULL`; indexed by `leg_pickup_status_idx` | One agreed definition (past-pickup legs; completed legs) — 4 variants live today (§3.2) | High |
| **Today's chauffeurs not confirmed** | Board flag rule | `reservations.Leg`, `reservations.LegStatus`, `drivers.Driver` | `Leg.status == 'in-progress'`, `Leg.driver_id NOT NULL`, `LegStatus.updated_by`, `Driver.profile` | **DERIVABLE** — the per-leg rule exists (`dispatching/utils.py:741`); grouping is `values('driver_id').annotate(Count)` | A grouped-by-chauffeur query + a policy on dispatcher-set "confirmed" and on affiliates | High |
| **Today's unreviewed flight alerts** | Flight tasks + pickup-move ack | `ops.OperationalTask`, `reservations.Leg` | `task_type='flight_verify'`, `status IN OPEN_STATUSES`, `leg__pickup_date`; `Leg.pickup_time_changed_at` vs `pickup_change_ack_at` | **DERIVABLE** — both parts exist; the task queue simply never filters by leg date | A date-scoped query; a decision on which of the four alert definitions counts (§3.4) | Medium-High |
| **Today's conflict / tight-turn tasks** | 30-min scanner | `ops.OperationalTask` | `task_type IN ('driver_conflict','tight_turn')`, `status IN OPEN_STATUSES`, `leg__pickup_date` | **AVAILABLE NOW** — indexed by `idx_ops_type_status` | None for today | High |
| **Tomorrow's unassigned legs** | Same board query with `date+1` | `reservations.Leg` | as above | **AVAILABLE NOW** | None. Note no `driver_assign` task is raised for tomorrow by design (`ops/tasks.py:1169`) | High |
| **Tomorrow's chauffeurs not confirmed** | Same rule with `date+1` | `reservations.Leg` | as above | **DERIVABLE** | Same as today's, plus the fact that nothing prompts a chauffeur to accept in advance | High |
| **Tomorrow's unreviewed flight issues** | Flight tasks for `date+1` | `ops.OperationalTask`, `reservations.Leg` | as above | **DERIVABLE** | Same date-scoped query. Refresh cadence for +1 day is ~4-hourly (`ops/tasks.py:1914`), so the count lags | Medium |
| **Tomorrow's conflict / tight-turn tasks** | Flight-triggered future re-check only | `ops.OperationalTask` | as above | **DERIVABLE, but structurally incomplete** | No routine tomorrow-board overlap scan exists (`_scan_driver_overlaps` is `pickup_date=today`); the row will usually read 0 | Medium |
| **Clocked-in dispatchers** | Time clock | `ops.TimeClockShift`, `auth.User`, `users.UserProfile` | `clock_out_at IS NULL`, `user_id`; roster from `office_staff_qs()` | **AVAILABLE NOW** — partial index `idx_tcshift_open`; already queried at `ops/views.py:4640`, `:4816` | Stale-shift handling (§6.3) and a "counts as floor dispatcher" flag | High |
| **Employees on break** | Time clock | `ops.TimeClockBreak` | `break_end_at IS NULL` via `TimeClockShift.open_break` | **AVAILABLE NOW** | None | High |
| **Task queue depth (per lane)** | Ops tasks | `ops.OperationalTask` | `status IN OPEN_STATUSES`, `task_type`, `assigned_to` | **AVAILABLE NOW** | A lane→task-type mapping (config) | High |
| **Oldest open task age** | Ops tasks | `ops.OperationalTask` | `Min(created_at)` per bucket | **AVAILABLE NOW** — the identical aggregate already runs at `ops/views.py:3200` | None (note the queue displays overdue-by-`due_at`, not age) | High |
| **GoHighLevel queue depth** | Lead reply flags | `reservations.Lead` | `needs_human_follow_up`, `last_reply_at`, `sms_opt_out`, `sequence_active` | **DERIVABLE** — `ops/leads_board.py:120` already buckets on these | It is *leads awaiting a human*, **not** GHL inbox depth. Label it honestly | Medium |
| **RingCentral text queue depth / oldest** | — | — | — | **INTEGRATION REQUIRED** | No RingCentral code of any kind (§7) | High |
| **RingCentral missed calls** | — | — | — | **INTEGRATION REQUIRED** | As above | High |
| **Gmail queue depth** | — | — | — | **INTEGRATION REQUIRED** | No inbound mail path at all (§7) | High |
| **RingCentral logged in / ready** | — | — | — | **NOT CURRENTLY PRACTICAL** | Client-side desktop state; no server-observable signal exists | High |
| **Who is on the floor vs working remotely** | Planned schedule only | `ops.StaffWeeklySchedule`, `ops.StaffScheduleOverride` | `location` (`office`/`remote`/blank) | **DERIVABLE (planned only)** | Nothing records location at punch time (§6.4) | Medium |

**Note on "confirmed" grouping.** The proposal asks that unconfirmed chauffeurs be grouped by
chauffeur rather than listed per leg. That is a single aggregate over the same filtered leg set —
`select_related("driver", "driver__profile")`, group by `driver_id`, count legs, keep the earliest
`pickup_time` for sorting. The board already does the equivalent `select_related` for its own render
(`dispatching/views.py:154-165`), so no N+1 is introduced.

---

## 6. Who Is On The Floor?

### 6.1 The source of truth

**`ops.TimeClockShift` with `clock_out_at IS NULL` is the only record of actual presence in the
system.** Everything else is a plan.

```python
# ops/services.py:206 — the canonical helper
def get_open_shift(user):
    return (TimeClockShift.objects.filter(user=user, clock_out_at__isnull=True)
            .prefetch_related("breaks").first())
```

```python
# ops/models.py:534 — derived state
@property
def state(self):
    if not self.is_open:
        return self.State.CLOCKED_OUT
    return self.State.ON_BREAK if self.open_break else self.State.CLOCKED_IN
```

Existing consumers: `ops/context_processors.py:44 timeclock_status` (navbar pill, uncached per user),
`ops/views.py:4640` (overview "who's on the clock now"), `:4816` (manage page `open_by_user`),
`:5011` (per-staff detail).

Roster: `ops/staff.py:21 office_staff_qs()`.

**Why the alternatives are not the truth:**

* `ops/coverage.py` — planned schedule only; its docstring states it reads no `TimeClockShift`.
* `StaffOnCall` (`ops/models.py:926`) — *planned* overnight cover. Its docstring is explicit that
  logging that someone actually took the on-call is "a separate 'actual' concept" and it is
  **not built** (also confirmed by `docs/staffing-phase-2-dispatcher-view.md`, "Related open thread").
* `StaffActivity` page views — a proxy for activity, not presence, and already used by the metrics
  pages for "active hours" without ever consulting the clock.
* Django sessions — 90-day cookie age (`business/settings.py:472`), so a live session says nothing
  about presence.

**There is no `on_duty_now()` function.** NOT FOUND IN CODEBASE — the staffing board draws a "now"
marker (`ops/views.py:5331`) but computes no list.

### 6.2 Reliability

**High for the core question.** Two DB constraints make the state unambiguous:
`uniq_open_shift_per_user` (`ops/models.py:505`) and `uniq_open_break_per_shift` (`:675`). Every
transition is funnelled through `ops/services.py` and raises `TimeClockError` rather than corrupting
state; the views convert that into a soft JSON error so a double-click just re-syncs
(`ops/views.py:4614`). Tests pin the behaviour including midnight-spanning shifts and DST.

### 6.3 Edge cases

| Case | What the code does today | Consequence for a Floor Card |
|---|---|---|
| **Forgot to clock out** | `auto_close_stale_shifts(max_hours=16)` (`ops/services.py:455`) closes it and **caps** `clock_out_at` at `clock_in + 16 h`, flagging `auto_closed`. It is called **lazily**, only from `timeclock_overview` (`ops/views.py:4636`) and `timeclock_manage` (`:4809`). No scheduler calls it. | Someone can read as "on the floor" for many hours after leaving. The card must either call the same helper or visually flag long-open shifts. → policy 11 |
| **Shift spans midnight** | Fully handled. `_tc_today_worked_seconds` (`ops/views.py:4494`) adds the still-open pre-midnight shift; `clock_in_schedule_check` (`ops/scheduling.py:230`) honours yesterday's crossing window. | Presence is correct across midnight. Which *calendar day* a checklist belongs to is a separate question → policy 16 |
| **Two people clock in at the same instant** | Both succeed (the constraint is per user). Ordering by `clock_in_at` alone can tie. | Lane A succession needs a deterministic tiebreak (`clock_in_at, id`) → policy 13 |
| **A goes on break** | Recorded exactly (`TimeClockBreak`, open = `break_end_at IS NULL`). No rule about duties. | Automatable either way once policy 10 is answered |
| **Unexpected clock-out** | Indistinguishable from a normal one. | Treat any clock-out as a Lane A handoff → policy 11 |
| **Clocks back in** | A brand-new `TimeClockShift`; no memory of a prior lane. | Needs a rule → policy 12 |
| **Admin punches someone in** | `admin_punch_in` (`ops/services.py:526`) stamps `edited_by`/`edited_at`, rejects future times and overlaps. | Distinguishable from a self-punch; safe to show as "punched in by Abdalla" |
| **Pending approval request** | `clock_in_or_request` (`:261`) files a `TimeClockRequest` and **does not** open a shift. | Correctly absent from the floor. Worth surfacing as "waiting for approval" so the Lead notices |
| **Approved but unused grant** | Valid 120 min; the shift opens only at their next punch. | Also correctly absent |
| **Working remotely** | Known from the schedule only (§6.4). | Show as a chip; do not infer availability → policy 15 |
| **Non-dispatcher staff account** | Appears in `office_staff_qs()` if `is_staff` and not driver/agent. | Needs an exclusion flag → policy 1 |
| **Schedule check errors** | `clock_in_or_request` fails **open** (`ops/services.py:281-283`) — a resolver bug never locks the roster out. | Good precedent: the Shift System must likewise never block clocking in or working the board |

### 6.4 Clocked in ≠ available for dispatch

Three distinct facts, only two of which the app knows:

1. **On the clock** — `TimeClockShift`. Known, reliable.
2. **On break** — `TimeClockBreak`. Known, reliable.
3. **At a dispatch desk and available** — **not known.** `WORK_LOCATION_CHOICES`
   (`ops/models.py:717`) lives on the *schedule* (`StaffWeeklySchedule.location`,
   `StaffScheduleOverride.location`, resolved into
   `resolve_staff_schedule()["location"]`), never on the punch. `TimeClockShift` has no location
   field. Grep for "dispatch floor" / "on the floor" across the repo returns nothing relevant.
   **NOT FOUND IN CODEBASE.**

Recommendation: the Floor Card shows *clocked in*, *on break*, and *planned location* as three
separate facts, with no derived "available" flag. If an explicit availability state is wanted later,
add a lightweight per-shift status (`available` / `heads-down` / `unavailable`) set by the person or
the Lead — which is also what "mark someone unavailable" in the Lead's powers implies.

---

## 7. External Communications Visibility

| System | What Django can see today | Existing integration | Missing capability | Cheapest next step | Complexity |
|---|---|---|---|---|---|
| **RingCentral calls** | Nothing | **NONE.** Only prose: `dispatching/confirmation_sms.py:29,39,230`, `confirmations.html:70,141,354`, `SOPS/dispatcher-operations-handbook.md:36,409`, `systems_audit.md:343`. No SDK in `requirements.txt`, no `RC_*` env var, no webhook | Call events, queue state, ring-group config | Human-verified checklist row. Later: an inbound receiver modelled on `payment/webhook.py:27` | Medium–High |
| **RingCentral SMS** | Nothing. The app's own guest texts go via Twilio, and replies to that number land in the **GHL** inbox (`drivers/wakeup.py` docstring) | NONE | Thread depth, oldest unanswered | As above; one receiver serves calls and texts | Medium–High |
| **RingCentral missed calls** | Nothing | NONE | Missed-call list and callback state | As above | Medium–High |
| **Gmail** | Outbound only: `EMAIL_*` SMTP (`business/settings.py:399-409`), every send logged to `ops.EmailLog` via `users/emails.py:21 log_email_sent` | Outbound **CODE INTEGRATION EXISTS**; inbound **NOT FOUND** (no `imaplib`, `googleapiclient`, `google.oauth2`, `django_mailbox`) | Unread count, label depth, oldest unread | Human-verified row. Later: read-only Gmail API count for one label, synced by the existing 30-min thread | Medium |
| **GoHighLevel** | Per-contact reply probe (`ghl_integration/services.py:567 contact_has_replied`), outbound SMS (`:652`), tags; inbound webhook sets `Lead.needs_human_follow_up` / `last_reply_at` (`ghl_integration/views.py:76`); `ops/leads_board.py` buckets leads | **CODE INTEGRATION EXISTS** (contacts + messages + tags). Inbox listing, unread count, opportunities/pipelines: **NOT FOUND** | True inbox depth | Show `Lead.needs_human_follow_up` count + oldest `last_reply_at` — data already local, zero API cost. Label it "leads awaiting a reply", not "GHL inbox" | Low |
| **WhatsApp** (the team's actual channel) | Nothing | **NOT FOUND** — no WhatsApp Business/Cloud API, no `whatsapp` reference anywhere. Also NOT FOUND: Google Chat, Slack, Discord | Any outbound post | **Generate the summary, let a human paste it** (§7.7). Automation later only via Twilio's WhatsApp channel, which the existing `drivers/sms.py` client could reach | Low as designed; Medium-High if automated |
| **Office-staff push** | Nothing. Web push exists but `DriverPushSubscription` is FK'd to `Driver` (`drivers/models.py:1099`) | Chauffeur-only, and auto-notices are paused (`WEBPUSH_AUTO_NOTICES` default False) | A staff subscription model + subscribe UI | Not needed for Phase 1 — the pasted WhatsApp message covers the summary | Medium |
| **Twilio (context)** | Outbound SMS/voice to guests, chauffeurs and founder phone lists | **CODE INTEGRATION EXISTS** (`drivers/sms.py:45 send`) | — | The only pre-wired path to an automated WhatsApp message later (Twilio WhatsApp sender), and already the channel for founder alerts | Low |
| **ntfy (historical)** | Nothing — removed 2026-07-18; `reservations/utils.py:643,658,887` are **no-op stubs** still called from 6 places (`reservations/views.py:561,717`, `ghl_integration/views.py:307`, `ghl_integration/tasks.py:937`, `drivers/signals.py:47`, `dispatching/management/commands/dispatch_alerts.py:187,218`) | Dead | — | Do not build on it. `docs/`+`systems_audit.md` describing NTFY escalation are stale | — |

**Credential architecture (names only, no values).** All secrets come from environment variables read
once in `business/settings.py` via `python-dotenv`, each with an inert default so the feature is
completely off when unset: `AEROAPI_KEY`, `SAMSARA_API_TOKEN`, `TWILIO_ACCOUNT_SID` /
`TWILIO_AUTH_TOKEN` / `TWILIO_PHONE_NUMBER`, `GHL_API_KEY`, `GHL_LOCATION_ID`,
`WEBPUSH_VAPID_PRIVATE_KEY` / `WEBPUSH_VAPID_PUBLIC_KEY`, `STRIPE_*`, `EMAIL_HOST_USER` /
`EMAIL_HOST_PASSWORD`, `GOOGLE_MAPS_API_KEY`. If an automated WhatsApp sender is ever added, its
credentials must follow exactly that pattern (an unset value means "send nothing" rather than an
error). `.env` sits at the repo root and **is** gitignored (`.gitignore:149`).

### 7.7 The completion message: generate it, let a human send it

The team's group channel is **WhatsApp**, and the preferred behaviour (founder direction,
2026-09-12) is that **a person sends the completion message**, not a bot.

That is both the cheaper option and the one that matches an existing house convention. Release notes
already work this way: `docs/release-notes/README.md` puts the team message in a **"Send this to the
team"** block that a human copies into the dispatcher group chat, precisely so nothing automated
speaks to the floor in the company's voice.

**Recommended Phase 1 behaviour.** On Open Complete and Close Complete, the checklist renders a
ready-to-paste block and a Copy button:

```
Saturday open complete — Bryan, 7:58 AM (on time; board safe 7:12)
Outstanding: 4:30 PM Disney → MCO unassigned, Bryan owns until 9:00 AM.
Sereen has not confirmed her 1:15 PM pickup; Luis owns follow-up by 10:00 AM.
```

Copy-to-clipboard is a few lines of JavaScript with no permissions, no credentials, no external
dependency and no failure mode that can touch the checklist. The copy rules from
`docs/release-notes/README.md` apply: name the person and the consequence, no field or file names,
plain sentences.

**If automation is ever wanted.** WhatsApp cannot take a simple incoming-webhook URL the way Google
Chat or Slack can. It requires the WhatsApp Business Platform (Meta Cloud API or a provider), a
registered business number, and — because an unprompted outbound message falls outside the 24-hour
customer-service window — **pre-approved message templates**, which do not suit free-form
shift summaries with variable line counts.

The cheapest viable path, if it is ever justified, is **Twilio's WhatsApp channel**: Twilio is
already wired (`drivers/sms.py:29 client()`, `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN`), so it would
reuse the existing client with a `whatsapp:` prefixed sender and recipient, plus a Meta sender
registration. Even then, template approval governs what can be sent unprompted. **Recommendation:
do not build it.** A Copy button plus a human is better operations and a tenth of the work.

**Recommendation.** Do not introduce another integration pattern. For Phase 1, the five human-verified
rows stay human-verified, the GHL row shows local lead data, and the only new outbound path is one
Chat poster that reuses `_run_in_background`. Everything the dispatch board renders must remain free
of external calls (§14).

---

## 8. Lane Assignment Architecture

### 8.1 What exists

Nothing. **NOT FOUND IN CODEBASE**: any lane, desk, queue-ownership or board-owner concept for office
staff. The word "lane" in this codebase means a timeline row (`ops/coverage.py:496 _lane_pack`), the
board's Unassigned strip (`dispatching/views.py:1533`), or a task-queue tab (`ops/views.py:152`).

What *does* exist and must be reused rather than duplicated:

* **An assigned shift duty** — `STAFF_ROLE_CHOICES` (`ops/models.py:703`) on `StaffWeeklySchedule`,
  `StaffScheduleOverride` and `StaffExtraShift`, resolved by `ops/coverage.py:544 _resolve_duty`
  (explicit assignment wins; otherwise earliest-in / latest-out). This is already the answer to "who
  opens today", and Lane A must start from it.
* **Per-task ownership** — `OperationalTask.assigned_to` (`ops/models.py:95`) with the claim flow.
  Lanes sit *above* this: a lane says which queue you watch, a task assignment says which item is
  yours.

### 8.2 The smallest additive architecture

Three new pieces in `ops`, and nothing else:

**(a) Configuration — one settings row.** Follow the `SchedulerSettings` precedent
(`dispatching/models.py:10`) but scoped to shift operations and **without** the module-global cache:

```
ops.ShiftSettings  (singleton, pk=1)
  board_safe_target      TimeField   default 07:15
  open_complete_target   TimeField   default 08:00
  close_target           TimeField   nullable
  lane_table             JSONField   headcount -> [{lane, label, queues[]}]
  ring_order             JSONField   headcount -> [user_id | role, ...]   (display only)
  thresholds             JSONField   queue -> {warn_depth, warn_oldest_min}
  summary_template       Text        # the pasteable completion message (see §7.7)
```

`lane_table` is a JSONField because it is a small editable matrix, not relational data — the same
judgement `OperationalTask.metadata` and `DayPlan.result_json` already make. The proposed 2/3/4-person
arrangement is **seed data, not code**.

**(b) Per-person flags.** No office-staff profile exists today (`users.UserProfile` carries only
`is_driver` / `is_travel_agent`), so add a thin one:

```
ops.StaffProfile  (OneToOne -> auth.User)
  is_floor_dispatcher  Boolean default True    # policy 1
  never_lane_a         Boolean default False   # policy 14
  notes                Char
```

Surfaced as toggles on the existing `timeclock_manage` roster rows (`ops/views.py:4807`) — no new
admin page.

**(c) Lane assignment records.** Derived, but recorded, because "who had the board at 14:05" must be
answerable later:

```
ops.LaneAssignment
  date            Date (indexed)
  user            FK auth.User
  lane            Char        # 'A', 'B', 'C', 'D'
  started_at      DateTime
  ended_at        DateTime null      # open row = currently holds the lane
  source          Char        # 'auto' | 'override' | 'opener'
  set_by          FK auth.User null  # the Lead, for overrides
  reason          Char blank
```

with a partial unique constraint on `(date, lane)` where `ended_at IS NULL`, mirroring
`uniq_open_shift_per_user` (`ops/models.py:505`) and `uniq_active_keoi_per_leg`
(`reservations/models.py:3702`) — the codebase's established way to say "at most one live row".

### 8.3 Why this is not a second scheduling system

The rule that keeps it additive: **`LaneAssignment` never stores who is working.** It stores who
holds a lane, and a lane can only be held by someone with an open `TimeClockShift`. Presence is
always re-read; the lane row is closed (`ended_at`) when the shift closes. Concretely:

* Eligible set = `office_staff_qs()` ∩ open `TimeClockShift` ∩ `StaffProfile.is_floor_dispatcher`.
* Lane count = `lane_table[len(eligible)]`, falling back to the nearest lower key.
* Lane A seed = the day's opener from `_resolve_duty` if they are clocked in, else the
  longest-clocked-in eligible person (`ORDER BY clock_in_at, id`).
* Succession on clock-out / long break = the next eligible person by the same ordering, skipping
  `never_lane_a`.
* A Lead override writes a row with `source='override'` and always wins until the next clock-out.

Every input is an existing fact. The only new state is the lane label and its audit.

### 8.4 Where the proposal needs a decision before this can be built

Eight behaviours are undefined and are listed as policy decisions 9–16 in §15: succession ordering,
break handling, unexpected clock-out, clock-back-in, simultaneous clock-in, only-never-A-remain,
remote eligibility, and midnight. Each maps directly to one branch of the succession function; none
is a technical question.

### 8.5 Call ring order

`ops/notify`-style automation is impossible: there is no RingCentral integration to read or write
(§7). **Display only** is the correct first implementation — a table in `ShiftSettings.ring_order`
rendered on the Floor Card for the current headcount, so the floor can set the phone system to match.
If RingCentral is integrated later (Phase 4), the same table becomes the desired state to push.

---

## 9. Opening / Closing Architecture

### 9.1 What exists

**NOT FOUND IN CODEBASE**: any checklist, opening, closing, shift-report, EOD or acknowledgement
model for office staff. Grep across `ops/`, `dispatching/`, `reservations/`, `users/`, `drivers/` for
`checklist` returns only a Bootstrap icon class.

**Exists as prose only**: `SOPS/dispatcher-operations-handbook.md:83` (shift-start),
`:95` (during-shift), `:105` (shift-handoff), `:462-489` (time clock and founder handoffs),
`:491-502` (systems dispatchers must not rely on). This handbook is the de facto specification for
the checklists and should be the source of the initial row text.

**Acknowledgement precedent for a single object** exists and is worth copying in spirit:
`Leg.pickup_change_ack_at` + `has_unacked_time_change` (`reservations/models.py:1345,1394`) with
`dispatching/views.py:7283 acknowledge_time_change` — a persisted "someone has seen this" stamp that
a board surface reads.

### 9.2 Proposed shape

Three new models in `ops`, plus the settings row from §8.2(a):

```
ops.ShiftChecklist
  date              Date (indexed)
  kind              Char           # 'open' | 'close'
  opened_by         FK auth.User null
  opened_at         DateTime null
  board_safe_at     DateTime null
  open_complete_at  DateTime null   # for kind='close', the completion stamp
  completed_by      FK auth.User null
  completed_at      DateTime null
  reopened_by       FK auth.User null
  reopened_at       DateTime null
  targets           JSONField       # the gate times in force that day (frozen at open)
  counts_snapshot   JSONField       # the four system counts at completion — HISTORY ONLY
  unique (date, kind)

ops.ShiftChecklistRow
  checklist   FK ShiftChecklist (related_name='rows')
  key         Char        # 'unassigned' | 'unconfirmed' | 'flight' | 'conflicts'
                          # | 'sms' | 'email' | 'ghl' | 'phone' | 'missed_calls'
  kind        Char        # 'system' | 'human'
  state       Char        # 'open' | 'clear' | 'documented'
  confirmed_by FK auth.User null
  confirmed_at DateTime null
  note        Char blank
  unique (checklist, key)

ops.ShiftException
  checklist     FK ShiftChecklist (related_name='exceptions')
  row           FK ShiftChecklistRow null
  what          Text                    # what is outstanding
  owner         FK auth.User            # who owns it
  next_action   Char                    # what happens next
  next_action_at DateTime null
  task          FK ops.OperationalTask null    # reference, never copy
  leg           FK reservations.Leg null
  keoi          FK reservations.LegKeoi null
  created_by    FK auth.User
  created_at    DateTime
  resolved_at   DateTime null
  carried_from  FK self null
  acknowledged_by FK auth.User null
  acknowledged_at DateTime null
```

### 9.3 How the automated rows work

**Computed at read time, never stored as truth.** One module, `ops/shift_checks.py`, exposing one
function per check, each taking a date and returning `(count, drill_url, detail)`:

```python
def unassigned_legs(target_date) -> CheckResult          # Open + Close
def unconfirmed_chauffeurs(target_date) -> CheckResult   # Open + Close, grouped by driver
def unreviewed_flight_alerts(target_date) -> CheckResult # Open + Close
def open_conflict_tasks(target_date) -> CheckResult      # CLOSE ONLY — see §1 scoping decision
```

**Open Shift runs three of these; Close Shift runs all four.** The row set per shift belongs in
`ShiftSettings`, not in code, so it can be changed without a deploy once the floor has used it.

Two rules make this safe:

1. **One leg-set helper.** `unassigned_legs` and `unconfirmed_chauffeurs` must both call a single
   shared, read-only queryset builder so the checklist can never disagree with itself. Given the four
   divergent variants documented in §3.2, this helper should be introduced in `ops/shift_checks.py`
   and used *by the checklist only* — do not refactor the board's existing queries as part of this
   project.
2. **Drill-through, not duplication.** Each row links to the existing surface: the board filtered to
   `?date=&driver=unassigned`, the task queue filtered to `?type=driver_conflict`, the leg detail.
   `ops/models.py` already carries the URL-building precedent in the task-detail contexts.

`ShiftChecklistRow.state` for a system row is **derived** (`clear` when the count is 0,
`documented` when an exception covers it, `open` otherwise) and is persisted only when the checklist
completes, as history. The row is never manually checkable — enforced by the view rejecting a
confirm POST for `kind='system'`.

### 9.4 Human rows

Five rows (`sms`, `email`, `ghl`, `phone`, `missed_calls`), each a POST that stamps `confirmed_by`
and `confirmed_at`. This is exactly the shape of `acknowledge_time_change` and of `task_claim` — a
small idempotent POST returning JSON, with the page re-syncing on a soft error.

### 9.5 Gates and the no-silent-exceptions rule

* **Board Safe** — allowed when every system row is `clear` or `documented`, and every carried-forward
  exception is acknowledged. Stamps `board_safe_at`, compared against `targets['board_safe']`.
* **Open Complete** — additionally requires every human row `clear` or `documented` and every
  exception to have an owner and a next action. Stamps `completed_at` and `completed_by`.
* **The gate is advisory for the floor and hard for the checklist.** It must never block the board,
  assignment, or the task queue. The precedent is `clock_in_or_request` failing open
  (`ops/services.py:281`): operational tooling never stops people working.

Completion requires, for each non-clear row, a `ShiftException` with all three fields. That is the
mechanism that makes "no silent exceptions" real.

### 9.6 Carry-forward

On opening a checklist, load `ShiftException` rows that are unresolved from the previous checklist of
the opposite kind (close → next open; open → same-day close), display them first, and require
`acknowledged_by` before Board Safe. A new exception created from a carried one keeps
`carried_from`, so the Lead view can show "this has been carried three shifts".

### 9.7 Reuse over rebuild

| Need | Reuse |
|---|---|
| Counting problems | `ops/tasks.py` scanners' own definitions; the board leg query |
| Owning a problem | `OperationalTask.assigned_to` when a task exists — the exception just points at it |
| Watching a trip | `LegKeoi` — never a new watch flag |
| Recording who confirmed | `confirmed_by`/`confirmed_at` on the row, plus a `StaffActivity` action type |
| Configuration | `ops.ShiftSettings` following the `SchedulerSettings` pattern |
| Page shape and nav | `ops/views.py` + `dispatching/urls.py` + `dispatcher_navbar.html` |

---

## 10. Board Handoff Architecture

### 10.1 What already exists structurally

Four of the five proposed sections can be pre-populated from data the app already keeps:

| Handoff section | Existing source | Pre-populate? |
|---|---|---|
| **Open conflicts** | `OperationalTask` where `task_type IN ('driver_conflict','tight_turn')` and open, for the day | **Automatic** |
| **Chauffeurs we're waiting on** | The unconfirmed query from §5, grouped by driver | **Automatic** |
| **Changes since opening** | `Leg.history` (simple-history, `reservations/models.py:2139`) and `reservations.AuditLog` filtered to `timestamp >= checklist.opened_at`; `dispatching/leg_timeline.py:551 build_leg_timeline` already merges these trails | **Automatic** (driver assignments, status changes, pickup moves) |
| **Watch items** | `LegKeoi` active flags with `operational_status` and `description` | **Automatic** |
| **Tasks currently in progress** | `OperationalTask` where `assigned_to = outgoing user` and `status = in_progress` | **Automatic** |
| *Anything else the outgoing person knows* | — | **Human free text** |

Only the last is genuinely new text. `docs/release-notes/2026-08-27-conflict-flags-you-can-trust.md`
also establishes the social rule to respect: a flag a dispatcher has touched belongs to them.

### 10.2 Proposed shape

```
ops.LaneHandoff
  lane_assignment  FK ops.LaneAssignment     # the change this documents
  from_user        FK auth.User
  to_user          FK auth.User null         # null when the lane goes unowned
  created_at       DateTime
  snapshot         JSONField                 # the five pre-populated sections as rendered
  note             Text                      # the human part
  acknowledged_by  FK auth.User null
  acknowledged_at  DateTime null
```

`snapshot` stores **ids and short labels**, not copies of the underlying text — `{"conflicts":
[{"task_id": 812, "title": "...", "leg_id": 30493}], "waiting_on": [{"driver_id": 17, "legs": 2}],
...}`. The handoff view then re-reads the live objects for display, so a task closed after the
handoff shows as closed rather than frozen.

### 10.3 What the handoff must not do

* Not create tasks. If the outgoing dispatcher wants a task, the queue already has
  `task_create_manual` (`ops/views.py:652`).
* Not create watch flags. `keoi_save` (`dispatching/keoi_views.py:98`) exists.
* Not copy a conflict's description. Reference the task id.
* Not block the lane change. The lane moves when the clock says so; the handoff is the record that
  it happened and what was said.

---

## 11. Lead View Architecture

### 11.1 The pattern to copy

`ops/views.py:2944 staff_kpis_view` and `:2226 staff_metrics_view` are the house style for a
manager-facing range view: a plain Django view, a querystring range (`?range=N` rolling and clamped
1–90, `?from=&to=` custom, `?date=` single), prior-period comparison, one full-page template in
`dispatching/templates/dispatching/`, superuser-gated. `ops/kpis.py:79 resolve_range` normalises the
range into aware bounds. `ops/views.py:3200` already demonstrates the per-user aggregate
(`Min("created_at")` by `assigned_to`) that the Lead view needs for outstanding work.

### 11.2 The query

For the last 7 days (default; the same range controls apply):

```python
ShiftChecklist.objects
  .filter(date__range=(start, end))
  .select_related("opened_by", "completed_by", "reopened_by")
  .prefetch_related(
      Prefetch("exceptions",
               queryset=ShiftException.objects.select_related("owner", "task", "leg")),
      "rows")
  .order_by("-date", "kind")
```

Two queries plus prefetches for the whole table. Each row renders: date, open completed by, open
completion time, board-safe target hit/missed, open-complete target hit/missed, close completed by,
close completion time, exception count with owners, carried-forward count, and outstanding ownership.

`targets` is frozen on the checklist at open time, so a later change to `ShiftSettings` cannot
retroactively turn a hit into a miss — the same discipline `DayPlan.bookings_as_of`
(`dispatching/models.py:416`) uses for "as of" honesty.

### 11.3 Access

Visible to `is_superuser` **or** the new `ops.review_checklists` permission (§12.3). The Lead's
extra actions (reopen a checklist, edit exception ownership, override a lane) are separate
permissions so they can be granted independently.

### 11.4 Scope discipline

This is an accountability view, not BI. It does not need charts, trend arrows or exports in Phase 1 —
`staff_kpis.html` already covers analytical depth for Abdalla, and a Lead reading a 7-row table wants
to see a gap, not a graph. If a CSV is wanted later, `ops/views.py:4690 timeclock_export_csv` is the
export precedent.

---

## 12. Audit Trail & Permissions

### 12.1 What already records who did what

| Question | Mechanism | Where |
|---|---|---|
| Who changed a reservation or a leg | django-simple-history snapshots, diffed pairwise | `HistoricalRecords()` on `Customer`:47, `RefundRequest`:815, `Leg`:2139 in `reservations/models.py`; middleware `simple_history.middleware.HistoryRequestMiddleware` (`settings.py:123`); rendering `dispatching/views.py:2226 _build_history_with_deltas` |
| Who reassigned a chauffeur | `Leg.driver_assigned_by` / `driver_assigned_at` (`reservations/models.py:1246,1254`), written by `set_leg_driver` (`dispatching/assignment.py:149-158`); plus an `AuditLog` row `driver_assigned` / `driver_unassigned` (`reservations/signals.py:807-825`) | — |
| Who changed a leg status | `Leg.status_changed_by/at` (`:1262,1270`) + one `LegStatus` row per change with `updated_by` (`reservations/models.py:3612`) | Driver taps, dispatcher dropdown, bulk update, auto-reset |
| Who completed a task | `OperationalTask.resolved_by` / `resolved_at` (`ops/models.py:137,136`) + `StaffActivity(TASK_COMPLETED)` | `ops/views.py:360`, `ops/services.py:102` |
| Who claimed / assigned / snoozed a task | `assigned_to`, `assigned_at` + `StaffActivity` action types | `ops/views.py:330,389,445` |
| Who logged a call/text/email on a task | `CommunicationAttempt.staff_user` (`ops/models.py:290`) | `ops/services.py:160` |
| Who acknowledged a pickup change | `Leg.pickup_change_ack_at` (`:1345`) | `dispatching/views.py:7283` |
| Who raised / worked / closed a watch flag | `LegKeoi.created_by` / `updated_by` / `closed_by` (`reservations/models.py:3675-3691`) | `dispatching/keoi_views.py` |
| Who sent an email | `EmailLog.sent_by` (`ops/models.py:387`) | `users/emails.py:21` |
| Who was clocked in | `TimeClockShift` rows; corrections stamped `edited_by`/`edited_at`; approvals `approved_by` | `ops/models.py:413`, `ops/services.py:503 _stamp` |
| Who approved time off / set a schedule | `StaffScheduleOverride.decided_by` / `decided_at` (`ops/models.py:868,875`) | `ops/views.py:5390` |
| Who staged / published a schedule draft | Five actor+timestamp pairs on `ScheduleDraft` + `ScheduleDraftEvent.actor` (`reservations/models.py:3816,3948`) | `dispatching/assignment.py:87` |
| What the advisor showed and who acted | `AdvisorEvent.applied_by` / `snoozed_by` / `task_filed_by` (`dispatching/models.py:538,555,563`) | `dispatching/advisor_events.py` |
| Operational behaviour (page views, task actions) | `StaffActivity` (`ops/models.py:317`), written by `ops/middleware.py:18` and the task views | — |
| Generic model-change log | `reservations.AuditLog` (`:3475`) via `reservations/signals.py:580 create_audit_log`, with the actor resolved from `reservations/middleware.py:95 ThreadLocalMiddleware` | Also written by `reservations/keoi.py:48`, `dispatching/pickup_moves.py:117`, `users/services.py:50`, `dispatching/fleet_sync.py:243` |

`dispatching/leg_timeline.py:551 build_leg_timeline` already merges four of these trails
(HistoricalLeg, AuditLog, `StaffActivity(FLIGHT_MATCHED)`, LegStatus) into one readable history with
dedupe and attribution recovery — proof that the existing trails are rich enough to answer "who did
what to this trip" without adding another table.

### 12.2 Gaps relevant to this project

1. **No office-shift events.** Nothing records that a shift was opened, a row confirmed, a lane
   taken, or a board handed over. **This is genuinely new** and is what `ShiftChecklist` /
   `ShiftChecklistRow` / `LaneAssignment` / `LaneHandoff` exist to hold.
2. **Auto-closures have no actor.** `close_task(..., auto=True)` leaves `resolved_by` NULL
   (`ops/services.py:112`). The Lead view must not count system closures as human work.
3. **`queryset.update()` bypasses history.** Documented at `dispatching/leg_timeline.py:14-20` and
   `dispatching/pickup_moves.py:15`. Any Shift-System write should use `save()` with `update_fields`,
   not bulk update, so its own trail is complete.
4. **Presence and activity are never joined.** `staff_metrics_view` derives "active hours" from page
   views; the time clock is a separate world. The Lead view should report *checklist* facts, not try
   to merge the two.
5. **`HistoricalReservation` orphan.** Migration `0076_historicalleg_historicalreservation` exists but
   `Reservation` carries no `HistoricalRecords()` today. UNCERTAIN — requires runtime confirmation
   whether the table is still populated; irrelevant to this project but worth a cleanup ticket.

**Recommendation:** extend `StaffActivity.ActionType` with shift actions rather than creating a
second activity log. Checklist state lives in its own tables because it must be queried as state
("is this row green now"), which a ledger cannot answer.

### 12.3 Permissions

**Today there are exactly two tiers.**

```python
# ops/views.py:70,74
def _is_superuser(user): return user.is_superuser
def _is_staff(user):     return user.is_staff or user.is_superuser
```

`dispatching/views.py` checks `request.user.is_staff` for board pages and `is_superuser` for money,
staffing and metrics pages (27 occurrences); `dispatcher_navbar.html:44,62,124` branches on
`is_superuser`; `dispatching/admin_mixins.py:7 DispatcherAdminMixin` hides ~12 financial fields from
non-superusers in Django admin. **No Django Group usage, no `is_dispatcher`, no lead/supervisor
concept.** `users.UserProfile` carries only `is_driver` and `is_travel_agent`.

**Two per-object permissions already exist**, and they are the correct precedent:

```python
# reservations/models.py:3700  (LegKeoi.Meta)
permissions = [("remove_keoi", "Can remove KEOI flags (with reason)")]
# reservations/models.py:3890  (ScheduleDraft.Meta)
permissions = [("use_schedule_sandbox", "Can build/hold sandbox schedules")]
```

checked as `request.user.has_perm("reservations.remove_keoi")`
(`dispatching/keoi_views.py:189`) and
`user.has_perm("reservations.use_schedule_sandbox")` (`dispatching/assignment.py:68`), with the
docstring noting they are granted per user or via a group in the Django admin.

**Recommended architecture for the Lead.** Do not create a third boolean tier and do not give Luis
`is_superuser` — that would expose revenue (`can_view_revenue`, `dispatching/views.py:86`), payroll,
refunds, commissions and staff metrics. Instead:

1. Declare permissions in `ops.ShiftChecklist.Meta` / `ops.LaneAssignment.Meta`:
   `override_lane`, `reassign_lanes`, `reopen_checklist`, `review_checklists`,
   `edit_exception_owner`, `mark_unavailable`.
2. Create a **"Dispatch Lead" Group** in the admin holding those six, and add Luis to it.
3. Gate each Lead action with `user_passes_test(lambda u: u.is_superuser or u.has_perm(...))`,
   matching the existing idiom.
4. Ordinary dispatchers keep `_is_staff` access to the checklists themselves — opening, confirming
   rows and writing exceptions are floor work, not Lead work.

This keeps the two existing tiers intact, is grantable and revocable without a deploy, and is exactly
how the sandbox permission is already administered.

### 12.4 Session and request context

`ThreadLocalMiddleware` (`reservations/middleware.py:95`, registered `settings.py:122`) makes
`request.user` available to signals, which is what lets `AuditLog` attribute changes made deep in
model code. Any Shift-System write inside a request inherits this for free; anything written from the
30-minute scheduler thread has no user and should record `None` rather than a placeholder — the
convention `close_task(auto=True)` already follows.

### 12.5 One security note outside this project's scope

`ghl_integration/views.py:76 ghl_webhook` is `@csrf_exempt` with **no signature or shared-secret
verification**, unlike `payment/webhook.py:32` which calls `stripe.Webhook.construct_event`. Anyone
who learns the URL can mark leads replied, cancel follow-up sequences and set `sms_opt_out`. Not a
blocker for the Shift System; worth its own ticket.

---

## 13. Source-of-Truth / Scheduling Boundary

### 13.1 Authoritative sources today

| Fact | Source of truth | Resolver / accessor |
|---|---|---|
| **Chauffeur schedule** (who can drive on date X, in what window) | `drivers.DriverWeeklySchedule` + `drivers.DriverDateOverride` | `drivers/availability.py:113 resolve_effective_availability`, via `Driver.get_effective_availability` (`drivers/models.py:406`) |
| **Reservation / leg assignment** (who drives leg N) | `reservations.Leg.driver` | **Written only through** `dispatching/assignment.py:126 set_leg_driver` |
| **Proposed assignment on a held day** | `reservations.DraftAssignment.proposed_driver` | `_apply_draft_overlay`; three-state semantics documented at `reservations/models.py:3917-3925` |
| **Office staff schedule** | `ops.StaffWeeklySchedule` + `StaffScheduleOverride` + `StaffExtraShift` (+ `StaffOnCall`) | `ops/scheduling.py:108 resolve_staff_schedule` |
| **Actual clocked-in status** | `ops.TimeClockShift` (`clock_out_at IS NULL`) | `ops/services.py:206 get_open_shift` |
| **Break status** | `ops.TimeClockBreak` (`break_end_at IS NULL`) | `TimeClockShift.open_break` (`ops/models.py:526`) |
| **Driver confirmation** | `reservations.Leg.status` leaving `in-progress`, with provenance in `LegStatus.updated_by` | `drivers/views.py:912 accept_job` |
| **Operator (farm-out) acceptance** | `reservations.Leg.operator_accepted_at` | `drivers/operator_views.py` |
| **Conflict / task status** | `ops.OperationalTask.status` | `ops/services.py:102/117`; scanners in `ops/tasks.py` |
| **Board watch flag** | `reservations.LegKeoi` (active = `closed_at IS NULL`) | `dispatching/keoi_views.py`, `reservations/keoi.py` |
| **Flight data** | `reservations.Flight` (`best_arrival_local()`:2961) | `ops/tasks.py:1981 _apply_flight_update` from AeroAPI |
| **Flight alert reviewed** | `OperationalTask(flight_verify).status`; `Leg.pickup_change_ack_at` | `dispatching/views.py:7233`, `:7283` |
| **Vehicle plan for a day** | `drivers.DriverVehicleAssignment` | `dispatching/views.py:4160 apply_day_setup` |
| **Scheduler tuning** | `dispatching.SchedulerSettings` (pk=1) | `get_settings()` |

Office-staff scheduling and chauffeur scheduling are **parallel, never shared**: different models,
different resolvers, different pages (`staffing_board` vs `inhouse_schedule`). The only coupling is
cosmetic — `ops/scheduling.py:25` imports a time formatter from `drivers/availability.py`. Nothing in
the scheduler, Day Setup or Day-Builder reads any `ops.Staff*` model.

### 13.2 The propose-only architecture the Shift System must not disturb

Three mechanisms enforce it today:

1. **A single write door.** `set_leg_driver` (`dispatching/assignment.py:126`) decides overlay vs
   live, stamps `driver_assigned_by/at` and `leg._reassigned_by`, and wraps live writes in
   `sanctioned_live_write()`.
2. **A runtime tripwire.** `_leg_driver_tripwire` (`:171`), connected in `DispatchingConfig.ready()`
   (`dispatching/apps.py:13`), raises `SandboxLeakError` in DEBUG/tests and logs an error in
   production on any other live `Leg.driver` write while a day is held.
3. **Advisors that return data only.** `conflict_advisor.py` ("STRICTLY READ-ONLY AND ADVISORY-ONLY",
   docstring line 5), `assignment_pipeline.py:48-55`, `day_setup.py:1-11`, `rebalance_advisor.py:24`,
   `fold_advisor.py:14`, `standby_mints.py:12-15`, `handoff_chain.py:5-7`. `DayPlan`
   (`dispatching/models.py:393`) is a job ledger whose docstring states the plan *"never writes a Leg
   or a DriverVehicleAssignment row — v1 is propose-only"*.

The scheduling-redesign work is **fully merged into `main`** (`git branch --merged main` lists
`scheduling-redesign/phase-1`; `main..scheduling-redesign/phase-1` is empty). The Day-Builder ships
**off** (`SchedulerSettings.opt_enabled` default `False`, `dispatching/models.py:215`). The live Day
Manager (D13) and one-click auto-apply (D14) are **documented but not built** —
`docs/scheduling-redesign/06_DAY_MANAGER.md`, `01_REVISED_SCOPE_AND_PLAN.md:54-55`.

One known exception to the front door, already documented as D14 gap (1): `auto_assign_drivers`'s
apply branch (`dispatching/views.py:13715-13726`) sets `leg.driver` inline and omits
`_reassigned_by` / `_status_change_user`. Not caused by, and not to be fixed by, this project — but
worth knowing that ops-task attribution is lost on that path.

### 13.3 What the Shift System must never become authoritative for

* **Who drives a leg.** No writes to `Leg.driver`, ever — not even "unassign so the checklist goes
  green".
* **Leg status.** No writes to `Leg.status`; confirmation is the chauffeur's or the dispatcher's act
  on the existing surfaces.
* **Chauffeur availability or schedules.** Read `drivers/availability.py` if ever needed; never write.
* **Vehicle plans, drafts, snapshots, `DayPlan`, scheduler dials.** Out of scope entirely.
* **Whether a conflict exists.** `OperationalTask` and the advisors own that. The checklist counts
  and links; it does not judge.
* **Who is working.** Derived from `TimeClockShift` on every read.
* **Office schedules.** `resolve_staff_schedule` stays the resolver; the Shift System reads it.

### 13.4 Where the proposal, as written, would cross the boundary

| Proposal element | Risk | Required constraint |
|---|---|---|
| "System rows become green when count = 0" | Tempting to cache the count on the row | Compute live; persist only into `counts_snapshot` at completion, as history |
| "Board handoff: open conflicts, chauffeurs we're waiting on" | Tempting to copy task text into the handoff | Store ids; re-read live objects for display (§10.2) |
| "Lane A ownership" | Tempting to store "currently working" on the lane row | `LaneAssignment` stores the lane only; presence always re-read (§8.3) |
| "Mark someone unavailable" | Tempting to write to the office schedule | A per-shift availability flag on the Shift System's own model, never a `StaffScheduleOverride` |
| "Call ring order" | Tempting to present it as system state | Display-only until a RingCentral integration exists (§8.5) |
| "Opener begins as A" | Tempting to re-declare who the opener is | Read `ops/coverage.py:544 _resolve_duty`; the duty already lives on the schedule |

---

## 14. Performance Risks

Context: the dashboard is the heaviest page in the app and was brought from ~20 s to ~2 s on a
240-leg day as recently as 2026-09-08
(`docs/release-notes/2026-09-08-busy-days-open-quickly.md`) by adding gzip and eliminating 23,000
hidden `<option>` elements. `reservations/middleware.py:33` logs any request over 500 ms.
Anything added to that page starts from a hostile baseline.

### HIGH

**H1 — A Floor Card rendered inline on the dispatch page.**
The board already evaluates one large leg queryset with ~10 `select_related` and 6
`prefetch_related` entries (`dispatching/views.py:150-190`) plus an `Exists()` annotation. Adding
per-request roster, clock, schedule-resolution and task-count work to that view puts new queries on
the critical path of the slowest page.
*Mitigation:* render the card shell server-side with no data, and fill it from **one** JSON endpoint
polled at 60 s, exactly as the advisor rail does (`_recovery_advisor.html:118 POLL_MS = 60000`,
compute cached 120 s at `advisor_views.py:44`) and as the driver portal does
(`drivers/views.py:790 board_state`, 60 s). Cache the endpoint's payload **globally** (not per user)
for 30–60 s so five dispatchers share one computation.

**H2 — Recomputing lanes per user per request.**
Lane derivation touches the roster, open shifts, the schedule resolver and the lane table. Per-user,
per-request is the wrong shape.
*Mitigation:* compute the lane map once per poll cycle in the same cached payload as H1; recompute
immediately on a clock event (a small cache invalidation in `timeclock_action`), mirroring
`cache.delete(f"capacity_planner_{date}")` which already runs at 18 assignment sites.

**H3 — External API calls on a board render.**
RingCentral or Gmail counts fetched synchronously would couple the dispatch board to a third party's
uptime. `ops/tasks.py:207-235 _reposition_minutes` documents the cost lesson: a paid Distance Matrix
call per leg pair on every 30-minute scan had to be removed.
*Mitigation:* any external counter is fetched by the existing 30-minute thread, written to a small
row or cache key, and only ever read by the page. Never call out from a request.

### MEDIUM

**M1 — The four system counts on every checklist load.**
Four queries, all indexed (`leg_pickup_status_idx`, `idx_ops_type_status`), over one day's data.
Acceptable on a page opened a few times a shift; not acceptable inside a 60-second poll.
*Mitigation:* the checklist page computes them on load and on explicit refresh. If they are added to
the Floor Card, they join the shared cached payload.

**M2 — The unconfirmed-chauffeur aggregate.**
A grouped query over the day's legs joining `Driver` → `profile`.
*Mitigation:* `values("driver_id").annotate(Count("id"), Min("pickup_time"))` plus one `Driver`
fetch for the names — two queries, no N+1. Never iterate legs and touch `leg.driver.profile`.

**M3 — Lead view over a date range.**
Left unguarded, `ShiftException` and `ShiftChecklistRow` would N+1 per checklist.
*Mitigation:* the `select_related` + `Prefetch` in §11.2; clamp the range as `staff_kpis_view` does
(1–90 days).

**M4 — LocMemCache under three workers.**
Without `REDIS_URL`, each gunicorn worker has its own cache
(`business/settings.py:203-213`). A cached Floor-Card payload is computed up to three times, and a
cache-invalidation on clock-in only clears one worker's copy.
*Mitigation:* keep TTLs short (30–60 s) so staleness self-heals, and treat the cache as an
optimisation, never as state. `FlightRefreshTask` (`dispatching/models.py:320`) is the cautionary
tale: cross-worker state had to move to the DB.

**M5 — Polling cost multiplied by open tabs.**
Two to five dispatchers with the board open is 2–5 requests/minute for the Floor Card.
*Mitigation:* shared cache key (H1), plus `visibilitychange` pausing as the driver portal already
does (`_driver_scripts.html:291-327`).

### LOW

**L1 — Checklist writes.** A handful of small INSERT/UPDATEs per shift. Negligible.

**L2 — The completion summary.** Rendered server-side into a Copy button (§7.7). No outbound
request, no external dependency, no failure mode. This is the cheapest element in the whole
proposal.

**L3 — StaffActivity rows for shift events.** A few rows per shift against a table already taking
page views deduped at 30 minutes.

### Explicitly not recommended

* **No websockets / SSE.** `docs/dispatch_audit.md` already rejected them for this deployment:
  3 workers × 4 threads = 12 request slots, no Redis guaranteed, no channels layer.
* **No new background daemon.** Three threads already exist with advisory locks; anything periodic
  belongs in the 30-minute cycle (`ghl_integration/scheduler.py:87`).
  `docs/scheduling-redesign/04_PLANNER_AND_BUILD_PLAN.md` bans a new periodic daemon outright.
* **No recomputation of conflicts by the Shift System.** Read `OperationalTask`; the scanners already
  pay that cost.

---

## 15. POLICY DECISIONS REQUIRED

Twenty-four decisions. None is a technical question; each changes what gets built.

* **Items 1–7 block Phase 1.** Items 8 and 20 have defaults clear enough to proceed on unless
  overridden (see §17).
* **Items 9–16 block Phase 3** (lanes).
* **Items 17–24** can be answered at any point before the relevant phase. Item 24 is a follow-up to
  the 2026-09-12 scoping decision in §1.

Three decisions have already been settled by founder direction and are recorded rather than asked:
**the completion message is sent by a person, not a bot** (§7.7); **the open-task count is a Close
check, not an Open check** (§1); and **the Lead's 7-day view ships in Phase 1**, with lane-related
permissions deferred to Phase 3 (§16).

**POLICY DECISION REQUIRED — Floor roster scope**
Question: Who counts as a "dispatcher on the floor" for the Floor Card, the lane rota and the ring
order — every active staff login that is not a chauffeur or travel agent (which today includes
Abdalla and would include any future bookkeeping or admin account), or an explicitly flagged subset?
Why it matters: `ops/staff.py:21 office_staff_qs()` is subtractive by design ("there is no
`is_dispatcher` field by design"). Lane A succession, headcount-to-lane mapping and ring order are all
computed from this list, so one non-dispatcher account silently changes the lane count.
Recommended default: keep `office_staff_qs()` as the roster and add an opt-out flag
(`StaffProfile.is_floor_dispatcher`, default True) toggled on the existing Manage Time Clock rows.

**POLICY DECISION REQUIRED — Dispatcher-entered confirmation**
Question: When a dispatcher sets a trip to "Confirmed" from the board dropdown, does that satisfy the
"chauffeurs have confirmed" checklist row, or must the press come from the chauffeur's own login?
Why it matters: both paths write `Leg.status='confirmed'`; only `LegStatus.updated_by`
(`reservations/models.py:3612`) distinguishes them. If dispatcher-set counts, the row measures status
hygiene; if not, it measures actual chauffeur acknowledgement — and a dispatcher can no longer clear
the row by tidying the board.
Recommended default: only the chauffeur's own press clears it. Show dispatcher-set confirmations as a
separate, non-blocking line so the information is not lost.

**POLICY DECISION REQUIRED — Affiliate legs in the checklist**
Question: Are farmed-out legs in scope for the "unassigned" and "chauffeurs not confirmed" rows?
Why it matters: an affiliate leg is *assigned* (`Leg.driver` is the operator company) but never uses
the confirmation ladder — acceptance is `Leg.operator_accepted_at` (`reservations/models.py:1092`).
Counted naively, every affiliate leg reads unconfirmed forever. The conflict scanners already exclude
affiliates entirely (`ops/tasks.py:507`).
Recommended default: unassigned counts affiliate legs as assigned (they are covered). Confirmation
shows operator acceptance as its own informational line, not part of the blocking count.

**POLICY DECISION REQUIRED — Past-pickup unassigned legs**
Question: At Open Shift, does a trip whose pickup time has already passed and still has no chauffeur
count toward the "unassigned today" row?
Why it matters: the board counts it (`dispatching/views.py:212`); the scanner deliberately does not
(`ops/tasks.py:1180` filters `pickup_time__gte=local_now_time`). The two answers differ most at
7:15 AM, exactly when Board Safe is judged.
Recommended default: show them, listed separately as "already past pickup", so the opener sees them
but they do not block Board Safe. A missed 5 AM trip is a post-mortem, not a pending action.

**POLICY DECISION REQUIRED — Definition of "unreviewed flight alert"**
Question: Which of the four live flight-alert definitions (§3.4) does the checklist row count?
Why it matters: only two have a persisted "reviewed" state — the `flight_verify` task (closed by Mark
Reviewed) and the pickup-move ack (`Leg.pickup_change_ack_at`). The live badges
(`flight_timing_flag`, `flight_disruption_flag`) are recomputed per render and can never reach zero
by anyone reviewing anything, so a row built on them would never go green.
Recommended default: count open `flight_verify` tasks for the date plus legs with an unacknowledged
pickup-time change. Display the live badges as context only.

**POLICY DECISION REQUIRED — Tomorrow's conflicts**
Question: Should a nightly scan check tomorrow's board for driver conflicts and tight turns, or is it
acceptable that the Close Shift row only reflects conflicts raised by a flight movement?
(Note: following the 2026-09-12 scoping decision, Close is now the **only** shift carrying this row.)
Why it matters: `_scan_driver_overlaps` (`ops/tasks.py:984`) is `pickup_date=today` only. Future
conflicts appear solely via `_handle_future_driver_conflict` (`:804`) when a flight moves ≥5 minutes.
The Close row will therefore often read 0 on a board a dispatcher can see is tight — which teaches
the floor that the row means nothing.
Recommended default: ship Phase 1 with today's rule and label the row honestly ("conflicts raised so
far"). Add a tomorrow-board pass in Phase 2 if the zero proves misleading. Note the cost is bounded:
the scanner runs on precomputed drive tables, not paid API calls (`ops/tasks.py:207-235`).

**POLICY DECISION REQUIRED — Which human rows are mandatory**
Question: Are all five human rows (texts, email, GoHighLevel, phone app ready, missed calls reviewed)
required on both Open and Close, or does each shift get a different set?
Why it matters: rows that are always skipped train the floor to skip the rest, and the
no-silent-exceptions rule means every skipped row costs a written note.
Recommended default: all five on both shifts, with skipping requiring the same three-part exception
note as anything else. Make the set editable in settings so it can be trimmed after a month of real
use rather than guessed now.

**POLICY DECISION REQUIRED — Who may complete and who may reopen a checklist**
Question: May any clocked-in dispatcher complete an Open or Close checklist, or only the person
holding the opener/closer duty? Who may reopen a completed one?
Why it matters: completion is the accountability record the Lead view reads. If anyone can complete
it, the record says who did; if only the duty-holder can, a genuine handover mid-open becomes a
blocked screen.
Recommended default: any clocked-in dispatcher may complete, and the person is recorded. Only the
Lead or Abdalla may reopen, stamped (`reopened_by`, `reopened_at`) and shown in the Lead view.
**Proceeding on this default unless overridden.**

**POLICY DECISION REQUIRED — Lane A succession order**
Question: When Lane A becomes vacant, who takes the board — the eligible person who has been clocked
in longest, the person whose schedule assigns them the opener/closer duty, or whoever the Lead names?
Why it matters: both inputs already exist (`TimeClockShift.clock_in_at`; `ops/coverage.py:544
_resolve_duty`), and they disagree routinely — the assigned opener is explicitly allowed not to be
the earliest person in (`ops/coverage.py:522 _role_notes`).
Recommended default: the day's assigned opener starts as A if clocked in; thereafter the eligible
person clocked in longest, ordered `(clock_in_at, id)`, skipping anyone flagged never-A. A Lead
override always wins and is recorded with a reason.

**POLICY DECISION REQUIRED — Lane A and breaks**
Question: Does Lane A pass to someone else when A starts a break, and if so after how long?
Why it matters: breaks are recorded precisely (`TimeClockBreak`), so either rule is automatable, but
"nobody owns the board for 40 minutes" is exactly the failure this project exists to prevent.
Recommended default: A keeps the board for breaks under 30 minutes; past that it passes automatically
and the Floor Card says so. A returning from break does not automatically reclaim it.

**POLICY DECISION REQUIRED — Unexpected clock-out and forgotten clock-out**
Question: Does Lane A pass on any clock-out regardless of cause, and how should the Floor Card treat
someone whose shift has been open implausibly long?
Why it matters: a forgotten clock-out is only closed when a superuser opens Manage Time Clock or the
overview, and is then capped at 16 hours (`ops/services.py:455`). Until then the person reads as on
the floor and could hold Lane A after going home.
Recommended default: any clock-out passes the lane. The Floor Card flags a shift open more than 14
hours as "check this", and the Floor Card endpoint calls `auto_close_stale_shifts()` on the same lazy
basis the two superuser pages already do.

**POLICY DECISION REQUIRED — Clocking back in**
Question: If A clocks out and later clocks back in the same day, do they resume Lane A?
Why it matters: a new clock-in creates a brand-new shift row with no memory of the previous lane, so
"resume" would need to be written deliberately.
Recommended default: no. They rejoin as the newest person on the floor; the Lead can hand the board
back explicitly.

**POLICY DECISION REQUIRED — Simultaneous clock-in tiebreak**
Question: When two people clock in within the same second, who is treated as senior for lane
assignment?
Why it matters: ordering on `clock_in_at` alone is not deterministic, and an unstable lane map that
reshuffles between page loads would destroy trust in the card.
Recommended default: order by `(clock_in_at, id)`; if the schedule names one of them opener, that
person wins.

**POLICY DECISION REQUIRED — Never-A eligibility**
Question: Who may be marked never-A, and who sets that flag?
Why it matters: it is a per-person capability judgement that changes the succession chain.
Recommended default: a per-person flag on the Manage Time Clock roster, set by Abdalla or the Lead,
with the reason recorded in a free-text note.

**POLICY DECISION REQUIRED — Only never-A dispatchers remain on the floor**
Question: If everyone clocked in is flagged never-A, does the system force the board onto one of
them, or leave Lane A unowned?
Why it matters: forcing it makes the never-A flag meaningless; leaving it unowned means the board has
no owner during a live shift, which must be loud rather than silent.
Recommended default: leave Lane A unowned, show it red on the Floor Card, and put it at the top of
the Lead view. The Lead decides.

**POLICY DECISION REQUIRED — Remote dispatchers and lanes**
Question: May someone whose schedule marks them remote hold a lane at all, and specifically Lane A?
Why it matters: the app knows "remote" only from the planned schedule
(`StaffWeeklySchedule.location`), never from the punch — so the rule is only as accurate as the
roster, and a WFH day flipped to in-office is a one-off override that may or may not have been
entered.
Recommended default: remote dispatchers may hold lanes but not Lane A, unless the Lead overrides.
Review after a month, because the underlying signal is planned rather than observed.

**POLICY DECISION REQUIRED — Which calendar day a midnight-spanning shift belongs to**
Question: If a shift runs past midnight, does the person close the outgoing day's checklist, open the
new day's, both, or neither?
Why it matters: checklists are keyed `(date, kind)`; the on-call window is 00:00–06:00
(`ops/models.py:946`); the time clock already handles the crossing correctly for hours but expresses
no opinion about ownership of the checklist.
Recommended default: checklists follow the calendar day. Whoever is on when the day rolls over closes
the outgoing day and opens the incoming one; if nobody is on, the first person in owns both, and the
Lead view shows the Open as late rather than missing.

**POLICY DECISION REQUIRED — Gate times**
Question: Are Board Safe 7:15 AM and Open Complete 8:00 AM the same every day of the week, is there a
Close target time, and who may change them?
Why it matters: the targets are frozen onto each checklist at open time so history stays honest, so a
change applies from the next shift forward, not retroactively.
Recommended default: one set of times for every day to start; add per-weekday overrides only if
weekends genuinely differ. Editable by Abdalla and the Lead. Set a Close target as well, so "closed
at 11:40 PM" reads as late rather than merely late-looking.

**POLICY DECISION REQUIRED — Exception ownership constraints**
Question: Must the owner of an outstanding exception be someone scheduled to work before the stated
next-action time?
Why it matters: `resolve_staff_schedule` can answer whether the named owner is even working, so the
system can warn — but enforcing it would block a legitimate "Abdalla owns this" note.
Recommended default: the owner must be a named person; if they are not scheduled before the next
action time, the checklist warns and asks for confirmation rather than refusing.

**POLICY DECISION REQUIRED — The Dispatch Lead access level**
Question: Should Luis receive a new Dispatch Lead permission group, or full superuser access?
Why it matters: superuser exposes revenue (`dispatching/views.py:86 can_view_revenue`), payroll, pay
rates, refunds, commissions, travel-agent management and staff metrics. There is currently no tier
between dispatcher and founder.
Recommended default: a "Dispatch Lead" Django group holding six new permissions (override lane,
reassign lanes, reopen checklist, review checklists, edit exception ownership, mark unavailable),
granted in the admin exactly as `use_schedule_sandbox` is today. No new boolean tier in code.
**Phase 1 declares only the three permissions that have something to gate** (`review_checklists`,
`reopen_checklist`, `edit_exception_owner`); the lane-related three arrive with lanes in Phase 3.
**Proceeding on this default unless overridden.**

**POLICY DECISION REQUIRED — The completion message and who sends it**
Question: Confirmed direction is that a person sends the completion summary into the team's WhatsApp
group rather than a bot. Does that hold for both Open and Close, and does the message name people?
Why it matters: it decides whether anything automated is built at all. A Copy button is trivial; a
WhatsApp sender needs the WhatsApp Business Platform, a registered number and approved templates
(§7.7), and templates do not suit variable-length shift summaries.
Recommended default: human sends it, both shifts, names included, in the voice of the existing
release-note "Send this to the team" blocks. Build no sender.

**POLICY DECISION REQUIRED — Ring order by headcount**
Question: What is the intended RingCentral ring order for one, two, three, four and five dispatchers
on the floor?
Why it matters: the app cannot read or set RingCentral (§7), so it can only display the intended
order for the floor to match manually. Without the table the Floor Card has nothing to show.
Recommended default: none proposed — this is a phone-system decision. Supply it as a simple
headcount-to-order table and it becomes settings data.

**POLICY DECISION REQUIRED — Queue warning thresholds**
Question: At what depth and age should each queue (tasks, texts, email, GoHighLevel) turn red on the
Floor Card?
Why it matters: thresholds drive only colour, so they are safe to change at any time — but a
threshold set too low produces a permanently red card that the floor learns to ignore, which is the
exact failure mode the conflict flags had before 2026-08-27.
Recommended default: none proposed. Start with no thresholds, watch a fortnight of real numbers on
the card, then set them from observed data.

**POLICY DECISION REQUIRED — A narrowed morning-conflict row**
Question: Now that the open-task count is off the Open checklist (§1 scoping decision), should Open
carry a much narrower replacement — only CRITICAL `driver_conflict` tasks on trips whose pickup is
before a configurable hour — or nothing at all?
Why it matters: "a chauffeur physically cannot make a 9:00 AM pickup" is genuinely a board-safety
question, and Board Safe is the gate that claims the board is safe to operate. The full count is
noise at 7:15 AM; a morning-only critical slice would typically be one or two rows, not forty. The
cost is one extra filter on a query that already exists. The risk is re-introducing a row the floor
learns to wave through.
Recommended default: ship Phase 1 with nothing, because the board already shows those conflicts in
red on the trips the opener is working. Revisit after a month, and only if a morning conflict is
actually missed. **Not a Phase 1 blocker either way.**

---

## 16. Phased Implementation Plan

Four phases, each shippable on its own and each leaving the board, the driver app and the scheduling
tools untouched. The ordering principle is smallest useful system first, not architectural
completeness: Phase 1 fixes the floor's own process and gives the Lead visibility over it, and every
later phase is optional.

**Sequencing note (founder direction, 2026-09-12, revised same day).** The Lead view was briefly
deferred to last, then pulled back into Phase 1 once its true cost was clear: it is a read query over
tables Phase 1 already writes, rendered with the page pattern `ops/views.py:2944 staff_kpis_view`
already establishes. Building it later would mean writing the same view against the same data for no
saving. **The Lead's permissions are split, though** — Phase 1 declares only the three that have
something to gate (review checklists, reopen a checklist, edit exception ownership); the three that
belong to lanes (override Lane A, reassign lanes, mark unavailable) are declared in Phase 3 with the
features they govern, rather than shipping permissions for functionality that does not exist.

### Phase 1 — Open / Close checklists, exceptions, and the Lead's view

**Goal.** Make the opening and closing standard real, give every unresolved item an owner and a next
action, and let the Dispatch Lead see whether the last seven days were actually run that way — all
without touching anything a dispatcher already uses.

**What gets built.**
* `Open Shift` (three live system counts) and `Close Shift` (the same three against tomorrow, plus
  the open conflict/tight-turn count), each with drill-through links, five tap-to-confirm human rows,
  the two gate stamps, and the exception form. Which rows appear on which shift is settings data, not
  code (§1 scoping decision).
* Exception notes with what / owner / next action, optionally referencing a task, leg or watch flag,
  carried forward to the next relevant checklist and requiring acknowledgement.
* A completion summary rendered into a Copy button for WhatsApp (§7.7).
* **The Lead's 7-day view**: one row per shift — who opened and when, Board Safe hit or missed, Open
  Complete hit or missed, who closed and when, exceptions with their owners, carried-forward count,
  outstanding ownership. Same range controls as the existing staff-metrics pages.
* **A "Dispatch Lead" Django group** carrying three permissions: `review_checklists`,
  `reopen_checklist`, `edit_exception_owner`. Luis is added to it; nobody's existing access changes.
* **Django admin registration** for the four new models, so Abdalla can inspect and repair checklist
  rows directly. Near-zero cost and the right place for corrections.

**Why the Lead view is in Phase 1 and not later.** It reads tables Phase 1 already writes and renders
them with a page pattern that already exists, so deferring it saves nothing and costs Luis his
visibility for the whole adoption period — which is the period where seeing whether the routine is
being followed matters most.

**Permissions that are NOT declared here.** `override_lane`, `reassign_lanes` and `mark_unavailable`
govern features that arrive in Phases 2–3 and are declared with them. Declaring a permission for
functionality that does not exist is how permission sets rot.

**Existing components reused.** The board's leg queryset and `driver IS NULL` test; the
`detect_leg_flags` "not confirmed" rule (`dispatching/utils.py:741`); `OperationalTask` counts and the
`OPEN_STATUSES` constant; `dismiss_flight_review` / `acknowledge_time_change` as the flight-reviewed
states; `TimeClockShift` for who is on; `office_staff_qs()` and `resolve_staff_schedule()` for the
roster and duties; `ops/views.py:2944 staff_kpis_view` + `ops/kpis.py:79 resolve_range` for the Lead
view's shape and range controls; the `Min("created_at")`-per-user aggregate at `ops/views.py:3200`;
the `user.has_perm` idiom from `dispatching/keoi_views.py:189` and `dispatching/assignment.py:60`;
`ops/views.py` + `dispatching/urls.py` + `dispatcher_navbar.html` for routing and nav; jazzmin admin
(`ops/admin.py`) for inspection and repair.

**New data / schema.** `ops.ShiftChecklist`, `ops.ShiftChecklistRow`, `ops.ShiftException`,
`ops.ShiftSettings` (singleton), plus new `StaffActivity.ActionType` values and three
`Meta.permissions`. One migration. The group itself is admin data, not a migration.

**Files / areas likely affected.** New: `ops/shift_checks.py` (the four count functions and the one
shared leg-set helper), `ops/shift_services.py` (open, confirm, document, complete, reopen, carry
forward), `ops/shift_views.py` or additions to `ops/views.py`, templates
`dispatching/templates/dispatching/shift_open.html`, `shift_close.html` and `shift_lead.html`, tests
under `ops/tests/`. Edited: `dispatching/urls.py` (routes), `dispatcher_navbar.html` (two dispatcher
links plus one Lead link), `ops/models.py`, `ops/admin.py`.

**External dependencies.** None.

**Risk.** Low. Read-only against the board and the task queue; the only writes are the checklist's
own rows. No background job, no external call, no change to any existing query.

**What changes for the dispatch floor on day one.** Two new links beside Clock. The opener works the
same board and the same task queue, but the counts are in one place, leftovers get a named owner, and
the message to the group is generated rather than composed from memory. Nothing blocks anyone: an
incomplete checklist blocks no board action — it shows as incomplete in the Lead view and nowhere
else. Luis can see the last seven days from the first week, which is the point of the promotion.

**What this phase deliberately does not solve.** Who owns the board mid-shift; lanes; the Floor Card;
ring order; any visibility into RingCentral, Gmail or the GHL inbox; a nightly tomorrow-conflict
scan. The Lead gets visibility in this phase, but no lever to pull beyond reopening a checklist and
reassigning an exception's owner.

---

### Phase 2 — The Floor Card

**Goal.** Answer "who is working and what is each person responsible for right now" on the page
dispatchers already keep open.

**What gets built.** A card on the dashboard showing: who is clocked in, who is on break, planned
office/WFH, each person's lane and its queues (in Phase 2 read from the schedule's opener/closer duty
and the lane table, not yet auto-assigned), task depth and oldest-item age, leads awaiting a human
reply, the human-confirmed state of the outside queues from the current checklist, and the intended
ring order for today's headcount. One cached JSON endpoint, polled at 60 s, shared across viewers.

**Existing components reused.** `TimeClockShift` / `TimeClockBreak`; `resolve_staff_schedule` for
location and duty; `OperationalTask` aggregates including the `Min("created_at")` pattern already at
`ops/views.py:3200`; `Lead.needs_human_follow_up` via the `ops/leads_board.py` classification; the
advisor rail's poll-and-cache shape (`advisor_views.py:44`, `_recovery_advisor.html:118`); the
`visibilitychange` pause from the driver portal.

**New data / schema.** Ring-order and threshold entries in `ShiftSettings`; `ops.StaffProfile` for
the per-person flags. The card itself stores nothing.

**Files / areas likely affected.** New: `ops/floor_card.py`, a JSON endpoint, a template include
under `dispatching/templates/dispatching/includes/`. Edited: `legs_filter.html` (one include),
`dispatching/urls.py`, `ops/views.py:4589 timeclock_action` (invalidate the cache on a clock event).

**External dependencies.** None.

**Risk.** Low to medium — entirely because of where it lives. The dashboard is the slowest page in the
app and was optimised as recently as 2026-09-08. Mitigations in §14 H1/H2/M4 are not optional:
shell rendered server-side with no data, one shared cache key, short TTL, no external calls.

**What changes for the dispatch floor on day one.** Everyone can see who is actually on, who is on
break, how deep the task queue is and how old its oldest item is, without asking. The ring order is
visible rather than remembered.

**What this phase deliberately does not solve.** Automatic lane assignment and Lane A succession; the
board handoff record; real text, call and email depth.

---

### Phase 3 — Lanes, Lane A and the board handoff

**Goal.** Make ownership of the board and of each queue explicit, automatic where the system can know
it, and recorded when it changes hands.

**What gets built.** Lane derivation from the clock and the lane table; Lane A succession per the
answers to policy decisions 9–16; a Lead override with a reason; never-A flags; and a short board
handoff form pre-populated from open conflict tasks, unconfirmed chauffeurs, changes since opening,
active watch flags, and the outgoing person's in-progress tasks, with free text for everything else.

**Existing components reused.** `TimeClockShift` events; `_resolve_duty` for the opener seed;
`OperationalTask` and `LegKeoi` for the pre-populated sections; `Leg.history` and `AuditLog` via
`dispatching/leg_timeline.py:551` for "changes since opening"; the partial-unique-constraint idiom for
"one live row".

**New data / schema.** `ops.LaneAssignment`, `ops.LaneHandoff`, lane table entries in
`ShiftSettings`, `never_lane_a` on `StaffProfile`, plus the three remaining Lead permissions
(`override_lane`, `reassign_lanes`, `mark_unavailable`) declared alongside the features they govern
and added to the group created in Phase 1.

**Files / areas likely affected.** New: `ops/lanes.py` (the succession function and the eligible-set
query), a handoff view and template. Edited: the Floor Card endpoint, `timeclock_action` (lane
recalculation on clock events), `ops/models.py`.

**External dependencies.** None.

**Risk.** Medium. Almost every policy decision in §15 lands here, and an ambiguous succession rule
produces a card the floor stops believing. The technical risk is small; the operational risk is real.

**What changes for the dispatch floor on day one.** Every person on the card carries a lane, the
board has a named owner at all times, and a change of owner produces a short written handoff instead
of a verbal one.

**What this phase deliberately does not solve.** Anything requiring RingCentral, Gmail or the GHL
inbox; setting the actual phone ring group.

---

### Phase 4 — Outside queues and thresholds

**Goal.** Replace the human-confirmed rows with observed numbers wherever the cost is justified.

**What gets built.** A RingCentral receiver for call and message events feeding text depth, oldest
unanswered and missed calls; a read-only Gmail unread count for one label; real red thresholds on the
Floor Card; optionally a richer completion summary.

**Existing components reused.** The inbound-webhook pattern from `payment/webhook.py:27` (with
signature verification, unlike the GHL webhook); the 30-minute scheduler for any polled counter; the
existing env-var-gated credential convention.

**New data / schema.** Small counter rows or cache keys per queue; threshold values in
`ShiftSettings`.

**Files / areas likely affected.** New integration module(s) under `ops/` or a new small app; a
receiver route; additions to `ghl_integration/scheduler.py:87 _run_batch_tasks`.

**External dependencies.** RingCentral developer account and webhook configuration; Google Cloud
project and OAuth for Gmail read-only. Both are outside the repo and outside this project's control.

**Risk.** Medium to high, almost entirely external: credentials, rate limits, token refresh, and the
discipline that an outage in any of them can never touch the dispatch page (§14 H3).

**What changes for the dispatch floor on day one.** The queue numbers on the card become observed
rather than asserted, and the corresponding checklist rows can go green on their own.

**What this phase deliberately does not solve.** Setting the RingCentral ring group from the app; any
automated message to the team's WhatsApp group.

---

## 17. Recommended Phase 1

Build the Open and Close checklists with exceptions, plus the Lead's 7-day view. Nothing else — no
lanes, no Floor Card, no external integration.

### What users would see

**Dispatchers.** Two new links beside Clock in the dispatcher navigation: **Open Shift** and **Close
Shift**.

The Open Shift page shows, in this order: any notes carried over from the previous close, each
needing one tap to acknowledge; **three** system rows with live counts and a link into the board for
that day; five human rows with a Confirm button each; and the two gate stamps.

* *Unassigned trips today* — count, linking to the board filtered to unassigned.
* *Chauffeurs not confirmed* — **grouped by chauffeur** ("George — 2 trips, first at 9:00 AM"), not
  one row per leg.
* *Flight alerts not reviewed* — open flight-verification tasks plus unacknowledged pickup-time
  changes, linking to each.

**No open-task count at Open** (§1 scoping decision, 2026-09-12). The task queue is large and is
worked through the shift, not at the gate; conflicts still appear as red flags on the board the
opener is already working.

A system row turns green by itself when its count reaches zero. It cannot be ticked by hand. A row
that is not zero at completion requires a note: what is outstanding, who owns it, what happens next.

Close Shift runs the same three checks against tomorrow **plus a fourth** — tomorrow's open conflict
and tight-turn tasks, where the volume is naturally small — with the same human rows and the same
note rule.

On completion the page shows a short summary and a **Copy** button. A dispatcher pastes it into the
team's WhatsApp group.

**The Dispatch Lead.** One page, reachable only by the Lead group and Abdalla: the last seven days,
one row per shift — who opened, when, whether
Board Safe and Open Complete were hit, who closed, when, the exceptions with their owners, and what
carried forward.

### What would be automated

* The four counts, live, every time the page loads.
* Grouping unconfirmed trips by chauffeur.
* Green-at-zero for system rows.
* Carrying unresolved exceptions into the next relevant checklist.
* Gate-time comparison and stamping.
* Composing the completion summary text.
* Recording who confirmed each row, who completed the checklist, and who reopened one.

### What would remain manual

* The five outside-queue rows: texts, email, GoHighLevel, phone app ready, missed calls reviewed.
* Deciding who owns an exception and what the next action is.
* **Sending** the completion message into WhatsApp.
* Everything on the board itself: assigning, calling, confirming, resolving.

### What database additions would be required

One migration in `ops`:

* `ShiftChecklist` — one per `(date, kind)`, with the two gate stamps, completion and reopen
  attribution, the frozen gate targets, and a completion-time snapshot of the four counts kept as
  history only.
* `ShiftChecklistRow` — one per check per checklist, with state, confirmer and timestamp.
* `ShiftException` — what, owner, next action, optional references to a task, leg or watch flag,
  carry-forward link and acknowledgement.
* `ShiftSettings` — singleton, holding the gate targets, the per-shift row set, and the summary
  template.
* Three `Meta.permissions` (`review_checklists`, `reopen_checklist`, `edit_exception_owner`) and new
  `StaffActivity.ActionType` values for shift events. The "Dispatch Lead" group itself is admin data,
  created once by hand.

No changes to any existing model, no new index on `Leg` or `OperationalTask` (the needed ones exist:
`leg_pickup_status_idx`, `idx_ops_type_status`).

### What existing code would be reused

| Need | Reused from |
|---|---|
| Unassigned legs | `dispatching/views.py:150` leg set + `driver IS NULL` |
| Not-confirmed rule | `dispatching/utils.py:741` inside `detect_leg_flags` |
| Chauffeur provenance | `reservations.LegStatus.updated_by` |
| Flight alerts and their reviewed state | `ops/tasks.py:620` scanner; `dispatching/views.py:7233`, `:7283` |
| Conflict and tight-turn counts | `ops.OperationalTask` + `OPEN_STATUSES` |
| Queue depth and oldest age | `ops/views.py:3200` aggregate pattern |
| Who is on / on break | `ops/services.py:206 get_open_shift` |
| Roster, duties, office vs WFH | `ops/staff.py:21`, `ops/scheduling.py:108`, `ops/coverage.py:544` |
| Lead view shape and range controls | `ops/views.py:2944` + `ops/kpis.py:79` |
| Permission mechanism | `user.has_perm` as in `dispatching/keoi_views.py:189` |
| Settings-page precedent | `dispatching/models.py:10` + `views.py:15969` (without the module-global cache) |
| Routing, templates, nav, admin | `dispatching/urls.py`, `dispatching/templates/dispatching/`, `dispatcher_navbar.html`, `ops/admin.py` |
| Summary copy voice | `docs/release-notes/README.md` "Send this to the team" rules |

### What should explicitly wait

* **Lanes and Lane A**, with their three remaining Lead permissions. Eight policy answers are
  outstanding; nothing is lost by running Phase 1 without them, and the checklists plus the Lead view
  will show whether board ownership is actually the gap.
* **The Floor Card.** It lives on the slowest page in the app and is worth doing once, after the
  checklist data exists to fill part of it.
* **Any RingCentral, Gmail or WhatsApp integration.** Human confirmation is honest and free; the
  integrations are expensive and, in WhatsApp's case, structurally unsuited to free-form summaries.
* **A nightly tomorrow-conflict scan.** Only worth adding if the Close row's usual zero proves
  misleading in practice.
* **Any change to the four divergent unassigned-leg definitions in existing code.** Introduce one
  shared helper for the checklist; do not refactor the board as part of this project.

### Blocking questions before writing Phase 1 code

**Seven decisions, all in §15:** floor roster scope (1), dispatcher-entered confirmation (2),
affiliate legs (3), past-pickup legs (4), the flight-alert definition (5), tomorrow's conflicts (6 —
now a Close-only row), and which human rows are mandatory (7).

Two more have recommended defaults clear enough to proceed on unless overridden:

* **Decision 8 (who may complete or reopen).** Proceeding as: any clocked-in dispatcher may complete;
  only the Lead or Abdalla may reopen, stamped and shown in the Lead view.
* **Decision 20 (the Lead access level).** Proceeding as: a "Dispatch Lead" Django group, **not**
  superuser — superuser would hand Luis revenue, payroll, pay rates, refunds and commissions
  (`dispatching/views.py:86 can_view_revenue` and the money pages behind it). Say so if you want the
  other answer; it changes one line of gating, not the design.

Everything else waits for its phase.

---

## Appendix — Documentation accuracy notes

Encountered while auditing; none blocks this project, each is a small cleanup ticket.

1. **`ops/OPS_SYSTEM.md` is stale.** It lists `ops/escalation.py` (line 100) which does not exist;
   states the ops views are superuser-only (line 110) when the queue and clock are `_is_staff`; and
   describes ntfy escalation that was removed.
2. **`systems_audit.md`** likewise describes an escalation engine at `ops/escalation.py:17`.
   NOT FOUND IN CODEBASE.
3. **Dead ntfy stubs.** `reservations/utils.py:643,658,887` are no-ops still called from six places;
   `send_lead_notification` and `send_driver_status_notification` build full messages before calling
   them.
4. **`dispatching/management/commands/dispatch_alerts.py`** has no automated trigger in the repo,
   writes `.dispatch_alerts_sent.json`, and sends nothing.
5. **`dispatching/flight_tracker.py`** (FlightLabs / AviationEdge) appears unused; all live paths use
   `dispatching/aeroapi_service.py`.
6. **`dispatching/rest_advisor.py:27 build_rest_advisories`** has no production caller, only tests.
7. **`SchedulerSettings.inter_job_buffer`** (`dispatching/models.py:117`) is documented as having no
   effect but is still editable on the Tuning page.
8. **`SchedulerSettings._settings_cache`** is a per-process module global with no cross-worker
   invalidation — a Tuning save reaches only the worker that served it until restart.
9. **`ghl_integration/views.py:76 ghl_webhook`** has no signature verification (§12.5).
10. **`LegStatus.STATUS_CHOICES`** includes `assigned`, which nothing writes, and omits `cancelled`,
    which `Leg.status` allows.
11. **`HistoricalReservation`** exists in migration `0076` but `Reservation` has no
    `HistoricalRecords()` today.
12. **`ops/coverage.py:139 day_coverage` / `:333 week_coverage` / `:305 _cell`** are called only by
    `ops/tests/test_coverage.py` — the documented tiered-target risk engine is not what the staffing
    board renders.

---

*Prepared 2026-09-12 against `main`. Read-only: no application code, migration, setting,
configuration or data was modified in producing this report.*
