# Leads System Overhaul — Release / PR

Recover warm leads that were slipping through the cracks, make outreach **safe to run
unattended**, and give staff a date-anchored cockpit to work a day's leads.

> Deep-dive on the nudge engine internals lives in [`docs/pre-pickup-nudge.md`](pre-pickup-nudge.md).
> This file is the branch-level summary + rollout runbook + audit.

---

## What's in this branch (5 things)

| # | Feature | One-liner |
|---|---------|-----------|
| 1 | **Pre-pickup nudge** | One SMS ~3 days before pickup to warm-but-unbooked leads (incl. people who replied weeks ago). Fills the dead gap between the 5-step sequence and the trip. |
| 2 | **SMS opt-out / STOP suppression** | STOP/UNSUBSCRIBE/etc. now hard-blocks **all** outbound SMS (initial, 5-step, and nudge). There was **no opt-out handling before** — this is the safety fix that makes unattended sending OK. |
| 3 | **Full inbound-reply persistence** | The webhook now saves the **full text** of customer replies (was a 200-char preview). History exists from now on. |
| 4 | **Discount → human (with SLA)** | A staff-set per-lead discount routes to a human (ops task due on the pickup day, escalates a day before) instead of auto-texting a price the booking flow can't honor. |
| 5 | **Leads Board dashboard** | Pick a date → see leads bucketed into Booked / Active (hands-off) / Safe-to-offer / Nudged / Lost. Preview+edit an offer before sending, log a follow-up, mark lost, and view the conversation timeline — all without leaving for the admin. |

### Before → After
- **Before:** quote form → 5 SMS over ~4 days → silence until the trip → auto-`LOST`. Anyone who replied "maybe later" had their sequence cancelled and got nothing. No opt-out handling anywhere. No staff view of leads by date outside Django admin.
- **After:** same 5 steps, **plus** a date-anchored nudge that recovers the quiet leads, **plus** a hard STOP block protecting every send path, **plus** a staff board to work any date and make safe offers.

---

## Database changes (migrations)

| Migration | Change | Risk |
|-----------|--------|------|
| `reservations/0108_lead_pre_pickup_discount` | `Lead.pre_pickup_discount` Decimal, default 0 | None (AddField, default) |
| `reservations/0109_lead_sms_opt_out` | `Lead.sms_opt_out` Bool, default False, `db_index` | None (AddField, default) |
| `ghl_integration/0004_alter_followupsequence_…` | Add 2 nudge segment choices + help-text edits | None (AlterField, metadata) |
| `ghl_integration/0005_seed_pre_pickup_templates` | Seed 2 editable step-6 templates (data migration) | Idempotent (`update_or_create`); reverse deletes step-6 rows |

All reversible. No backfills, no large-table rewrites.

---

## How it works (short)

- **Scheduler:** `send_pre_pickup_nudges()` runs hourly inside the existing advisory-locked batch (`ghl_integration/scheduler.py`). Date-anchored (`pickup-3d`) + idempotent (one nudge per lead via a step-6 `FollowUpTask` + phone-level dedup), so the cadence can't double-fire.
- **Send path:** reuses `GoHighLevelService.send_sms`, the 8am–9pm ET send window, the editable `FollowUpSequence` templates, the renderer, and the SMS→email fallback.
- **Opt-out choke point:** `send_sms` now calls `_is_opted_out(contact_id)` (keyed by `normalized_phone`) and hard-blocks — so it protects the initial SMS, the 5-step sequence, **and** the nudge with one check.
- **Board:** `ops/leads_board.py` reuses the nudge engine's guards to classify each lead. Offers go through a preview/edit modal; the send path accepts the edited text verbatim while keeping every hard guard.

---

## New management commands

```bash
# Pre-pickup nudge (same code path the scheduler runs hourly)
python manage.py send_pre_pickup_nudges            # live
python manage.py send_pre_pickup_nudges --dry-run  # classify only, no sends/writes
python manage.py send_pre_pickup_nudges --lead 1234

# Demo data to preview the Leads Board (one lead per bucket)
python manage.py seed_leads_board_demo                 # today + 5 days
python manage.py seed_leads_board_demo --date 2026-06-10
python manage.py seed_leads_board_demo --clear         # remove demo leads
```

**Leads Board URL:** `/dispatching/leads-board/?date=YYYY-MM-DD` (staff login).

---

## Security & compliance

- **Opt-out is airtight (4 layers):** candidate query excludes opted-out → engine early-skip → `send_manual` early-skip → **`send_sms` hard-block** (the real choke point). STOP detection is **exact-match** on the trimmed body, so "please cancel my ride" does **not** trip it. Opt-out propagates to every lead sharing the phone.
- **Board endpoints:** all `@login_required` + `@user_passes_test(_is_staff)`; mutating actions are `@require_POST`; CSRF token sent on every POST; all server data inserted into the DOM is escaped (`esc()` / Django auto-escape) — no XSS.
- **No new secrets, no `csrf_exempt`, no `mark_safe`.**

---

## Testing

