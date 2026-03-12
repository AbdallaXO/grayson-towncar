# Lead Automation System

**Last updated**: 2026-03-11

---

## What This Is

Grayson Towncar is a luxury transportation company in Orlando. Customers find us through Google Ads, Meta Ads, and organic search, then submit a quote request through the website. That quote request creates a **Lead** in our system and syncs it to **GoHighLevel (GHL)**, our CRM.

Before this system was built, the lead flow looked like this:

1. Customer submits quote form
2. Lead gets synced to GHL
3. One generic SMS gets sent: *"Hey, do you still need transportation?"*
4. **Nothing else ever happens**

No follow-ups, no lead nurturing, no segmentation, no retry on failures, no pipeline visibility. If the customer didn't respond to that single text, the lead was effectively dead. This was almost certainly causing significant lost revenue — industry data shows 80% of sales require 5+ follow-ups, and we were doing exactly one.

## The Goal

Build an automated outbound lead nurturing system that:

- **Sends up to 5 follow-up SMS messages** over 4 days, spaced intelligently
- **Segments leads by trip type** (airport transfer, cruise, Disney, large group, etc.) for future message customization
- **Respects a send window** (8 AM - 9 PM Eastern only) — no texts at 2 AM
- **Stops automatically** when a lead replies, converts to a booking, or their trip date passes
- **Tracks everything** — every SMS sent, every reply, every conversion, every failed sync
- **Never loses a lead** — failed GHL API calls get logged, retried with exponential backoff, and alert us when they permanently fail
- **Tags leads in GHL** at every lifecycle stage so the CRM reflects reality (new-lead, sms-sent, replied, hot-lead, converted, etc.)
- **Provides analytics** — conversion funnels, revenue by source, follow-up effectiveness, pipeline health

This is an **outbound-only** automation system. Humans own all inbound conversations. The system's job is to get the lead to respond — not to respond back.

## How It Works

### Lead Flow

```
Customer submits quote form on graysontowncar.com
    |
    v
QuoteFormHandlerView creates Lead + Quote
    |-- Captures UTM data (gclid, fbclid, utm_source, etc.)
    |-- Sets priority (HIGH if pickup within 14 days)
    |-- Sends ntfy notification to team
    |-- Fires Meta Conversions API "Lead" event
    |
    v
Signal: sync_lead_to_ghl_on_create (background thread)
    |-- Creates/updates contact in GHL
    |-- Applies lifecycle tags: new-lead, google-ads/meta-ads, urgent-trip
    |-- Logs sync to GHLSyncLog
    |
    v
Scheduler (every 30 min): batch_send_unsent_leads
    |-- Finds leads with no initial SMS sent
    |-- For each: atomic claim -> create GHL contact -> send SMS -> mark CONTACTED
    |-- Logs every API call to GHLSyncLog
    |-- Applies "sms-sent" tag
    |-- Starts follow-up sequence (steps 2-5)
    |
    v
Follow-Up Engine (every 30 min): process_follow_up_batch
    |-- Checks for due FollowUpTasks within send window
    |-- For each: check stop conditions -> render template -> send via GHL
    |-- Stop conditions: replied, converted, trip date passed
    |-- After step 5: marks sequence complete, leaves status for human decision
    |
    v
If lead replies (GHL webhook):
    |-- Sets has_replied=True, upgrades priority
    |-- Cancels all pending follow-up tasks
    |-- Applies tags: replied, hot-lead
    |-- Sets needs_human_follow_up=True
    |-- Sends urgent ntfy notification to team
    |
    v
If lead books a reservation:
    |-- auto_convert_lead_on_reservation signal matches by email/phone
    |-- Marks lead CONVERTED, stores converted_reservation FK
    |-- Cancels all pending follow-up tasks
    |-- Applies tags: converted, customer
```

### Follow-Up Sequence

All timing is relative to when the initial SMS (Step 1) is sent:

| Step | Timing | Purpose |
|---|---|---|
| 1 | Immediate | Initial contact — "Do you still need transportation?" |
| 2 | +4 hours | Warm follow-up — highlight value-adds (car seats, meetup, grocery stops) |
| 3 | +20 hours | Social proof — reviews, low-pressure check-in |
| 4 | +2 days | Urgency — quoted rate still available |
| 5 | +4 days | Final — graceful close, leave door open |

After Step 5 completes with no reply: the sequence is marked complete but the lead is **not** automatically marked cold. That's a human decision.

### Send Window

Messages only go out between **8:00 AM and 9:00 PM US/Eastern**. If a task comes due at 11 PM, it gets rescheduled to 8:15 AM the next morning. The window is checked at the **moment of sending**, not just at scheduling time.

