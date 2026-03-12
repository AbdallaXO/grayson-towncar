# Staff Productivity & Operational Task Layer — Full Implementation Plan

> **Date:** 2026-03-11
> **Author:** Claude (Opus 4.6), commissioned by Grayson Towncar ownership
> **Status:** Approved for implementation

---

## Modifications to Original Proposal

Two changes were made after the initial design review:

1. **No hard blocking on confirmations.** Flight verification is NOT a hard dependency for sending confirmation texts. Instead, a **soft non-blocking warning** is shown — if a confirmation is about to be sent on a leg that still has an open `flight_verify` task, a small notice such as "Flight not yet verified" appears, but the send proceeds normally without restriction.

2. **Everything else proceeds exactly as proposed.** All 5 phases, all 3 models (`OperationalTask`, `CommunicationAttempt`, `StaffActivity`), automated task generation, communication logging, escalation engine, NTFY alerts to owner, and the private superuser-only owner dashboard.

---

## 1. Codebase Understanding

### Architecture Summary
- Django 5.1 on Railway (single gunicorn worker, PostgreSQL)
- No Redis/Celery. Background work uses daemon threads via `_run_in_background()` in `reservations/utils.py`
- A lightweight scheduler in `ghl_integration/scheduler.py` runs a daemon thread every 30 min for lead follow-ups, retry processing, and lost lead detection
- Staff work from custom dashboards (not just Django Admin)

### Key Models and Relationships

**Core Booking Chain:**
- `Reservation` → `Customer` (FK), `Rate`, `Vehicle`, `Leg` (1:M), `Payment` (1:M)
- `Leg` → `Reservation` (FK), `Driver` (FK), `Flight` (1:1), `Cruise` (1:1), `LegStatus` (1:M history)
- `Flight` — stores scheduled/estimated/actual arrival times, terminal, gate, baggage claim. Updated via AeroAPI.

**Payment Tracking (on Reservation):**
- `@cached_property total_paid` — sum of paid payments minus refunds
- `@cached_property amount_owed` — total_price - total_paid
- `@cached_property payment_status` — unpaid/partial/paid
- `Payment` model tracks Stripe checkout/payment intent, status (pending/card_saved/paid/failed/refunded)

**Lead Pipeline:**
- `Lead` — status (NEW/CONTACTED/INTERESTED/FUTURE_CONTACT/CONVERTED/LOST/COLD), priority, segment, `contact_attempts`, `last_contact_date`, `next_follow_up`, `sequence_active`, `needs_human_follow_up`
- `FollowUpTask` (ghl_integration) — work queue: lead FK, step_number, status (pending/sent/cancelled/failed/skipped), scheduled_at, attempts
- `LeadActivity` (ghl_integration) — audit log: activity_type (sms_sent/failed/reply_received/converted/status_change/sequence events), description, metadata JSON
- `GHLSyncLog` — dead letter queue with exponential backoff retry

**Existing Audit Infrastructure:**
- `AuditLog` — model_name, object_id, action (created/updated/deleted/driver_assigned/status_changed/payment_processed), field_name, old_value, new_value, user FK, timestamp
- `LegStatus` — leg FK, status, timestamp, updated_by FK, notes (status timeline)
- `HistoricalRecords` — on Customer, Reservation, Leg (django-simple-history, full field-level change tracking)

### Current Dashboards & Staff Workflows

| Dashboard | File | Purpose |
|-----------|------|---------|
| Dispatcher Dashboard | `dispatching/views.py:index` | Day's legs, driver assignment, status flags |
| Legs Dashboard | `dispatching/views.py:legs_list` | All legs with filtering/pagination |
| Confirmations | `dispatching/views.py:confirmations_view` | Preview + send SMS confirmations |
| Capacity Planner | `dispatching/views.py:capacity_planner` | Auto-assign, swap suggestions, snapshots |
| Driver Payments | `dispatching/views.py:driver_payment_management` | Pay drivers, date range filtering |
| Payment Portal | `dispatching/views.py:dispatcher_payment_portal` | Charge cards, view transactions |
| Analytics | `dispatching/views.py:analytics_dashboard` | Conversion funnels, revenue |
| Lead Analytics | `dispatching/views.py:lead_analytics` | Lead funnel, 30/90/all-time |
| Statistics | `dispatching/views.py:statistics_page` | Comprehensive stats (superuser only) |
| Booking Wizard | `dispatching/views.py:dispatcher_booking_*` | 7-step booking form |

