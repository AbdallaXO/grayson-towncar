# Staff Metrics System

Complete reference for the Staff Metrics auditing and productivity system.

---

## Current Implementation (Completed)

### Phase 0: Bug Fixes

| Fix | Description | Files |
|-----|-------------|-------|
| CSS truncation | Change history values (private notes, locations) no longer cut off at 150px | `staff_detail.html` |
| Signal-cascaded fields | When a driver is reassigned, auto-set pay fields (`driver_base_pay`, `driver_gratuity`, `driver_additional`) are filtered out of change history | `ops/views.py` |
| Driver name resolution | Change history shows driver names instead of FK IDs | `ops/views.py` |
| Clickable legs/reservations | "Leg #427" in change history links to the reservation via UUID | `staff_detail.html` |
| 12-hour time format | Pickup times show "9:53 AM" instead of "09:53:00" | `ops/views.py` |
| Footer removed | Staff Metrics and Staff Detail pages no longer show the site footer | `staff_metrics.html` |

### Phase 1: Quick Wins (View + Template Changes Only)

All of these use data that was already being recorded but not surfaced.

| Feature | Description | Data Source |
|---------|-------------|-------------|
| **First / Last Active** | Table showing when each staff member was first and last seen today | `StaffActivity` MIN/MAX timestamps |
| **Key Actions by Staff** | Driver assignments, payment actions, status changes per staff | `AuditLog` grouped by user + action |
| **Reservations & Legs Modified** | Count of unique legs and reservations each person edited (not just created) | `Leg.history` / `Reservation.history` |
| **Staleness Badges** | Assigned tasks on staff detail show color-coded age: green (<1d), yellow (1-3d), red (3+d) | `OperationalTask.created_at` |
| **Full-Range Activity Timeline** | Overview timeline shows the full selected range (up to 200 actions), not just today | `StaffActivity` |
| **Corrections & Overrides** | Flags when the same field on a leg was changed by a different user within 24 hours | `Leg.history` cross-user comparison |

### Phase 2: Email Tracking

| Component | Description | Files |
|-----------|-------------|-------|
| **EmailLog model** | New model tracking every email sent: type, sender, recipient, reservation, subject, success, timestamp | `ops/models.py` |
| **Logging in email functions** | 6 email functions now create EmailLog records on successful send | `users/emails.py` |
| **sent_by plumbing** | `request.user` passes through views -> services -> email functions as `sent_by` | `dispatching/views.py`, `users/services.py`, `users/emails.py` |
| **Overview: Emails Sent by Staff** | Table with confirmations, payment reminders, statements, total per staff | `staff_metrics.html` |
| **Detail: Emails Sent** | Per-staff email type breakdown grid + recent email list (last 25) | `staff_detail.html` |

**Email types tracked:** Reservation Confirmation, Payment Reminder, Driver Payment Statement, Agent Commission Statement, Agency Commission Statement, Lead Quote, Admin Report

### Phase 3: Enhanced Views

| Feature | Description | Files |
|---------|-------------|-------|
| **Per-Staff Trend Indicators** | Each staff table row (completions, comms, reservations, emails) shows a green/red arrow with percentage change comparing current period to prior equivalent period. Tooltip shows prior count. | `ops/views.py`, `staff_metrics.html`, `ops/templatetags/ops_filters.py` |
| **Daily Staff Summary Card** | JS-driven accordion on overview: each day in the range expands to a compact table showing per-staff first/last active, reservations, revenue, tasks resolved, driver assigns, legs modified, comms, emails. Most recent day expanded by default. "Show more" toggle for ranges > 30 days. | `ops/views.py`, `staff_metrics.html` |
| **Unified Chronological Action Feed** | On staff detail page, merges StaffActivity, CommunicationAttempt, EmailLog, change history, and AuditLog into one scrollable timeline sorted by timestamp. Filter pills (All, Changes, Emails, Comms, Tasks, Audits) with count badges. | `ops/views.py`, `staff_detail.html` |

---

## Architecture

### Files

| File | Purpose |
|------|---------|
| `ops/models.py` | `OperationalTask`, `CommunicationAttempt`, `StaffActivity`, `EmailLog` |
| `ops/views.py` | `staff_metrics_view` (overview), `staff_detail_view` (per-staff) |
| `ops/middleware.py` | `StaffActivityMiddleware` — page view tracking on `/dispatching/` |
| `ops/signals.py` | Auto-close tasks on payment/driver assignment/cancellation |
| `ops/tasks.py` | Automated scanners that generate operational tasks every 30 min |
| `ops/services.py` | Task creation/closure helpers, commission payout orchestration |
| `ops/admin.py` | Django admin for all ops models |
| `ops/templatetags/ops_filters.py` | Template filters: `get_item`, `trend_key`, `get_trend` |
| `ops/management/commands/seed_staff_metrics.py` | Seed realistic test data |
| `reservations/models.py` | `AuditLog` model, `Reservation`/`Leg` with django-simple-history |
| `reservations/signals.py` | Creates `AuditLog` entries on Reservation/Leg save |
| `users/emails.py` | All email send functions with `log_email_sent()` calls |
| `users/services.py` | Commission payout processing (forwards `sent_by`) |
| `dispatching/urls.py` | Routes: `/staff-metrics/`, `/staff-metrics/<user_id>/` |
| `dispatching/templates/dispatching/staff_metrics.html` | Overview dashboard template |
| `dispatching/templates/dispatching/staff_detail.html` | Per-staff detail template |