### Lead Segments

Leads are auto-classified based on pickup/dropoff location keywords:

| Segment | Detection |
|---|---|
| Airport Transfer | "MCO", "airport", "SFB", "OIA" in locations |
| Cruise Transfer | "port canaveral", "cruise", "terminal" in locations |
| Theme Park | "disney", "universal", "seaworld", "legoland" in locations |
| Large Group | Van14/Sprinter vehicle or price > $300 |
| Repeat Customer | Phone/email matches existing Customer with reservations |
| Abandoned Quote | Has Quote with status EXPIRED |
| General | Default fallback |

Currently all segments use the same universal message templates. Segment-specific variants can be added via Django admin without a code deploy.

### GHL Sync Reliability

Every GHL API call (create contact, send SMS, update status, add/remove tag) is logged to `GHLSyncLog`:

- **On success**: Logged as SUCCESS, resolved immediately
- **On failure**: Logged as FAILED, scheduled for retry with exponential backoff (5min, 15min, 45min, 2h, 6h)
- **After 5 failed attempts**: Promoted to DEAD_LETTER
- **Every 30 minutes**: `retry_failed_syncs` picks up failed entries due for retry
- **Every 6 hours**: `alert_dead_letter_syncs` sends an ntfy notification if there are unresolved dead letters

### GHL Lifecycle Tags

Tags are applied/removed at each lifecycle event so the CRM stays in sync:

| Event | Tags Added | Tags Removed |
|---|---|---|
| Lead created | `new-lead`, `google-ads`/`meta-ads`, `urgent-trip` | — |
| Initial SMS sent | `sms-sent` | — |
| Lead replies | `replied`, `hot-lead` | `new-lead` |
| Status -> Interested | `interested` | `new-lead` |
| Converted to booking | `converted`, `customer` | `new-lead`, `hot-lead`, `interested` |
| Status -> Lost | `lost` | `hot-lead`, `interested` |
| Sequence completed | `sequence-complete` | — |

---

## Technical Architecture

### No Celery

The system does **not** use Celery. All background work runs via:

- **`ghl_integration/runner.py`**: `run_in_background(func, *args)` — spawns daemon threads for one-off tasks (replaces `.delay()`)
- **`ghl_integration/scheduler.py`**: A single background daemon thread that wakes up every 30 minutes to run batch tasks (replaces Celery beat)
- **`ghl_integration/apps.py`**: Starts the scheduler once per process. With `runserver`, only starts in the child process (`RUN_MAIN=true`). With Gunicorn, starts in each worker.

### Duplicate SMS Prevention

Two layers:

1. **Process-level**: The scheduler only starts in the reloader child process (not the parent watcher), preventing two schedulers from running under `runserver`
2. **Row-level**: `sync_lead_to_ghl_and_send_sms` uses `select_for_update()` to atomically claim a lead before sending. If two threads try the same lead, the second one sees `initial_sms_sent=True` and skips.

### Database Models

**In `reservations/models.py`:**
- `Lead` — contact info, trip details, status, priority, GHL fields, UTM tracking, follow-up fields (`segment`, `sequence_active`, `sequence_completed_at`, `needs_human_follow_up`, `converted_reservation` FK, `initial_sms_sent`, `initial_sms_sent_at`)
- `Quote` — FK to Lead, trip details, pricing, status

**In `ghl_integration/models.py`:**
- `FollowUpSequence` — message templates per (step_number, segment). Editable via Django admin.
- `FollowUpTask` — work queue. One row per scheduled message. Status: PENDING/SENT/CANCELLED/FAILED/SKIPPED.
- `LeadActivity` — audit log. Every SMS sent, reply received, conversion, sequence start/stop/complete.
- `GHLSyncLog` — dead letter queue. Every GHL API call logged with request/response payloads, retry scheduling, error details.

---

## Implementation Status

### Phase 1 — Foundation & Quick Wins -- COMPLETE
- New Lead model fields for follow-up tracking
- `COLD` status added to Lead
- `converted_reservation` FK for revenue attribution
- Batch interval reduced from 60 min to 30 min

### Phase 2 — Follow-Up Engine Core -- COMPLETE
- Segmentation engine, timing helpers, template renderer
- `FollowUpSequence`, `FollowUpTask`, `LeadActivity`, `GHLSyncLog` models
- Core tasks: `start_follow_up_sequence`, `process_follow_up_batch`, `cancel_lead_sequence`
- Background runner + scheduler (Celery fully removed)
- Duplicate SMS fix (atomic claim + process guard)
- Sequence cancellation on reply and conversion
- 5 seed message templates for "general" segment