### Communication Infrastructure

| Channel | Location | Usage |
|---------|----------|-------|
| Twilio SMS | `dispatching/confirmation_sms.py` | Guest confirmations, tracks `leg.confirmation_sms_sent_at` |
| Email | `users/emails.py` | Confirmation, payment reminder (with AJAX endpoint), lead quote, statements |
| GHL SMS | `ghl_integration/services.py` | Lead follow-up sequences via GoHighLevel |
| NTFY Push | `reservations/utils.py` | Lead alerts, dispatch alerts, driver status (3 topics) |
| Dispatch Alerts | `dispatching/management/commands/dispatch_alerts.py` | 5-10 min via Task Scheduler, NTFY for flight mismatches + dispatch flags |

---

## 2. Current Operational Workflow Map

### Live Leads
- **What happens:** Lead created via website form (`QuoteFormHandlerView`), GHL webhook, or manual entry. `post_save` signal syncs to GHL, sends initial SMS (or email fallback). 5-step automated follow-up sequence runs over 4 days. NTFY alerts owner on new lead.
- **Staff action required:** Respond to inbound calls/texts via RingCentral/GHL. Handle `needs_human_follow_up` flag.
- **Failure point:** Between live lead responses, staff have no structured queue. Follow-up sequences are automated but staff-initiated follow-ups have no structure.

### Unpaid Reservations
- **What happens:** Reservation created → customer gets checkout link. Payment webhook updates status.
- **Staff action required:** Identify unpaid upcoming reservations manually. Call customer, then text/email.
- **Failure point:** No structured detection. No follow-up persistence. No record of who called or when.

### Flight Verification
- **What happens:** `refresh_all_flights()` pulls AeroAPI data. `Leg.has_flight_time_mismatch(30)` detects discrepancies. Dashboard shows red flags.
- **Staff action required:** See the red flag → call guest → correct the pickup time.
- **Failure point:** If guest doesn't answer, there's nothing persistent. No follow-up tracking, no call log, no retry schedule.

### Guest Confirmations
- **What happens:** `confirmations_view()` shows legs for a date. Staff can send single or batch SMS.
- **Dependency issue:** Intended workflow is refresh flights → match times → then confirm. But nothing enforces this.
- **Failure point:** No task telling staff "these confirmations are ready to send."

### Driver Assignment / Scheduling
- **What happens:** Capacity planner has auto-assign, swap suggestions, schedule snapshots.
- **Failure point:** No task surfacing unassigned legs. Owner must proactively check.

---

## 3. What Already Exists — Leverage Map

### Reusable As-Is
| Asset | Location | How to Reuse |
|-------|----------|-------------|
| `FollowUpTask` pattern | `ghl_integration/models.py` | Blueprint for `OperationalTask` |
| `LeadActivity` pattern | `ghl_integration/models.py` | Blueprint for `CommunicationAttempt` |
| `GHLSyncLog` retry pattern | `ghl_integration/models.py` | Blueprint for task retry with exponential backoff |
| `_run_in_background()` | `reservations/utils.py` | Background task execution |
| Scheduler daemon | `ghl_integration/scheduler.py` | Add ops task scanning to existing 30-min loop |
| `send_dispatch_alert_notification()` | `reservations/utils.py` | NTFY for task escalations |
| `Leg.has_flight_time_mismatch()` | `reservations/models.py:1056` | Detects flight discrepancies |
| `Reservation.payment_status` | `reservations/models.py:396` | Identifies unpaid/partial/paid |
| `detect_leg_flags()` | `dispatching/utils.py:593` | Real-time dispatch flags |
| `can_view_statistics()` | `dispatching/views.py:80` | Superuser-only check pattern |

### Partially Exists — Needs Extension
| Asset | What Exists | What's Missing |
|-------|-------------|----------------|
| Payment reminder flow | Email exists via AJAX | No persistent task, no call/text tracking |
| Flight mismatch detection | Detection + dashboard flags + NTFY | No persistent follow-up task |
| Confirmation workflow | Full SMS preview + send | No soft warning for unverified flights |
| Lead follow-up | Full 5-step automation | Not unified with broader ops queue |
| Scheduler | 30-min daemon thread for leads | Needs ops task scanning added |