### Data Sources

| Source | What It Provides | How It's Populated |
|--------|------------------|--------------------|
| `StaffActivity` | Page views, task actions (claim, complete, snooze, assign, create, comm_logged) | Middleware (page views) + explicit recording in views |
| `OperationalTask` | Task completions, assignments, resolution timing | Auto-scanners (30 min) + manual creation |
| `CommunicationAttempt` | Call/SMS/email attempts with outcomes | Manual logging via task queue UI |
| `EmailLog` | Every email sent from the system with type and sender | `log_email_sent()` in email functions |
| `AuditLog` | Driver assignments, payment processing, status changes | Signals on Reservation/Leg save |
| `Reservation.history` | Field-level change tracking (django-simple-history) | Automatic on every save |
| `Leg.history` | Field-level change tracking (django-simple-history) | Automatic on every save |
| `Reservation.created_by` | Who created each reservation + revenue | Set on creation |

### What Staff Metrics Currently Answers

| Question | Answer Available? | How |
|----------|-------------------|-----|
| Who was active first today? | Yes | First/Last Active table (StaffActivity MIN) |
| Who was active last today? | Yes | First/Last Active table (StaffActivity MAX) |
| Who completed the most tasks? | Yes | Tasks Completed leaderboard |
| Who created the most reservations + revenue? | Yes | Reservations by Staff table |
| Who modified the most legs/reservations? | Yes | Modifications table |
| Who assigned the most drivers? | Yes | Key Actions table (AuditLog) |
| Who processed payments? | Yes | Key Actions table (AuditLog) |
| Who sent the most emails? | Yes | Emails Sent table (EmailLog) |
| What type of emails did they send? | Yes | Emails Sent breakdown (confirmations, reminders, statements) |
| Who communicated with customers? | Yes | Communication Volume table |
| Did someone's work get corrected? | Yes | Corrections & Overrides section |
| What fields did someone change? | Yes | Change History (staff detail, with old/new values) |
| Does someone have stale unresolved work? | Yes | Staleness badges on assigned tasks |
| What's the team's task creation vs completion trend? | Yes | Task Trend chart |
| How fast are leads being contacted? | Yes | Lead Response Times |
| Is someone trending up or down? | Yes | Trend indicators (arrows + % vs prior period) |
| What did each staff member do on a specific day? | Yes | Daily Staff Summary accordion |
| What's the full chronological story of someone's work? | Yes | Unified Action Feed on staff detail (filterable) |

---

## What's Next

### Phase 4: Accountability & Quality

| Feature | Description | Effort | Value |
|---------|-------------|--------|-------|
| **Login/Session Tracking** | Use Django's `user_logged_in` signal to record exact login timestamps. More reliable than first page view. | Small | Medium |
| **Task Time-to-Resolution** | Compute `resolved_at - created_at` per staff as an average. Show on overview and detail. | Small | Medium |
| **Workload Distribution Chart** | Visual showing how tasks are distributed across staff. Who's overloaded, who's idle. | Medium | Medium |
| **Productivity Scoring** | Weighted composite score per staff: reservations (high), tasks resolved, driver assigns, modifications, comms, emails. Configurable weights. | Large | High — single "productivity" number |
| **Manager Intervention Flags** | Auto-detect when a supervisor modifies another staff member's work. Requires defining supervisor relationships. | Medium | High |

### Phase 5: Advanced Reporting

| Feature | Description | Effort | Value |
|---------|-------------|--------|-------|
| **Shift Coverage View** | Gantt-style visual showing when each staff member was active, with gaps highlighted | Large | Medium |
| **Weekly/Monthly PDF Reports** | Auto-generated summary reports per staff for review meetings | Large | Medium |
| **Real-time Activity Feed** | WebSocket or polling-based live stream on overview | Large | Low (nice to have) |
| **Export to CSV** | Download staff metrics data for external analysis | Small | Medium |

---

## Seed Data

A management command generates realistic test data for all 3 staff members:

```bash
# Create seed data (one full simulated work day)
python manage.py seed_staff_metrics

# Clear seed data only (tagged records, won't touch real data)
python manage.py seed_staff_metrics --clear
```

Seed data is tagged with `metadata.seed=True` or `[SEED]` in notes and can be safely removed without affecting real data.

---

## Access

- **Overview:** `/dispatching/staff-metrics/` (superuser only)
- **Staff Detail:** `/dispatching/staff-metrics/<user_id>/` (superuser only)
- **Admin:** Django admin for `OperationalTask`, `CommunicationAttempt`, `StaffActivity`, `EmailLog`