34 tests, all green:
- `ghl_integration/test_pre_pickup.py` (23) — resolver priority, eligibility window, dedup (incl. phone-level), throttle, send-window defer, discount→human, SMS-fail→email, opt-out skip, send_sms guard, webhook STOP propagation + full-body persistence, discount due-date.
- `ops/tests/test_leads_board.py` (11) — auth gate, bucketing, date filter, send offer (sends + blocks opted-out), offer preview (renders / discount-not-sendable / edited-text-sent), create task, mark lost, detail timeline.

```bash
python manage.py test ghl_integration.test_pre_pickup ops.tests.test_leads_board
# regression on the users/emails.py refactor:
python manage.py test ops.tests.test_unpaid_reminders
```

---

## Rollout runbook

1. **Merge & deploy.** No env changes required; the scheduler auto-starts from `ghl_integration/apps.py:ready()`.
2. **Migrate:** `python manage.py migrate` (adds the 2 Lead fields + seeds the 2 nudge templates).
3. **Verify quietly first:** `python manage.py send_pre_pickup_nudges --dry-run` to see who would be nudged before anything sends.
4. **QA the board:** `seed_leads_board_demo`, open `/dispatching/leads-board/?date=<that date>`, then `--clear`.
5. **Tune copy if desired** in Django admin → *Follow-Up Sequence Templates* (step 6) — no redeploy.
6. **Tuning knobs** (code constants): nudge window `PICKUP_DAYS_MIN/MAX` and throttle `MIN_OUTBOUND_GAP_HOURS` in `ghl_integration/pre_pickup.py`; board "active vs safe" thresholds `ACTIVE_REPLY_DAYS` / `ACTIVE_OUTBOUND_HOURS` in `ops/leads_board.py`.

---

## Audit summary

**Verdict: safe to merge — no BLOCKER/HIGH.** Compliance opt-out path airtight, staff endpoints auth-gated + XSS-safe, dedup correct, SLA math correct, migrations consistent. Open notes:

- **MEDIUM (product call):** the opt-out keyword set includes `CANCEL`/`END`/`QUIT`. For a car service where "cancel" is overloaded, a customer texting "Cancel" (meaning cancel a booking) would be permanently SMS-suppressed. The set was chosen deliberately; consider trimming to `STOP`/`STOPALL`/`UNSUBSCRIBE` if that risk matters. (`ghl_integration/views.py` `OPT_OUT_KEYWORDS`.)
- **LOW:** the webhook's reply-enrichment runs before the opt-out block, so a STOP lead is also flagged `needs_human_follow_up`/`INTERESTED` and shows in the board's "Active" bucket. Sends are still blocked; it only mildly pollutes the pipeline. Optional: short-circuit enrichment when the body is an opt-out keyword.
- **LOW:** dead defensive guard (`pickup_date <= today`) unreachable given the ≥2-day window — harmless.

---

## Follow-ups (not in this branch)

- Verify GHL's actual DND behavior with their support; read GHL DND on send-failure so a STOP can't become an email; backfill existing STOPs already in GHL.
- "Smart" booking link: include a one-tap link **only** when a real per-trip booking page resolves (not the generic fallback); reply-driven otherwise. (Default copy is currently link-free.)
- Older GHL conversation threads on demand in the Details modal (local history starts from this deploy).
- Nudge attribution funnel (sent → replied≤72h → booked) and/or a holdout for causal lift.

---

## File inventory (THIS branch only)

**⚠️ The working tree also contains UNRELATED uncommitted changes that are NOT part of this work and must be excluded from the commit** (do **not** `git add -A`): `payment/views.py`, `payment/webhook.py`, `payment/templates/stripe/success.html`, `reservations/views.py`, `reservations/conversions.py`, `dispatching/views.py`, `content/static/js/guest-quote.js`, `content/staticfiles/js/guest-quote.js`, `docs/META_SETUP_GUIDE.md`, `docs/sales-pipeline-automation-review.md`. Also, `makemigrations --check` is dirty from an unrelated `users.partnerform.agency_size` field — handle separately.

**Modified (this branch):**
`dispatching/urls.py` · `ghl_integration/models.py` · `ghl_integration/scheduler.py` · `ghl_integration/services.py` · `ghl_integration/tasks.py` · `ghl_integration/templates_engine.py` · `ghl_integration/views.py` · `reservations/admin.py` · `reservations/models.py` · `users/emails.py`

**New (this branch):**
`ghl_integration/pre_pickup.py` · `ghl_integration/management/commands/send_pre_pickup_nudges.py` · `ghl_integration/migrations/0004_alter_followupsequence_delay_hours_and_more.py` · `ghl_integration/migrations/0005_seed_pre_pickup_templates.py` · `ghl_integration/test_pre_pickup.py` · `ops/leads_board.py` · `ops/management/commands/seed_leads_board_demo.py` · `ops/tests/test_leads_board.py` · `dispatching/templates/dispatching/leads_board.html` · `reservations/migrations/0108_lead_pre_pickup_discount.py` · `reservations/migrations/0109_lead_sms_opt_out.py` · `docs/pre-pickup-nudge.md` · `docs/leads-system-overhaul.md`