### Missing Entirely
- Unified operational task model
- Staff task queue dashboard
- Communication attempt logging for reservations
- Staff activity/performance tracking
- Owner metrics dashboard
- Structured retry/snooze/escalation for non-lead tasks

---

## 4. Design Proposal — Operational Task Layer

### New App: `ops`

### Model: `OperationalTask`
- `task_type` — CharField: `lead_response`, `payment_chase`, `flight_verify`, `guest_confirm`, `driver_assign`, `coverage_gap`, `manual`
- `status` — CharField: `pending`, `in_progress`, `snoozed`, `completed`, `cancelled`, `escalated`
- `priority` — SmallIntegerField (1=Critical, 2=High, 3=Medium, 4=Low)
- `title` — CharField(200)
- `description` — TextField
- **Related objects (all nullable FK):** `reservation`, `leg`, `lead`
- **Assignment:** `assigned_to`, `created_by` (User FKs)
- **Scheduling:** `due_at`, `snoozed_until`, `escalate_at`
- **Retry:** `attempts`, `max_attempts`, `last_attempt_at`, `next_retry_at`
- **Dependency:** `blocked_by` (self FK) — NOT USED FOR HARD BLOCKING per modification #1
- **Resolution:** `resolved_at`, `resolved_by`, `resolution_notes`
- **Metadata:** `metadata` (JSONField)

### Model: `CommunicationAttempt`
- `task` (FK → OperationalTask)
- `channel` — call / sms / email
- `outcome` — answered / voicemail / no_answer / busy / sent / delivered / failed / bounced
- `staff_user`, `contact_value`, `notes`, `duration_seconds`, `metadata`, `created_at`

### Model: `StaffActivity`
- `user` (User FK)
- `action_type` — page_view / task_claimed / task_completed / task_snoozed / comm_logged
- `path`, `task` (nullable FK), `metadata`, `ip_address`, `created_at`

### Task Generation Triggers

| Task Type | Trigger |
|-----------|---------|
| `lead_response` | Signal: `post_save` on Lead (created=True) |
| `payment_chase` | Signal: `post_save` on Reservation (created, confirmed, unpaid) + Scheduler scan |
| `flight_verify` | Scheduler: arrival legs with `has_flight_time_mismatch()` |
| `guest_confirm` | Scheduler: legs for tomorrow, no `confirmation_sms_sent_at`, driver assigned |
| `driver_assign` | Signal: `post_save` on Leg (created, driver=None) + Scheduler scan |
| `coverage_gap` | Scheduler: next 3 days, no driver |

### Auto-Close Conditions

| Task Type | Auto-Close When |
|-----------|----------------|
| `lead_response` | Lead status → converted or lost |
| `payment_chase` | Payment.status → paid |
| `flight_verify` | `has_flight_time_mismatch()` returns False, or staff marks verified |
| `guest_confirm` | `confirmation_sms_sent_at` is set |
| `driver_assign` / `coverage_gap` | Leg.driver goes from None → Driver |

### Escalation Rules

| Task Type | Escalate After | Action |
|-----------|---------------|--------|
| `lead_response` | 15 minutes | Priority → CRITICAL, NTFY to owner |
| `payment_chase` | 48h or 24h before pickup | Priority → CRITICAL, NTFY |
| `flight_verify` | 4 hours | Priority → CRITICAL |
| `guest_confirm` | Pickup date 6 PM | Priority → CRITICAL, NTFY |
| `driver_assign` | 24h before pickup | Priority → CRITICAL, NTFY |

---

## 5. Staff Queue / Priority Logic

1. **CRITICAL (1):** Live leads (<15 min), same-day flight mismatches, same-day unpaid, escalated
2. **HIGH (2):** New leads (>15 min), payment chase (pickup within 7 days), flight verification
3. **MEDIUM (3):** Guest confirmations, driver assignment (3+ days out)
4. **LOW (4):** Coverage gaps 5+ days out, manual cleanup

Within same priority: sorted by `due_at ASC`.

