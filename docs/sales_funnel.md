# Grayson Towncar — Complete Funnel Audit

**Subject:** Supreme Transportations Orlando LLC (dba Grayson Towncar) — luxury private ground transportation, Orlando FL. ~100–120 legs/day across MCO airport transfers, Disney/Universal resort transfers, Port Canaveral cruise transfers, and family transport with child seats.

**Scope of this document:** Every step from "stranger sees an ad" to "completed paid trip" to "repeat / referral" — as actually implemented in the Django codebase at `c:\Users\14078\Desktop\grayson-towncar` and the integrations it touches (Stripe, Twilio-via-GHL, GoHighLevel, AeroAPI, Meta Conversions API). Things that live outside this repo are explicitly marked `[EXTERNAL]` or `[NEEDS MANUAL INPUT]`.

**Stack reference (for outside readers):**

- Django 5.1 on Railway, single Gunicorn worker, PostgreSQL.
- SMS: GoHighLevel ("GHL") is the SMS broker — Twilio is **not** used for inbound; outbound confirmations use Twilio directly.
- Payments: Stripe (Checkout Sessions + SetupIntents + off-session PaymentIntents).
- Flights: FlightAware AeroAPI.
- Ads/attribution: Meta Conversions API for `Lead` / `InitiateCheckout` / `Purchase` events. Google Ads is **only** attributed via `gclid` cookie capture; no server-side Google Ads Conversion API events are fired.
- Background work: custom daemon thread (`reservations/utils.py:_run_in_background`) + a custom 30-min loop scheduler (`ghl_integration/scheduler.py`). **No Celery beat**, despite session memory comment — `LEAD_AUTOMATION_STATUS.md` confirms Celery was fully removed.

**Internal vocabulary used throughout (defined once):**

- **Lead** (`reservations/models.py:Lead`) — record created when a stranger submits the quote form. Contact info + trip context + UTM + status (`new` → `contacted` → `interested` → `converted` / `lost` / `cold`).
- **Quote** (`reservations/models.py:Quote`) — a child of Lead. One Lead can accumulate multiple Quotes (re-quotes for the same intent). There is **no separate Reservation-side Quote**; reservation pricing is computed once and stored on the Reservation row.
- **Customer** (`reservations/models.py:Customer`) — person actually booking. Linked to Stripe customer ID + saved card.
- **Reservation** (`reservations/models.py:Reservation`) — a booking. Holds pricing, attribution, payment state.
- **Leg** (`reservations/models.py:Leg`) — one pickup→dropoff within a Reservation. A round-trip = 2 Legs. Dispatch operates on Legs, not Reservations.
- **TravelAgent** (`users.TravelAgent`) — logged-in affiliate who books on behalf of guests; earns commission post-trip.
- **GHL** — GoHighLevel CRM. Holds contact records, owns the SMS channel, fires webhooks back to Django on inbound messages.

---

## 1. AWARENESS / TRAFFIC `[EXTERNAL — partial code evidence]`

### Definition

Stranger encounters Grayson Towncar via paid ads, organic search, referral, or direct.

### ENTRY

- **Google Ads** — paid search. Evidence inside the repo: `gclid` cookie is read in `reservations/views.py:QuoteFormHandlerView` and on the public booking POST in `reservations/views.py:reservation_form`. Attribution helper `reservations/attribution.py` maps `gclid present → "google_ads"`, otherwise falls back to UTM rules. Persisted as `Reservation.attribution_source` (migration `0093`) and on `Lead.utm_*` fields.
- **Meta (Facebook/Instagram) Ads** — same pattern via `fbclid`. Also captured server-side: `_fbp` and `_fbc` cookies are forwarded to Meta CAPI in `reservations/conversions.py:send_purchase_event` (lines 128–132) for ad-side deduplication.
- **Organic search / direct / referral** — `utm_source` rules in `reservations/attribution.py`; "google" without `gclid` → `"google_organic"`.
- `[NEEDS MANUAL INPUT]` Actual ad creative, campaign structure, daily budget, audiences, keywords — none of this lives in the repo.

### WHAT THE SYSTEM DOES

Captures `gclid`, `fbclid`, `utm_source`, `utm_medium`, `utm_campaign`, `utm_term`, `utm_content` from query string into cookies (handled in the request middleware/cookie stamping — visible because the booking view reads them out of either POST or cookies). These persist for the session so a visitor who clicks an ad, browses several pages, and then quotes still gets the source attached.

### HUMAN TOUCH

None at this stage.

### CONVERSION MECHANIC

Visitor reaches a quote-able surface: either `/get-a-quote/` (the dedicated form page, currently staff-protected per the explorer report), `/book-orlando-transportation/<rate_pk>` (direct deep-link), or `/rates-booking/` (rate list). The "Get a Quote" form is the dominant first-touch.

### MEASUREMENT

- **Tracked:** UTM + click IDs land on every Lead and Reservation row (see `Lead` model + migration 0093 backfilling `attribution_source` on Reservation).
- **Not tracked server-side:** any traffic that bounces before submitting the quote form. Frontend GA/Pixel may cover that, but no server-side awareness ping is recorded — and there is **no Google Ads server-side conversion** (Meta CAPI only).

### KNOWN GAPS / RISKS

- The `/get-a-quote/` route in `reservations/views_quote_experiment.py:guest_quote_page` is gated to staff (explorer notes "experimental, staff-protected") — if that is still true in production, organic visitors cannot reach the dedicated quote page and must use `/book-orlando-transportation/<pk>`. **Confirm in production whether the gating was lifted.**
- No Google Ads server-side conversion event. Only client-side gtag (if present in templates `[NEEDS MANUAL INPUT]`). This means optimizing Google Ads campaigns for actual bookings (not just form fills) is impossible without manually uploading offline conversions.

---

## 2. LEAD CAPTURE — Quote Form

### Definition

A stranger submits the quote form. A `Lead` and `Quote` row are created.

### ENTRY

1. **AJAX endpoint:** `POST /quote-form-handler/` → `reservations/views.py:QuoteFormHandlerView` (line 324). This is the public path most leads come through.
2. **Page form:** `GET /get-a-quote/` → `reservations/views_quote_experiment.py:guest_quote_page` (staff-protected — flagged above).
3. **Contact form:** `POST /users/contact-grayson-towncar/` → `users/views.py:contact` (line 76). Creates a `ContactUsForm` row, **not** a Lead. Picked up downstream by the Ops task queue for manual triage. Honeypot field `website` + 3-second submission floor + Cyrillic/URL/spam-keyword filter (`users/forms.py:ContactUsFormSubmission`).

### WHAT THE SYSTEM DOES (for the quote-form path)

On POST to `/quote-form-handler/`:

1. **Form validation** via `reservations/forms.py:LeadForm` (line 367) — captures `first_name`, `last_name`, `email`, `phone`, `pickup_date`. Additional POST fields (`pickup_location`, `dropoff_location`, `vehicle_id`, `estimated_price`, `trip_type`) and UTM cookies are read manually.
2. **Duplicate detection:** within the last 7 days, same email OR same phone, same pickup/dropoff/date/trip_type → instead of creating a new Lead, attach a new `Quote` row to the existing Lead and reset its status if it was previously `lost`/`converted`. Priority is bumped to `high` if pickup is within 14 days.
3. **Lead created** with `status = "new"`, `priority = "high"` (if pickup ≤14d) or `"medium"`, `segment = "general"`, `normalized_phone` = last 10 digits (used for matching).
4. **Quote created** with `status = "pending"`, `is_current = True` (any prior `is_current` for the lead is flipped off).
5. **Signal `sync_lead_to_ghl_on_create`** (`reservations/signals.py:295`) fires in a daemon thread: creates/updates the GHL contact, stamps `lead.ghl_contact_id` and `ghl_synced_at`, applies "created" lifecycle tags. **No SMS is sent at this moment.** SMS is sent by the scheduler batch (§4).
6. **Background notifications**:
   - `send_lead_notification(lead)` → ntfy desktop/mobile push to the team.
   - `send_lead_event(lead, request)` → Meta Conversions API `Lead` event (hashed email/phone/name + client IP). `reservations/conversions.py:73`.