### Phase 3 — GHL Sync Improvements -- COMPLETE
- Sync logging wired into all GHL API operations
- `retry_failed_syncs` task (every 30 min, exponential backoff)
- `alert_dead_letter_syncs` task (every 6 hours, ntfy alerts)
- Lifecycle tag management (`apply_lifecycle_tags` with 13+ tag types)
- Tags wired into: lead creation, SMS send, webhook reply, auto-conversion
- Reply and conversion activity logging
- **Lost lead auto-detection**: `detect_lost_leads` task runs hourly, marks leads as LOST when pickup date passes without conversion, logs transition details (previous status, days past pickup, contact attempts, had_replied), applies `lost` tag in GHL
- **Production safety migration** (`0083_backfill_initial_sms_sent`): sets `initial_sms_sent=True` for all existing non-new leads to prevent mass SMS resend on deploy
- **Analytics restricted to superusers**: dispatchers redirected to dashboard (cannot see revenue data)

### Phase 4 — Analytics Dashboard -- COMPLETE
- Full-page dashboard at `/dispatching/lead-analytics/` (superusers only)
- KPI cards, conversion funnel, daily trend chart (Chart.js)
- Segment performance, follow-up engine stats, UTM source breakdown
- Revenue by source, pipeline health alerts, automation status
- Date range filtering (7d/14d/30d/60d/90d)

### Phase 5 — Pipeline & Advanced Features -- NOT STARTED

| Task | Description |
|---|---|
| GHL Pipeline setup | Manually configure pipeline stages in GHL UI |
| Opportunity management | `create_opportunity` / `update_opportunity_stage` service methods |
| `ghl_opportunity_id` on Lead | Store GHL opportunity reference |
| Wire opportunities into signals | Auto-create/move on status changes |
| Expanded custom field mappings | Sync priority, UTM fields, has_replied, contact_attempts to GHL |
| Follow-up effectiveness metrics | Conversion-by-step stats on analytics dashboard |
| Sync health monitoring | Dashboard widget for sync success rates, dead letters |

**Prerequisites for Phase 5 (manual steps):**
1. Create a pipeline in GHL with stages: New Lead, Contacted, Interested, Quoted, Converted, Lost — get pipeline ID + stage IDs
2. Create custom fields in GHL for: Priority, UTM Source, UTM Medium, UTM Campaign, Has Replied, Contact Attempts — get field IDs
3. Verify GHL API permissions for Opportunities API

---

## Key Files

| File | What It Does |
|---|---|
| `ghl_integration/models.py` | FollowUpSequence, FollowUpTask, LeadActivity, GHLSyncLog |
| `ghl_integration/tasks.py` | All task functions — SMS sync, follow-up engine, retry, dead letter alerts |
| `ghl_integration/services.py` | GHL API service class, sync logging helpers, lifecycle tag management |
| `ghl_integration/scheduler.py` | Background daemon scheduler (replaces Celery beat) |
| `ghl_integration/runner.py` | `run_in_background()` — replaces Celery `.delay()` |
| `ghl_integration/apps.py` | Scheduler startup guard (child process / gunicorn worker only) |
| `ghl_integration/views.py` | GHL webhook handler for inbound SMS replies |
| `ghl_integration/segmentation.py` | `classify_lead()` — auto-classifies leads by trip type |
| `ghl_integration/timing.py` | Send window helpers — 8AM-9PM Eastern enforcement |
| `ghl_integration/templates_engine.py` | `render_follow_up_message()` — SMS template renderer |
| `ghl_integration/admin.py` | Django admin for all GHL integration models |
| `reservations/models.py` | Lead model with all follow-up and tracking fields |
| `reservations/signals.py` | Lead creation sync, status sync, auto-convert, lifecycle tags |
| `dispatching/views.py` | Lead analytics dashboard view (end of file) |
| `dispatching/templates/dispatching/lead_analytics.html` | Analytics dashboard template |

---

## Expected Business Impact

| Metric | Before | After |
|---|---|---|
| Initial response time | Up to 60 min | Under 30 min |
| Touch points per lead | 1 | Up to 5 |
| Leads lost to failed syncs | Unknown (silent failures) | Tracked, retried, alerted |
| Pipeline visibility | None | Full funnel in GHL with tags |
| Revenue attribution | None | Per-channel tracking via converted_reservation FK |
| Lead conversion rate (est.) | ~15% | ~25-30% (industry benchmarks for 5-touch sequences) |
