# Pre-Pickup Nudge

One last SMS, fired ~3 days before a lead's pickup date, to win back **warm-but-unbooked leads** —
including those who replied weeks ago ("let me think") and then went quiet.

---

## Before → After

### Before

A web quote form creates a `Lead` (+ `Quote`). An in-process scheduler runs a **5-step SMS
follow-up sequence** over the first ~4 days (delays 0h / 4h / 20h / 48h / 96h), then **goes silent**.
That sequence also **cancels on the lead's first reply**.

```
Quote submitted
   │
   ▼
Step1(0h) ─ Step2(4h) ─ Step3(20h) ─ Step4(48h) ─ Step5(96h)
   └──────────── first ~4 days ────────────┘
                                            │
                            … then nothing until the trip …
                                            │
                                            ▼
                                pickup passes → auto-marked LOST
```

**The gap:** a lead whose pickup is two weeks out gets nothing between day 4 and the trip. Worse, a
lead who replied "let me think about it" and never booked has their sequence **cancelled** — so they
get *zero* further contact, then quietly become `LOST`.

| Scenario | Before |
|---|---|
| Pickup 14 days out, never replied | 5 texts in first 4 days, then silence → LOST |
| Replied "maybe later", never booked | Sequence cancelled → **no further contact** → LOST |
| Pickup 3 days out, slow-fill date | No date-aware touch; no offer engine |

### After

A **separate, date-anchored scanner** runs hourly and fires **one** nudge at **pickup − 3 days**,
catching both populations the form sequence misses. It reuses all the existing SMS plumbing, so it
reads as a natural continuation of the cadence — not a second system.

```
Quote submitted
   │
   ▼
Step1 ─ Step2 ─ Step3 ─ Step4 ─ Step5        ← unchanged 5-step form sequence (steps 1–5)
   └──────── first ~4 days ────────┘
                                       … gap …
                                            │
                       pickup − 3 days  ────┤
                                            ▼
                              ┌─────────────────────────────┐
                              │   PRE-PICKUP NUDGE (step 6)  │
                              │   one SMS, offer-aware       │
                              └─────────────────────────────┘
                                            │
                                            ▼
                                  pickup passes → LOST (if still unbooked)
```

| Scenario | After |
|---|---|
| Pickup 14 days out, never replied | Still gets the 5 steps **+ one nudge 3 days before pickup** |
| Replied "maybe later", never booked | **Now gets the nudge** (not suppressed on reply) |
| Pickup 3 days out, slow-fill date | Offer engine picks urgency / cruise / discount-to-human |

---

## What it does — the offer engine

The scanner resolves **one variant per lead** (first match wins):