### HUMAN TOUCH

None at the moment of capture. The team gets an ntfy push so they know a lead came in.

### CONVERSION MECHANIC

There is no direct "move to next stage" action here. The lead now waits for the next 30-min scheduler tick to receive Step 1 of the SMS sequence (§4).

### MEASUREMENT

- **Tracked:** every Lead/Quote row in DB; `GHLSyncLog` entry for the contact create; Meta CAPI Lead event; ntfy push.
- **Not tracked:** time from page load to form submit; form-field abandonment; whether the visitor saw a price they didn't like; A/B-test variant id (no A/B framework present).

### KNOWN GAPS / RISKS

- Phone normalization is "last 10 digits" — a foreign phone or extension-suffixed phone will mis-match against an existing customer and create a duplicate Lead.
- The Contact Form `ContactUsForm` row is dropped into an ops queue with no automated SMS/email kickoff. If ops doesn't manually convert it to a Lead, it dies in the queue.
- ntfy is the only synchronous human-alert. If ntfy is down or muted, leads pile up silently for 30 minutes until the scheduler fires the SMS — and any failure between (e.g. GHL down) extends that gap.

---

## 3. AUTOMATED FIRST RESPONSE — Initial SMS

### Definition

The first text message Grayson sends a brand-new lead, automatically.

### ENTRY

Lead exists in DB with `initial_sms_sent = False`. Picked up by `batch_send_unsent_leads` (`ghl_integration/tasks.py:256`).

### WHAT THE SYSTEM DOES

1. **Scheduler tick** (`ghl_integration/scheduler.py`, `INTERVAL_SECONDS = 1800` = 30 min). Single Postgres advisory lock ensures only one Gunicorn worker per cycle runs the batch.
2. **Send window check** — `is_within_send_window()` (`ghl_integration/timing.py`): 8:00 AM – 9:00 PM US/Eastern. Outside the window, batch returns immediately; a lead created at 2 AM waits until 8 AM. Window is re-checked at the moment of sending, not just at scheduling.
3. **Atomic claim** with `select_for_update(skip_locked=True)` to prevent two workers double-sending the same lead.
4. **Skip conditions** (lines 58–94):
   - Lead already `contacted` or `converted` — skip.
   - Duplicate phone on another lead — skip (prevents double-texting round-trippers).
   - No phone → fall back to email via `get_sms_template(lead)` rendered into a fallback email template.
5. **Create / update GHL contact** via `service.create_or_update_contact(lead)` if not already.
6. **Send SMS** via `service.send_sms(contact_id, message)` (line 118). Message body comes from `get_sms_template(lead)` — currently a generic "Hey, do you still need transportation?" style opener per `LEAD_AUTOMATION_STATUS.md`. The actual text is editable in Django admin via `FollowUpSequence` rows.
7. **Stamp** `lead.initial_sms_sent = True`, `lead.status = "contacted"`, `lead.initial_sms_sent_at = now`.
8. **Log** to `GHLSyncLog` (action=`SEND_SMS`, status=`SUCCESS` or `FAILED`).
9. **Apply GHL tag** `sms-sent` via `apply_lifecycle_tags`.
10. **Kick off the 5-step follow-up sequence** by calling `start_follow_up_sequence(lead_id)` (next stage).

### HUMAN TOUCH

None. Entirely automated.

### CONVERSION MECHANIC

Goal of Step 1 is to get a reply. A reply triggers the GHL webhook (§5) and **cancels** the sequence — human takes over.

### MEASUREMENT