### Queue UI: `/dispatching/task-queue/`
- Summary bar with count badges per task type
- Filter tabs: All | My Tasks | By Type | Overdue Only
- Priority-sorted table with color-coded rows
- Quick actions: Claim, Complete, Snooze, Log Call
- Task detail panel with communication history and logging form
- Navbar badge showing pending task count

---

## 6. Activity Logging & Performance Tracking

**Active (from task system):** Task claimed/completed/snoozed, communication attempts, response times
**Passive (via middleware):** Page views on dispatching URLs, deduplicated to 5-minute windows

### Meaningful Metrics
- Lead response time (creation → first contact)
- Tasks completed per day per staff
- Overdue task count
- Payment chase success rate
- Follow-up completion rate

---

## 7. Owner Dashboard: `/dispatching/staff-metrics/`

Superuser-only. Shows:
- **Daily:** Open tasks by type/priority, overdue tasks, staff activity timeline
- **Weekly:** Tasks completed per staff, communication volume, payment collection rate
- **Monthly:** Reservations booked per staff, lead conversion rate, response time trends

---

## 8. Implementation Phases

### Phase 1: Core Model + Manual Task Queue (MVP)
- Create `ops/` app with all 3 models
- Register in settings, migration
- Task queue view + template + URL
- API endpoints: claim, complete, snooze, create manual task
- Navbar badge via context processor

### Phase 2: Automated Task Generation + Auto-Close
- Signal-based triggers on Lead, Reservation, Leg, Payment
- Scheduler-based scanning for flight mismatches, confirmations, coverage gaps
- Auto-close logic
- Dedup logic

### Phase 3: Communication Logging + Soft Flight Warning
- `log_communication()` helper
- Task detail view with communication history + logging form
- Soft warning in confirmations view (non-blocking)
- Integration with existing email/SMS flows

### Phase 4: Owner Dashboard + Staff Activity Tracking
- StaffActivityMiddleware (passive, invisible to staff)
- Superuser-only metrics dashboard with Chart.js
- Aggregation queries for response times, throughput, queue health

### Phase 5: Escalation Engine + NTFY + Polish
- Rule engine for auto-escalation
- NTFY integration for escalated tasks
- Snooze expiration processing
- Queue UI refinements

---

## 9. Risks & Tradeoffs

- **Unified vs separate task tables:** Unified. Single `OperationalTask` with `task_type` discriminator.
- **Threads vs Celery:** Stay with threads. 30-min scanning is acceptable latency.
- **Staff workflow complexity:** Start with queue as supplementary view, not replacement.
- **False accountability:** Focus on outcome metrics (response time, completion rate), not activity metrics (page views, clicks).

---

## 10. Files to Create
- `ops/__init__.py`, `ops/apps.py`, `ops/admin.py`
- `ops/models.py` — OperationalTask, CommunicationAttempt, StaffActivity
- `ops/tasks.py` — Scanner functions
- `ops/signals.py` — Task creation/closure on model events
- `ops/services.py` — log_communication(), create_task() helpers
- `ops/middleware.py` — StaffActivityMiddleware (Phase 4)
- `ops/context_processors.py` — pending_task_count for navbar
- `ops/escalation.py` — Escalation rule engine (Phase 5)
- `dispatching/templates/dispatching/task_queue.html`
- `dispatching/templates/dispatching/task_detail.html`
- `dispatching/templates/dispatching/staff_metrics.html` (Phase 4)

## Files to Modify
- `business/settings.py` — Add `ops` to INSTALLED_APPS, context processor, middleware
- `dispatching/urls.py` — Add task-queue, task-detail, staff-metrics URLs
- `dispatching/views.py` — Add task_queue_view, staff_metrics_view; modify confirmations_view for soft warning
- `reservations/signals.py` — Add task creation triggers
- `ghl_integration/scheduler.py` — Add ops task generation to 30-min loop
- `payment/webhook.py` — Add auto-close of payment_chase on payment success
- `users/emails.py` — Add comm logging to payment reminder AJAX
- `dispatching/templates/dispatching/confirmations.html` — Add soft warning for unverified flights
- `dispatching/templates/dispatching/dispatcher_navbar.html` — Add task count badge
