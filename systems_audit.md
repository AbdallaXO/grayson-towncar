# Grayson Towncar — Operations & SOP Audit

## Context

Owner of Grayson Towncar (Orlando luxury transportation) wants to systematize the business so daily operations don't require him. The Django codebase already has substantial dashboards, an `ops/` task layer shipped in Mar 2026, AeroAPI flight tracking, Stripe payments, Twilio confirmation SMS, GHL lead automation, and a commission/agency program. The goal is **delegation, accountability, and removal of owner-as-memory** — not a rebuild.

**Headline finding:** the bones are mostly here. The biggest leverage isn't new models, it's: (1) **turning on the escalation engine you already shipped and disabled** ([ops/tasks.py:254-255](ops/tasks.py#L254-L255)), (2) **closing the communication-attempt logging loop** for SMS confirmations and call/text outreach, (3) **a single "manage-by-exception" dashboard** that surfaces overdue tasks + unassigned legs + unpaid + unverified flights + payouts due in one place, and (4) **eliminating duplicate-detection-as-separate-page** by surfacing duplicates inside the schedule builder.

---

## A. Codebase map (operations-relevant)

### Apps
| App | Role |
|-----|------|
| [business/](business/) | Django project (settings, root URLs) |
| [reservations/](reservations/) | Core domain: `Reservation`, `Leg`, `Flight`, `Cruise`, `AuditLog`, `LegStatus`, payment-status `@cached_property`s |
| [dispatching/](dispatching/) | Dashboards, schedule builder, confirmations, payment portal, swap optimizer, AeroAPI service, payout views, analytics |
| [drivers/](drivers/) | `Driver`, `DriverWeeklySchedule`, `DriverDateOverride`, `FleetVehicle`, `DriverVehicleAssignment`, plus `drivers/availability.py` resolver (single source of truth as of commit `1434a41e`) |
| [ops/](ops/) | `OperationalTask`, `CommunicationAttempt`, `StaffActivity`, `EmailLog`; scanners, escalation, signals, middleware, KPIs |
| [payment/](payment/) | `Payment` model, Stripe webhook ([payment/webhook.py](payment/webhook.py)) |
| [users/](users/) | `Customer`, `TravelAgent`, `Agency`, `CommissionPayout`, `AgencyCommissionPayout`, email helpers, newsletter subscribe |
| [ghl_integration/](ghl_integration/) | `Lead`, `FollowUpTask`, `LeadActivity`, `GHLSyncLog`, 30-min daemon scheduler |
| [services/](services/) | ~empty stub |
| [rates/](rates/) | Pricing matrix + `Vehicle` tier model |

### Key files (operations)
- Schedule build & dispatch dashboard: [dispatching/views.py:93 `index`](dispatching/views.py#L93), [dispatching/views.py:1738 `legs_list`](dispatching/views.py#L1738)
- Confirmations: [dispatching/views.py:3824 `confirmations_view`](dispatching/views.py#L3824), [dispatching/confirmation_sms.py:126 `leg_to_row`](dispatching/confirmation_sms.py#L126), [:197 `get_confirmation_message`](dispatching/confirmation_sms.py#L197), [:389 `send_confirmation_via_twilio`](dispatching/confirmation_sms.py#L389)
- Capacity planner + vehicle assignment: [dispatching/views.py:2275 `update_inhouse_vehicle_assignment`](dispatching/views.py#L2275), [:2345 `copy_vehicle_assignments`](dispatching/views.py#L2345), [:7704 `capacity_planner`](dispatching/views.py#L7704), [:8737 `smart_schedule_builder`](dispatching/views.py#L8737)
- Swap optimizer: [dispatching/swap_optimizer.py:40 `find_swaps`](dispatching/swap_optimizer.py#L40)
- Driver availability resolver: [drivers/availability.py:113 `resolve_effective_availability`](drivers/availability.py#L113), [:284 `is_pickup_within_window`](drivers/availability.py#L284), [:214 `format_availability_label`](drivers/availability.py#L214)
- Flight verification: [dispatching/aeroapi_service.py](dispatching/aeroapi_service.py), [dispatching/flight_tracker.py](dispatching/flight_tracker.py), [reservations/models.py:1430 `Leg.has_flight_time_mismatch`](reservations/models.py#L1430), [:1462 `get_flight_time_mismatch_display`](reservations/models.py#L1462), [dispatching/views.py:4313 `refresh_all_flights`](dispatching/views.py#L4313)
- Dispatch alerts: [dispatching/management/commands/dispatch_alerts.py](dispatching/management/commands/dispatch_alerts.py), [dispatching/utils.py:681 `detect_leg_flags`](dispatching/utils.py#L681), `.dispatch_alerts_sent.json` (filesystem dedupe)
- Duplicate detection: [dispatching/views.py:12589 `duplicate_reservations`](dispatching/views.py#L12589) (standalone page)
- Payment status: [reservations/models.py:469-515](reservations/models.py#L469-L515) `total_paid` / `amount_owed` / `payment_status`
- Stripe webhook: [payment/webhook.py:45 `stripe_webhook`](payment/webhook.py#L45)
- Payment reminder email: [users/emails.py:198 `send_payment_reminder_ajax`](users/emails.py#L198), [:23 `log_email_send`](users/emails.py#L23)
- Ops task model: [ops/models.py:15 `OperationalTask`](ops/models.py#L15), [:255 `CommunicationAttempt`](ops/models.py#L255), [:309 `StaffActivity`](ops/models.py#L309), [:360 `EmailLog`](ops/models.py#L360)
- Ops scanners: [ops/tasks.py:236 `generate_ops_tasks`](ops/tasks.py#L236), `_scan_flight_mismatches`, `_scan_driver_overlaps`, `_scan_unassigned_legs`, `_scan_unpaid_reservations`, `_scan_uncontacted_forms`, `_scan_confirmation_texts`, `_auto_close_resolved_tasks`, `_reopen_snoozed_tasks`, [:1185 `auto_refresh_flights`](ops/tasks.py#L1185)
- Escalation engine (shipped, disabled): [ops/escalation.py:17 `run_escalations`](ops/escalation.py#L17), referenced but commented out in [ops/tasks.py:254-255](ops/tasks.py#L254-L255)
- Ops services: [ops/services.py:14 `create_task`](ops/services.py#L14), [:94 `close_task`](ops/services.py#L94), [:152 `log_communication`](ops/services.py#L152)
- Task UI: [ops/views.py:37 `task_queue_view`](ops/views.py#L37), [:1521 `task_detail_view`](ops/views.py#L1521), templates [dispatching/templates/dispatching/task_queue.html](dispatching/templates/dispatching/task_queue.html), [task_detail.html](dispatching/templates/dispatching/task_detail.html)
- Owner-side dashboards: [ops/views.py:1586 `staff_metrics_view`](ops/views.py#L1586), [:2140 `revenue_kpis_view`](ops/views.py#L2140), [:2266 `staff_kpis_view`](ops/views.py#L2266), [:2801 `staff_detail_view`](ops/views.py#L2801), [:3306 `admin_tasks_view`](ops/views.py#L3306), [:3537 `admin_tasks_bulk_action`](ops/views.py#L3537)
- Commission/agency: [users/models.py:109 `TravelAgent`](users/models.py#L109), [:494 `CommissionPayout`](users/models.py#L494), [:535 `AgencyCommissionPayout`](users/models.py#L535), [:564 `Agency`](users/models.py#L564), payout views at [dispatching/views.py:12118 `agency_payouts_report`](dispatching/views.py#L12118) / [:12345 `process_agent_payout_view`](dispatching/views.py#L12345) / [:12388 `process_agency_payout_view`](dispatching/views.py#L12388) / [:12469 `process_bulk_payout_view`](dispatching/views.py#L12469)
- Lead/GHL: [ghl_integration/models.py](ghl_integration/models.py), [ghl_integration/scheduler.py:76 `_run_batch_tasks`](ghl_integration/scheduler.py#L76), [:131 auto_refresh_flights call](ghl_integration/scheduler.py#L131), [:141 generate_ops_tasks call](ghl_integration/scheduler.py#L141)
- Audit log: [reservations/models.py:2347 `AuditLog`](reservations/models.py#L2347), [:2452 `LegStatus`](reservations/models.py#L2452)
- NTFY push: [reservations/utils.py:616 `send_dispatch_alert_notification`](reservations/utils.py#L616)
- Management commands: [reservations/management/commands/backfill_agent_commissions.py](reservations/management/commands/backfill_agent_commissions.py), [normalize_airlines.py](reservations/management/commands/normalize_airlines.py), [recalculate_leg_revenue_shares.py](reservations/management/commands/recalculate_leg_revenue_shares.py), [update_completed_reservations.py](reservations/management/commands/update_completed_reservations.py), [ops/management/commands/seed_ops_tasks.py](ops/management/commands/seed_ops_tasks.py)

### Templates of note
[dispatching/templates/dispatching/](dispatching/templates/dispatching/): `task_queue.html`, `task_detail.html`, `admin_tasks.html`, `confirmations.html`, `inhouse_schedule.html`, `daily_capacity_planner.html`, `schedule_board.html`, `legs_list.html`, `duplicate_reservations.html`, `agency_payouts_report.html`, `admin_agent_payout_detail.html`, `staff_metrics.html`, `staff_kpis.html`, `revenue_kpis.html`, `lead_analytics.html`, `statistics.html`, `dispatcher_navbar.html`.

---

## B. Current workflow map

### Scheduling / Dispatch
- **Dispatcher dashboard** [dispatching/views.py:93 `index`](dispatching/views.py#L93) loads selected date's legs, in-house vehicle rows per driver, timelines, and pulls previous-day assignments for the "copy" preview.
- **Vehicle assignment** is AJAX via [:2275](dispatching/views.py#L2275); "Use Previous Day" is a 2-step preview/apply via [:2345 `copy_vehicle_assignments`](dispatching/views.py#L2345) — preview marks `is_off_today` but does **not block** copying off-day drivers.
- **Swap optimizer** [dispatching/swap_optimizer.py:40](dispatching/swap_optimizer.py#L40) finds chained swaps for an unplaceable leg, respecting availability + vehicle tier.
- **Conflict scanner**: [ops/tasks.py `_scan_driver_overlaps`](ops/tasks.py) creates CRITICAL `driver_conflict` tasks (in-house only, same-day), using effective ready times + travel.

### Driver availability
- Single source of truth: [drivers/availability.py:113 `resolve_effective_availability`](drivers/availability.py#L113) with priority: single-date `DriverDateOverride` → range override (most recently updated) → `DriverWeeklySchedule` → `Driver.default_*` fields.
- Exception types on [drivers/models.py:266 `DriverDateOverride`](drivers/models.py#L266): `off`, `available_until`, `available_after`, `available_window`, `unavailable_window`, `flexible`, `note_only`. "All Day" = flexible defaults, NOT a fixed 24h shift.
- Feasibility check: [drivers/availability.py:284 `is_pickup_within_window`](drivers/availability.py#L284) returns `(ok, reason)`.
- Surfaces in `index`, `inhouse_schedule`, `capacity_planner` via formatters at [:214 `format_availability_label`](drivers/availability.py#L214) and [:242 `format_availability_tooltip`](drivers/availability.py#L242).

### Payments
- Payment status is computed live: [reservations/models.py:469-515](reservations/models.py#L469-L515) `total_paid` / `amount_owed` / `payment_status`.
- Stripe webhook [payment/webhook.py:45](payment/webhook.py#L45) updates payment, sets `Reservation.status="confirmed"`, fires audit log; `ops/signals.py:213` auto-closes `payment_chase` tasks.
- Scanner [ops/tasks.py `_scan_unpaid_reservations`](ops/tasks.py) creates `payment_chase` tasks for confirmed unpaid reservations in next 7 days, priority by proximity.
- Manual reminder: [users/emails.py:198 `send_payment_reminder_ajax`](users/emails.py#L198) — when fired with a task, calls `log_communication()` to record the attempt.

### Flights
- AeroAPI service [dispatching/aeroapi_service.py](dispatching/aeroapi_service.py) auto-picks `/flights/` vs `/schedules/` based on distance from now.
- Tiered auto-refresh: [ops/tasks.py:1185 `auto_refresh_flights`](ops/tasks.py#L1185) — today every 30 min, next 2 days every 4h, days 3–7 daily, all triggered from the GHL scheduler loop.
- Mismatch logic lives on Leg ([:1430](reservations/models.py#L1430), [:1462](reservations/models.py#L1462)) — **not persisted**, recomputed on access.
- `flight_verify` tasks created by [ops/tasks.py `_scan_flight_mismatches`](ops/tasks.py); same-day mismatches escalate to `driver_conflict` if in-house schedule actually breaks.

### Confirmations
- Preview + batch send: [dispatching/views.py:3824 `confirmations_view`](dispatching/views.py#L3824).
- Per-trip-type templates in [dispatching/confirmation_sms.py:197](dispatching/confirmation_sms.py#L197) — handles airport arrival/departure, cruise (Port Canaveral, hotel pickups), car seats, store stops, return trips.
- Soft warning for unverified flights at [dispatching/views.py:3919-3925](dispatching/views.py#L3919-L3925) — **never hard-blocks**.
- Persistent state: only `leg.confirmation_sms_sent_at` (timestamp). No CommunicationAttempt is created for SMS sends today.

### Travel agents / commissions
- TravelAgent / Agency / CommissionPayout / AgencyCommissionPayout fully modeled in [users/models.py](users/models.py).
- Reservation.commission_amount + commission_paid tracked, audit-logged on change.
- Payouts: preview/process/bulk views exist ([dispatching/views.py:12345-12566](dispatching/views.py#L12345-L12566)). Statements email via EmailLog tracking.
- Backfill via [reservations/management/commands/backfill_agent_commissions.py](reservations/management/commands/backfill_agent_commissions.py).
- **No ops task** auto-reminds when payouts are due. No recurring statement send.

### Communication logging
- Email: `EmailLog` ([ops/models.py:360](ops/models.py#L360)) tracks confirmation, payment_reminder, driver_statement, agent_commission, agency_commission. Created at [users/emails.py:23 `log_email_send`](users/emails.py#L23).
- Twilio SMS: only `confirmation_sms_sent_at` timestamp. **No `CommunicationAttempt`** created on confirmation send, even though the model is built for it.
- `CommunicationAttempt`: only created via [ops/services.py:152 `log_communication`](ops/services.py#L152), called from `task_log_comm` endpoint and payment reminder AJAX. Calls/SMS outside the task UI aren't captured.

### Audit
- `AuditLog` ([reservations/models.py:2347](reservations/models.py#L2347)) — field-level change tracking for Reservation/Leg.
- `LegStatus` ([:2452](reservations/models.py#L2452)) — status timeline with `updated_by`.
- `django-simple-history` only on Customer. Reservation/Leg use AuditLog signals instead.
- `StaffActivity` ([ops/models.py:309](ops/models.py#L309)) + [ops/middleware.py](ops/middleware.py) — passive page view tracking, deduped 30-min windows.

### Lead pipeline
- Lead → FollowUpTask → LeadActivity → GHLSyncLog (DLQ).
- 30-min scheduler ([ghl_integration/scheduler.py:76](ghl_integration/scheduler.py#L76)) batches lead follow-ups, retries failed syncs, marks lost leads, then calls `generate_ops_tasks()` and `auto_refresh_flights()`.

---

## C. SOP opportunities (priority order, all map to existing screens)

1. **Building Tomorrow's Schedule** — Owner: lead dispatcher. Screen: [dispatching/views.py:93 `index`](dispatching/views.py#L93) + capacity_planner.
2. **Unpaid Reservation Follow-Up** — Owner: dispatcher on shift. Screen: task queue filtered to `payment_chase` + reservation_view.
3. **Flight Verification** — Owner: dispatcher on shift. Screen: task queue filtered to `flight_verify`.
4. **Daily Dispatch Checklist (morning + EOD)** — Owner: lead dispatcher.
5. **Sending Confirmations** — Owner: dispatcher. Screen: [confirmations_view](dispatching/views.py#L3824).
6. **Driver Availability / Days Off** — Owner: dispatcher; Approver: owner. Screen: `inhouse_schedule`.
7. **Farm-Out Decision Process** — Owner: dispatcher; Approver: owner. (No system support yet — SOP must include a manual log until a model lands.)
8. **Travel Agent Commission Payments** — Owner: bookkeeping/owner. Screen: agency_payouts_report + payout views.
9. **Customer Complaint / Refund Handling** — Owner: customer service.
10. **End-of-Day Review** — Owner: lead dispatcher; Cc: owner.

(Full outlines in Section H.)

---

## D. Automation opportunities

### Automate immediately (signals/scheduler hooks already exist — flip a switch)
- **Re-enable escalation engine** ([ops/escalation.py:17](ops/escalation.py#L17)). It is shipped and currently dead code (`return {"escalated": 0}`). Owner-only NTFY for tasks that pass `escalate_at`. Reduces "did anyone do this?" anxiety.
- **Auto-log `CommunicationAttempt` for confirmation SMS** at the success/failure return of [`send_confirmation_via_twilio`](dispatching/confirmation_sms.py#L389). Same for failed sends (channel=sms, outcome=failed) so dispatchers see what went wrong.
- **Auto-create `payment_chase` task on Stripe webhook payment failure**, not just on scanner pass.
- **Surface duplicate reservations as a task**: extend `_scan_unpaid_reservations` or add `_scan_duplicates` reusing the query from [dispatching/views.py:12589](dispatching/views.py#L12589) so duplicates show in the task queue with both reservation links.
- **Commission payout task generation**: new scanner `_scan_commission_payouts_due` — every Monday, create a `commission_payout` task per agent with unpaid commissions ≥ $X, due Friday.

### Semi-automate (system proposes, staff confirms)
- **Auto-assign suggestions**: the swap optimizer already proposes; surface "AI suggests Driver X for Leg Y" inline on capacity_planner unassigned legs.
- **Confirmation send: bundle "ready to send" tasks** — instead of per-leg, surface a `confirmation_texts` task per date (already shipped at [`_scan_confirmation_texts`](ops/tasks.py)) and add a one-click "send all" button that defaults to the soft-warning-clean subset.
- **Payment reminder send** — current AJAX is one-at-a-time. Add a queued "send all overdue reminders" inside the `payment_chase` queue with template selection.

### Keep manual but track with tasks
- **Calls to guests** — staff must log call attempt + outcome to the task. No way to automate the call itself, but make the log mandatory before snooze.
- **Farm-out decisions** — no automated scoring yet; require dispatcher to add a `farm_out` note to the leg (new field) + log decision rationale in task.
- **Refunds** — manual approval flow; track via task with the Stripe refund handler.
- **Driver day-off requests** — staff creates `DriverDateOverride`, dispatcher reviews; both events should write StaffActivity.

### Future advanced automation
- **Farm-out scoring**: score legs by distance from in-house coverage, predicted overtime, vehicle constraints; recommend farm-out candidates.
- **Schedule quality scoring**: per-day metric (assignment density, swap count, deadhead miles).
- **Driver workload balancing**: hours-per-week ceiling enforcement with auto-suggest reassign.
- **AI-assisted customer responses**: draft templated replies for common complaints / refund asks.
- **Demand forecasting**: lead/booking trends → staffing forecasts for next 14 days.

---

## E. Gap analysis

| # | Workflow | Current state | Pain point | Risk | Recommended fix | Complexity | Impact |
|---|---|---|---|---|---|---|---|
| 1 | Escalation | Engine built but disabled at [ops/tasks.py:254-255](ops/tasks.py#L254-L255) | Tasks past `escalate_at` rot silently | Forgotten unpaid trips, missed confirmations, late driver assignments | Uncomment the call + add NTFY topic for owner; keep daily NTFY-quiet-hours setting | **S** | **H** |
| 2 | Comm log on SMS | `CommunicationAttempt` only via manual UI | Confirmation send history lives only as a single timestamp; failures invisible in UI | Resent or missed confirms; no proof of contact for refund disputes | Add `log_communication()` call in `send_confirmation_via_twilio` success/failure paths | **S** | **H** |
| 3 | Duplicate detection | Standalone page [:12589](dispatching/views.py#L12589) only | Dispatcher must remember to check; duplicates surface late | Same customer charged twice or both rides run | New `_scan_duplicates()` scanner producing a `duplicate_check` task; surface in dispatcher index | **S** | **H** |
| 4 | Farm-out tracking | No model, no field | Outsourced rides live in texts/memory | Double-booking, missed payouts to subs, no audit trail | Add `Leg.farm_out_status` (none/pending/confirmed/declined) + `farm_out_provider` (FK to a new `ExternalProvider` model) + `farm_out_cost`; show in dispatcher dashboard | **M** | **H** |
| 5 | Driver conflict alerts only same-day | Scanner [`_scan_driver_overlaps`](ops/tasks.py) is in-house, same-day only | Future overlap surprises happen | Cancellations / scrambling | Extend scanner to next 3 days; lower priority for further-out conflicts | **S** | **M** |
| 6 | Off-day assignment | Feasibility soft warning only; copy_vehicle_assignments doesn't block | Driver scheduled on day off via "Use Previous Day" | No-show, frustrated driver | Add explicit confirm dialog when applying copy to off-day drivers; record StaffActivity | **S** | **M** |
| 7 | Manage-by-exception dashboard | Multiple dashboards but no unified one | Owner has to check 4 places to know what's broken today | Owner stays in loop daily | Single `/ops/owner-board/` aggregating task counts, unassigned legs, unpaid + payouts due + flight unverified count, last 24h NTFYs | **M** | **H** |
| 8 | Commission payout reminder | Manual; views exist but no scheduler trigger | Owner forgets to pay agents | Agent churn, complaints | Add `_scan_commission_payouts_due()` weekly + `commission_payout` task type | **S** | **M** |
| 9 | Agent statement send | EmailLog model logs them, but send is manual | No monthly cadence | Agent relationship friction | Management command `send_monthly_agent_statements` + scheduler cron entry | **M** | **M** |
| 10 | "Lead needs human follow up" | `Lead.needs_human_follow_up` boolean | Not wired to ops queue | Hot lead falls through cracks | Add `_scan_lead_human_followup()` producing `lead_response` task | **S** | **H** |
| 11 | Vehicle tier mismatch | No check at assignment time | Wrong-class vehicle assigned (sedan to van trip) | Customer complaint at pickup | Compare `Leg.rate.vehicle.tier` vs `FleetVehicle.vehicle_type` on assign; warn | **S** | **M** |
| 12 | Refund handling | Stripe SDK in place; no task type | Refunds done ad hoc, not surfaced | No SLA, no audit | `refund_request` task type + `RefundRequest` model linked to Reservation | **M** | **M** |
| 13 | Customer complaint | No model | Lives in inbox / texts | Owner is the only one who knows the history | `Complaint` model + task + tag on Customer/Reservation | **M** | **M** |
| 14 | Schedule export | CSV from confirmations + dispatcher view | No "send to driver group chat" button | Manual copy-paste daily | "Copy schedule to clipboard" button on dispatcher index, formatted for SMS/group chat | **S** | **L** |
| 15 | Dispatch alerts dedupe via JSON file | `.dispatch_alerts_sent.json` on disk | Lost on container restart, no audit | Re-alert spam | Move dedupe state to a small `DispatchAlertSent` table | **S** | **M** |
| 16 | StaffActivity coverage | Only page views + task actions tracked | Charging, swap, refresh-flights actions invisible | Performance reviews lack data | Sprinkle `record_activity()` in key POST handlers (charge, swap apply, refresh flights) | **S** | **M** |
| 17 | SOP discoverability | Docs in repo root MDs | Staff don't read repo files | SOPs go unread | New `/ops/sops/` page rendering markdown SOPs; link from task detail by task_type | **M** | **H** |
| 18 | Hard thresholds hardcoded | `OTW_LEAD_MINUTES`, 30-min flight skew, 60-min cooldown all in code | Tuning requires deploy | Misaligned alerts | `OpsSetting` model with admin-editable thresholds | **S** | **M** |

---

## F. Recommended build roadmap

### Phase 1 — Control the chaos (1–2 weeks)
1. **Re-enable escalation engine** with owner NTFY. (S)
2. **Auto-log CommunicationAttempt for confirmation SMS** (success + failure). (S)
3. **Add `_scan_duplicates()` scanner** + `duplicate_check` task type. (S)
4. **Off-day assignment confirm dialog** in `copy_vehicle_assignments` preview. (S)
5. **Vehicle-tier mismatch warning** on assign. (S)
6. **Move dispatch_alerts dedupe to DB** (`DispatchAlertSent` model). (S)
7. **"Lead needs human follow-up" → ops task** wiring. (S)

### Phase 2 — Delegation & accountability (2–3 weeks)
1. **Owner manage-by-exception board** at `/ops/owner-board/` aggregating: overdue task count by type, unassigned legs (next 7d), unpaid reservations (next 7d), unverified flights, payouts due, last-24h escalations, current on-shift staff. (M)
2. **Sprinkle StaffActivity** on key POST handlers (charge, swap apply, refresh flights, payout process, refund). (S)
3. **SOP browser** at `/ops/sops/` — markdown docs in `ops/sops/` rendered with checklists; per-task-type "View SOP" link from task detail. (M)
4. **OpsSetting model** for thresholds (`OTW_LEAD_MINUTES`, flight skew, cooldown windows, escalation deltas). (S)
5. **Farm-out tracking model + UI** (Leg.farm_out_status, ExternalProvider). (M)
6. **"Send all overdue payment reminders"** action on the `payment_chase` filter. (S)

### Phase 3 — Automation (3–4 weeks)
1. **Commission payout scanner** (`commission_payout` task type) — weekly cron, per-agent. (S)
2. **Monthly agent statement send** management command + scheduler entry. (M)
3. **Refund request flow**: `RefundRequest` model, task type, Stripe refund handler, customer notification. (M)
4. **Complaint model + workflow** (`complaint` task type, tags on Customer). (M)
5. **Confirmation batch UX**: collapse all confirmation tasks into one "Send tomorrow's confirmations" workflow with per-leg toggle, warnings, and one-click send. (M)
6. **Customer-facing "is my flight tracked?" status link** — link in confirmation SMS to a public read-only page. (M)

### Phase 4 — Optimization (ongoing, after Phases 1–3)
1. **Farm-out scoring** for each leg (recommend in-house vs sub). (L)
2. **Schedule quality score** per day. (M)
3. **Driver workload balancing** with hours-per-week ceiling. (M)
4. **AI-assisted customer responses** drafting in task detail. (L)
5. **Demand forecasting** for staffing. (L)
6. **Public PDF agent statements** + automated payment-method reminder when info incomplete. (M)

---

## G. First 3 code changes to make

These are intentionally **small, reversible, high-leverage**, and require **no new models** — they activate or close loops on infrastructure that already exists.

### #1 — Turn the escalation engine back on
**File:** [ops/tasks.py:254-255](ops/tasks.py#L254-L255)
**Change:** Replace the two-line comment with a call to `run_escalations()` from [ops/escalation.py:17](ops/escalation.py#L17). Add a new `ESCALATION_QUIET_HOURS` setting (e.g., 22:00–07:00) that the NTFY helper respects so the owner isn't woken at 3 AM.
**Why:** It's the cheapest possible win — the engine, NTFY plumbing, escalate_at timestamps on tasks, and a "ESCALATED" status are all already shipped. Doing nothing means tasks rot.
**Side effects:** Owner starts getting NTFY pushes immediately; tune `escalate_at` defaults if too noisy. Reversible by re-disabling.

### #2 — Auto-log `CommunicationAttempt` for every confirmation SMS send
**File:** [dispatching/confirmation_sms.py:389 `send_confirmation_via_twilio`](dispatching/confirmation_sms.py#L389), called from [`send_confirmations_for_date`](dispatching/confirmation_sms.py#L424).
**Change:** After each Twilio call, if the leg has any open `confirmation_texts` or `flight_verify` task, attach a `CommunicationAttempt(channel='sms', outcome=delivered|failed, contact_value=phone, metadata={'leg_id': leg.id})`. If no task exists, attach to a synthetic "ad-hoc" task so the log isn't lost. Re-use [ops/services.py:152 `log_communication`](ops/services.py#L152).
**Why:** Right now the only proof a confirmation went out is a single timestamp on the leg; failures are silent unless someone reads logs. Closing this gives staff a real send history visible in the task detail and the customer's reservation page.
**Side effects:** Higher row counts in `CommunicationAttempt`. Negligible perf cost.

### #3 — Make duplicate reservations a task, not a separate page
**File:** new scanner in [ops/tasks.py](ops/tasks.py) called `_scan_duplicates()`, hooked into `generate_ops_tasks()`.
**Change:** Reuse the query already in [dispatching/views.py:12589 `duplicate_reservations`](dispatching/views.py#L12589) — same customer + same pickup_date, one paid + one unpaid in the last 90 days. Create a `duplicate_check` task type (CRITICAL if same-day, HIGH otherwise) with metadata `{"reservation_ids": [a, b]}`. Auto-close when one is cancelled.
**Why:** Today a dispatcher must remember to visit `/duplicate-reservations/`. Moving it into the task queue means it surfaces automatically and the navbar badge increases — the same channel staff already check daily.
**Side effects:** May produce one-time backfill of historical duplicates; gate the first run behind a date cutoff.

**Not in the first 3 (deliberately):** the manage-by-exception owner board (Phase 2) is high-impact but moderate effort; farm-out (Phase 2) requires a new model and UI; refunds/complaints (Phase 3) need cross-team agreement on workflow first.

---

## H. SOP outlines

Stored as markdown under a proposed `ops/sops/` directory, rendered in-app per Phase 2.

### SOP 1 — Building Tomorrow's Schedule
- **Purpose:** Produce a fully-assigned, flight-verified, confirmation-ready schedule for the next operating day by EOD today.
- **Owner:** Lead dispatcher on shift.
- **Trigger:** 3:00 PM daily.
- **Tools/screens:** Dispatcher dashboard (`/dispatching/`), capacity planner, task queue.
- **Steps:**
  1. Open dispatcher dashboard, set date to **tomorrow**.
  2. Open **Task Queue** filter `due_at ≤ tomorrow EOD` — clear any CRITICAL items first.
  3. Click **Refresh Flights** if not yet auto-run for tomorrow.
  4. Resolve all `flight_verify` tasks (call guest if mismatch >30 min).
  5. Open **Capacity Planner** — review unassigned legs; resolve all `driver_assign` tasks.
  6. Click **Use Previous Day** for vehicle assignments; confirm dialog flags any off-day drivers (Phase 1 fix). Approve / replace.
  7. Resolve any `driver_conflict` tasks (use Swap Optimizer where available).
  8. Resolve any `duplicate_check` tasks (Phase 1 fix).
  9. Open **Confirmations** for tomorrow — verify count = leg count; send batch.
  10. Mark "Schedule built" StaffActivity (button on dashboard, Phase 2).
- **Decision rules:** Never copy previous-day assignment to a driver whose `effective_availability.status == 'off'` without explicit confirmation. Never send confirmation to a leg with an open `flight_verify` task more than 60 min mismatch.
- **Templates:** Group-chat schedule export.
- **DoD:** Zero open `driver_assign` + `flight_verify` + `duplicate_check` tasks for tomorrow's date; all confirmations sent.
- **Common mistakes:** Skipping flight refresh; assigning off-day driver via copy; sending confirmations before verifying flight mismatches.
- **Escalation:** If any leg unresolved by 9 PM, owner is notified via escalation engine NTFY.

### SOP 2 — Unpaid Reservation Follow-Up
- **Purpose:** Convert unpaid reservations to paid before pickup, or cancel/flag the booking.
- **Owner:** Dispatcher on shift.
- **Trigger:** New `payment_chase` task arrives in queue (auto-created by `_scan_unpaid_reservations`).
- **Tools/screens:** Task queue → task detail.
- **Steps:**
  1. Claim task.
  2. Click **Send Payment Reminder Email** (logs CommunicationAttempt).
  3. Wait 2 hours. If no payment, **Call** the customer; log call attempt + outcome.
  4. If voicemail: log voicemail + send SMS.
  5. If no response in 24h and pickup ≤ 24h away: **Escalate** (flag → owner).
  6. If paid: task auto-closes via Stripe webhook signal.
- **Decision rules:** ≤ 24h to pickup unpaid = STOP — call owner. Never refuse a known returning customer without owner OK.
- **Templates:** Email reminder ([users/emails.py](users/emails.py)). SMS reminder template (Phase 1).
- **DoD:** Reservation paid OR cancelled OR explicitly escalated with owner approval.
- **Common mistakes:** Sending only one email; not logging the call; closing task before pickup confirmed paid.
- **Escalation:** Auto via engine if `escalate_at` (24h before pickup) passes.

### SOP 3 — Flight Verification
- **Purpose:** Match pickup time to actual flight arrival for every airport arrival leg before confirmation.
- **Owner:** Dispatcher on shift.
- **Trigger:** `flight_verify` task in queue.
- **Tools/screens:** Task detail (`_build_flight_verify_context`), customer phone.
- **Steps:**
  1. Claim task.
  2. Inspect mismatch direction + minutes (already in task context).
  3. If mismatch ≤ 30 min: adjust leg pickup time silently (no customer contact).
  4. If 30–90 min: text customer with corrected pickup time; log SMS.
  5. If > 90 min or arrival date shifted (overnight): call customer; log call.
  6. Mark task complete with resolution notes.
- **Decision rules:** Overnight flips (arrived late prev day, pickup next AM) always require customer confirmation. Cruise pickups: trust port time, not flight time.
- **Templates:** Corrected-pickup SMS template (new).
- **DoD:** Leg pickup_time within 30 min of best-available arrival OR explicit customer confirmation logged.
- **Common mistakes:** Trusting scheduled arrival when actual is available; missing date flip.

### SOP 4 — Daily Dispatch Checklist (morning + EOD)
- **Purpose:** Ensure shift handoff is clean and nothing rolls into next day.
- **Owner:** Lead dispatcher.
- **Trigger:** Shift start + 6 PM EOD.
- **Tools/screens:** Owner board (Phase 2), task queue.
- **Steps (morning):**
  1. Review escalated tasks from overnight.
  2. Scan task queue overdue filter.
  3. Verify all today's legs have assigned drivers + vehicles.
  4. Refresh flights for today's arrivals.
- **Steps (EOD):**
  1. Confirm all today's `picked-up` legs have moved to `completed`.
  2. Tomorrow's schedule built per SOP 1.
  3. Driver payment reconciliation for today (if Friday).
  4. Note any farm-outs scheduled for next day.
- **DoD:** Owner board shows zero overdue and zero critical-priority items at shift end.
- **Templates:** EOD summary group chat post.

### SOP 5 — Sending Confirmations
- **Purpose:** Send accurate, complete confirmation SMS to every tomorrow's leg.
- **Owner:** Dispatcher.
- **Trigger:** `confirmation_texts` task or 5 PM trigger.
- **Tools/screens:** `confirmations_view`.
- **Steps:**
  1. Verify all `flight_verify` and `duplicate_check` tasks for tomorrow are closed.
  2. Open confirmations preview; scan rows.
  3. For each leg with soft warning ("flight unverified"), open the related task and resolve before sending that leg.
  4. Click **Send All**; system logs CommunicationAttempt per leg (Phase 1 fix).
  5. For failed sends, attempt manual send + log call attempt.
- **Decision rules:** Never send to a leg with pickup date mismatch flagged. Travel agent on the reservation = SMS template includes agent CC. Car seats / store stops always included.
- **DoD:** Every tomorrow leg has `confirmation_sms_sent_at` OR a CommunicationAttempt with explanation.
- **Common mistakes:** Sending then noticing the flight mismatch; missing car seat note.

### SOP 6 — Driver Availability / Days Off
- **Purpose:** Capture driver availability accurately so scheduling reflects reality.
- **Owner:** Dispatcher (entry); owner (approval for date ranges > 3 days).
- **Trigger:** Driver request received (call/text/RingCentral).
- **Tools/screens:** Driver detail → DriverDateOverride form, inhouse_schedule.
- **Steps:**
  1. Confirm driver's request: date(s), partial day vs full day, reason category.
  2. Add `DriverDateOverride`: exception_type, date (or date range), optional window times, notes (max 200 char).
  3. Verify any already-assigned legs for that day → reassign now if needed.
  4. Reply to driver confirming.
- **Decision rules:** Range > 3 days needs owner approval (StaffActivity flag, Phase 2). Day-of "I'm sick" cancellations = CRITICAL escalation.
- **Templates:** Driver confirmation SMS / email.
- **DoD:** Override saved; no orphan assignments remain.

### SOP 7 — Farm-Out Decision Process *(SOP first, model lands in Phase 2)*
- **Purpose:** Decide and track when a leg is dispatched to an external provider.
- **Owner:** Lead dispatcher; owner approves new providers.
- **Trigger:** Capacity planner shows unassigned leg + no in-house solution found by Swap Optimizer.
- **Tools/screens:** Today: leg detail "notes" field. After Phase 2: leg detail `farm_out_status` selector + `farm_out_provider` dropdown.
- **Steps:**
  1. Confirm Swap Optimizer found no viable in-house solution.
  2. Identify provider (preferred list maintained in `ExternalProvider`).
  3. Send leg details to provider; await acceptance.
  4. Update leg: status=`assigned`, `farm_out_status=confirmed`, `farm_out_cost`, `farm_out_provider`.
  5. Note margin impact in EOD report.
- **Decision rules:** Never farm out a leg with car seat or other special requirement without explicit confirmation from provider. Never farm out for a known returning customer without owner OK.
- **DoD:** Leg has farm-out provider + cost + acceptance proof (text screenshot in task notes).

### SOP 8 — Travel Agent Commission Payments
- **Purpose:** Pay agents on time, every period; protect agent relationships.
- **Owner:** Bookkeeper or owner.
- **Trigger:** `commission_payout` task (Phase 3 scanner) or monthly schedule.
- **Tools/screens:** [Agency Payouts Report](dispatching/views.py#L12118), [Process Agent Payout](dispatching/views.py#L12345), [Bulk Payout](dispatching/views.py#L12469).
- **Steps:**
  1. Open task → preview agent's unpaid commissions.
  2. Verify `payment_info_complete` on agent + agency.
  3. For agency-handled agents: bulk by agency.
  4. Issue payment via stored method.
  5. Mark payout paid in app → emails statement (EmailLog).
- **Decision rules:** Skip agents with incomplete payment info → create separate `missing_payment_info` task for them.
- **Templates:** Statement email already exists ([users/emails.py](users/emails.py)).
- **DoD:** All eligible payouts processed for the period; statements emailed.

### SOP 9 — Customer Complaint / Refund Handling
- **Purpose:** Resolve complaints quickly with clear audit trail.
- **Owner:** Customer service; owner approves refunds > $X.
- **Trigger:** Inbound call/email/text from customer expressing dissatisfaction.
- **Tools/screens:** Reservation detail, task queue. After Phase 3: `complaint` + `refund_request` task types.
- **Steps:**
  1. Open reservation; create manual `complaint` task with description.
  2. Log all communications (CommunicationAttempt).
  3. If refund requested: create `refund_request` task; gather Stripe payment ID, amount.
  4. If amount ≤ threshold: dispatcher refunds + records reason.
  5. If amount > threshold: route to owner.
  6. Final resolution communicated to customer; close tasks with resolution notes.
- **Decision rules:** Refund threshold per type (no-show vs late vs missed vs service complaint). Always tag VIP customer for owner visibility.
- **DoD:** Resolution communicated, refund processed (or denied with reason), complaint task closed.

### SOP 10 — End-of-Day Review
- **Purpose:** Wrap clean, surface anything that needs owner attention.
- **Owner:** Lead dispatcher; owner reviews next AM.
- **Trigger:** 9 PM.
- **Tools/screens:** Owner board (Phase 2), staff metrics, NTFY history.
- **Steps:**
  1. Scan today's escalations.
  2. Verify all legs `completed` or noted (no-show/cancel) with reason.
  3. Tomorrow's schedule built (SOP 1 DoD met).
  4. Post EOD summary in owner group chat: legs run, escalations, anything pending owner decision.
- **DoD:** Owner can wake up tomorrow and act only on the EOD summary items.

---

## Verification (how to test recommendations end-to-end)

When implementation begins, validate Phase 1 changes by:
1. **Escalation:** seed a task with `escalate_at = now - 1 min`, run `python manage.py shell -c "from ops.tasks import generate_ops_tasks; generate_ops_tasks()"`, confirm NTFY arrives + task status = `escalated`.
2. **SMS comm log:** send a test confirmation to a real leg with Twilio sandbox; verify CommunicationAttempt row created with channel='sms' and outcome='delivered'.
3. **Duplicate scanner:** run [reservations/management/commands/seed_duplicate_test.py](reservations/management/commands/seed_duplicate_test.py), call `generate_ops_tasks()`, confirm a `duplicate_check` task appears with both reservation IDs in metadata.
4. **Off-day confirm dialog:** mark a driver off via DriverDateOverride; click "Use Previous Day" on the dispatcher dashboard for a date that previously had that driver assigned; confirm a confirmation dialog appears and StaffActivity is recorded if proceeded.
5. **Owner board (Phase 2):** add `/ops/owner-board/` URL, render aggregations, eyeball counts vs admin queries.

---

## Quick wins (do these even before Phase 1 scoping)
- Move `.dispatch_alerts_sent.json` to a DB table (Item 15) — one Bash deploy and a small migration.
- Add a "Copy schedule to clipboard" button on the dispatcher dashboard for the driver group chat (Item 14).
- Add a navbar link to `/dispatching/duplicate-reservations/` (one line in `dispatcher_navbar.html`) until the scanner ships.
- Document the disabled escalation engine in `ops/OPS_SYSTEM.md` so future devs know it's intentional, not an oversight.

## Risky areas (flag before automating)
- **Twilio cost** if escalation NTFY fires too aggressively — keep quiet hours and cap retries.
- **Stripe refunds** — currently no `RefundRequest` model; do not automate refund issuance until the workflow is modeled.
- **Cancelling a reservation via auto-rules** — never auto-cancel an unpaid reservation; always require human click.
- **AI-assisted customer responses** (Phase 4) — never auto-send; draft + human review only.
- **Farm-out scoring** — do not surface as a "should farm out" recommendation until you have 3+ months of cost-per-leg data on in-house vs farmed.
