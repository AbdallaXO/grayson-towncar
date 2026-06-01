# Sales Pipeline & Lead Follow-Up Automation — Review

**Audit date:** 2026-06-01
**Scope:** The full lead lifecycle (`Lead → Quote → Reservation → Leg`) and the GoHighLevel (GHL) SMS follow-up automation that nurtures leads toward a booking.
**Method:** Direct code inspection. Every claim below is cited to `file:line` in the current tree.

---

## 1. Executive Summary

A web quote-form submission creates a **Lead** (and a **Quote** record). A lightweight in-process background scheduler picks the lead up within ~30 minutes, syncs it to GHL, and sends an initial SMS (falling back to email if SMS fails). It then runs a **5-step follow-up sequence** (delays 0h / 4h / 20h / 48h / 96h) with per-segment editable message templates, all confined to an **8 AM–9 PM Eastern** send window. The sequence stops automatically when the lead **replies**, **converts**, or the **trip date passes**. Replies are caught two ways — a GHL webhook and an API-polling fallback — and flag the lead for human follow-up. Leads whose pickup date passes without converting are auto-marked **LOST** hourly.

When a Reservation is created, a signal auto-converts the matching lead and cancels its sequence. The core engine is **healthy and actively running**. The weak points are at the *edges* of the pipeline: reply triage is under-surfaced (the `needs_human_follow_up` flag has no dashboard/alert), the `Quote` model and `COLD` status are effectively dead, and there is **no test coverage** on any of it.

### Scorecard

| Area | Status | Notes |
|---|---|---|
| Scheduler / triggering | ✅ Working | 30-min daemon thread, Postgres advisory lock |
| Initial SMS + email fallback | ✅ Working | Batched, send-window aware, phone-dedup |
| 5-step follow-up sequence | ✅ Working | Editable templates, per-segment |
| Reply detection | ✅ Working | Webhook + API-poll fallback |
| Sequence cancellation | ✅ Working | On reply / convert / date-passed |
| Lost-lead detection | ✅ Working | Hourly |
| Send window enforcement | ✅ Working | 8 AM–9 PM ET, hard guard |
| Dead-letter retry queue | ✅ Working | `GHLSyncLog`, retries + 6h alert |
| Lead → Reservation conversion | ✅ Working | Auto `post_save` signal; matches email → phone |
| `COLD` status | ❌ Dead | Defined, never assigned |
| `Quote` model lifecycle | ❌ Orphaned | Created, never sent/accepted |
| `needs_human_follow_up` surfacing | ⚠️ Hidden | Flag set, no dashboard/alert |
| Automated tests | ❌ None | Zero coverage |

---

## 2. Pipeline Map

```
Web quote form
   │
   ▼
 Lead  ──(scheduler picks up)──►  initial SMS / email  ──►  5-step follow-up sequence
   │                                                              │
   │  (created alongside)                                stops on: reply │ convert │ date-passed
   ▼
 Quote (status: pending — never advances today)
   │
   ▼  (auto post_save signal matches lead by email/phone → converts + cancels sequence)
 Reservation ──► Leg(s) ──► payment + unpaid-reminder automation ──► completed
```

### Lead statuses — `reservations/models.py:2200`
`NEW` → `CONTACTED` → `INTERESTED` → `FUTURE_CONTACT` → `CONVERTED` / `LOST` / `COLD`

- **NEW** — created from the form, not yet contacted.
- **CONTACTED** — initial SMS/email sent.
- **INTERESTED / FUTURE_CONTACT** — manual triage states.
- **CONVERTED** — auto-linked to a Reservation by the `auto_convert_lead_on_reservation` signal on booking (manual admin action is a fallback).
- **LOST** — pickup date passed without conversion (auto, hourly).
- **COLD** — *defined but never set by code* (see Finding 1).

### Lead segments (drive which template set is used)
`GENERAL`, `AIRPORT_TRANSFER`, `CRUISE_TRANSFER`, `THEME_PARK`, `LARGE_GROUP`, `REPEAT_CUSTOMER`, `ABANDONED_QUOTE` — classified by `classify_lead()` at sequence start.

### Quote statuses — `reservations/models.py:2372`
`PENDING` → `SENT` → `ACCEPTED` / `REJECTED` / `EXPIRED` — *only `PENDING` is ever used today* (see Finding 2).

---

## 3. How the Automation Is Triggered

There is **no Celery, cron, or external scheduler.** Everything runs in an in-process daemon thread.

- **Spawned from:** `GhlIntegrationConfig.ready()` → `ghl_integration/scheduler.py:start_scheduler()` (`scheduler.py:157`).
- **Cycle:** every **30 minutes** (`scheduler.py:27`, `INTERVAL_SECONDS = 30 * 60`); first run **+60 s** after startup (`scheduler.py:58`).
- **Single-leader guarantee:** a Postgres session advisory lock, ID `737_201` (`scheduler.py:30`, `_try_advisory_lock` at `scheduler.py:33`), so only **one** Gunicorn worker executes the batch each cycle. On SQLite/dev it always wins (single process).