| Priority | Variant | When | Action |
|---|---|---|---|
| 1 | `discount` | A staff-set `Lead.pre_pickup_discount > 0` | **Routes to a human** — flags `needs_human_follow_up`, opens a `MANUAL` ops task. **No automated SMS** (the booking flow has no coupon field, so we never auto-quote a price we can't honor). |
| 2 | `cruise_urgency` | Lead segment is `cruise_transfer` | Sends the cruise SMS ("the ship won't wait"). |
| 3 | `urgency` | Default | Sends the urgency SMS with a one-tap booking link. |

> The **free-upgrade** variant from the original brief was intentionally **dropped for v1** — there's
> no reliable fleet-availability check, and a promised upgrade dispatch can't honor is a broken promise.

---

## How it works

```
hourly (scheduler, under a single-leader advisory lock)
   │
   ▼
PrePickupNudgeEngine.process()
   │
   ├─ candidate query:  pickup_date in (today+2 … today+3)
   │                    AND status not converted/lost
   │                    AND no existing step-6 task   ← "one nudge ever"
   │
   └─ per lead:
        guards → already-nudged? · same-phone already nudged? ·
                 outbound in last 48h? · form step due in next 24h? · has a channel?
        resolve variant
        ├─ discount  → flag human + MANUAL ops task + SKIPPED ledger row (no SMS)
        └─ urgency / cruise:
             if outside 8 AM–9 PM ET → defer (write nothing)
             claim step-6 row → send_sms()
               ├─ ok        → SENT row + bump last_contact_date + LeadActivity
               └─ sms fails → quote-email fallback + flag human  (or FAILED if email also fails)
```

**Dedup / idempotency.** Every nudge writes a `FollowUpTask` at **step 6**. The existing
`(lead, step_number)` unique constraint is the database-level "one nudge ever" guarantee. The scanner
is safe to run every cycle.

**Coexistence.** The existing follow-up batch was scoped to `step_number <= 5`, and the nudge engine
never leaves a `PENDING` step-6 row — so the two engines can never double-handle the same lead.

---

## Files

### Added
| File | Purpose |
|---|---|
| `ghl_integration/pre_pickup.py` | The engine: `PrePickupNudgeEngine`, `resolve_pre_pickup_offer`, `get_pre_pickup_discount` (stub), `send_pre_pickup_nudges()`. |
| `ghl_integration/management/commands/send_pre_pickup_nudges.py` | Manual/cron entry point — `--dry-run`, `--lead <id>`. |
| `reservations/migrations/0108_lead_pre_pickup_discount.py` | Adds `Lead.pre_pickup_discount` (Decimal, default 0). |
| `ghl_integration/migrations/0004_alter_followupsequence_…py` | Registers the two nudge variants as editable template segments. |
| `ghl_integration/migrations/0005_seed_pre_pickup_templates.py` | Seeds the urgency + cruise_urgency SMS templates (step 6). |
| `ghl_integration/test_pre_pickup.py` | 15 tests (resolver, eligibility, dedup, throttle, send window, discount-route, email fallback, phone dedup). |
| `docs/pre-pickup-nudge.md` | This document. |

### Changed
| File | Change |
|---|---|
| `reservations/models.py` | New `Lead.pre_pickup_discount` field. |
| `reservations/admin.py` | Field exposed under "Lead Details". |
| `users/emails.py` | Extracted `resolve_booking_url(lead)` (shared by the quote email + the nudge); `GENERIC_BOOKING_URL` constant. |
| `ghl_integration/templates_engine.py` | `render_follow_up_message(..., extra=None)` — lets the nudge inject `{booking_link}`. |
| `ghl_integration/models.py` | Two new nudge template segments; help-text updates. |
| `ghl_integration/tasks.py` | Follow-up batch scoped to `step_number <= 5` (defensive partition). |
| `ghl_integration/scheduler.py` | Hourly call to `send_pre_pickup_nudges()` inside the advisory-lock batch. |

---

## Reused (not rebuilt)

- **SMS send path** — `GoHighLevelService.send_sms` / `create_or_update_contact`.
- **Send window** — `ghl_integration/timing.py` (8 AM–9 PM ET; out-of-window → defer).
- **Email fallback** — `ghl_integration/tasks.py:_try_email_fallback` → `users/emails.py:send_lead_quote_email`.
- **Editable templates** — `FollowUpSequence` rows, admin-editable with no redeploy.
- **Reply handling** — the existing webhook + poll already set `has_replied` / cancel; no new reply path.
- **Ops tasks** — `ops.services.create_task` for the discount-to-human task.

---

## Behavior decisions

| Decision | Choice | Why |
|---|---|---|
| Discount honoring | Route to a human (flag + ops task), no auto-SMS | Booking flow has no coupon/price-override (only customer-entered Stripe promo codes). |
| Free upgrade | Dropped for v1 | No reliable fleet-availability check; avoid broken promises. |
| Replied-but-unbooked leads | Included | They're the core target; not suppressed on `needs_human_follow_up`. |
| Throttle | Skip if any outbound in last 48h | Don't interrupt a rep working the lead in the final days. |
| Phone dedup | One nudge per person | Round-trip = two leads, same phone — avoid double-texting. |
| Cadence | Hourly | Date-anchored + idempotent; absorbs a missed cycle without double-firing. |

---

## Verification

- **Unit tests:** `python manage.py test ghl_integration.test_pre_pickup` → 15 pass.
- **Regression:** `python manage.py test ops.tests.test_unpaid_reminders` → 22 pass (validates the `users/emails.py` refactor).
- **Dry run (no sends, no writes):** `python manage.py send_pre_pickup_nudges --dry-run`.
- **Single lead:** `python manage.py send_pre_pickup_nudges --lead <id>`.
- **Live shell check:** a seeded lead 3 days out resolved `sent:pre_pickup_urgency`; cruise → `pre_pickup_cruise_urgency`; discount set → `pre_pickup_discount` (routed to human).

---

## Known limitations / follow-ups

- **Throttle blind spot:** anchored on `last_contact_date` + automated-send timestamps. A rep texting
  from GHL directly may not write `last_contact_date`, so it might not suppress the nudge. A future
  GHL last-outbound poll could close this.
- **Booking link** depends on a current `Quote` with a matchable `Rate`; otherwise it falls back to
  the generic `…/rates-booking/` page (same as the quote email).
- **Travel-agent exclusion** is a no-op today — leads carry no TA flag (TA lives on `Reservation`).
- **Discount rules layer** is deliberately deferred — `get_pre_pickup_discount()` is a thin seam that
  returns the manually-set field; a date-fill-based rules engine can slot in later without touching
  the resolver.
- **Free-upgrade variant** can be revisited later (gated + dry-run-first) using the design captured in
  the implementation plan.

---

## Appendix — complete change inventory

Every file touched by this feature, with literal diffs for changes to **existing** code and line
counts for **new** files. This is the full set (`git status` / `git diff`), nothing omitted.

### New files (net-new, self-contained)

| Lines | File |
|---:|---|
| 413 | `ghl_integration/pre_pickup.py` |
| 208 | `ghl_integration/test_pre_pickup.py` |
| 73 | `ghl_integration/management/commands/send_pre_pickup_nudges.py` |
| 54 | `ghl_integration/migrations/0005_seed_pre_pickup_templates.py` |
| 28 | `ghl_integration/migrations/0004_alter_followupsequence_delay_hours_and_more.py` |
| 13 | `reservations/migrations/0108_lead_pre_pickup_discount.py` |
| — | `docs/pre-pickup-nudge.md` (this document) |

> Migration `0004` is auto-generated. It's an `AlterField` on four `FollowUpSequence` fields, but only
> one change is functional — adding the two `pre_pickup_*` segment choices. The other three
> (`step_number`, `delay_hours`, `message_template`) are **help-text-only** edits, cosmetic.

### Modified files (`git diff --stat`)

```
 ghl_integration/models.py           | 10 ++++--
 ghl_integration/scheduler.py        | 14 +++++++++
 ghl_integration/tasks.py            |  6 ++++
 ghl_integration/templates_engine.py |  9 +++++-
 reservations/admin.py               |  1 +
 reservations/models.py              | 11 +++++++
 users/emails.py                     | 62 +++++++++++++++++++++++++------------
 7 files changed, 89 insertions(+), 24 deletions(-)
```

#### `reservations/models.py` — new `Lead.pre_pickup_discount` field
```diff
@@ class Lead(models.Model):
     needs_human_follow_up = models.BooleanField(default=False, help_text="Flagged for human closer after lead replied")
 
+    # Pre-pickup nudge: a deliberate, backend-set discount (default 0) for the
+    # one-touch SMS fired ~3 days before pickup. Nothing sets this automatically
+    # — staff set it per lead/date. When > 0 the nudge routes the lead to a human
+    # to book with the discount applied manually, because the booking flow has no
+    # coupon/price-override mechanism (only customer-entered Stripe promo codes).
+    pre_pickup_discount = models.DecimalField(
+        max_digits=10, decimal_places=2, default=0,
+        help_text="Deliberate discount ($) for the pre-pickup nudge. Default 0. "
+                  "When > 0, the lead is routed to a human to book with the discount applied.",
+    )
```

#### `reservations/admin.py` — expose the field
```diff
@@ class LeadAdmin(admin.ModelAdmin):  # "Lead Details" fieldset
                 ("contact_attempts", "last_contact_date"),
                 "next_follow_up",
+                "pre_pickup_discount",
```

#### `ghl_integration/models.py` — nudge template segments + help text
```diff
@@ class FollowUpSequence(models.Model):
         ("abandoned_quote", "Abandoned Quote"),
+        # Pre-pickup nudge variants (step 6). These are keyed by offer "variant"
+        # rather than lead segment — see ghl_integration/pre_pickup.py.
+        ("pre_pickup_urgency", "Pre-Pickup Nudge — Urgency"),
+        ("pre_pickup_cruise_urgency", "Pre-Pickup Nudge — Cruise Urgency"),
     ]
     step_number = models.PositiveSmallIntegerField(
-        help_text="Step in the sequence (1-5)"
+        help_text="Step in the sequence (1-5 = form follow-up; 6 = pre-pickup nudge)"
     )
     ...
     delay_hours = models.PositiveIntegerField(
-        help_text="Hours after Step 1 send for this step (0=immediate, 4, 20, 48, 96)"
+        help_text="Hours after Step 1 send for this step (0=immediate, 4, 20, 48, 96). Unused for the date-anchored step-6 nudge."
     )
     message_template = models.TextField(
-        help_text="...{vehicle_name} placeholders"
+        help_text="...{vehicle_name} placeholders. Step-6 nudge templates also support {booking_link}."
     )
```

#### `ghl_integration/templates_engine.py` — optional `extra` placeholders
```diff
-def render_follow_up_message(template_str, lead):
+def render_follow_up_message(template_str, lead, extra=None):
     ...
+    Pass ``extra`` (a dict of pre-formatted strings) to inject additional
+    placeholders not derived from the lead — e.g. the pre-pickup nudge supplies
+    {booking_link}. ``extra`` values override the built-ins on key collision.
     ...
     })
+
+    if extra:
+        values.update(extra)
 
     try:
         return template_str.format_map(values)
```

#### `ghl_integration/tasks.py` — partition the form batch from the nudge
```diff
@@ def process_follow_up_batch():
         FollowUpTask.objects.filter(
             status=FollowUpTask.StatusChoices.PENDING,
             scheduled_at__lte=now,
+            # Scope to the canonical 5-step form sequence. The pre-pickup nudge
+            # (step 6, ghl_integration/pre_pickup.py) is a self-contained sender
+            # that writes its own terminal FollowUpTask rows and never leaves a
+            # PENDING step-6 row — this filter is defensive partitioning so the
+            # two engines can never double-handle the same task.
+            step_number__lte=5,
         )
```

#### `ghl_integration/scheduler.py` — hourly wiring
```diff
@@ def _run_batch_tasks():   # inside the `_cycle_count % 2 == 0` hourly block
             logger.error(f"detect_lost_leads error: {e}", exc_info=True)
 
+        # 4b. Pre-pickup nudge (every 2 cycles = every hour). Date-anchored
+        #     (pickup-3d) and idempotent via the (lead, step 6) dedup, so the
+        #     hourly cadence absorbs a missed cycle without double-firing.
+        try:
+            from ghl_integration.pre_pickup import send_pre_pickup_nudges
+            result = send_pre_pickup_nudges()
+            if result and (result.get("sent", 0) or result.get("routed_to_human", 0)):
+                logger.info(
+                    f"Pre-pickup nudges: {result.get('sent', 0)} sent, "
+                    f"{result.get('routed_to_human', 0)} routed to human"
+                )
+        except Exception as e:
+            logger.error(f"send_pre_pickup_nudges error: {e}", exc_info=True)
```

#### `users/emails.py` — extract the shared `resolve_booking_url()` helper
The largest existing-code change. The booking-URL logic that was **inline** inside
`send_lead_quote_email` is lifted into a reusable module-level `resolve_booking_url(lead, quote=None)`
(+ a `GENERIC_BOOKING_URL` constant), then `send_lead_quote_email` calls it. Net behavior of the email
is unchanged; the nudge now reuses the exact same resolver.
```diff
+GENERIC_BOOKING_URL = "https://graysontowncar.com/rates-booking/"
+
+
+def resolve_booking_url(lead, quote=None):
+    """Best-effort direct booking URL … falls back to the generic page."""
+    if quote is None:
+        quote = lead.quotes.filter(is_current=True).select_related("vehicle").first()
+    if quote and quote.vehicle:
+        try:
+            from rates.models import Rate
+            rate = Rate.objects.filter(vehicle=quote.vehicle,
+                route__origin__name__iexact=quote.pickup_location,
+                route__destination__name__iexact=quote.dropoff_location).first()
+            if not rate:
+                rate = Rate.objects.filter(vehicle=quote.vehicle,
+                    route__origin__name__iexact=quote.dropoff_location,
+                    route__destination__name__iexact=quote.pickup_location).first()
+            if rate:
+                return f"https://graysontowncar.com/book-orlando-transportation/{rate.pk}"
+        except Exception:
+            logger.debug(f"Could not resolve booking rate for lead #{lead.id}")
+    return GENERIC_BOOKING_URL
+
+
 def send_lead_quote_email(lead, booking_url=None):
     ...
         quote = lead.quotes.filter(is_current=True).select_related("vehicle").first()
-        # (≈20 lines of inline Rate-matching logic removed) …
-        if not booking_url and quote and quote.vehicle:
-            ...inline rate lookup...
+        if not booking_url:
+            booking_url = resolve_booking_url(lead, quote=quote)
 
         context = {
             "lead": lead, "quote": quote,
-            "booking_url": booking_url or "https://graysontowncar.com/rates-booking/",
+            "booking_url": booking_url or GENERIC_BOOKING_URL,
         }
```

### Not committed
All changes are in the working tree only — nothing has been committed or pushed.
(A throwaway `_tmp_verify_nudge.py` was used for the live shell check and **deleted** afterward.)