- **Tracked:** `Lead.initial_sms_sent`, `initial_sms_sent_at`. `GHLSyncLog` rows for every attempt. `LeadActivity` row `SMS_SENT`.
- **Not tracked:** which message variant was sent (everything is the "general" segment template — there are no variant slots until segmentation is wired). Open rate / delivery receipt are not surfaced (GHL has them but they aren't pulled back to Django).

### KNOWN GAPS / RISKS

- **Up to 30-minute response gap.** A lead submitted at 8:01 AM gets a text around 8:30 AM. Industry benchmark for first contact is <5 min. The session memory's "Expected impact" table claims "<30 min" — that is the design intent, not a fast first response.
- **No Twilio fallback.** If GHL API is degraded, the SMS doesn't go out and the lead sits in `new` until retry. `retry_failed_syncs` (every 30 min) will retry up to 5 times with exponential backoff (5m, 15m, 45m, 2h, 6h) before dead-lettering, but the lead's mental clock has already started.
- **Send-window edge case:** a high-priority same-day pickup quoted at 10 PM still waits until 8 AM next morning. There is no "urgent / pickup is tomorrow" override in `is_within_send_window`.

---

## 4. AUTOMATED NURTURE — 5-Message Follow-Up Sequence

### Definition

Up to 5 SMS messages, scheduled relative to Step 1 send time, designed to stop on reply or conversion. Implemented entirely in `ghl_integration/tasks.py`.

### ENTRY

Step 1 just sent. `start_follow_up_sequence(lead_id)` (line 421) called from the end of `sync_lead_to_ghl_and_send_sms`.

### WHAT THE SYSTEM DOES

**Timing constants** (`tasks.py:412`, confirmed verbatim):

```
STEP_DELAYS = {1: 0, 2: 4, 3: 20, 4: 48, 5: 96}  # hours
```

So relative to Step 1:

- Step 2: +4 hours — warm follow-up (value-adds: car seats, meetup, grocery stops).
- Step 3: +20 hours — social proof / low-pressure check-in.
- Step 4: +48 hours — urgency / quoted rate still available.
- Step 5: +96 hours — final graceful close.

**Models:**

- `FollowUpSequence` (`ghl_integration/models.py:15`) — message templates keyed by `(step_number, segment)`. Segments: `general`, `airport_transfer`, `cruise_transfer`, `theme_park`, `large_group`, `repeat_customer`, `abandoned_quote`. Currently every lead resolves to `general` per `LEAD_AUTOMATION_STATUS.md`.
- `FollowUpTask` — one row per scheduled message, with `status` in `{PENDING, SENT, CANCELLED, FAILED, SKIPPED}`, indexed on `(status, scheduled_at)`.

**`start_follow_up_sequence` actions:**

1. Skip if lead is already converted, has no phone, or sequence already active (lines 437–464).
2. Classify lead via `segmentation.classify_lead(lead)` (location keyword match — `mco/airport/sfb/oia` → airport, `port canaveral/cruise/terminal` → cruise, etc.).
3. Compute `scheduled_at` for steps 2–5 using `STEP_DELAYS`, then push each through `adjust_to_send_window(...)` to slide any after-9PM time forward to 8:15 AM next morning.
4. Insert FollowUpTask rows in `PENDING`. Audit FollowUpTask for step 1 inserted as `SENT`.
5. Log `LeadActivity(activity_type=SEQUENCE_STARTED)`.

**`process_follow_up_batch` (every 30 min, `ghl_integration/tasks.py:526`)** is the worker that actually sends:

- Find `PENDING` tasks where `scheduled_at <= now`, batch cap 100.
- For each, lock with `select_for_update(skip_locked=True)`.
- **Stop conditions** (must be re-checked at send time):
  1. `lead.has_replied = True` → cancel.
  2. `lead.converted = True` → cancel.
  3. `lead.pickup_date < now.date()` → cancel (trip already passed).
  4. `service.contact_has_replied(contact_id)` — extra defense against missed GHL webhook (lines 609–625).
- Render template via `render_follow_up_message(template, lead)` (`templates_engine.py`) — fills `{first_name}`, `{pickup_location}`, `{dropoff_location}`, `{pickup_date}`, `{estimated_price}`, `{vehicle_name}`.
- Send via `service.send_sms`. Rate limit 1 second between sends.
- On success: mark task `SENT`, increment `lead.contact_attempts`, log `LeadActivity(SMS_SENT)`.
- After Step 5: mark `lead.sequence_active = False`, `sequence_completed_at = now`. **Lead is NOT auto-marked cold** — that's a human decision.
- On failure: 3 retries then `FAILED`.

**Cancellation paths:**

- GHL webhook `ghl_integration/views.py:70` on inbound reply → `cancel_lead_sequence(lead_id, reason="replied")`.
- `auto_convert_lead_on_reservation` signal in `reservations/signals.py:206` on Reservation create → `cancel_lead_sequence(lead_id, reason="converted")`.
- `detect_lost_leads()` task (hourly) → cancels when `pickup_date < today`.

### HUMAN TOUCH

- A reply hits the GHL webhook (§5). That webhook **does not auto-reply** — it sets `needs_human_follow_up = True`, fires an urgent ntfy push titled `"Lead Replied: {name}"`, and stops the sequence. Outbound human reply happens manually in the GHL inbox.

### CONVERSION MECHANIC

The whole sequence exists to push a reply or a self-serve booking. Self-serve booking is via the reservation form (§7); replies are handled by humans in GHL who then either send a manual quote or walk the customer through booking on the phone.

### MEASUREMENT

- **Tracked:** every `LeadActivity` (`SMS_SENT`, `REPLY_RECEIVED`, `SEQUENCE_STARTED`, `SEQUENCE_COMPLETED`); `Lead.contact_attempts`; `GHLSyncLog` for delivery success/failure; analytics dashboard at `/dispatching/lead-analytics/` (superusers only) shows conversion funnel, segment performance, step-level send counts, source breakdown.
- **Not tracked:** per-step reply rate (you can derive it from `LeadActivity` joins, but no dashboard surfaces it — `Phase 5` of `LEAD_AUTOMATION_STATUS.md` lists "Follow-up effectiveness metrics" as **not started**).
- **Not tracked:** delivery receipts, undelivered numbers, opt-outs. STOP keywords are presumably handled by GHL/Twilio, but no model field reflects opt-out status server-side. **`[NEEDS MANUAL INPUT]` — confirm STOP handling in GHL.**

### KNOWN GAPS / RISKS

- **Templates not segmented.** `LEAD_AUTOMATION_STATUS.md` explicitly states: "Currently all segments use the same universal message templates." A Disney-family Lead and a cruise Lead receive identical copy. Major personalization opportunity left on the table.
- **No SMS at all if no phone.** Fallback email is sent for Step 1 only; the 5-step follow-up sequence is SMS-only and skips entirely. Email-only leads get one touch and are abandoned.
- **No human reply guarantee.** Inbound replies trigger an ntfy push, but if the team doesn't answer fast, the lead has been told they're interesting (reply received) and then experiences silence. There is no auto "thanks, we'll be right with you" reply.
- **Step 5 → ?** After Step 5, the lead is left in `contacted` / `interested` indefinitely until `detect_lost_leads` flips it to `lost` once the pickup date passes. There is no formal re-engagement campaign months later — see §13.

---

## 5. REPLY HANDLING — Inbound SMS via GHL Webhook

### Definition

A customer replies to any Grayson SMS. GHL receives it (GHL owns the number), then POSTs to Django.

### ENTRY

HTTPS POST to `/ghl/webhook/` → `ghl_integration/views.py:ghl_webhook` (line 70). `csrf_exempt`, `require_POST`.

### WHAT THE SYSTEM DOES

1. **Parse flexibly.** Accepts `type`/`event`/`eventType` and `contactId` from top level or `customData` nested. Recognizes inbound if keywords `inbound`, `replied`, `reply` appear in event type.
2. **Find ALL matching leads** via `_find_leads_for_webhook(contact_id, phone)`:
   - Primary match on `Lead.ghl_contact_id`.
   - Secondary match on last-10-digits of phone (covers cases where the same person submitted multiple quote forms with different trip details, creating multiple Lead rows).
3. **For every matching lead** (atomic):
   - `has_replied = True`, `last_reply_at = now`, `needs_human_follow_up = True`.
   - Priority upgrade: LOW→MEDIUM, MEDIUM→HIGH, HIGH/URGENT→URGENT.
   - Status: `new` or `contacted` → `interested`.
4. **Cancel sequence** via `cancel_lead_sequence(lead.id, reason="replied")` in background.
5. **Apply lifecycle tags** in GHL: add `replied`, `hot-lead`; remove `new-lead`. Best-effort background thread.
6. **Log** `LeadActivity(REPLY_RECEIVED)` with message preview and list of all updated lead IDs.
7. **ntfy push** (urgent priority): `"Lead Replied: {name}"` with phone, SMS preview, status. **This is synchronous** (blocks the webhook briefly) — comment says priority="urgent".

Response: `{"status": "success", "lead_ids": [...], "contact_id": "..."}`.

### HUMAN TOUCH

The entire reply conversation from this point is human-driven in GHL. There is no AI auto-reply path implemented in this repo. (The prompt to this audit assumed a "GHL AI agent" — see §13 for that gap.)

### CONVERSION MECHANIC

Closer (founder or dispatcher) reads the reply in GHL, replies manually, may send a payment link out-of-band or walk the customer through the booking form. **There is no in-DB record of who handled the reply, what they offered, or whether they sent a payment link** — that conversation lives only in GHL.

### MEASUREMENT

- **Tracked:** `has_replied`, `last_reply_at`, `needs_human_follow_up`, `LeadActivity` row, GHL tags.
- **Not tracked server-side:** outbound human reply, time-to-first-human-reply, what offer was made. None of that surfaces on `/dispatching/lead-analytics/`.

### KNOWN GAPS / RISKS

- **Single-person dependency.** If the ntfy receiver doesn't notice, the reply ages without response. There is no SLA escalation.
- **No de-dup on the webhook itself.** If GHL retries (network blip), the webhook re-runs all updates — idempotent for the boolean flags but it will log a second `REPLY_RECEIVED` activity and re-fire another ntfy. (`webhook` does not check for an existing `LeadActivity` in a tight window.)
- **`needs_human_follow_up = True` never gets cleared automatically.** Even after the lead converts (which sets `converted=True`), `needs_human_follow_up` stays True unless someone toggles it. So queue dashboards can look stale.

---

## 6. AFFILIATE / TRAVEL-AGENT PARALLEL FUNNEL

### Definition

A Travel Agent (logged-in affiliate) books on behalf of a guest. Runs as a sidecar to the public funnel and joins back at payment.

### ENTRY

- **Agent signup:** `POST /users/agent/register/` → `users/views.py:register_agent` (line 273). Creates a `User` + `TravelAgent` profile in one transaction.
- **Agent login:** `POST /users/agent/login/`.
- **Agent dashboard:** `GET /users/agent/dashboard/` → `users/views.py:agent_dashboard` (line 375). Shows reservations where `travel_agent = request.user.travelagent`, plus commission stats.

### WHAT THE SYSTEM DOES

**Booking path:** the agent uses the **same** public booking URL `/book-orlando-transportation/<rate_pk>`. The view (`reservations/views.py:reservation_form`, lines 191–206) detects `request.user.is_authenticated` and looks up a `TravelAgent` profile:

```python
travel_agent = TravelAgent.objects.get(user=request.user)
reservation.travel_agent = travel_agent
reservation.created_by = request.user
reservation.modified_by = request.user
```

**Pricing:** identical to public — agents pay the customer rate; there is no agent-side discount or markup mechanism. Commission is computed post-hoc.

**Commission calculation** — `reservations/signals.py:update_agent_commission_data` (line 62):

- Fires on every `Reservation.save()` where `travel_agent` is not null.
- Recomputes per-agent rollups: `pending_commissions` (reservations not yet completed), `unpaid_commissions` (completed but `commission_paid=False`).
- Per-reservation commission default: `commission_amount = base_price × (TravelAgent.commission_rate / 100)`.

**Personal-trip exclusion:** `POST /users/agent/<uuid>/mark-personal/` → `agent_mark_personal_trip` (line 582). Sets `commission_excluded=True`, `commission_excluded_reason`, `commission_excluded_at`, `commission_excluded_by`; zeros out `commission_amount`. Useful for agents booking themselves.

**Confirmation email:** the customer (guest of record on the reservation) receives the standard confirmation email — same template as public bookings (see §8).

**Commission statement email:** `users/emails.py:send_agent_commission_statement` (line 657–726). **Manual send** from staff/admin. Lists completed unpaid reservations with commission amounts.

### HUMAN TOUCH

- Staff manually creates `AgentPayout` records to mark commissions paid (no admin UI evidence of automation; reservations get `commission_paid=True` field flip).
- Staff manually fires `send_agent_commission_statement` from admin.

### CONVERSION MECHANIC

Agent submits the same booking form a customer would. They are not the cardholder by default — the **guest's** card or the agent's saved card is used. Cardholder ambiguity is **not enforced in code**: whoever's card is used at the payment portal step pays.

### MEASUREMENT

- **Tracked:** `TravelAgent.{pending_commissions, unpaid_commissions}`, `Reservation.{travel_agent, commission_amount, commission_paid, commission_paid_at, commission_excluded*}`, `Reservation.booking_source` derived after travel_agent is set.
- **Not tracked:** agent referral attribution before account creation (e.g. "agent shared a link"); per-agent quote→book conversion rate (the lead flow attribution doesn't tag agent-originated quote requests).

### KNOWN GAPS / RISKS

- **No agent-side discount or net rate.** All agents see retail pricing. Premium agents can't be given preferential rate cards inside the system.
- **No invoice / NET-30 option.** Agents pay through Stripe like a retail customer; no aged AR.
- **Commission exclusion only via agent self-flag.** Easy to miss — and once payment is processed, reversal is manual.
- **No automated payout workflow.** Statements are manual email; payouts are tracked off-platform `[NEEDS MANUAL INPUT]`.

---

## 7. BOOKING — Public Reservation Form

### Definition

A customer (or an authenticated agent) submits the booking form. A `Reservation`, `Customer`, and one or two `Leg` rows are created.

### ENTRY

`GET/POST /book-orlando-transportation/<rate_pk>` → `reservations/views.py:reservation_form` (line 96). Rate PK in the URL ties the booking to a specific `(Vehicle, Route)` price row.

Other parallel entry to the same destination:

- **Dispatcher manual booking** — 6-step wizard at `/dispatching/booking/{start,customer,reservation,legs,pricing,review}/` (`dispatching/views.py:5126–5597`, finalized by `create_dispatcher_reservation` at line 5769). Same models, no Stripe, no UTM capture, no Meta CAPI event.

### WHAT THE SYSTEM DOES (public POST)

1. **Customer get-or-create** on `(email, phone_number)`. If existing → `Customer.is_returning = True`.
2. **Duplicate-reservation cleanup**: any unpaid reservation with the same email + last_name + rate + pickup_date created in the last 10 minutes is deleted to dodge double-submit.
3. **Reservation created** with `status = "confirmed"` (note: status here means "trip status", not "paid"), `is_paid = False`. Locked-in `base_price`, `additional_charges`, `gratuity_amount`, `gratuity_percentage`, `total_price`. UTM/click IDs and attribution source recorded.
4. **Leg(s) created**: one for one-way, two for round-trip. Optional `Flight` / `Cruise` child rows attached.
5. **Signals fire** (`reservations/signals.py`):
   - `reservation_saved` (line 19) → background thread sends internal confirmation email to staff.
   - `update_agent_commission_data` (line 62) → recalc commission rollups if `travel_agent` is set.
   - `auto_convert_lead_on_reservation` (line 206) → look up matching Lead by email primary, normalized phone fallback. If found: `Lead.status = "converted"`, `converted = True`, `converted_at = now`, `converted_reservation = reservation`. Cancel any active follow-up sequence. Apply GHL tags `converted`/`customer`; remove `new-lead`/`hot-lead`/`interested`. Log `LeadActivity(CONVERTED)`.
   - `store_reservation_old_values` + `log_reservation_changes` (line 547) → AuditLog for change history.
6. **Meta CAPI `InitiateCheckout` event** fires server-side from the view (best-effort try/except). Hashed email/phone/name + IP/UA + zipcode.
7. **Redirect** to `/payment/checkout-session/<reservation_uuid>/` to start Stripe.

### HUMAN TOUCH

None on the public path. Dispatcher path is entirely human-driven (staff types it in).

### CONVERSION MECHANIC

- **Public:** customer must complete Stripe checkout for the booking to be considered "paid". Until then the Reservation exists with `is_paid=False` but `status="confirmed"` — appears on the dispatcher capacity planner regardless.
- **Dispatcher:** the moment of save IS the conversion. No Stripe step; payment is handled separately or off-platform.

### MEASUREMENT

- **Tracked:** `Reservation.{attribution_source, utm_*, gclid, fbclid, booking_source, is_repeat_booking, created_at}`. AuditLog. Meta `InitiateCheckout`.
- **Not tracked:** form abandonment after pageview but before submit. Time-to-decision after seeing price. No event when the customer arrives on Stripe Checkout but doesn't complete payment (see §8 risk).

### KNOWN GAPS / RISKS

- **Reservation status `"confirmed"` is misleading.** It is set immediately on form save **before** payment. Dispatchers seeing "confirmed" can't tell at a glance whether the trip is actually paid. Must check `is_paid` separately.
- **Unpaid reservations remain visible to dispatch.** `capacity_planner` (`dispatching/views.py:8050`) filters out only `status='cancelled'`. Unpaid trips show up alongside paid ones — drivers can get assigned to trips that never got paid for.
- **Duplicate cleanup window is 10 minutes.** A user who hesitates and resubmits 11 minutes later creates two reservations, one of which is real and one orphaned.
- **Dispatcher path captures no UTM** — anything booked over the phone has zero attribution.

---

## 8. PAYMENT & CONFIRMATION

### Definition

The customer pays. The Reservation flips to `is_paid=True`. Confirmation email goes out. Meta `Purchase` event fires.

### ENTRY

- **Public:** auto-redirect from form save → Stripe Checkout (`mode="payment"`).
- **Dispatcher portal:** `/dispatcher_payment_portal/<reservation_uuid>/` → `dispatching/views.py:dispatcher_payment_portal` (line 2611). Three options:
  1. "Make a Payment" — Stripe Checkout (`mode="payment"`), custom amount.
  2. "Save Card" — Stripe SetupIntent (`mode="setup"`).
  3. "Use Saved Card" — off-session PaymentIntent (`confirm=True`) → immediate charge.

### WHAT THE SYSTEM DOES

**Stripe webhook** at `payment/webhook.py`:

- Verifies signature against `STRIPE_WEBHOOK_SECRET`.
- Handles `checkout.session.completed`:
  - **mode=`payment`**: retrieve PaymentIntent. If `payment_status='paid'` → create `Payment` (status=`paid`), set `Reservation.is_paid=True`, fire confirmation email **and** Meta `Purchase` event in background threads.
  - **mode=`setup`**: retrieve SetupIntent, save card to Stripe customer (`save_card_to_customer`, line 257), persist card brand/last4/exp on `Customer`. **No confirmation email** at this step — the customer hasn't been charged yet.

**Confirmation email** (`users/emails.py:send_reservation_confirmation`, line 84):

- Subject: `"Thank you for booking with Grayson Towncar!"`
- Template: `users/confirmation_email.html`.
- From: `reservations@graysontowncar.com`. To: `reservation.customer.email`.
- Retry: 3 attempts with exponential backoff 1s, 2s, 4s (`_send_email_with_retry`). If all 3 fail, **only `logger.error` is written — customer is never told the email didn't go**.

**Meta CAPI `Purchase` event** (`payment/webhook.py:226–233`, function in `reservations/conversions.py:99`):

- Hashed email/phone/name/zipcode + external_id (reservation id) + `_fbp` / `_fbc` cookies if present.
- `value = reservation.total_price`, `currency = "USD"`.
- `event_id = f"{stripe_payment_intent}_{unix_time}"` for de-duplication with the browser-side pixel.

**Confirmation SMS:** **NOT fired automatically by the webhook.** The confirmation SMS is dispatched by humans on a **separate** Confirmations page the day before pickup — see §10. The session memory's reference to "single send_single" and "batch send_confirmations_for_date" are these manual operations, not auto-fired-on-payment.

### HUMAN TOUCH

- Dispatcher uses portal when collecting payment by phone.
- Dispatcher can manually resend confirmation via `/dispatching/send_confirmation_email/` AJAX (`users/emails.py:62–81`).

### CONVERSION MECHANIC

The instant Payment row is saved with `status="paid"`. Downstream consumers (signals on Payment) update `Reservation.is_paid`, `paid_amount`, `first_paid_at`.

### MEASUREMENT

- **Tracked:** `Payment` rows (status, amount, stripe_payment_intent_id); `Reservation.is_paid`, `paid_amount`, `first_paid_at`; `EmailLog` entry for confirmation; Meta `Purchase` event.
- **Not tracked server-side:** Stripe Checkout abandonment (the customer who reached Stripe but bailed). Stripe webhooks for `checkout.session.expired` are **not** handled in `payment/webhook.py:24–72`. The Reservation stays unpaid forever unless a human notices.
- **Not tracked:** card-decline-then-retry funnel — failed `Payment` rows exist but no aggregate dashboard.

### KNOWN GAPS / RISKS

- **No `checkout.session.expired` handler.** A bailed Stripe session produces no signal. The unpaid Reservation only gets chased by the Unpaid Reminder engine (§10) — but that engine waits 2 hours after booking for the first reminder.
- **Email-failure silent fail.** Customer never learns the confirmation didn't reach them. They might assume the trip didn't book.
- **Payment status vs trip status confusion.** `Reservation.status="confirmed"` does **not** imply paid. Multiple places in the code that operate on "confirmed" reservations (capacity planner, dispatch confirmations) don't filter by `is_paid`.
- **No Google Ads `Purchase` conversion**. Only Meta CAPI Purchase fires. Google Ads optimizes blind unless offline conversions are manually uploaded.
- **`mode=setup` ambiguity.** A saved card means the Reservation is "confirmed" but unpaid. Whoever set up "save card" needs to remember to charge it later — no automation forces this.

---

## 9. UNPAID-RESERVATION CHASE — Payment Reminder Engine

### Definition

Automated email sequence to chase reservations where the customer didn't complete payment.

### ENTRY

A Reservation exists, has not been paid, has a `pickup_date` in the future.

### WHAT THE SYSTEM DOES

`ops/unpaid_reminders.py` (`UnpaidReminderEngine.process()`), invoked from the scheduler.

**Stages (per the explorer reading of the file):**
| Stage | Trigger relative to … | Email field stamped |
|---|---|---|
| First | +2h after booking | `unpaid_first_reminder_sent_at` |
| Second | +24h after first | `unpaid_second_reminder_sent_at` |
| Three-day | 3 days before pickup | `unpaid_three_day_warning_sent_at` |
| Final | 24h before pickup | `unpaid_final_warning_sent_at` |
| Auto-cancel flag | 2h before pickup | no email — just flags the reservation |

**Guards:**

- `EXCLUDE_TRAVEL_AGENT = True` → agent bookings excluded by default.
- `MIN_GAP_BETWEEN_AUTO_REMINDERS_HOURS = 6` — prevents double-send when stages overlap.
- Recent staff contact in last 6h skips the reminder.
- Duplicate-suspected flag skips.

**Template:** `users/payment_reminder_email.html` with per-stage subject lines (in `users/emails.py:198–204`).

**Logging:** `EmailLog` row + `CommunicationAttempt` against any open PAYMENT_CHASE ops task.

### HUMAN TOUCH

- Dispatcher can also send a one-off reminder via dashboard.
- "Auto-cancel flag" is just a flag — actual cancellation is staff-decided.

### CONVERSION MECHANIC

Customer clicks the link in the reminder, lands on the payment portal, completes Stripe, becomes paid.

### MEASUREMENT

- **Tracked:** every stage timestamp on `Reservation`; `EmailLog` per send; `CommunicationAttempt` if linked to an ops task.
- **Not tracked:** reminder-attributed conversion rate (which stage actually closes the gap). Could be inferred from `first_paid_at` vs stage stamps but no dashboard surfaces it.

### KNOWN GAPS / RISKS

- **No SMS in the chase.** Only email. If the customer ignores email (or it lands in spam), they never get nudged on SMS — even though we have their phone and an SMS broker.
- **`EXCLUDE_TRAVEL_AGENT=True` means agent-booked unpaid reservations are never chased.** Whether intentional or not, this is a silent leak if an agent expected the guest to pay and the guest doesn't.
- **No customer-readable "pay now" SMS link** — only manual portal URLs sent by staff.

---

## 10. PRE-TRIP — Confirmation SMS, Flight Tracking, Verification

### Definition

The day before service. The system locks in flight times, the dispatcher reviews the schedule, the customer gets a manual day-before SMS, and the driver gets assigned.

### ENTRY

A Reservation exists with `pickup_date == tomorrow` (typically).

### WHAT THE SYSTEM DOES

**Capacity planner** (`dispatching/views.py:capacity_planner`, around line 8010):

- Fetches `Leg.objects.filter(pickup_date=selected_date).exclude(reservation__status='cancelled').exclude(status='cancelled')`.
- Heavy scheduling computations cached for 60s per date.
- Computes `suggest_assignments_clustered(_unassigned_legs, _inhouse_for_suggestions, selected_date)` — clusters geographically/temporally adjacent legs and proposes driver assignments.
- Driver assignment is **mostly manual**; "Auto-Assign" button applies the suggestion set en masse.

**Driver assignment** (`Leg.save()` in `reservations/models.py:1309–1451`):

- When `driver` is set: computes `base_pay`, `gratuity_amount`, night bonus, `profit_estimate = revenue_share - total_driver_pay`.
- Status auto-resets to `"in-progress"` if driver unassigned.
- AuditLog row written.
- **No automatic SMS/notification to driver on assignment** — no Twilio `driver_notification` send fires on `Leg.save()`. Drivers learn via the driver dashboard / app (poll).

**Confirmation SMS — manual, day-before:**

- `dispatching/confirmation_sms.py:get_confirmation_message` builds a per-trip-type message:
  - Hotel→airport (departure): pickup time, "meet at main lobby", car seats note.
  - Airport→hotel (arrival): flight tracking note, baggage claim pickup instructions, Publix grocery stop if applicable.
  - Cruise variants (airport→port, hotel→port, port→hotel): port terminal details.
- Footer: `"407-212-7190"` + auto-message disclaimer.
- `send_confirmation_via_twilio(leg, row, message)` (line 389) — **direct Twilio**, not GHL — using `TWILIO_ACCOUNT_SID/AUTH_TOKEN/PHONE_NUMBER`.
- `Leg.confirmation_sms_sent_at = now` on success.
- Batch send via `send_confirmations_for_date(target_date, skip_already_sent=True)` — dispatched in background thread (per session memory: `dispatching/views.py:2661`). Single-leg `send_single` (~line 2637) is synchronous for UX.

**Flight tracking** (`dispatching/aeroapi_service.py`):

- `Flight` rows (`reservations/models.py:1908–2081`) hold scheduled/estimated/actual times, terminal, gate, baggage carousel.
- Session reuses one `requests.Session` with `x-apikey` header set once (P1 fix in session memory).
- AeroAPI is called on demand from the dispatcher UI ("Refresh Flight Status" button) and also from a periodic `auto_refresh_flights` ops task per scheduler cycle.
- `Leg.has_flight_time_mismatch()` (line 1528–1558) flags when the booked pickup time disagrees with the latest flight data.

**Flight verification email** (`dispatching/flight_verify_views.py`, `flight_verify_email.py`):

- Customer-facing self-serve link to confirm/update their flight times.
- `Leg.flight_verification_email_sent_at` stamps when sent. **Trigger code not surfaced in the explored modules** — likely sent manually from the leg detail page or by another ops task `[CONFIRM IN PRODUCTION]`.

### HUMAN TOUCH

- Dispatcher reviews capacity planner.
- Dispatcher manually triggers day-before confirmations from the Confirmations page.
- Dispatcher manually reassigns based on AeroAPI flight delays.

### CONVERSION MECHANIC

By the time we're here, "conversion" is downstream — service-delivery is what we're earning. The relevant mechanic is **avoiding service failures**: late driver, missed flight pickup, lost passenger.

### MEASUREMENT

- **Tracked:** `Leg.{status, driver, driver_assigned_at, confirmation_sms_sent_at, flight_verification_email_sent_at}`; `LegStatus` history; AeroAPI cache hits; `RouteTimingMetric` aggregates for planning.
- **Not tracked:** driver acknowledgement of assignment; whether the customer actually read the confirmation SMS; flight verification response rate.

### KNOWN GAPS / RISKS

- **Driver gets no proactive notification on assignment.** They have to log into the dashboard to know. A late assignment or a reassignment can slip.
- **Confirmation SMS is manual.** If the dispatcher doesn't run the batch, customers don't get a day-before reminder. There is no fail-safe cron that sends it.
- **Capacity-planner 60s cache** can show stale assignment suggestions during high-churn periods (note in session memory).
- **`flight_verification_email_sent_at` trigger is fuzzy.** Either it's a manual click or a missing automation — needs production confirmation.

---

## 11. SERVICE DELIVERY — In-Trip Status

### Definition

The trip happens. Status transitions are recorded.

### ENTRY

A driver starts working a Leg. Default status `"in-progress"` (line 969 of `reservations/models.py`).

### WHAT THE SYSTEM DOES

**Leg status flow** (`reservations/models.py:LegStatus`, lines 1556–2654):

- `in-progress` → `confirmed` (driver confirmed) → `on-the-way` → `on-location` → `picked-up` → `completed`.
- `cancelled` from any state.
- Each transition writes a `LegStatus` history row with `status`, `timestamp`, `updated_by`, optional `notes`.
- `drivers/signals.py:leg_status_changed` (line 30–52) fires `send_driver_status_notification(...)` in a daemon thread on transition (likely ntfy/internal — Twilio not confirmed in signal code).

**Reservation roll-up** (`reservations/models.py:check_and_update_completion_status`, line 690–718):

- Auto-runs on Leg save.
- If all non-cancelled legs are `completed` → `Reservation.status = "completed"`.

**Driver-side updates:** the driver presumably updates status from a mobile interface (the codebase has driver dashboards). The exact endpoint isn't covered by the explorer reports.

### HUMAN TOUCH

The driver IS the human touch at this stage.

### CONVERSION MECHANIC

Status `completed` is the "trip finished" gate. It unlocks commission roll-up (§6), is the trigger for any post-trip automation that exists (§12 — but most of it isn't built).

### MEASUREMENT

- **Tracked:** `Leg.status`, full `LegStatus` history with user attribution; `DriverLocation` (per drivers/signals); driver pay roll-up on the Leg row.
- **Not tracked:** customer-side service experience (no in-trip NPS); on-time performance at the per-trip level (no `picked_up_at` vs scheduled comparison stored as a metric).

### KNOWN GAPS / RISKS

- **No SMS or push to customer when the driver is on-the-way / on-location.** The day-before confirmation is the last touch the customer gets until the driver shows up. Industry standard ("Your driver Alex is 5 min away in a black Suburban, plate ABC-123") is missing — the data is there to send it, the automation isn't wired.
- **`completed` is irrevocable in code** — there is no formal "trip went wrong, refund" flow tracked at the Leg level. Refunds happen separately via Stripe and Payment model.

---

## 12. POST-TRIP — Retention, Reviews, Repeat

### Definition

After the leg completes. The customer has been delivered. The next goal is repeat business / 5-star review.

### ENTRY

`Leg.status` transitions to `completed`, triggering `check_and_update_completion_status` → `Reservation.status = "completed"`.

### WHAT THE SYSTEM DOES

**The honest answer:** almost nothing.

Confirmed implementations:

- **Travel agent commission settlement** kicks in via `update_agent_commission_data` signal (recomputes the agent's `unpaid_commissions`). Statement emails are manual.
- **Lead status** was already set to `converted` at booking — no further status change post-completion.
- **`Customer.is_returning`** is set on the next reservation when the customer rebooks (get-or-create finds an existing row).
- **Saved Stripe card** is reused if the dispatcher chooses "Use Saved Card" in the portal for the next booking — there is **no customer-facing one-click rebook**.

Not implemented (confirmed across all three explorer reports + repo grep):

- **No automated thank-you email.** A `users/thankyou_email.html` template exists but no signal or scheduler task fires it on leg/reservation completion.
- **No review-solicitation SMS or email.** No code references to Google Reviews, Yelp, TripAdvisor in user-facing automation.
- **No referral/loyalty mechanic.** No Referral or Loyalty model.
- **No customer portal** for "my trips" — only Travel Agents have a logged-in dashboard.
- **No re-engagement nurture for past customers** ("haven't seen you in 6 months, book your next trip and get $X off").

### HUMAN TOUCH

- Manual review requests, if they happen, happen out-of-band (probably by the founder via personal text). No system record.
- Manual rebook calls / texts to past customers around peak season `[NEEDS MANUAL INPUT]`.

### CONVERSION MECHANIC

Repeat business is essentially **word-of-mouth + brand recall**. There is no system-side push.

### MEASUREMENT

- **Tracked:** `Customer.is_returning`, `Customer.reservation_count` (likely backfilled), `Reservation.is_repeat_booking` flag, `first_paid_at`, repeat-booking analytics via the `/dispatching/lead-analytics/` page for source attribution.
- **Not tracked:** time-since-last-trip per customer; lifetime value; review submission rate; reviewer identity-to-customer mapping; referral source.

### KNOWN GAPS / RISKS

- **Biggest single funnel leak in the entire system.** A completed customer is the cheapest-to-acquire next customer, and we send them zero outreach.
- **Reviews drive Google Ads quality score and Maps ranking.** With no solicitation step, reviews are entirely passive.

---

## 13. THINGS THE PROMPT ASSUMED EXIST — BUT DON'T

The audit prompt named four automations as if they were in place. Three of the four are not, or are not what the prompt described:

| Prompt claim                                                  | Reality                                                                                                                                                                                                                                                                                                                                                                                                          |
| ------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| "After-hours email automation (Apps Script + Claude API)"     | **Not in this repo.** Grep across the entire codebase (including `.env`, `docs/`, `business/settings.py`, every app) finds zero references to Anthropic, Claude API, Apps Script, or "after hours". All email automation runs through Django SMTP + `_send_email_with_retry`. Either this lives entirely in a Google Workspace Apps Script project outside the repo `[NEEDS MANUAL INPUT]` or it does not exist. |
| "The GHL AI agent"                                            | **No GHL-AI auto-replier in this codebase.** The GHL webhook (`ghl_integration/views.py:70`) marks `needs_human_follow_up=True` and pushes a notification — replies are human-driven. There is no inbound message classifier, no autoresponder, no "AI booking bot" wired to the webhook. Any AI agent activity lives entirely on the GHL platform side `[NEEDS MANUAL INPUT]`.                                  |
| "5-message lead follow-up SMS sequence with stop-on-response" | **This one exists** — fully implemented as described in §4, with stop-on-reply, stop-on-conversion, stop-on-pickup-passed. Confirmed in `ghl_integration/tasks.py` and `LEAD_AUTOMATION_STATUS.md`.                                                                                                                                                                                                              |
| "Pre-trip re-engagement workflow for unconverted leads"       | **Not in this repo.** Search for `reengage`, `re_engage`, `winback`, `win_back`, `pretrip` returns nothing. The only "lost lead" handling is `detect_lost_leads()` which simply marks expired leads `LOST` — it does not re-engage them.                                                                                                                                                                         |

These four items represent the largest mismatches between operator mental model and code reality.

---

## 14. FUNNEL MAP

```
                                      [ EXTERNAL ]
                Google Ads ── gclid ──┐
                  Meta Ads ── fbclid ──┤            (no GA conversion API; Meta CAPI only)
        Organic / Direct ── utm ──────┤
                  Referral ───────────┤
                                      │
                                      ▼
                          ┌────────────────────────┐
                          │  Quote Form / Booking  │           Contact Form ───► ContactUsForm
                          │  Form (public web)     │                         (ops queue, manual)
                          └───────────┬────────────┘
                                      │
        ┌─────────────────────────────┴───────────────────────────────┐
        │ Quote form path                                              │ Direct-book path
        ▼                                                              ▼
    Lead + Quote rows (status=new)                              Customer + Reservation
    Meta CAPI: Lead                                             (status=confirmed, is_paid=False)
    GHL contact sync (no SMS yet)                               Meta CAPI: InitiateCheckout
    ntfy push to team                                           auto_convert_lead_on_reservation
        │                                                              │
        │  ─── (≤30-min wait, send window 8am-9pm ET) ──►              │  Redirect to
        ▼                                                              ▼  Stripe Checkout
    batch_send_unsent_leads:                                         (mode=payment)
      • create/update GHL contact
      • send Step-1 SMS
      • status=contacted, initial_sms_sent=true
      • start_follow_up_sequence
        │
        ▼
    Follow-Up Engine (process_follow_up_batch, every 30 min, 8am-9pm ET):
       Step 2 (+4h) → Step 3 (+20h) → Step 4 (+2d) → Step 5 (+4d)
       Stops on: replied / converted / pickup passed
       After Step 5 → sequence_active=False  (lead held, not auto-cold)
        │
   ┌────┴────┐
   │         │
   ▼         ▼
 No reply   Reply → GHL webhook /ghl/webhook/
   │         │   has_replied=true, status=interested, priority↑
   │         │   cancel_lead_sequence
   │         │   ntfy "Lead Replied" (URGENT)
   │         │   GHL tags: replied, hot-lead
   │         │      │
   │         │      ▼
   │         │   HUMAN closes via GHL inbox manually
   │         │      │
   │         │      ▼
   │         │   Customer books online ──┐  OR  Dispatcher manual booking
   │         │                            │     (6-step wizard, no UTM, no Stripe)
   │         │                            ▼
   │         │                  Customer + Reservation + Leg(s)
   │         │                  (status=confirmed, is_paid=False)
   │         │                            │
   │         │                  ┌─────────┴──────────────────────┐
   │         │                  │                                 │
   │         │                  ▼                                 ▼
   │         │            Public path                       Dispatcher path
   │         │            Stripe Checkout                   No Stripe step
   │         │                  │                                 │
   │         │  Stripe webhook checkout.session.completed         │ payment handled
   │         │  ─ mode=payment → Payment(paid)                    │ separately or
   │         │      Reservation.is_paid=True                      │ via dispatcher
   │         │      send_reservation_confirmation EMAIL (3-retry) │ portal
   │         │      Meta CAPI: Purchase                           │ (Make Pay / Save Card / Use Saved)
   │         │  ─ mode=setup → card stored, NO email              │
   │         │                                                    │
   │         └─────────────────────┬──────────────────────────────┘
   │                               │
   ▼                               ▼
detect_lost_leads (hourly):    UNPAID? → UnpaidReminderEngine
  pickup_date < today              T+2h, T+24h, T-3d, T-1d, T-2h flag
  → status=LOST                    EMAIL ONLY (no SMS), agents excluded
  → cancel sequence                EmailLog + CommunicationAttempt
  → GHL tag "lost"                       │
                                         ▼
                                    Paid?  → YES ──► Dispatch Pipeline
                                                                 │
        ┌────────────────────────────────────────────────────────┘
        ▼
   Dispatch Capacity Planner (cached 60s):
     suggest_assignments_clustered  →  manual or auto-assign drivers
     AeroAPI flight tracking (on-demand + ops task)
     flight_verification_email (manual / unclear trigger)
        │
        ▼
   Day-before: MANUAL Confirmation SMS via Twilio (not GHL)
     trip-type-specific copy, Leg.confirmation_sms_sent_at stamped
     (no automatic driver-assigned SMS to driver)
        │
        ▼
   Service: Leg status flow
     in-progress → confirmed → on-the-way → on-location → picked-up → completed
     LegStatus history rows; ntfy on transitions (no customer SMS in-trip)
        │
        ▼
   All legs completed → Reservation.status=completed
        │
        ▼
   Post-trip:
     ─ Agent commission rollup updated (manual payout, manual statement email)
     ─ NO thank-you email automation
     ─ NO review request
     ─ NO referral mechanic
     ─ NO customer portal / one-click rebook
     ─ Customer.is_returning auto-set ONLY when they book again


─────────────── AFFILIATE / TRAVEL AGENT PARALLEL ────────────────

   Agent signup ─► User + TravelAgent
        │
        ▼
   Agent login → /users/agent/dashboard/
        │
        ▼
   Same /book-orlando-transportation/<pk> form
   Reservation.travel_agent set on save
   Same pricing as retail (no agent discount/markup)
        │
        ▼
   Same payment flow as public (no NET-30, no invoice)
        │
        ▼
   On Reservation.save: update_agent_commission_data
     (pending/unpaid totals refresh)
        │
        ▼
   Agent can self-flag personal trip (mark-personal)
   On completion: commission_amount frozen, awaits payout
   Manual payout marking + manual statement email
```

---

## 15. MEASUREMENT MATRIX — What's tracked vs. what's missing

| Funnel stage         | Currently tracked                                                                                            | NOT tracked but should be                                                                                |
| -------------------- | ------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------- |
| Awareness            | gclid, fbclid, utm\_\* persisted to Lead + Reservation; attribution_source derived                           | Pre-form-fill bounce, paid-channel cost per Lead (no spend feed), no Google Ads conversion API           |
| Lead capture         | Lead row, Quote row, GHL contact, Meta CAPI Lead, ntfy push, `EmailLog`                                      | Form-field abandonment, time on form, A/B variant                                                        |
| Initial SMS          | `initial_sms_sent`, `initial_sms_sent_at`, `GHLSyncLog`, `LeadActivity(SMS_SENT)`                            | Delivery receipts, opt-outs (STOP keyword), variant if any                                               |
| Nurture sequence     | Every step's `FollowUpTask`, `LeadActivity(SMS_SENT)`, `GHLSyncLog`, contact_attempts                        | Per-step reply rate, per-step conversion lift, segment-specific performance                              |
| Inbound reply        | `has_replied`, `last_reply_at`, `needs_human_follow_up`, ntfy push, `LeadActivity(REPLY_RECEIVED)`           | Time-to-human-reply, who responded, what was offered, manual-quote-to-booking rate                       |
| Conversion (booking) | `Reservation` row, `auto_convert_lead_on_reservation` signal, Meta `InitiateCheckout`, AuditLog              | Form-to-payment dropoff (no Stripe `checkout.session.expired` handler)                                   |
| Payment              | `Payment` row, `Reservation.is_paid/paid_amount/first_paid_at`, Meta `Purchase`, `EmailLog` for confirmation | Stripe Checkout abandonment, card-decline retry funnel, confirmation-email delivery failure (silent log) |
| Unpaid chase         | 4 stage timestamps on Reservation, `EmailLog`, `CommunicationAttempt`                                        | Attributable conversion per stage; SMS chase not implemented                                             |
| Pre-trip             | `Leg.confirmation_sms_sent_at`, `flight_verification_email_sent_at`, AeroAPI cache, `RouteTimingMetric`      | Confirmation SMS automatic firing (it's manual), driver acknowledgement of assignment                    |
| Service delivery     | `LegStatus` history, driver pay roll-up                                                                      | Customer-side in-trip touchpoints, on-time performance metrics                                           |
| Post-trip            | `Customer.is_returning`, `reservation_count`, `is_repeat_booking` flag, agent commission rollups             | Thank-you sent, review collected, referral source, churn signal, win-back                                |
| Affiliate channel    | `TravelAgent.pending/unpaid_commissions`, `Reservation.travel_agent/commission_*`                            | Per-agent quote→book rate, agent-share-link attribution, agent NPS                                       |

---

## 16. TOP 5 SUSPECTED LEAK POINTS (ranked)

### #1 — Post-trip retention is a total void

**Evidence:** Grep for `thankyou`, `review`, `post_trip`, `referral`, `loyalty` returns templates without triggers (`users/thankyou_email.html` exists; no signal/scheduler fires it). `LEAD_AUTOMATION_STATUS.md` Phase 5 lists nothing related. No customer portal exists — agents have one, customers don't.

**Why it leaks:** A completed customer who paid, was driven well, and went home cold-storage with zero outreach is the cheapest LTV expansion you'll ever skip. Compounds quarterly. Also costs Google Maps ranking via missing review velocity.

### #2 — Reservation `status="confirmed"` decouples from `is_paid`

**Evidence:** `reservations/models.py:107–109` sets default status to `"confirmed"` on save, _before_ payment. `dispatching/views.py:8050` capacity planner filters only `status='cancelled'` — unpaid trips appear in dispatch identically to paid ones. There is no Stripe `checkout.session.expired` handler in `payment/webhook.py:24–72`.

**Why it leaks:** Dispatchers may assign drivers to unpaid trips. Customers who bailed at Stripe disappear from the funnel until the +2h reminder, which is email-only. Real-money trips are getting confused with intent trips.

### #3 — No SMS in the Unpaid-Reservation chase, no SMS in the pre-trip flow

**Evidence:** `ops/unpaid_reminders.py` uses `users/payment_reminder_email.html` only — no Twilio/GHL send. We have phone numbers, we have a working SMS broker, but every chase touch is email-only. `EXCLUDE_TRAVEL_AGENT=True` also means agent-booked unpaid bookings get **zero** reminders.

**Why it leaks:** SMS open rates ~98% vs email ~20% in this industry. Unpaid chases on email alone for high-intent ground-transport buyers is a deliberate handicap. Pre-trip "driver is on the way" SMS to passengers is also absent (§11).

### #4 — Human-reply SLA on hot leads depends on one ntfy push

**Evidence:** `ghl_integration/views.py:243–248` — a single ntfy push titled "Lead Replied: {name}" with priority `urgent` is the _only_ synchronous human-alert when a hot lead replies. `needs_human_follow_up` is set but never auto-cleared, and the codebase has no SLA/escalation timer.

**Why it leaks:** A lead who texted back is the warmest data point in the funnel. If the founder/dispatcher misses the ntfy (overnight, phone off, do-not-disturb), the lead cools fast. There's no second escalation, no team-paging, and no "auto-acknowledge so they know they were heard" reply.

### #5 — Initial-response gap can reach 30+ minutes

**Evidence:** `ghl_integration/scheduler.py:INTERVAL_SECONDS = 1800`. `LEAD_AUTOMATION_STATUS.md` even calls this out: "Initial response time: Before = up to 60 min, After = under 30 min." That is the design target, not a fast response. `is_within_send_window` will further hold any 9pm–8am lead until 8:15 AM with no urgency override for next-day pickups.

**Why it leaks:** Lead-to-call studies (Harvard Business Review, Lead Connect) consistently show conversion drops sharply after the 5-minute mark. We're 6x past that on a happy-path morning lead, and effectively a full overnight on a 10pm lead — even when their pickup is the very next day.

---

## 17. NOTES FOR THE REVIEWING AI

- All `file:line` references are accurate to the working tree at audit time (HEAD = `8da90b87`, branch `main`). Code locations may drift with future edits.
- `[NEEDS MANUAL INPUT]` markers indicate facts that require checking systems outside this repo (GHL platform UI, Google Workspace Apps Scripts, Stripe Dashboard, ad-account settings).
- The session memory at `MEMORY.md` was used only for cross-referencing the recently applied performance fixes; it is not authoritative for funnel behavior. The behavior asserted here comes from the code as written today, the three Explorer sub-agent reports, and the `docs/` + root `LEAD_AUTOMATION_STATUS.md` documentation files.
- Three of the four automations the prompt assumed exist do not exist in the form described (§13). Treat any external claim that they do exist with skepticism until verified in the corresponding external platform.