### Tasks run each cycle (`_run_batch_tasks`, `scheduler.py:76`)

| # | Task | Cadence | Source |
|---|---|---|---|
| 1 | `batch_send_unsent_leads()` | every cycle | `scheduler.py:86` |
| 2 | `process_follow_up_batch()` | every cycle | `scheduler.py:94` |
| 3 | `retry_failed_syncs()` | every cycle | `scheduler.py:105` |
| 4 | `detect_lost_leads()` | every **2** cycles (~hourly) | `scheduler.py:112` |
| 5 | `alert_dead_letter_syncs()` | every **12** cycles (~6 h) | `scheduler.py:121` |
| 6 | `auto_refresh_flights()` | every cycle (tiered internally) | `scheduler.py:131` |
| 7 | `generate_ops_tasks()` | every cycle | `scheduler.py:141` |

> **Operational note:** because the engine lives inside Gunicorn, follow-ups only run while the web process is up. There is no standalone worker — a fully crashed/parked web process means no SMS goes out. The advisory lock keeps it to one worker, which is correct, but also means scheduler health = web health.

---

## 4. The Follow-Up Sequence

- **Initial contact** — `sync_lead_to_ghl_and_send_sms(lead_id)` (`ghl_integration/tasks.py:24`): creates/links a GHL contact, sends the step-1 SMS, falls back to email on SMS failure (sets `needs_human_follow_up`), sets `status=contacted`, then queues the rest of the sequence. Deduped by normalized phone (last 10 digits).
- **Sequence build** — `start_follow_up_sequence(lead_id)` (`tasks.py:421`): classifies the lead's segment, then creates `FollowUpTask` rows for steps 2–5.
- **Step delays:** step 1 = 0h, step 2 = **4h**, step 3 = **20h**, step 4 = **48h**, step 5 = **96h** — stored on editable `FollowUpSequence` templates (`ghl_integration/models.py:15`), so messaging changes need **no deploy**.
- **Work queue** — `FollowUpTask` (`models.py:55`): `PENDING / SENT / CANCELLED / FAILED / SKIPPED`, unique per `(lead, step)`, stores the rendered body for audit.
- **Engine** — `process_follow_up_batch()` (`tasks.py:526`): pulls due `PENDING` tasks and, **before each send**, re-checks the stop conditions and the send window.

### Stop conditions (checked at send time)
1. Lead has **replied** (`has_replied`).
2. Lead is **converted**.
3. **Trip date** has passed.
4. Safety-net: live GHL conversation check for an inbound reply (`tasks.py:609`).

Cancellation is centralised in `cancel_lead_sequence(lead_id, reason)` (`tasks.py:750`) — reasons: `replied`, `converted`, `expired_date`, `manual`. After step 5 with no reply, the sequence is marked **complete** (`sequence_active=False`, `sequence_completed_at`) — but the lead status is deliberately **not** changed (`tasks.py:707`).

### Send window — `ghl_integration/timing.py`
- **8:00 AM – 9:00 PM**, `America/New_York` (`timing.py:13-15`).
- Out-of-window sends are pushed to **8:15 AM** next morning (`adjust_to_send_window`, `timing.py:62`).
- Enforced at three points: when queuing the initial batch, when scheduling steps, and again at the moment of each send.

### 4.1 The actual message copy

> ⚠️ **Source vs. live data.** Only the **`general`** segment templates are seeded in code (migration `ghl_integration/migrations/0002_seed_followup_templates.py`). `FollowUpSequence` rows are editable in Django admin, so the **live database may contain additional segment-specific copy** (airport, cruise, theme-park, etc.) that is not visible from source. The text below is the seeded baseline. Placeholders `{first_name}`, `{pickup_location}`, `{dropoff_location}`, `{pickup_date}`, `{estimated_price}`, `{vehicle_name}` are filled per-lead at send time.

**Initial text (Step 1) — what's actually sent.** The live first SMS is **not** rendered from the Step-1 template; it's built by `get_sms_template()` (`ghl_integration/services.py:917`). The Step-1 `FollowUpSequence` row exists only so the record is logged as already-sent.

> Hey {first_name}, this is Grayson Towncar. Do you still need transportation from {pickup_location} to {dropoff_location} on {pickup_date}?

*(Pickup date is formatted as "Month DD", e.g. "June 14". An older doc, `docs/ghl_lead_automation_complete.md:299`, shows a "Reply YES to confirm or call 407-212-7190!" tail — that line is **not** in the live `services.py` version.)*

**Step 2 — +4h** (`general`)

> Hey {first_name}! Wanted to make sure my earlier message came through about your {pickup_location} → {dropoff_location} trip. A few things we handle that most don't expect: complimentary car seats, baggage claim meetup, and grocery stops along the way — no extra charge. Happy to hold your spot for {pickup_date} if you want to lock it in. Just say the word!

**Step 3 — +20h** (`general`)

> Hey {first_name} — honest question: still looking for a ride on {pickup_date}, or did you find something? Either way no worries! If you're still deciding, we've got 1,000+ five-star reviews and I'd love to answer anything before you book. Just shoot me a text 🙂

**Step 4 — +48h** (`general`)

> Hey {first_name}, your quoted rate of {estimated_price} for the {pickup_date} trip is still available — wanted to give you a heads up before it changes. I can send you the direct booking link if you want to grab it quick, takes under a minute. Want me to send it over?

**Step 5 — +96h** (`general`, final message)

> Hey {first_name}, last message from me — totally understand if your plans changed or you went another direction. If you ever need a ride in Orlando, we're always here. Take care! 🙏

**Email fallback** (when SMS fails — bad/UK number): `send_lead_quote_email()` (`users/emails.py:844`) sends an HTML quote email using the lead's current `Quote` (route + vehicle + a direct booking URL). Template: `users/templates/users/lead_quote_email.html`. The lead is then flagged `needs_human_follow_up=True` and gets **no** SMS sequence.

---

## 5. Reply Detection

Two independent paths, so a missed webhook doesn't strand a lead in the sequence:

1. **Webhook (primary)** — `ghl_integration/views.py:ghl_webhook`. On an inbound message it sets `has_replied=True` (`views.py:156`), `needs_human_follow_up=True` (`views.py:158`), upgrades priority, and calls `cancel_lead_sequence(lead.id, reason="replied")` (`views.py:189`). It matches **all** leads sharing the contact ID + phone so duplicates are all stopped.
2. **API-poll fallback (safety net)** — inside `process_follow_up_batch()` (`tasks.py:609`), before sending each step it calls `service.contact_has_replied(...)`; if true it sets the same flags and cancels the sequence. Worst-case detection latency ≈ one 30-min cycle.

### 5.1 Conversion (automatic)

When a new Reservation is saved, the `post_save` signal `auto_convert_lead_on_reservation` (`reservations/signals.py:206`) runs:

1. **Matches an open lead** — by `email__iexact` first, then by `normalized_phone` (last 10 digits, format-agnostic), restricted to leads in `new / contacted / interested / future_contact` (`signals.py:220-233`).
2. **Converts it** — sets `status=converted`, `converted=True`, `converted_at`, `converted_reservation=instance`, and appends an audit note (`signals.py:236-249`).
3. **Cancels the active sequence** — background `cancel_lead_sequence(reason="converted")` (`signals.py:252-256`).
4. **Applies GHL "converted" lifecycle tags** + logs a `LeadActivity` (`signals.py:263-288`).

A manual admin action "Mark as Converted" (`reservations/admin.py:2253`) exists as a fallback/override. Historical gaps were backfilled (`reservations/migrations/0086_backfill_lead_converted_reservation.py`, plus the `backfill_lead_reservations` command).

---

## 6. Supporting Automation (context)

These overlap the customer-comms surface and are worth knowing alongside the lead pipeline:

- **Unpaid-reminder engine** — `ops/unpaid_reminders.py` (`UnpaidReminderEngine.process()`): 5-stage timeline (booking +2h, +24h, pickup −3d, pickup −24h, pickup −2h flag) with duplicate detection, a travel-agent exclusion toggle, staff-hold override, and a 6h adjacency throttle. Run via `generate_ops_tasks()` and the `send_unpaid_reminders` management command (`--dry-run` supported).
- **Ops task scanners** — `ops/tasks.py:generate_ops_tasks()`: flight mismatches, driver overlaps, unassigned legs, uncontacted contact-forms, confirmation-text batching, auto-close/snooze. These create `OperationalTask` rows for staff.
- **Email infrastructure** — `users/emails.py`, Django `EmailMultiAlternatives` from `reservations@graysontowncar.com`, HTML templates under `users/templates/users/`. Logged to `ops.models.EmailLog`.

---

## 7. Findings & Recommendations

Strengths first — the load-bearing parts are solid: **automatic lead→reservation conversion** (signal cancels the sequence the moment they book), **dual-path reply detection**, **phone-normalized dedup** (prevents double-texting the same person across duplicate leads), **strict send-window enforcement**, an **editable template system** (no redeploy to change messaging), and a **dead-letter retry queue** (`GHLSyncLog`) so transient GHL failures self-heal.

The gaps, prioritized:

| # | Finding | Severity | Recommendation |
|---|---|---|---|
| 1 | **`COLD` status never assigned.** Defined at `reservations/models.py:2208` but no code path sets it. After step 5 with no reply the lead silently stays `INTERESTED`/`CONTACTED` (`tasks.py:707`). | Medium | Either auto-set `COLD` when `sequence_completed_at` is reached with no reply, or delete the unused state to remove the false impression that cold-leads are tracked. |
| 2 | **`Quote` model orphaned.** A `Quote` is created on every form submit, always `status=pending` (`reservations/views.py` ~`539`); nothing ever sends a quote or moves it to `SENT`/`ACCEPTED`. No quote email exists. | Medium | Decide its fate: wire a real "send quote" flow with status transitions + a quote email, *or* drop the model so it stops implying a lifecycle that isn't there. |
| 3 | **Conversion auto-match has edge cases.** Auto-conversion works (`signals.py:206`), but matching is exact-email-then-normalized-phone and uses `.first()`. A lead who books with a *different* email **and** phone than they used on the form won't be matched (sequence keeps running until reply/date-passed); and if multiple open leads match, only one converts. | Low | Acceptable as-is for most cases. Optional: log unmatched new-Reservation customers for periodic manual reconciliation, and consider fuzzy/last-name matching. |
| 4 | **`needs_human_follow_up` set but not surfaced.** The flag is set correctly on reply (`views.py:158`, `tasks.py:614`), email-fallback, and stale-rescue — but there is no dashboard view or alert. Staff must hunt for it in Django admin. | **High** | Add a "Leads needing follow-up" list (filtered queryset on the staff dashboard) and/or an ntfy/email alert to the sales team the moment a lead replies. The reply is the hottest moment in the funnel — it shouldn't depend on someone refreshing admin. |
| 5 | **Zero automated tests** for the follow-up suite (`ghl_integration/tests.py` is an empty placeholder). | **High** | Add tests covering: stop-condition short-circuits, send-window adjustment, phone dedup, reply detection (both paths), and sequence cancellation reasons. These are pure-logic functions and cheap to test. |
| 6 | **No SMS on lead creation.** Initial contact waits for the next 30-min batch (`batch_send_unsent_leads`), plus send-window delay — so an evening lead may not hear back until 8:15 AM. | Low/Medium | Optionally fire `sync_lead_to_ghl_and_send_sms(lead_id)` in a background thread from the form-submit view (still inside the send window) for near-instant first contact; keep the batch as the safety net. |
| 7 | **GHL is a single point of failure** for all outbound SMS. The dead-letter queue mitigates transient errors, but unresolved failures are only alerted every ~6h (`scheduler.py:121`). | Low | Confirm the 6h dead-letter alert cadence is acceptable for sales urgency; tighten if a stuck queue for half a day is too long. |

---

## 8. Appendix — File Reference Map

| Component | Location |
|---|---|
| Lead model | `reservations/models.py:2200` |
| Quote model | `reservations/models.py:2372` |
| Lead creation (quote form view) | `reservations/views.py` (`QuoteFormHandlerView`) |
| Auto-conversion signal | `reservations/signals.py:206` (`auto_convert_lead_on_reservation`) |
| Manual conversion action (fallback) | `reservations/admin.py:2253` |
| Scheduler (daemon thread) | `ghl_integration/scheduler.py` |
| Scheduler startup hook | `ghl_integration/apps.py:ready()` |
| Initial SMS + email fallback | `ghl_integration/tasks.py:24` |
| Batch initial sends | `ghl_integration/tasks.py:256` |
| Sequence builder | `ghl_integration/tasks.py:421` |
| Follow-up engine | `ghl_integration/tasks.py:526` |
| Cancel sequence | `ghl_integration/tasks.py:750` |
| Lost-lead detection | `ghl_integration/tasks.py:933` |
| Sequence / task / activity models | `ghl_integration/models.py:15` |
| Initial-SMS copy (live) | `ghl_integration/services.py:917` (`get_sms_template`) |
| Follow-up step copy (seeded) | `ghl_integration/migrations/0002_seed_followup_templates.py` |
| Email-fallback copy | `users/emails.py:844`, template `users/templates/users/lead_quote_email.html` |
| Reply webhook | `ghl_integration/views.py:70` |
| Send-window timing | `ghl_integration/timing.py` |
| GHL service (SMS, reply poll) | `ghl_integration/services.py` |
| Unpaid-reminder engine | `ops/unpaid_reminders.py` |
| Ops task scanners | `ops/tasks.py:236` |
| Email sending + logging | `users/emails.py`, `ops/models.py:363` (`EmailLog`) |

> This is a point-in-time audit. If you want me to act on any finding — the follow-up dashboard/alert for replied leads (#4) and a test suite for the automation (#5) are the highest-leverage — say which and I'll scope it as a separate change.
