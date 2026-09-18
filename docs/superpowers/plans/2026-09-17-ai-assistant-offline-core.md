# AI Reservations Assistant — Offline Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build everything needed to generate a grounded draft reply from a Lead and an inbound message, and prove it works by replaying real historical conversations locally — with no webhook wiring, no UI, and no possibility of sending.

**Architecture:** Django assembles a "context packet" of facts (rate-card price, vehicle capability, policy, knowledge, conversation history, missing trip details) before any model call. `brain.draft_reply(packet)` takes a plain dict and returns a dataclass — no ORM, no network in tests. Code-level guards then check the output for invented prices and other violations. A management command replays historical `LeadActivity` replies through the whole chain and writes drafts to a file.

**Tech Stack:** Django 5.1.4, Python 3.10+, `anthropic` SDK, Claude `claude-haiku-4-5`, SQLite locally / Postgres on Railway.

**Spec:** [docs/superpowers/specs/2026-09-17-ai-reservations-assistant-design.md](../specs/2026-09-17-ai-reservations-assistant-design.md)

**Scope note:** This is plan 1 of 2. It covers spec §4–§7, §10, §12, §13, §15. Plan 2 covers the live pipeline (§8), approval UI (§9.1), feedback capture (§9), and metrics (§14). This plan produces working, independently testable software: a replay harness that shows real drafts.

## Global Constraints

- **The assistant never fetches facts.** No tool calls, no ORM access from `brain.py`. Django builds the packet; the model only writes prose from it. (Spec §4.1)
- **No new pricing code.** All prices come from `dispatching.quote_engine`. (Spec §6.2)
- **v1 answers rate-card routes only.** Anything off-card omits pricing and escalates. (Spec §6.2)
- **Only `QuoteResult.price` may reach a customer.** `breakdown` and `notes` go in `packet["internal"]`. (Spec §6.2)
- **`manage.py test` makes zero network calls.** `brain.draft_reply` is stubbed at the dict boundary. (Spec §13)
- **Both enable flags default `False`.** Installing this code changes nothing. (Spec §15)
- Model id: `claude-haiku-4-5`. No `output_config.effort` — Haiku 4.5 rejects it. No `thinking` block. `max_tokens: 1024`. (Spec §10.1)
- Test files are named `tests_*.py` inside the app and run with `./manage.py test ai_assistant.tests_<module>`. Use `django.test.TestCase`, matching `dispatching/tests_fleet_desk.py`.
- Commit messages: end with `Release-Note: none` (nothing here is dispatcher-visible) and the `Co-Authored-By` trailer this repo uses.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `ai_assistant/__init__.py`, `apps.py` | app registration |
| `ai_assistant/models.py` | `AiConversationTurn`, `AiDraft`, `KnowledgeItem`, `AiFeedback` |
| `ai_assistant/pricing.py` | rate-card lookup via `quote_engine`; nothing else prices |
| `ai_assistant/vehicles.py` | capacity, car seats, luggage; smallest-vehicle-that-fits |
| `ai_assistant/history.py` | conversation reconstruction from `LeadActivity` |
| `ai_assistant/slots.py` | what we know vs what's missing |
| `ai_assistant/context.py` | assembles the packet, assigns fact ids |
| `ai_assistant/brain.py` | the single Claude call; dict in, dataclass out |
| `ai_assistant/guards.py` | post-generation checks (§7) |
| `ai_assistant/prompts/v1.txt` | the system prompt |
| `ai_assistant/management/commands/ai_replay.py` | offline backtest |
| `ai_assistant/tests_*.py` | one test module per unit above |

---

### Task 1: App scaffold, dependency, settings

**Files:**
- Create: `ai_assistant/__init__.py`, `ai_assistant/apps.py`, `ai_assistant/models.py` (empty), `ai_assistant/migrations/__init__.py`
- Create: `ai_assistant/tests_settings.py`
- Modify: `requirements.txt`
- Modify: `business/settings.py` (add to `OUR_APPS`, add settings block)

**Interfaces:**
- Consumes: nothing
- Produces: the `ai_assistant` app label; settings `AI_ASSISTANT_ENABLED`, `AI_ASSISTANT_LIVE_SEND`, `AI_ASSISTANT_MODEL`, `AI_PROMPT_VERSION`, `AI_HUMAN_TAKEOVER_MINUTES`, `AI_HISTORY_TURNS`, `AI_DAILY_SPEND_CAP_USD`, `ANTHROPIC_API_KEY`

- [ ] **Step 1: Write the failing test**

Create `ai_assistant/tests_settings.py`:

```python
"""The AI assistant ships switched off.

Run with:  ./manage.py test ai_assistant.tests_settings
"""
from django.apps import apps
from django.conf import settings
from django.test import TestCase


class SettingsDefaults(TestCase):
    def test_the_app_is_installed(self):
        self.assertTrue(apps.is_installed("ai_assistant"))

    def test_both_switches_are_off_by_default(self):
        # Installing this code must change nothing until someone turns it on.
        self.assertFalse(settings.AI_ASSISTANT_ENABLED)
        self.assertFalse(settings.AI_ASSISTANT_LIVE_SEND)

    def test_the_model_is_pinned(self):
        self.assertEqual(settings.AI_ASSISTANT_MODEL, "claude-haiku-4-5")

    def test_the_operational_defaults_are_set(self):
        self.assertEqual(settings.AI_PROMPT_VERSION, "v1")
        self.assertEqual(settings.AI_HUMAN_TAKEOVER_MINUTES, 15)
        self.assertEqual(settings.AI_HISTORY_TURNS, 10)
        self.assertEqual(settings.AI_DAILY_SPEND_CAP_USD, 10)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `./manage.py test ai_assistant.tests_settings`
Expected: FAIL — `ModuleNotFoundError: No module named 'ai_assistant'`

- [ ] **Step 3: Create the app package**

```bash
mkdir -p ai_assistant/migrations ai_assistant/management/commands ai_assistant/prompts
touch ai_assistant/__init__.py ai_assistant/migrations/__init__.py
touch ai_assistant/management/__init__.py ai_assistant/management/commands/__init__.py
touch ai_assistant/models.py
```

`ai_assistant/apps.py`:

```python
from django.apps import AppConfig


class AiAssistantConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "ai_assistant"
    verbose_name = "AI Assistant"
```

- [ ] **Step 4: Add the dependency**

Append to `requirements.txt`:

```
anthropic
```

Then install: `pip install anthropic`

- [ ] **Step 5: Register the app**

In `business/settings.py`, add `"ai_assistant"` to the end of the `OUR_APPS` list:

```python
OUR_APPS = [
    "rates",
    "reservations.apps.ReservationsConfig",
    "users",
    "services",
    "blog",
    "payment",
    "drivers",
    "dispatching",
    "ghl_integration",
    "ops.apps.OpsConfig",
    "ai_assistant",
]
```

- [ ] **Step 6: Add the settings block**

In `business/settings.py`, below the existing `GHL_API_KEY` line, add:

```python
# ── AI reservations assistant ────────────────────────────────────────────────
# Both switches default OFF. Installing the code changes nothing; someone has to
# deliberately turn it on. AI_ASSISTANT_LIVE_SEND gates EVERY outbound message,
# including ones a dispatcher approved — so a local machine can never text a
# real customer, no matter what else is misconfigured.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AI_ASSISTANT_ENABLED = os.environ.get("AI_ASSISTANT_ENABLED", "False").lower() == "true"
AI_ASSISTANT_LIVE_SEND = os.environ.get("AI_ASSISTANT_LIVE_SEND", "False").lower() == "true"
AI_ASSISTANT_MODEL = os.environ.get("AI_ASSISTANT_MODEL", "claude-haiku-4-5")
AI_PROMPT_VERSION = os.environ.get("AI_PROMPT_VERSION", "v1")
AI_HUMAN_TAKEOVER_MINUTES = int(os.environ.get("AI_HUMAN_TAKEOVER_MINUTES", "15"))
AI_HISTORY_TURNS = int(os.environ.get("AI_HISTORY_TURNS", "10"))
AI_DAILY_SPEND_CAP_USD = int(os.environ.get("AI_DAILY_SPEND_CAP_USD", "10"))
```

- [ ] **Step 7: Run the test and watch it pass**

Run: `./manage.py test ai_assistant.tests_settings`
Expected: PASS, 4 tests

- [ ] **Step 8: Commit**

```bash
git add ai_assistant/ requirements.txt business/settings.py
git commit -m "$(cat <<'EOF'
Add the AI assistant app, switched off

Both enable flags default to false and the send flag gates every outbound
message, approved or not. A developer machine cannot text a customer even if
everything else is misconfigured.

Release-Note: none

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Models and migration

**Files:**
- Modify: `ai_assistant/models.py`
- Create: `ai_assistant/tests_models.py`
- Create: `ai_assistant/migrations/0001_initial.py` (generated)

**Interfaces:**
- Consumes: `reservations.Lead`, `django.contrib.auth.models.User`, `simple_history.models.HistoricalRecords`
- Produces: `AiConversationTurn`, `AiDraft`, `KnowledgeItem`, `AiFeedback`, and the choice classes `TurnStatus`, `DraftStatus`, `Confidence`, `FeedbackKind`, `RejectReason`

- [ ] **Step 1: Write the failing test**

Create `ai_assistant/tests_models.py`:

```python
"""Storage for turns, drafts, knowledge and feedback.

Run with:  ./manage.py test ai_assistant.tests_models
"""
from django.test import TestCase

from ai_assistant.models import (
    AiConversationTurn, AiDraft, AiFeedback, KnowledgeItem,
)
from reservations.models import Lead


class TurnAndDraft(TestCase):
    def setUp(self):
        self.lead = Lead.objects.create(first_name="Vincent", phone="4075551234")

    def test_a_turn_starts_pending_with_an_empty_packet(self):
        turn = AiConversationTurn.objects.create(
            lead=self.lead, inbound_text="Just 2",
        )
        self.assertEqual(turn.status, AiConversationTurn.Status.PENDING)
        self.assertEqual(turn.context_packet, {})
        self.assertEqual(turn.extracted, {})
        self.assertIsNone(turn.extracted_applied_at)
        self.assertEqual(turn.attempts, 0)

    def test_a_draft_hangs_off_exactly_one_turn(self):
        turn = AiConversationTurn.objects.create(lead=self.lead, inbound_text="hi")
        draft = AiDraft.objects.create(
            turn=turn, reply_text="Hello!", model_id="claude-haiku-4-5",
            prompt_version="v1",
        )
        self.assertEqual(turn.draft, draft)
        self.assertEqual(draft.status, AiDraft.Status.AWAITING_REVIEW)
        self.assertFalse(draft.needs_human)
        self.assertFalse(draft.flagged)
        self.assertFalse(draft.auto_sent)

    def test_feedback_records_what_was_written_and_what_was_sent(self):
        turn = AiConversationTurn.objects.create(lead=self.lead, inbound_text="hi")
        draft = AiDraft.objects.create(
            turn=turn, reply_text="Hi there", model_id="m", prompt_version="v1",
        )
        fb = AiFeedback.objects.create(
            draft=draft, kind=AiFeedback.Kind.EDITED,
            original_text="Hi there", final_text="Hey there!",
        )
        # The diff between these two is the training signal.
        self.assertNotEqual(fb.original_text, fb.final_text)


class Knowledge(TestCase):
    def test_a_knowledge_item_is_active_and_versioned(self):
        item = KnowledgeItem.objects.create(
            topic="car seats", answer="Car seats are included free.",
        )
        self.assertTrue(item.is_active)
        item.answer = "Two car seats are included free."
        item.save()
        # simple_history keeps every edit, so a wrong answer is traceable.
        self.assertEqual(item.history.count(), 2)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `./manage.py test ai_assistant.tests_models`
Expected: FAIL — `ImportError: cannot import name 'AiConversationTurn'`

- [ ] **Step 3: Write the models**

Replace `ai_assistant/models.py`:

```python
"""Storage for the AI reservations assistant.

Four tables, and the reason for each:

  AiConversationTurn  one inbound message — the unit of work. Carries the FULL
                      context packet the model was given, because when a draft
                      is wrong you must be able to tell whether the model
                      reasoned badly or was handed a bad fact. Those have
                      completely different fixes.
  AiDraft             one generated reply, plus what was actually sent. The
                      diff between reply_text and sent_text is the highest
                      value signal in the system.
  KnowledgeItem       company facts a dispatcher can correct without a deploy.
  AiFeedback          every review outcome. An approval is as informative as a
                      rejection.
"""
from django.conf import settings
from django.db import models
from django.utils import timezone
from simple_history.models import HistoricalRecords


class AiConversationTurn(models.Model):
    """One inbound customer message the assistant was asked to answer."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        DRAFTED = "drafted", "Drafted"
        ESCALATED = "escalated", "Escalated"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"
        SUPERSEDED = "superseded", "Superseded"

    class SkipReason(models.TextChoices):
        OPTED_OUT = "opted_out", "Opted out"
        HUMAN_ACTIVE = "human_active", "Human active"
        DISABLED = "disabled", "Disabled"
        DUPLICATE = "duplicate", "Duplicate"

    lead = models.ForeignKey(
        "reservations.Lead", on_delete=models.CASCADE, related_name="ai_turns"
    )
    ghl_contact_id = models.CharField(max_length=100, blank=True, db_index=True)
    inbound_text = models.TextField()
    received_at = models.DateTimeField(default=timezone.now, db_index=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    skip_reason = models.CharField(
        max_length=20, choices=SkipReason.choices, blank=True
    )
    # Exactly what the model was given. Never prune this — it is the only way to
    # separate a reasoning failure from a context bug after the fact.
    context_packet = models.JSONField(default=dict, blank=True)
    # Trip details the model found in the message. PROPOSED only: a dispatcher
    # applies them. A bad extraction written straight to the Lead would corrupt
    # the record with nothing pointing at which turn did it.
    extracted = models.JSONField(default=dict, blank=True)
    extracted_applied_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["-received_at"]
        indexes = [models.Index(fields=["status", "received_at"])]

    def __str__(self):
        return f"Turn #{self.pk} — lead #{self.lead_id} — {self.status}"


class AiDraft(models.Model):
    """One generated reply, and what a human did with it."""

    class Status(models.TextChoices):
        AWAITING_REVIEW = "awaiting_review", "Awaiting review"
        SENT = "sent", "Sent"
        REJECTED = "rejected", "Rejected"
        SUPERSEDED = "superseded", "Superseded"

    class Confidence(models.TextChoices):
        HIGH = "high", "High"
        MEDIUM = "medium", "Medium"
        LOW = "low", "Low"

    turn = models.OneToOneField(
        AiConversationTurn, on_delete=models.CASCADE, related_name="draft"
    )
    reply_text = models.TextField()
    needs_human = models.BooleanField(default=False)
    reason = models.TextField(blank=True)
    confidence = models.CharField(
        max_length=10, choices=Confidence.choices, default=Confidence.MEDIUM
    )
    facts_used = models.JSONField(default=list, blank=True)
    flagged = models.BooleanField(default=False, db_index=True)
    flag_detail = models.TextField(blank=True)

    model_id = models.CharField(max_length=60)
    prompt_version = models.CharField(max_length=20)
    input_tokens = models.PositiveIntegerField(default=0)
    cached_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    latency_ms = models.PositiveIntegerField(default=0)

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.AWAITING_REVIEW,
        db_index=True,
    )
    sent_text = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    auto_sent = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Draft #{self.pk} — {self.status}"


class KnowledgeItem(models.Model):
    """A company fact the assistant may state, editable without a deploy."""

    topic = models.CharField(max_length=80, db_index=True)
    trigger_hint = models.CharField(
        max_length=200, blank=True,
        help_text="When this applies, in plain words. Guidance for the model, "
                  "not a matching rule.",
    )
    answer = models.TextField(
        help_text="Write it the way you would say it to a customer."
    )
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=100)
    history = HistoricalRecords()

    class Meta:
        ordering = ["sort_order", "topic"]

    def __str__(self):
        return self.topic


class AiFeedback(models.Model):
    """What a human did with a draft. Written on every outcome, always."""

    class Kind(models.TextChoices):
        APPROVED = "approved", "Approved as written"
        EDITED = "edited", "Edited then sent"
        REJECTED = "rejected", "Rejected"

    class RejectReason(models.TextChoices):
        WRONG_PRICE = "wrong_price", "Wrong price"
        WRONG_POLICY = "wrong_policy", "Wrong policy"
        WRONG_FACTS = "wrong_facts", "Wrong facts"
        WRONG_TONE = "wrong_tone", "Wrong tone"
        MISSED_THE_QUESTION = "missed_the_question", "Missed the question"
        SHOULD_HAVE_ESCALATED = "should_have_escalated", "Should have escalated"
        OTHER = "other", "Other"

    draft = models.ForeignKey(
        AiDraft, on_delete=models.CASCADE, related_name="feedback"
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    # Fixed codes are countable. Free text alone gives anecdotes, not numbers.
    reason_code = models.CharField(
        max_length=30, choices=RejectReason.choices, blank=True
    )
    original_text = models.TextField()
    final_text = models.TextField(blank=True)
    note = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.kind} on draft #{self.draft_id}"
```

- [ ] **Step 4: Generate the migration**

Run: `./manage.py makemigrations ai_assistant`
Expected: creates `ai_assistant/migrations/0001_initial.py` with 5 models (4 plus `HistoricalKnowledgeItem`)

- [ ] **Step 5: Run the test and watch it pass**

Run: `./manage.py test ai_assistant.tests_models`
Expected: PASS, 4 tests

- [ ] **Step 6: Commit**

```bash
git add ai_assistant/models.py ai_assistant/migrations/ ai_assistant/tests_models.py
git commit -m "$(cat <<'EOF'
Store the whole context packet, not just the answer

When a draft is wrong you have to know whether the model reasoned badly or was
handed a bad fact — those need completely different fixes, and you cannot tell
them apart afterwards without the packet. So every turn keeps the full input.

Drafts keep what was written next to what was actually sent. That diff is the
training signal.

Release-Note: none

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Rate-card pricing

**Files:**
- Create: `ai_assistant/pricing.py`
- Create: `ai_assistant/tests_pricing.py`

**Interfaces:**
- Consumes: `dispatching.quote_engine.match_location`, `.quote_all_vehicles`; `rates.models.Location`
- Produces:
  - `card_prices(pickup_text, dropoff_text, trip_type) -> dict | None`
    Returns `None` when either end fails to match a `Location`, or when no
    result is a rate-card hit. Otherwise:
    `{"route": str, "trip_type": str, "by_vehicle": {vehicle_type: {"label": str, "price": str, "oneway": str, "roundtrip": str}}, "internal": {vehicle_type: {"notes": [str], "breakdown": dict}}}`

- [ ] **Step 1: Write the failing test**

Create `ai_assistant/tests_pricing.py`:

```python
"""Prices come from the dispatcher quote engine or they do not come at all.

Run with:  ./manage.py test ai_assistant.tests_pricing
"""
from decimal import Decimal

from django.test import TestCase

from ai_assistant import pricing
from rates.models import Location, Rate, Route, Vehicle


class CardPrices(TestCase):
    def setUp(self):
        self.mco = Location.objects.create(
            name="Orlando International Airport", aliases="MCO, Orlando Airport"
        )
        self.disney = Location.objects.create(
            name="All WDW Disney Property Resorts", aliases="Disney, WDW"
        )
        self.route = Route.objects.create(origin=self.mco, destination=self.disney)
        self.towncar = Vehicle.objects.create(
            vehicle_type="towncar", capacity=4, luggage_capacity=4
        )
        self.van = Vehicle.objects.create(
            vehicle_type="van", capacity=10, luggage_capacity=10
        )
        Rate.objects.create(
            vehicle=self.towncar, route=self.route,
            oneway_price=Decimal("140.00"), round_trip_price=Decimal("260.00"),
        )
        Rate.objects.create(
            vehicle=self.van, route=self.route,
            oneway_price=Decimal("225.00"), round_trip_price=Decimal("420.00"),
        )

    def test_a_card_route_prices_every_vehicle_on_the_card(self):
        result = pricing.card_prices("MCO", "Disney", "oneway")
        self.assertIsNotNone(result)
        self.assertEqual(result["by_vehicle"]["towncar"]["price"], "140.00")
        self.assertEqual(result["by_vehicle"]["van"]["price"], "225.00")

    def test_a_round_trip_uses_the_round_trip_column(self):
        result = pricing.card_prices("MCO", "Disney", "roundtrip")
        self.assertEqual(result["by_vehicle"]["towncar"]["price"], "260.00")

    def test_an_unmatched_pickup_yields_no_pricing_at_all(self):
        # No price is the correct answer. A guess is not.
        self.assertIsNone(pricing.card_prices("123 Nowhere Rd", "Disney", "oneway"))

    def test_an_unmatched_dropoff_yields_no_pricing_at_all(self):
        self.assertIsNone(pricing.card_prices("MCO", "123 Nowhere Rd", "oneway"))

    def test_a_route_with_no_card_entry_yields_no_pricing(self):
        tampa = Location.objects.create(name="Tampa", aliases="TPA")
        self.assertIsNone(pricing.card_prices("MCO", "Tampa", "oneway"))

    def test_dispatcher_only_text_is_quarantined_from_the_customer_half(self):
        # quote_engine attaches notes like "Quote this, not a custom price."
        # Those must never sit anywhere a reply could pick them up.
        result = pricing.card_prices("MCO", "Disney", "oneway")
        customer_half = str(result["by_vehicle"])
        self.assertNotIn("Quote this", customer_half)
        self.assertIn("towncar", result["internal"])

    def test_a_blank_address_yields_no_pricing(self):
        self.assertIsNone(pricing.card_prices("", "Disney", "oneway"))
        self.assertIsNone(pricing.card_prices("MCO", None, "oneway"))
```

- [ ] **Step 2: Run it and watch it fail**

Run: `./manage.py test ai_assistant.tests_pricing`
Expected: FAIL — `ModuleNotFoundError: No module named 'ai_assistant.pricing'`

- [ ] **Step 3: Write the implementation**

Create `ai_assistant/pricing.py`:

```python
"""Rate-card prices for the assistant. This module writes no pricing rules.

Everything here delegates to dispatching.quote_engine, which calls itself "the
single source of truth for dispatcher price estimates" and encodes founder
decisions the assistant must not relitigate: the published card beats the
formula outright, gratuity is a separate line, airport fees never apply to a
card price.

The assistant quotes what a dispatcher would quote because it is the same
function. Do not add a second pricing path here. If a price cannot be found,
the correct output is None — the packet then has no pricing and the turn
escalates to a human. A guess is never the correct output.

v1 SCOPE: rate-card routes only. When both ends match a Location the card price
needs no distance and the answer is exact. An unmatched end would fall to the
mileage formula, which needs a Google Maps lookup — an external dependency in
the reply path, and more ways to be wrong. Those escalate instead.
"""
import logging

from dispatching import quote_engine
from rates.models import Location

logger = logging.getLogger(__name__)


def card_prices(pickup_text, dropoff_text, trip_type):
    """Every rate-card price for this route, or None.

    Returns None when either address fails to match a Location, or when no
    vehicle has a card entry for the matched route. None means "escalate",
    never "make something up".
    """
    if not pickup_text or not dropoff_text:
        return None

    locations = list(Location.objects.all())
    pickup_loc, _ = quote_engine.match_location(pickup_text, locations)
    dropoff_loc, _ = quote_engine.match_location(dropoff_text, locations)
    if not (pickup_loc and dropoff_loc):
        return None

    try:
        results = quote_engine.quote_all_vehicles(
            trip_type=trip_type,
            pickup_location=pickup_loc,
            dropoff_location=dropoff_loc,
        )
    except (KeyError, ValueError) as exc:
        # An unknown vehicle type or bad input is a data problem. Surface it in
        # the log and price nothing — the turn escalates.
        logger.warning(
            "Quote engine refused %s -> %s: %s", pickup_text, dropoff_text, exc
        )
        return None

    card = [r for r in results if r.is_rate_card]
    if not card:
        return None

    by_vehicle = {}
    internal = {}
    for r in card:
        # QuoteResult.price is the ONLY figure a guest may see. breakdown and
        # notes are dispatcher-facing — one note literally reads "Quote this,
        # not a custom price." They go in the internal half and stay there.
        by_vehicle[r.vehicle_type] = {
            "label": r.vehicle_label,
            "price": str(r.price),
            "oneway": str(r.card_oneway) if r.card_oneway is not None else None,
            "roundtrip": str(r.card_roundtrip) if r.card_roundtrip is not None else None,
        }
        internal[r.vehicle_type] = {
            "notes": list(r.notes),
            "breakdown": dict(r.breakdown),
        }

    return {
        "route": f"{pickup_loc.name} to {dropoff_loc.name}",
        "trip_type": trip_type,
        "by_vehicle": by_vehicle,
        "internal": internal,
    }
```

- [ ] **Step 4: Run the test and watch it pass**

Run: `./manage.py test ai_assistant.tests_pricing`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add ai_assistant/pricing.py ai_assistant/tests_pricing.py
git commit -m "$(cat <<'EOF'
Quote from the dispatcher's own engine, or quote nothing

The assistant calls the same quote_engine dispatchers do, so it cannot drift
from what the rate card says. When either address does not match a known
location, the answer is no price at all and the conversation goes to a human —
the mileage formula needs a maps lookup and more chances to be wrong.

The engine's internal notes are quarantined. One of them reads "Quote this, not
a custom price", which is advice for a dispatcher and would read as nonsense to
a customer.

Release-Note: none

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Vehicle capability and fit

**Files:**
- Create: `ai_assistant/vehicles.py`
- Create: `ai_assistant/tests_vehicles.py`

**Interfaces:**
- Consumes: `rates.models.Vehicle`; `dispatching.quote_engine.VEHICLE_TIER_ORDER`
- Produces:
  - `vehicle_facts() -> list[dict]` — one dict per vehicle, smallest first, keys: `vehicle_type`, `label`, `capacity`, `luggage_capacity`, `carseats_display`, `included_carseats`, `included_boosters`, `extra_carseat_fee`, `extra_booster_fee`
  - `smallest_that_fits(passengers, luggage) -> dict | None` — the first vehicle in tier order meeting both, or `None` if nothing does

- [ ] **Step 1: Write the failing test**

Create `ai_assistant/tests_vehicles.py`:

```python
"""Capacity and car seats are lookups, not judgement calls.

Run with:  ./manage.py test ai_assistant.tests_vehicles
"""
from decimal import Decimal

from django.test import TestCase

from ai_assistant import vehicles
from rates.models import Vehicle


class VehicleFacts(TestCase):
    def setUp(self):
        Vehicle.objects.create(
            vehicle_type="towncar", capacity=4, luggage_capacity=4,
            included_carseats=2, included_boosters=2,
            carseats_display="Up to 2 car seats included",
            extra_carseat_fee=Decimal("0.00"),
            extra_booster_fee=Decimal("0.00"),
        )
        Vehicle.objects.create(
            vehicle_type="suv", capacity=6, luggage_capacity=6,
            included_carseats=3, included_boosters=3,
        )
        Vehicle.objects.create(
            vehicle_type="van", capacity=10, luggage_capacity=10,
            included_carseats=4, included_boosters=4,
        )

    def test_facts_come_back_smallest_first(self):
        order = [v["vehicle_type"] for v in vehicles.vehicle_facts()]
        self.assertEqual(order, ["towncar", "suv", "van"])

    def test_facts_carry_the_car_seat_detail(self):
        towncar = vehicles.vehicle_facts()[0]
        self.assertEqual(towncar["included_carseats"], 2)
        self.assertEqual(towncar["carseats_display"], "Up to 2 car seats included")

    def test_nine_passengers_needs_the_van(self):
        fit = vehicles.smallest_that_fits(passengers=9, luggage=4)
        self.assertEqual(fit["vehicle_type"], "van")

    def test_three_passengers_two_bags_fit_the_towncar(self):
        fit = vehicles.smallest_that_fits(passengers=3, luggage=2)
        self.assertEqual(fit["vehicle_type"], "towncar")

    def test_luggage_can_bind_before_seats_do(self):
        # Four people fit a towncar; six suitcases do not.
        fit = vehicles.smallest_that_fits(passengers=4, luggage=6)
        self.assertEqual(fit["vehicle_type"], "suv")

    def test_a_party_nothing_fits_returns_nothing(self):
        self.assertIsNone(vehicles.smallest_that_fits(passengers=40, luggage=0))

    def test_unknown_counts_do_not_guess(self):
        # If we do not know the party size we cannot pick a vehicle.
        self.assertIsNone(vehicles.smallest_that_fits(passengers=None, luggage=None))
```

- [ ] **Step 2: Run it and watch it fail**

Run: `./manage.py test ai_assistant.tests_vehicles`
Expected: FAIL — `ModuleNotFoundError: No module named 'ai_assistant.vehicles'`

- [ ] **Step 3: Write the implementation**

Create `ai_assistant/vehicles.py`:

```python
"""What each vehicle can carry, and which one a party actually needs.

Car seats and luggage are the single largest answerable category of inbound
question (11.3% of 45,046 stored replies), and every answer is already in the
Vehicle row. Nothing here is a judgement call — it is comparison against
columns, which is exactly what the assistant must not be left to do from
memory.
"""
from dispatching.quote_engine import VEHICLE_TIER_ORDER
from rates.models import Vehicle


def _tier_index(vehicle_type):
    """Position in the smallest-to-largest ordering the quote engine uses."""
    try:
        return VEHICLE_TIER_ORDER.index(vehicle_type)
    except ValueError:
        # A vehicle type the engine does not price sorts last rather than
        # crashing the packet build.
        return len(VEHICLE_TIER_ORDER)


def _money(value):
    return str(value) if value is not None else None


def vehicle_facts():
    """Every vehicle's capability, smallest first."""
    rows = []
    for v in Vehicle.objects.all():
        rows.append({
            "vehicle_type": v.vehicle_type,
            "label": v.get_vehicle_type_display(),
            "capacity": v.capacity,
            "luggage_capacity": v.luggage_capacity,
            "carseats_display": v.carseats_display or "",
            "included_carseats": v.included_carseats,
            "included_boosters": v.included_boosters,
            "extra_carseat_fee": _money(v.extra_carseat_fee),
            "extra_booster_fee": _money(v.extra_booster_fee),
        })
    rows.sort(key=lambda r: _tier_index(r["vehicle_type"]))
    return rows


def smallest_that_fits(passengers, luggage):
    """The smallest vehicle carrying this party, or None.

    None means one of two things and the caller must treat both as "ask a
    human": we do not know the party size yet, or nothing we own is big enough.
    Neither is a case for picking something and hoping.
    """
    if passengers is None and luggage is None:
        return None

    need_seats = passengers or 0
    need_bags = luggage or 0
    for row in vehicle_facts():
        if row["capacity"] >= need_seats and row["luggage_capacity"] >= need_bags:
            return row
    return None
```

- [ ] **Step 4: Run the test and watch it pass**

Run: `./manage.py test ai_assistant.tests_vehicles`
Expected: PASS, 7 tests

- [ ] **Step 5: Commit**

```bash
git add ai_assistant/vehicles.py ai_assistant/tests_vehicles.py
git commit -m "$(cat <<'EOF'
Answer car seats and capacity from the vehicle row

Car seats and luggage are the biggest single category of inbound question and
every answer is already a column on the vehicle. Comparing numbers is not
something the assistant should be doing from memory, so it never sees the
question without the table.

Not knowing the party size returns nothing rather than a default. A guessed
vehicle is a guessed price.

Release-Note: none

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Conversation history

**Files:**
- Create: `ai_assistant/history.py`
- Create: `ai_assistant/tests_history.py`
- Modify: `ghl_integration/tasks.py` (store sent text on new `sms_sent` activities)

**Interfaces:**
- Consumes: `ghl_integration.models.LeadActivity`, `FollowUpSequence`; `ghl_integration.templates_engine.render_follow_up_message`
- Produces: `conversation_for(lead, limit=None) -> list[dict]` with keys `from` (`"customer"` or `"us"`), `text`, `at` (ISO string), oldest first

**Background the implementer needs:** inbound replies store their text in
`LeadActivity.metadata["message_body"]` (recent) or `["message_preview"]`
(older, 200 chars — fine, SMS segments are 160). Outbound `sms_sent` rows store
**no body at all** — only `description="Follow-up step 2 sent"` and
`metadata={"step": 2, "segment": "airport_transfer"}`. So our side of the
conversation is reconstructed by re-rendering the template for that step and
segment. New sends start recording their text so future reconstruction is
unnecessary.

- [ ] **Step 1: Write the failing test**

Create `ai_assistant/tests_history.py`:

```python
"""Rebuilding both sides of a conversation from the activity log.

Run with:  ./manage.py test ai_assistant.tests_history
"""
from django.test import TestCase
from django.utils import timezone

from ai_assistant import history
from ghl_integration.models import FollowUpSequence, LeadActivity
from reservations.models import Lead


class Conversation(TestCase):
    def setUp(self):
        self.lead = Lead.objects.create(
            first_name="Vincent",
            pickup_location="Orlando International Airport",
            dropoff_location="All WDW Disney Property Resorts",
        )
        FollowUpSequence.objects.create(
            step_number=1, segment="general", delay_hours=0,
            message_template=(
                "Hey {first_name}, this is Grayson Towncar. Do you still need "
                "transportation from {pickup_location} to {dropoff_location}?"
            ),
        )

    def test_an_inbound_reply_is_read_from_the_metadata(self):
        LeadActivity.objects.create(
            lead=self.lead, activity_type=LeadActivity.ActivityType.REPLY_RECEIVED,
            description="SMS reply received: Just 2",
            metadata={"message_body": "Just 2"},
        )
        convo = history.conversation_for(self.lead)
        self.assertEqual(convo[0]["from"], "customer")
        self.assertEqual(convo[0]["text"], "Just 2")

    def test_an_older_reply_falls_back_to_the_preview(self):
        LeadActivity.objects.create(
            lead=self.lead, activity_type=LeadActivity.ActivityType.REPLY_RECEIVED,
            description="SMS reply received",
            metadata={"message_preview": "Got a better price $82.00"},
        )
        convo = history.conversation_for(self.lead)
        self.assertEqual(convo[0]["text"], "Got a better price $82.00")

    def test_our_outbound_text_is_rebuilt_from_the_template(self):
        # sms_sent rows store no body, only which step went out. Without this
        # rebuild, "Just 2" has no question attached and reads as noise.
        LeadActivity.objects.create(
            lead=self.lead, activity_type=LeadActivity.ActivityType.SMS_SENT,
            description="Follow-up step 1 sent",
            metadata={"step": 1, "segment": "general"},
        )
        convo = history.conversation_for(self.lead)
        self.assertEqual(convo[0]["from"], "us")
        self.assertIn("Do you still need transportation", convo[0]["text"])
        self.assertIn("Vincent", convo[0]["text"])

    def test_a_newly_stored_body_is_preferred_over_rebuilding(self):
        LeadActivity.objects.create(
            lead=self.lead, activity_type=LeadActivity.ActivityType.SMS_SENT,
            description="Follow-up step 1 sent",
            metadata={"step": 1, "segment": "general", "message_body": "Exact text"},
        )
        convo = history.conversation_for(self.lead)
        self.assertEqual(convo[0]["text"], "Exact text")

    def test_the_conversation_reads_oldest_first(self):
        LeadActivity.objects.create(
            lead=self.lead, activity_type=LeadActivity.ActivityType.SMS_SENT,
            description="Follow-up step 1 sent", metadata={"step": 1, "segment": "general"},
        )
        LeadActivity.objects.create(
            lead=self.lead, activity_type=LeadActivity.ActivityType.REPLY_RECEIVED,
            description="reply", metadata={"message_body": "Just 2"},
        )
        convo = history.conversation_for(self.lead)
        self.assertEqual([m["from"] for m in convo], ["us", "customer"])

    def test_the_limit_keeps_the_most_recent_exchanges(self):
        for i in range(12):
            LeadActivity.objects.create(
                lead=self.lead,
                activity_type=LeadActivity.ActivityType.REPLY_RECEIVED,
                description="reply", metadata={"message_body": f"msg {i}"},
            )
        convo = history.conversation_for(self.lead, limit=5)
        self.assertEqual(len(convo), 5)
        self.assertEqual(convo[-1]["text"], "msg 11")

    def test_activity_that_is_not_a_message_is_ignored(self):
        LeadActivity.objects.create(
            lead=self.lead, activity_type=LeadActivity.ActivityType.STATUS_CHANGE,
            description="status changed", metadata={},
        )
        self.assertEqual(history.conversation_for(self.lead), [])

    def test_an_unrenderable_step_is_skipped_not_crashed(self):
        LeadActivity.objects.create(
            lead=self.lead, activity_type=LeadActivity.ActivityType.SMS_SENT,
            description="Follow-up step 99 sent",
            metadata={"step": 99, "segment": "nonexistent"},
        )
        self.assertEqual(history.conversation_for(self.lead), [])
```

- [ ] **Step 2: Run it and watch it fail**

Run: `./manage.py test ai_assistant.tests_history`
Expected: FAIL — `ModuleNotFoundError: No module named 'ai_assistant.history'`

- [ ] **Step 3: Write the implementation**

Create `ai_assistant/history.py`:

```python
"""Both sides of a conversation, rebuilt from the activity log.

This matters more than anything else in the context packet. Real stored
messages include "Just 2", "Confirm" and "Or 5 suitcases." — each one
meaningless without the question it answers. An assistant handed only the
latest inbound text is guessing at what was asked.

TWO SIDES, TWO PROBLEMS.

  Inbound  text is in metadata["message_body"] on recent rows and
           metadata["message_preview"] on older ones (200 chars, which covers
           an SMS segment comfortably).

  Outbound rows store NO body — only description="Follow-up step 2 sent" and
           metadata={"step": 2, "segment": "airport_transfer"}. So we
           re-render the FollowUpSequence template for that step and segment.
           New sends now record their text (see ghl_integration.tasks), so
           reconstruction is a bridge for history, not a permanent mechanism.
"""
import logging

from django.conf import settings

from ghl_integration.models import FollowUpSequence, LeadActivity
from ghl_integration.templates_engine import render_follow_up_message

logger = logging.getLogger(__name__)

_MESSAGE_TYPES = (
    LeadActivity.ActivityType.REPLY_RECEIVED,
    LeadActivity.ActivityType.SMS_SENT,
)


def _inbound_text(meta):
    return (meta.get("message_body") or meta.get("message_preview") or "").strip()


def _outbound_text(meta, lead):
    """Our side: the stored body if we have one, else re-render the template."""
    stored = (meta.get("message_body") or "").strip()
    if stored:
        return stored

    step = meta.get("step")
    segment = meta.get("segment") or "general"
    if step is None:
        return ""

    template = (
        FollowUpSequence.objects.filter(step_number=step, segment=segment).first()
        or FollowUpSequence.objects.filter(step_number=step, segment="general").first()
    )
    if not template:
        # A step we can no longer reconstruct. Dropping it is better than
        # inserting a placeholder the model would try to interpret.
        return ""
    return render_follow_up_message(template.message_template, lead).strip()


def conversation_for(lead, limit=None):
    """The last `limit` messages for this lead, oldest first.

    Returns [{"from": "customer"|"us", "text": str, "at": iso8601}].
    """
    if limit is None:
        limit = getattr(settings, "AI_HISTORY_TURNS", 10)

    rows = (
        LeadActivity.objects
        .filter(lead=lead, activity_type__in=_MESSAGE_TYPES)
        # created_at is auto_now_add, so two rows written in the same
        # microsecond would otherwise order arbitrarily and the thread would
        # read backwards. id breaks the tie.
        .order_by("-created_at", "-id")[: limit * 2]  # over-fetch; blanks drop out
    )

    messages = []
    for row in rows:
        meta = row.metadata if isinstance(row.metadata, dict) else {}
        if row.activity_type == LeadActivity.ActivityType.REPLY_RECEIVED:
            who, text = "customer", _inbound_text(meta)
        else:
            who, text = "us", _outbound_text(meta, lead)
        if not text:
            continue
        messages.append({
            "from": who,
            "text": text,
            "at": row.created_at.isoformat(),
        })

    messages.reverse()          # oldest first — how a person reads a thread
    return messages[-limit:]
```

- [ ] **Step 4: Run the test and watch it pass**

Run: `./manage.py test ai_assistant.tests_history`
Expected: PASS, 8 tests

- [ ] **Step 5: Record the text of new outbound messages**

So future conversations need no reconstruction. In `ghl_integration/tasks.py`,
find where a `LeadActivity` with `activity_type=SMS_SENT` is created after a
successful send and add the rendered body to its `metadata` dict:

```python
metadata={
    "step": step_number,
    "segment": segment,
    # Store what actually went out. Rebuilding our own side of a conversation
    # from a template is a bridge for old rows, not something to keep doing.
    "message_body": message,
},
```

Search for it with: `grep -n "SMS_SENT" ghl_integration/tasks.py`

- [ ] **Step 6: Run the full suite for regressions**

Run: `./manage.py test ai_assistant ghl_integration`
Expected: PASS — the existing `ghl_integration` tests must be untouched by this

- [ ] **Step 7: Commit**

```bash
git add ai_assistant/history.py ai_assistant/tests_history.py ghl_integration/tasks.py
git commit -m "$(cat <<'EOF'
Rebuild both sides of the conversation, not just theirs

Real stored replies include "Just 2", "Confirm" and "Or 5 suitcases." — all
meaningless without the question they answer. The assistant needs the thread or
it is guessing at what was asked.

Our own sent messages never recorded their text, only which follow-up step went
out, so historical ones are re-rendered from the template. New sends now store
what they said, which makes the rebuild a bridge rather than a fixture.

Release-Note: none

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Known and missing trip details

**Files:**
- Create: `ai_assistant/slots.py`
- Create: `ai_assistant/tests_slots.py`

**Interfaces:**
- Consumes: `reservations.models.Lead`; `ai_assistant.models.AiConversationTurn`
- Produces:
  - `SLOTS: tuple[str, ...]` — `("passenger_count", "luggage_count", "car_seats", "booster_seats", "return_date", "flight_number")`
  - `known_for(lead) -> dict` — merged from prior turns' `extracted`, most recent wins, nulls ignored
  - `missing_for(lead, known) -> list[str]` — required slots not yet known, in ask-order

- [ ] **Step 1: Write the failing test**

Create `ai_assistant/tests_slots.py`:

```python
"""What we know about a trip, and what we still have to ask.

Run with:  ./manage.py test ai_assistant.tests_slots
"""
from django.test import TestCase

from ai_assistant import slots
from ai_assistant.models import AiConversationTurn
from reservations.models import Lead


class Known(TestCase):
    def setUp(self):
        self.lead = Lead.objects.create(first_name="Elizabeth", trip_type="oneway")

    def test_nothing_is_known_at_the_start(self):
        self.assertEqual(slots.known_for(self.lead), {})

    def test_a_detail_from_an_earlier_turn_is_remembered(self):
        AiConversationTurn.objects.create(
            lead=self.lead, inbound_text="3 passengers and 2 pieces",
            extracted={"passenger_count": 3, "luggage_count": 2},
        )
        self.assertEqual(
            slots.known_for(self.lead),
            {"passenger_count": 3, "luggage_count": 2},
        )

    def test_a_later_correction_wins(self):
        AiConversationTurn.objects.create(
            lead=self.lead, inbound_text="4 bags", extracted={"luggage_count": 4},
        )
        AiConversationTurn.objects.create(
            lead=self.lead, inbound_text="Or 5 suitcases..",
            extracted={"luggage_count": 5},
        )
        self.assertEqual(slots.known_for(self.lead)["luggage_count"], 5)

    def test_a_null_never_overwrites_something_we_know(self):
        AiConversationTurn.objects.create(
            lead=self.lead, inbound_text="3 people", extracted={"passenger_count": 3},
        )
        AiConversationTurn.objects.create(
            lead=self.lead, inbound_text="thanks!", extracted={"passenger_count": None},
        )
        self.assertEqual(slots.known_for(self.lead)["passenger_count"], 3)


class Missing(TestCase):
    def test_a_one_way_trip_does_not_need_a_return_date(self):
        lead = Lead.objects.create(first_name="Vincent", trip_type="oneway")
        self.assertNotIn("return_date", slots.missing_for(lead, {}))

    def test_a_round_trip_does_need_one(self):
        lead = Lead.objects.create(first_name="Elizabeth", trip_type="roundtrip")
        self.assertIn("return_date", slots.missing_for(lead, {}))

    def test_what_we_know_stops_being_missing(self):
        lead = Lead.objects.create(first_name="Vincent", trip_type="oneway")
        missing = slots.missing_for(lead, {"passenger_count": 3})
        self.assertNotIn("passenger_count", missing)
        self.assertIn("luggage_count", missing)

    def test_passenger_count_is_asked_for_first(self):
        # It decides the vehicle, which decides the price. Nothing else is
        # worth asking until it is known.
        lead = Lead.objects.create(first_name="Vincent", trip_type="oneway")
        self.assertEqual(slots.missing_for(lead, {})[0], "passenger_count")
```

- [ ] **Step 2: Run it and watch it fail**

Run: `./manage.py test ai_assistant.tests_slots`
Expected: FAIL — `ModuleNotFoundError: No module named 'ai_assistant.slots'`

- [ ] **Step 3: Write the implementation**

Create `ai_assistant/slots.py`:

```python
"""What we know about a trip and what we still have to ask for.

A Lead is thin. It holds a name, a pickup, a dropoff, ONE date, a trip type, a
vehicle and a price. It does not hold the party size, the luggage, whether
anyone needs a car seat, or — on a round trip — when they come back. Those
decide which vehicle is right, and the vehicle decides the price. Answering
from the Lead alone would confidently quote a towncar to a party of nine.

So the assistant collects. The packet states its own gaps and the prompt asks
for ONE of them at a time; a customer who gets a form back stops replying.
"""
from ai_assistant.models import AiConversationTurn

# Ask-order matters. passenger_count is first because it picks the vehicle,
# and the vehicle picks the price — everything else is detail by comparison.
SLOTS = (
    "passenger_count",
    "luggage_count",
    "car_seats",
    "booster_seats",
    "return_date",
    "flight_number",
)

# Only asked when the trip type calls for it.
_CONDITIONAL = {"return_date": lambda lead: lead.trip_type == "roundtrip"}


def known_for(lead):
    """Everything prior turns extracted, most recent value winning.

    A null never overwrites a known value — "thanks!" does not erase the party
    size someone gave three messages ago.
    """
    known = {}
    turns = (
        AiConversationTurn.objects
        .filter(lead=lead)
        .exclude(extracted={})
        .order_by("received_at")
    )
    for turn in turns:
        if not isinstance(turn.extracted, dict):
            continue
        for key, value in turn.extracted.items():
            if key in SLOTS and value is not None:
                known[key] = value
    return known


def missing_for(lead, known):
    """Slots we still need, in the order to ask for them."""
    missing = []
    for slot in SLOTS:
        if known.get(slot) is not None:
            continue
        gate = _CONDITIONAL.get(slot)
        if gate and not gate(lead):
            continue
        missing.append(slot)
    return missing
```

- [ ] **Step 4: Run the test and watch it pass**

Run: `./manage.py test ai_assistant.tests_slots`
Expected: PASS, 8 tests

- [ ] **Step 5: Commit**

```bash
git add ai_assistant/slots.py ai_assistant/tests_slots.py
git commit -m "$(cat <<'EOF'
Track what the lead does not tell us

A lead holds one date, no party size, no luggage and no car seats — and those
decide the vehicle, which decides the price. Answering from the lead alone
would quote a towncar to a party of nine and sound confident doing it.

The packet now states its own gaps in ask-order, party size first, so the
assistant asks for one thing at a time instead of sending back a form.

Release-Note: none

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: The context packet

**Files:**
- Create: `ai_assistant/context.py`
- Create: `ai_assistant/tests_context.py`

**Interfaces:**
- Consumes: `ai_assistant.pricing.card_prices`, `.vehicles.vehicle_facts`, `.vehicles.smallest_that_fits`, `.history.conversation_for`, `.slots.known_for`, `.slots.missing_for`, `.models.KnowledgeItem`; `reservations.refund_policy.CANCELLATION_POLICY_SENTENCE`
- Produces: `build_context_packet(lead, inbound_text) -> dict` with top-level keys in this fixed order: `lead`, `conversation`, `inbound`, `known`, `missing`, `pricing`, `vehicles`, `policies`, `knowledge`, `internal`

- [ ] **Step 1: Write the failing test**

Create `ai_assistant/tests_context.py`:

```python
"""The packet is the product. Everything the model may say comes from here.

Run with:  ./manage.py test ai_assistant.tests_context
"""
from decimal import Decimal

from django.test import TestCase

from ai_assistant import context
from ai_assistant.models import KnowledgeItem
from rates.models import Location, Rate, Route, Vehicle
from reservations.models import Lead
from reservations.refund_policy import CANCELLATION_POLICY_SENTENCE


class PacketFixture:
    """Shared setUp only. Not a TestCase — subclassing a TestCase to reuse its
    fixture silently re-runs every one of its tests in each child."""

    def setUp(self):
        self.mco = Location.objects.create(
            name="Orlando International Airport", aliases="MCO"
        )
        self.disney = Location.objects.create(
            name="All WDW Disney Property Resorts", aliases="Disney, WDW"
        )
        route = Route.objects.create(origin=self.mco, destination=self.disney)
        self.towncar = Vehicle.objects.create(
            vehicle_type="towncar", capacity=4, luggage_capacity=4,
            included_carseats=2, included_boosters=2,
        )
        self.van = Vehicle.objects.create(
            vehicle_type="van", capacity=10, luggage_capacity=10,
            included_carseats=4, included_boosters=4,
        )
        Rate.objects.create(
            vehicle=self.towncar, route=route,
            oneway_price=Decimal("140.00"), round_trip_price=Decimal("260.00"),
        )
        Rate.objects.create(
            vehicle=self.van, route=route,
            oneway_price=Decimal("225.00"), round_trip_price=Decimal("420.00"),
        )
        self.lead = Lead.objects.create(
            first_name="Vincent",
            pickup_location="Orlando International Airport",
            dropoff_location="All WDW Disney Property Resorts",
            trip_type="oneway", vehicle=self.towncar,
            estimated_price=Decimal("140.00"),
        )


class Packet(PacketFixture, TestCase):
    def test_the_packet_carries_the_lead_and_the_inbound_message(self):
        packet = context.build_context_packet(self.lead, "how much for a van?")
        self.assertEqual(packet["lead"]["first_name"], "Vincent")
        self.assertEqual(packet["inbound"], "how much for a van?")

    def test_a_card_route_gets_priced(self):
        packet = context.build_context_packet(self.lead, "how much?")
        self.assertEqual(
            packet["pricing"]["by_vehicle"]["towncar"]["price"], "140.00"
        )

    def test_an_unknown_route_gets_no_pricing_key_at_all(self):
        # This is the mechanism. No price in the packet, no price in the reply.
        self.lead.dropoff_location = "123 Nowhere Road, Kissimmee"
        self.lead.save()
        packet = context.build_context_packet(self.lead, "how much?")
        self.assertIsNone(packet["pricing"])

    def test_the_cancellation_policy_is_verbatim(self):
        packet = context.build_context_packet(self.lead, "can I cancel?")
        self.assertEqual(
            packet["policies"]["cancellation"], CANCELLATION_POLICY_SENTENCE
        )

    def test_active_knowledge_is_included_and_inactive_is_not(self):
        KnowledgeItem.objects.create(topic="car seats", answer="Free car seats.")
        KnowledgeItem.objects.create(
            topic="old answer", answer="Wrong.", is_active=False
        )
        packet = context.build_context_packet(self.lead, "car seats?")
        topics = [k["topic"] for k in packet["knowledge"]]
        self.assertIn("car seats", topics)
        self.assertNotIn("old answer", topics)

    def test_dispatcher_only_notes_land_in_internal_and_nowhere_else(self):
        packet = context.build_context_packet(self.lead, "how much?")
        customer_visible = {k: v for k, v in packet.items() if k != "internal"}
        self.assertNotIn("Quote this", str(customer_visible))

    def test_the_key_order_is_stable_so_caching_works(self):
        a = context.build_context_packet(self.lead, "one")
        b = context.build_context_packet(self.lead, "two")
        self.assertEqual(list(a.keys()), list(b.keys()))

    def test_every_fact_carries_an_id(self):
        packet = context.build_context_packet(self.lead, "how much?")
        ids = context.fact_ids(packet)
        self.assertIn("pricing.towncar", ids)
        self.assertIn("policy.cancellation", ids)
        self.assertIn("vehicle.towncar", ids)

    def test_fact_ids_are_stable_across_turns(self):
        a = context.fact_ids(context.build_context_packet(self.lead, "one"))
        b = context.fact_ids(context.build_context_packet(self.lead, "two"))
        self.assertEqual(sorted(a), sorted(b))


class RequoteWhenTheVehicleChanges(PacketFixture, TestCase):
    def test_a_party_of_nine_is_priced_as_a_van_not_a_towncar(self):
        from ai_assistant.models import AiConversationTurn
        AiConversationTurn.objects.create(
            lead=self.lead, inbound_text="9 people 4 suitcases",
            extracted={"passenger_count": 9, "luggage_count": 4},
        )
        packet = context.build_context_packet(self.lead, "does that still work?")
        pricing = packet["pricing"]
        self.assertEqual(pricing["quoted_vehicle"], "towncar")
        self.assertEqual(pricing["required_vehicle"], "van")
        self.assertEqual(pricing["required_price"], "225.00")
        self.assertIn("9", pricing["required_reason"])

    def test_a_party_that_fits_triggers_no_change(self):
        from ai_assistant.models import AiConversationTurn
        AiConversationTurn.objects.create(
            lead=self.lead, inbound_text="3 of us, 2 bags",
            extracted={"passenger_count": 3, "luggage_count": 2},
        )
        packet = context.build_context_packet(self.lead, "ok?")
        self.assertIsNone(packet["pricing"]["required_vehicle"])

    def test_a_party_nothing_fits_kills_the_pricing(self):
        from ai_assistant.models import AiConversationTurn
        AiConversationTurn.objects.create(
            lead=self.lead, inbound_text="40 of us",
            extracted={"passenger_count": 40},
        )
        packet = context.build_context_packet(self.lead, "ok?")
        self.assertIsNone(packet["pricing"])
```

- [ ] **Step 2: Run it and watch it fail**

Run: `./manage.py test ai_assistant.tests_context`
Expected: FAIL — `ModuleNotFoundError: No module named 'ai_assistant.context'`

- [ ] **Step 3: Write the implementation**

Create `ai_assistant/context.py`:

```python
"""Everything the assistant is allowed to know, assembled before it runs.

THE WHOLE DESIGN IS IN THIS FILE. The model is handed facts; it is not given
the ability to fetch them. There is no tool call, no database handle, no
retrieval step it controls. That is the difference between "we told it to use
the rate card" and "it has no other option" — and the first one is exactly what
GoHighLevel's assistant does wrong.

Two consequences to respect when editing:

  * A fact that is not in the packet CANNOT appear in a reply. If the assistant
    should be able to say something, put it here. If it should not, leave it
    out and let the turn escalate.
  * packet["internal"] is dispatcher-only and must never be rendered into the
    customer-facing half of the prompt. ai_assistant.guards checks for leaks.

KEY ORDER IS FIXED on purpose. Prompt caching is a prefix match, so a packet
that serialises its keys in a different order every turn silently costs full
price on every call.
"""
from django.conf import settings

from ai_assistant import history, pricing, slots, vehicles
from ai_assistant.models import KnowledgeItem
from reservations.refund_policy import CANCELLATION_POLICY_SENTENCE


def _lead_facts(lead):
    return {
        "first_name": lead.first_name or "",
        "pickup_location": lead.pickup_location or "",
        "dropoff_location": lead.dropoff_location or "",
        "pickup_date": lead.pickup_date.isoformat() if lead.pickup_date else None,
        "trip_type": lead.trip_type or "",
        "quoted_price": str(lead.estimated_price) if lead.estimated_price else None,
        "quoted_vehicle": lead.vehicle.vehicle_type if lead.vehicle else None,
        "status": lead.status or "",
    }


def _pricing_section(lead, known, card):
    """Card prices, re-quoted to the vehicle the party actually needs.

    `card` is the already-fetched result of pricing.card_prices — passed in so
    the packet build hits the quote engine once, not twice.

    Returns None whenever a price cannot be established exactly. None means the
    turn escalates; it never means "use the old quote and hope".
    """
    if not card:
        return None

    quoted_vehicle = lead.vehicle.vehicle_type if lead.vehicle else None
    passengers = known.get("passenger_count")
    luggage = known.get("luggage_count")

    required_vehicle = None
    required_reason = ""
    required_price = None

    fit = vehicles.smallest_that_fits(passengers, luggage)
    if fit is None and (passengers or luggage):
        # They told us the party size and nothing we own carries it. Pricing
        # anything here would be a fiction.
        return None

    if fit and quoted_vehicle and fit["vehicle_type"] != quoted_vehicle:
        current = next(
            (v for v in vehicles.vehicle_facts()
             if v["vehicle_type"] == quoted_vehicle),
            None,
        )
        too_big = current and (
            (passengers or 0) > current["capacity"]
            or (luggage or 0) > current["luggage_capacity"]
        )
        if too_big:
            required_vehicle = fit["vehicle_type"]
            required_reason = (
                f"{passengers or 0} passengers and {luggage or 0} bags exceed the "
                f"{current['label']} ({current['capacity']} seats, "
                f"{current['luggage_capacity']} bags)"
            )
            row = card["by_vehicle"].get(required_vehicle)
            if not row:
                # The card has no entry for the vehicle they need. A human
                # prices that, not us.
                return None
            required_price = row["price"]

    return {
        "route": card["route"],
        "trip_type": card["trip_type"],
        "by_vehicle": card["by_vehicle"],
        "quoted_vehicle": quoted_vehicle,
        "quoted_price": str(lead.estimated_price) if lead.estimated_price else None,
        "required_vehicle": required_vehicle,
        "required_reason": required_reason,
        "required_price": required_price,
    }


def build_context_packet(lead, inbound_text):
    """The complete, ordered set of facts for one turn."""
    known = slots.known_for(lead)
    missing = slots.missing_for(lead, known)
    card = pricing.card_prices(
        lead.pickup_location, lead.dropoff_location, lead.trip_type or "oneway"
    )
    priced = _pricing_section(lead, known, card)

    return {
        # Order is load-bearing — see the module docstring.
        "lead": _lead_facts(lead),
        "conversation": history.conversation_for(
            lead, limit=getattr(settings, "AI_HISTORY_TURNS", 10)
        ),
        "inbound": inbound_text,
        "known": known,
        "missing": missing,
        "pricing": priced,
        "vehicles": vehicles.vehicle_facts(),
        "policies": {"cancellation": CANCELLATION_POLICY_SENTENCE},
        "knowledge": [
            {"id": f"knowledge.{k.pk}", "topic": k.topic,
             "trigger_hint": k.trigger_hint, "answer": k.answer}
            for k in KnowledgeItem.objects.filter(is_active=True)
        ],
        # Dispatcher-only. Never rendered into the customer-facing prompt.
        "internal": (card or {}).get("internal", {}),
    }


def fact_ids(packet):
    """Stable string ids for every fact in the packet.

    The model reports which ones it used, and two things depend on that: the
    money guard resolves quoted amounts against the pricing facts claimed, and
    a wrong draft can be traced to the fact that misled it.

    Ids are derived from position and key — never a counter or a uuid — so the
    same fact has the same id on every turn.
    """
    ids = []
    for key, value in (packet.get("lead") or {}).items():
        if value not in (None, ""):
            ids.append(f"lead.{key}")
    for key, value in (packet.get("known") or {}).items():
        ids.append(f"known.{key}")
    for vehicle_type in ((packet.get("pricing") or {}).get("by_vehicle") or {}):
        ids.append(f"pricing.{vehicle_type}")
    for row in packet.get("vehicles") or []:
        ids.append(f"vehicle.{row['vehicle_type']}")
    for key in packet.get("policies") or {}:
        ids.append(f"policy.{key}")
    for item in packet.get("knowledge") or []:
        ids.append(item["id"])
    return ids
```

- [ ] **Step 4: Run the test and watch it pass**

Run: `./manage.py test ai_assistant.tests_context`
Expected: PASS, 12 tests

- [ ] **Step 5: Commit**

```bash
git add ai_assistant/context.py ai_assistant/tests_context.py
git commit -m "$(cat <<'EOF'
Decide what is true before the model runs

The assistant is handed facts and has no way to fetch any. No tool call, no
database handle, no retrieval it controls. A price that is not in the packet
cannot appear in a reply, which is a property of the architecture rather than
an instruction we hope it follows.

An unmatched route produces no pricing key at all, and a party of nine gets the
van's card price with the reason attached — still a lookup, just a different
row. When nothing on the card fits them, pricing disappears and a human picks
it up.

Release-Note: none

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: The Claude call

**Files:**
- Create: `ai_assistant/brain.py`
- Create: `ai_assistant/prompts/v1.txt`
- Create: `ai_assistant/tests_brain.py`

**Interfaces:**
- Consumes: `anthropic` SDK; settings `ANTHROPIC_API_KEY`, `AI_ASSISTANT_MODEL`, `AI_PROMPT_VERSION`
- Produces:
  - `@dataclass DraftResult`: `reply_text: str`, `needs_human: bool`, `reason: str`, `confidence: str`, `facts_used: list[str]`, `extracted: dict`, `model_id: str`, `prompt_version: str`, `input_tokens: int`, `cached_tokens: int`, `output_tokens: int`, `latency_ms: int`
  - `draft_reply(packet: dict) -> DraftResult`
  - `render_system_prompt(packet: dict) -> str` and `render_user_message(packet: dict) -> str` (split so the cacheable half is separable)

- [ ] **Step 1: Write the failing test**

Create `ai_assistant/tests_brain.py`:

```python
"""The model call. Takes a dict, returns a dataclass, touches no database.

Run with:  ./manage.py test ai_assistant.tests_brain

Nothing here reaches the network. brain.draft_reply is the single seam the rest
of the suite stubs, which is why it must never grow an ORM import.
"""
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from ai_assistant import brain

PACKET = {
    "lead": {"first_name": "Vincent", "pickup_location": "MCO",
             "dropoff_location": "Disney", "pickup_date": "2026-12-04",
             "trip_type": "oneway", "quoted_price": "140.00",
             "quoted_vehicle": "towncar", "status": "interested"},
    "conversation": [
        {"from": "us", "text": "Do you still need transportation?", "at": "2026-12-01T10:00:00Z"},
        {"from": "customer", "text": "Just 2", "at": "2026-12-01T10:05:00Z"},
    ],
    "inbound": "Just 2",
    "known": {},
    "missing": ["passenger_count", "luggage_count"],
    "pricing": {"route": "MCO to Disney", "trip_type": "oneway",
                "by_vehicle": {"towncar": {"label": "Towncar", "price": "140.00",
                                           "oneway": "140.00", "roundtrip": "260.00"}},
                "quoted_vehicle": "towncar", "quoted_price": "140.00",
                "required_vehicle": None, "required_reason": "", "required_price": None},
    "vehicles": [{"vehicle_type": "towncar", "label": "Towncar", "capacity": 4,
                  "luggage_capacity": 4, "carseats_display": "2 included",
                  "included_carseats": 2, "included_boosters": 2,
                  "extra_carseat_fee": "0.00", "extra_booster_fee": "0.00"}],
    "policies": {"cancellation": "Full refund 48 hours before pickup."},
    "knowledge": [{"id": "knowledge.1", "topic": "car seats",
                   "trigger_hint": "", "answer": "Car seats are free."}],
    "internal": {"towncar": {"notes": ["Quote this, not a custom price."],
                             "breakdown": {}}},
}


class PromptRendering(TestCase):
    def test_the_system_prompt_carries_the_stable_facts(self):
        system = brain.render_system_prompt(PACKET)
        self.assertIn("Car seats are free.", system)
        self.assertIn("Full refund 48 hours before pickup.", system)

    def test_the_system_prompt_never_carries_internal_notes(self):
        # "Quote this, not a custom price" is advice for a dispatcher and would
        # read as nonsense to a customer.
        self.assertNotIn("Quote this", brain.render_system_prompt(PACKET))

    def test_the_user_message_carries_the_volatile_half(self):
        # Caching is a prefix match, so the per-turn content goes last.
        user = brain.render_user_message(PACKET)
        self.assertIn("Just 2", user)
        self.assertIn("140.00", user)

    def test_a_packet_without_pricing_says_so_explicitly(self):
        packet = dict(PACKET, pricing=None)
        user = brain.render_user_message(packet)
        self.assertIn("No price is available", user)


class DraftReply(TestCase):
    def _fake_response(self, parsed):
        response = MagicMock()
        response.parsed_output = parsed
        response.usage.input_tokens = 2500
        response.usage.output_tokens = 120
        response.usage.cache_read_input_tokens = 2000
        return response

    @override_settings(ANTHROPIC_API_KEY="test-key")
    def test_a_normal_reply_comes_back_as_a_dataclass(self):
        parsed = {
            "reply_text": "Happy to help — how many people are travelling?",
            "needs_human": False, "reason": "", "confidence": "high",
            "facts_used": ["lead.first_name"],
            "extracted": {"passenger_count": 2},
        }
        with patch("ai_assistant.brain._client") as client:
            client.return_value.messages.parse.return_value = self._fake_response(parsed)
            result = brain.draft_reply(PACKET)

        self.assertEqual(result.reply_text, "Happy to help — how many people are travelling?")
        self.assertFalse(result.needs_human)
        self.assertEqual(result.confidence, "high")
        self.assertEqual(result.extracted["passenger_count"], 2)
        self.assertEqual(result.input_tokens, 2500)
        self.assertEqual(result.cached_tokens, 2000)
        self.assertGreaterEqual(result.latency_ms, 0)

    @override_settings(ANTHROPIC_API_KEY="test-key")
    def test_an_escalation_comes_back_marked(self):
        parsed = {
            "reply_text": "", "needs_human": True,
            "reason": "Customer claims they already paid.",
            "confidence": "low", "facts_used": [], "extracted": {},
        }
        with patch("ai_assistant.brain._client") as client:
            client.return_value.messages.parse.return_value = self._fake_response(parsed)
            result = brain.draft_reply(PACKET)

        self.assertTrue(result.needs_human)
        self.assertIn("already paid", result.reason)

    @override_settings(ANTHROPIC_API_KEY="")
    def test_a_missing_api_key_raises_rather_than_silently_doing_nothing(self):
        with self.assertRaises(brain.BrainNotConfigured):
            brain.draft_reply(PACKET)

    @override_settings(ANTHROPIC_API_KEY="test-key")
    def test_a_malformed_response_raises(self):
        with patch("ai_assistant.brain._client") as client:
            client.return_value.messages.parse.return_value = self._fake_response(
                {"reply_text": "hi"}  # missing required keys
            )
            with self.assertRaises(brain.BrainOutputInvalid):
                brain.draft_reply(PACKET)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `./manage.py test ai_assistant.tests_brain`
Expected: FAIL — `ModuleNotFoundError: No module named 'ai_assistant.brain'`

- [ ] **Step 3: Write the system prompt**

Create `ai_assistant/prompts/v1.txt`:

```
You write SMS replies for Grayson Towncar, a private car service in Orlando.

THE ONE RULE
You may only state facts that appear in the FACTS section below or in the TRIP
section of the message. You have no other knowledge of this company. If a
customer asks something those facts do not answer, set needs_human to true and
say why. Never estimate, never approximate, never reason toward a number.

Specifically, you must never:
  - State a price that is not written in the facts you were given.
  - Say a date, time, or vehicle is available. You cannot see the schedule.
  - Reword the cancellation policy. Quote it exactly or not at all.
  - Confirm that anyone has paid, been refunded, or been charged.
  - Answer a request describing more than one separate trip.

ESCALATE (needs_human = true) when:
  - No price is available and the customer is asking about cost.
  - The customer asks about availability, or a specific driver or vehicle.
  - The customer mentions payment, a refund, or a charge.
  - The customer describes multiple trips or a complex itinerary.
  - The customer is upset, or is disputing something.
  - Anything at all is unclear. Escalating costs a dispatcher thirty seconds.
    Being wrong costs a customer.

COLLECTING DETAILS
The MISSING list names what we still need to know. Ask for ONE of them, the
first one listed, and only when it is natural to do so. Never send a list of
questions — people stop replying to forms.
If the customer's message contains any of these details, put them in
"extracted". Leave a field null unless they actually said it. Do not infer.

IF THE VEHICLE HAS TO CHANGE
When the facts show a required_vehicle different from the quoted one, say
plainly that the party needs the larger vehicle, give the reason, and quote the
required_price. That price is from the same published rate card — it is not an
upcharge you invented.

HOW TO WRITE
  - Warm, brief, human. You are a person at a car service, not a chatbot.
  - Under 300 characters wherever possible. This is a text message.
  - No emoji unless the customer used one first.
  - Use their first name once, naturally, not in every message.
  - If they are declining politely, accept it warmly and do not push.
  - Never say "as an AI" or refer to yourself as a system.

Return your answer in the required JSON structure. Put the message itself in
reply_text. If needs_human is true, reply_text may be empty.
```

- [ ] **Step 4: Write the implementation**

Create `ai_assistant/brain.py`:

```python
"""The single model call. A dict goes in; a dataclass comes out.

THIS MODULE MUST NEVER IMPORT A DJANGO MODEL. That boundary is what lets the
whole test suite run without a network call and without a database: everything
downstream stubs draft_reply, and everything upstream builds a plain dict. If
an ORM import appears here, the suite silently starts needing fixtures it
should not need.

Model notes (Haiku 4.5 specifically):
  * No output_config.effort — Haiku 4.5 rejects it.
  * No thinking block. This is short, grounded writing, not reasoning.
  * max_tokens 1024. A long reply to a text message is itself a defect.
"""
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "reply_text": {"type": "string"},
        "needs_human": {"type": "boolean"},
        "reason": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "facts_used": {"type": "array", "items": {"type": "string"}},
        "extracted": {
            "type": "object",
            "properties": {
                "passenger_count": {"type": ["integer", "null"]},
                "luggage_count": {"type": ["integer", "null"]},
                "car_seats": {"type": ["integer", "null"]},
                "booster_seats": {"type": ["integer", "null"]},
                "return_date": {"type": ["string", "null"]},
                "flight_number": {"type": ["string", "null"]},
            },
            "additionalProperties": False,
        },
    },
    "required": [
        "reply_text", "needs_human", "reason", "confidence", "facts_used",
        "extracted",
    ],
    "additionalProperties": False,
}

_REQUIRED_KEYS = set(OUTPUT_SCHEMA["required"])


class BrainNotConfigured(RuntimeError):
    """No API key. Fail loudly — a silent no-op looks like a working assistant."""


class BrainOutputInvalid(ValueError):
    """The model returned something we cannot trust. Escalate, never guess."""


@dataclass
class DraftResult:
    reply_text: str
    needs_human: bool
    reason: str
    confidence: str
    facts_used: list = field(default_factory=list)
    extracted: dict = field(default_factory=dict)
    model_id: str = ""
    prompt_version: str = ""
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0


def _client():
    """Built per call so a key rotation does not need a restart."""
    import anthropic

    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def _load_prompt(version):
    return (PROMPT_DIR / f"{version}.txt").read_text(encoding="utf-8")


def render_system_prompt(packet):
    """The half that is identical on every turn — so it caches.

    Never include packet["internal"]: it holds dispatcher-only notes such as
    "Quote this, not a custom price", which would read as nonsense to a
    customer and worse to a competitor.
    """
    version = getattr(settings, "AI_PROMPT_VERSION", "v1")
    sections = [_load_prompt(version), "", "FACTS", ""]

    sections.append("Cancellation policy (quote exactly, never reword):")
    sections.append(packet["policies"]["cancellation"])
    sections.append("")

    sections.append("Our vehicles:")
    for v in packet.get("vehicles") or []:
        sections.append(
            f"  - {v['label']} ({v['vehicle_type']}): seats {v['capacity']}, "
            f"luggage {v['luggage_capacity']}, "
            f"{v['included_carseats']} car seats and {v['included_boosters']} "
            f"boosters included"
            + (f". {v['carseats_display']}" if v["carseats_display"] else "")
        )
    sections.append("")

    if packet.get("knowledge"):
        sections.append("What we tell customers:")
        for k in packet["knowledge"]:
            hint = f" (when: {k['trigger_hint']})" if k.get("trigger_hint") else ""
            sections.append(f"  - [{k['id']}] {k['topic']}{hint}: {k['answer']}")

    return "\n".join(sections)


def render_user_message(packet):
    """The per-turn half. Goes after the cache breakpoint."""
    lead = packet["lead"]
    out = ["TRIP", ""]
    out.append(f"Customer: {lead['first_name']}")
    out.append(f"From: {lead['pickup_location']}")
    out.append(f"To: {lead['dropoff_location']}")
    out.append(f"Date: {lead['pickup_date'] or 'not given'}")
    out.append(f"Trip type: {lead['trip_type'] or 'not given'}")
    out.append(f"Originally quoted: {lead['quoted_price'] or 'nothing yet'} "
               f"({lead['quoted_vehicle'] or 'no vehicle chosen'})")
    out.append("")

    if packet.get("known"):
        out.append("What they have already told us:")
        for key, value in packet["known"].items():
            out.append(f"  - {key}: {value}")
        out.append("")

    if packet.get("missing"):
        out.append(f"Still missing (ask for the FIRST one only): "
                   f"{', '.join(packet['missing'])}")
        out.append("")

    p = packet.get("pricing")
    if not p:
        out.append("PRICES")
        out.append("No price is available for this trip. If they ask about cost, "
                   "set needs_human to true. Do not estimate.")
    else:
        out.append(f"PRICES — published rate card, {p['route']}, {p['trip_type']}")
        for vehicle_type, row in p["by_vehicle"].items():
            out.append(f"  - [pricing.{vehicle_type}] {row['label']}: ${row['price']}")
        if p.get("required_vehicle"):
            out.append("")
            out.append(
                f"VEHICLE MUST CHANGE: they need the {p['required_vehicle']} "
                f"at ${p['required_price']} because {p['required_reason']}. "
                f"Say so plainly and give that price."
            )
    out.append("")

    if packet.get("conversation"):
        out.append("CONVERSATION SO FAR (oldest first)")
        for m in packet["conversation"]:
            who = "Them" if m["from"] == "customer" else "Us"
            out.append(f"  {who}: {m['text']}")
        out.append("")

    out.append("THEIR LATEST MESSAGE")
    out.append(packet["inbound"])
    return "\n".join(out)


def draft_reply(packet):
    """One grounded reply for one inbound message."""
    if not getattr(settings, "ANTHROPIC_API_KEY", ""):
        raise BrainNotConfigured(
            "ANTHROPIC_API_KEY is not set — refusing to pretend to run."
        )

    model = getattr(settings, "AI_ASSISTANT_MODEL", "claude-haiku-4-5")
    version = getattr(settings, "AI_PROMPT_VERSION", "v1")

    started = time.monotonic()
    response = _client().messages.parse(
        model=model,
        max_tokens=1024,
        system=[{
            "type": "text",
            "text": render_system_prompt(packet),
            # Stable across turns — this is the bulk of the tokens and where
            # the caching saving lives. Verify with cache_read_input_tokens.
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": render_user_message(packet)}],
        output_config={"format": {
            "type": "json_schema",
            "schema": OUTPUT_SCHEMA,
        }},
    )
    latency_ms = int((time.monotonic() - started) * 1000)

    parsed = getattr(response, "parsed_output", None)
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError as exc:
            raise BrainOutputInvalid(f"Response was not JSON: {exc}") from exc
    if not isinstance(parsed, dict) or not _REQUIRED_KEYS.issubset(parsed):
        missing = _REQUIRED_KEYS - set(parsed or {})
        raise BrainOutputInvalid(f"Response missing keys: {sorted(missing)}")

    usage = response.usage
    return DraftResult(
        reply_text=parsed["reply_text"],
        needs_human=bool(parsed["needs_human"]),
        reason=parsed.get("reason", ""),
        confidence=parsed.get("confidence", "medium"),
        facts_used=list(parsed.get("facts_used") or []),
        extracted={k: v for k, v in (parsed.get("extracted") or {}).items()
                   if v is not None},
        model_id=model,
        prompt_version=version,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        cached_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        latency_ms=latency_ms,
    )
```

- [ ] **Step 5: Run the test and watch it pass**

Run: `./manage.py test ai_assistant.tests_brain`
Expected: PASS, 8 tests

> If `messages.parse` is unavailable in the installed SDK version, fall back to
> `messages.create(...)` with the same `output_config` and `json.loads` the
> first text block. Keep the `BrainOutputInvalid` behaviour identical.

- [ ] **Step 6: Commit**

```bash
git add ai_assistant/brain.py ai_assistant/prompts/ ai_assistant/tests_brain.py
git commit -m "$(cat <<'EOF'
One model call, no database handle

brain takes a plain dict and returns a dataclass, and it must never import a
model. That boundary is what keeps the whole suite off the network — everything
downstream stubs this one function.

The stable facts go in the system half where they cache; the trip and the
latest message go after, where they change every turn. A packet with no price
says so in words, so the model is told to escalate rather than left to notice
an absence.

Release-Note: none

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Grounding guards

**Files:**
- Create: `ai_assistant/guards.py`
- Create: `ai_assistant/tests_guards.py`

**Interfaces:**
- Consumes: `ai_assistant.brain.DraftResult` (duck-typed — takes any object with `reply_text` / `facts_used`), the packet dict
- Produces:
  - `@dataclass GuardVerdict`: `flagged: bool`, `escalate: bool`, `detail: str`
  - `check(result, packet) -> GuardVerdict`

- [ ] **Step 1: Write the failing test**

Create `ai_assistant/tests_guards.py`:

```python
"""The checks that run after the model and before a human sees anything.

Run with:  ./manage.py test ai_assistant.tests_guards

A prompt instruction is a request. These are conditions.
"""
from types import SimpleNamespace

from django.test import TestCase

from ai_assistant import guards

PACKET_WITH_PRICE = {
    "inbound": "how much?",
    "pricing": {"by_vehicle": {"towncar": {"label": "Towncar", "price": "140.00",
                                           "oneway": "140.00", "roundtrip": "260.00"}},
                "quoted_price": "140.00", "required_price": None},
    "policies": {"cancellation": "Full refund 48 hours before pickup."},
    "internal": {"towncar": {"notes": ["Quote this, not a custom price."],
                             "breakdown": {}}},
}

PACKET_NO_PRICE = dict(PACKET_WITH_PRICE, pricing=None)


def draft(text, facts=None):
    return SimpleNamespace(reply_text=text, facts_used=facts or [])


class Money(TestCase):
    def test_a_price_from_the_packet_passes(self):
        v = guards.check(draft("That trip is $140."), PACKET_WITH_PRICE)
        self.assertFalse(v.flagged)

    def test_a_price_that_is_nowhere_in_the_packet_is_flagged(self):
        v = guards.check(draft("I can do $95 for you."), PACKET_WITH_PRICE)
        self.assertTrue(v.flagged)
        self.assertIn("95", v.detail)

    def test_any_price_at_all_is_flagged_when_the_packet_has_none(self):
        v = guards.check(draft("It's about $150."), PACKET_NO_PRICE)
        self.assertTrue(v.flagged)

    def test_decimals_and_commas_are_understood(self):
        v = guards.check(draft("The total is $140.00."), PACKET_WITH_PRICE)
        self.assertFalse(v.flagged)


class Availability(TestCase):
    def test_claiming_a_slot_is_free_is_flagged(self):
        v = guards.check(draft("Yes, we have a car available that morning."),
                         PACKET_WITH_PRICE)
        self.assertTrue(v.flagged)

    def test_ordinary_wording_is_not_flagged(self):
        v = guards.check(draft("Happy to help with that trip."), PACKET_WITH_PRICE)
        self.assertFalse(v.flagged)


class Policy(TestCase):
    def test_paraphrasing_the_refund_terms_is_flagged(self):
        v = guards.check(
            draft("You can cancel any time for a full refund."), PACKET_WITH_PRICE
        )
        self.assertTrue(v.flagged)

    def test_quoting_it_exactly_is_fine(self):
        v = guards.check(
            draft("Our policy: Full refund 48 hours before pickup."),
            PACKET_WITH_PRICE,
        )
        self.assertFalse(v.flagged)


class InternalLeak(TestCase):
    def test_dispatcher_only_text_in_a_reply_is_flagged(self):
        v = guards.check(
            draft("Quote this, not a custom price."), PACKET_WITH_PRICE
        )
        self.assertTrue(v.flagged)


class AlwaysEscalate(TestCase):
    def test_a_payment_claim_escalates(self):
        packet = dict(PACKET_WITH_PRICE, inbound="I paid already")
        v = guards.check(draft("Thanks, all set!"), packet)
        self.assertTrue(v.escalate)

    def test_a_multi_leg_request_escalates(self):
        packet = dict(
            PACKET_WITH_PRICE,
            inbound="we need three different reservations, airport to hotel then hotel to the port",
        )
        v = guards.check(draft("Sure!"), packet)
        self.assertTrue(v.escalate)

    def test_an_ordinary_message_does_not_escalate(self):
        v = guards.check(draft("Happy to help."), PACKET_WITH_PRICE)
        self.assertFalse(v.escalate)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `./manage.py test ai_assistant.tests_guards`
Expected: FAIL — `ModuleNotFoundError: No module named 'ai_assistant.guards'`

- [ ] **Step 3: Write the implementation**

Create `ai_assistant/guards.py`:

```python
"""Checks that run after the model and before a human sees the draft.

A prompt instruction is a request. These are conditions.

A flagged draft still reaches the approval queue — visibly marked, never
auto-sendable. Hiding it would hide the failure mode you most need to see.
"""
import re
from dataclasses import dataclass

# $140, $140.00, $1,400 — and bare "140 dollars".
_MONEY = re.compile(r"\$\s?([0-9][0-9,]*(?:\.[0-9]{1,2})?)|([0-9][0-9,]*)\s*dollars",
                    re.IGNORECASE)

# Guard 2 is the one rule that cannot be checked exactly — availability is
# semantic, not textual. This list is a net with holes, backed up by the prompt
# telling the model to escalate availability questions. Expect to tune it from
# real transcripts; that is the intent, not a defect.
_AVAILABILITY = (
    "we have", "is available", "are available", "we can fit you in",
    "that time works", "still open", "we've got a car", "we have a car",
    "slot is free", "we are free",
)

_PAYMENT = ("i paid", "i've paid", "ive paid", "already paid", "payment went",
            "charged me", "my refund", "got charged")

_MULTI_LEG = ("three different reservation", "3 different reservation",
              "two different reservation", "multiple reservation",
              "separate reservations", "then hotel to", "and then from")

_REFUND_WORDS = ("refund", "cancellation policy", "cancel for free",
                 "money back")


@dataclass
class GuardVerdict:
    flagged: bool
    escalate: bool
    detail: str


def _amounts(text):
    found = set()
    for whole, bare in _MONEY.findall(text or ""):
        raw = (whole or bare or "").replace(",", "")
        if not raw:
            continue
        try:
            found.add(round(float(raw), 2))
        except ValueError:
            continue
    return found


def _packet_amounts(packet):
    allowed = set()
    p = packet.get("pricing") or {}
    for row in (p.get("by_vehicle") or {}).values():
        for key in ("price", "oneway", "roundtrip"):
            if row.get(key):
                allowed.add(round(float(row[key]), 2))
    for key in ("quoted_price", "required_price"):
        if p.get(key):
            allowed.add(round(float(p[key]), 2))
    return allowed


def check(result, packet):
    """Run every guard. Returns what to flag and whether to force a human."""
    reply = (result.reply_text or "")
    lowered = reply.lower()
    inbound = (packet.get("inbound") or "").lower()
    problems = []
    escalate = False

    # 1. Money must trace to a fact we handed over.
    said = _amounts(reply)
    if said:
        allowed = _packet_amounts(packet)
        invented = sorted(said - allowed)
        if invented:
            problems.append(
                "Reply names " + ", ".join(f"${a:,.2f}" for a in invented)
                + " which is not in the packet"
            )

    # 2. Availability — best effort, see the module notes.
    hit = next((p for p in _AVAILABILITY if p in lowered), None)
    if hit:
        problems.append(f"Reply may claim availability ({hit!r})")

    # 3. Policy is quoted, not paraphrased.
    policy = (packet.get("policies") or {}).get("cancellation", "")
    if any(w in lowered for w in _REFUND_WORDS) and policy and policy not in reply:
        problems.append("Reply discusses refunds without quoting the policy verbatim")

    # 4. Nothing dispatcher-only may appear.
    for block in (packet.get("internal") or {}).values():
        for note in block.get("notes", []):
            if note and note.lower()[:30] in lowered:
                problems.append("Reply contains dispatcher-only text")
                break

    # 5 & 6. Some inbound messages are never ours to answer.
    if any(p in inbound for p in _PAYMENT):
        escalate = True
        problems.append("Customer mentioned payment — always a human")
    if any(p in inbound for p in _MULTI_LEG):
        escalate = True
        problems.append("Customer described multiple trips — always a human")

    return GuardVerdict(
        flagged=bool(problems),
        escalate=escalate,
        detail="; ".join(problems),
    )
```

- [ ] **Step 4: Run the test and watch it pass**

Run: `./manage.py test ai_assistant.tests_guards`
Expected: PASS, 12 tests

- [ ] **Step 5: Commit**

```bash
git add ai_assistant/guards.py ai_assistant/tests_guards.py
git commit -m "$(cat <<'EOF'
Check the reply against the facts it was given

Every dollar figure in a draft has to trace to something in the packet.
Architecturally the assistant should not be able to invent a price; this catches
the case where it does anyway, and it is the check that would have caught what
GoHighLevel is doing now.

Payment claims and multi-trip requests go to a human regardless of what the
draft says. The availability check is a phrase list and will miss things — it is
written down as best-effort rather than dressed up as certainty.

Release-Note: none

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: The replay harness

**Files:**
- Create: `ai_assistant/management/commands/ai_replay.py`
- Create: `ai_assistant/tests_replay.py`

**Interfaces:**
- Consumes: everything above
- Produces: `./manage.py ai_replay [--limit N] [--category NAME] [--out PATH] [--live] [--lead-id ID]`

**Behaviour contract:**
- `--limit` defaults to **25**. Above 500 the command refuses without `--i-know-it-costs`.
- Dry run is the **default**: no API call, packets only. `--live` opts in to calling the model.
- The command never imports or calls any send path.

- [ ] **Step 1: Write the failing test**

Create `ai_assistant/tests_replay.py`:

```python
"""The offline backtest. Cheap by default, and incapable of sending.

Run with:  ./manage.py test ai_assistant.tests_replay
"""
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from ai_assistant.brain import DraftResult
from ghl_integration.models import LeadActivity
from reservations.models import Lead


class Replay(TestCase):
    def setUp(self):
        self.lead = Lead.objects.create(
            first_name="Vincent", pickup_location="MCO",
            dropoff_location="Disney", trip_type="oneway",
        )
        for text in ["Just 2", "how much for a van?", "we are all set thanks"]:
            LeadActivity.objects.create(
                lead=self.lead,
                activity_type=LeadActivity.ActivityType.REPLY_RECEIVED,
                description="reply", metadata={"message_body": text},
            )

    def test_the_default_run_makes_no_api_call(self):
        out = StringIO()
        with patch("ai_assistant.brain.draft_reply") as brain:
            call_command("ai_replay", limit=3, stdout=out)
            brain.assert_not_called()
        self.assertIn("Just 2", out.getvalue())

    def test_a_live_run_drafts_a_reply_for_each_message(self):
        out = StringIO()
        fake = DraftResult(
            reply_text="How many travelling?", needs_human=False, reason="",
            confidence="high", facts_used=["lead.first_name"], extracted={},
            model_id="claude-haiku-4-5", prompt_version="v1",
        )
        with patch("ai_assistant.brain.draft_reply", return_value=fake) as brain:
            call_command("ai_replay", limit=3, live=True, stdout=out)
            self.assertEqual(brain.call_count, 3)
        self.assertIn("How many travelling?", out.getvalue())

    def test_a_big_run_is_refused_unless_you_say_you_mean_it(self):
        # 45,046 stored replies at ~$0.004 each is about $170. The default has
        # to be cheap and the expensive path has to be deliberate.
        with self.assertRaises(CommandError) as ctx:
            call_command("ai_replay", limit=5000, live=True)
        self.assertIn("cost", str(ctx.exception).lower())

    def test_the_default_limit_is_small(self):
        out = StringIO()
        for i in range(60):
            LeadActivity.objects.create(
                lead=self.lead,
                activity_type=LeadActivity.ActivityType.REPLY_RECEIVED,
                description="reply", metadata={"message_body": f"msg {i}"},
            )
        call_command("ai_replay", stdout=out)
        self.assertLessEqual(out.getvalue().count("INBOUND"), 25)

    def test_a_category_filter_narrows_the_sample(self):
        out = StringIO()
        call_command("ai_replay", category="carseats", limit=10, stdout=out)
        self.assertNotIn("how much for a van?", out.getvalue())

    def test_the_command_cannot_send(self):
        import ai_assistant.management.commands.ai_replay as mod
        source = open(mod.__file__, encoding="utf-8").read()
        self.assertNotIn("send_sms", source)
        self.assertNotIn("send_now", source)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `./manage.py test ai_assistant.tests_replay`
Expected: FAIL — `Unknown command: 'ai_replay'`

- [ ] **Step 3: Write the command**

Create `ai_assistant/management/commands/ai_replay.py`:

```python
"""Replay real historical customer messages through the assistant, offline.

This is how the design gets validated before a customer is involved. The local
snapshot holds ~45,000 stored replies, each attached to the Lead it concerns —
so a replay is not a simulation, it is the real input.

TWO SAFETY PROPERTIES, both deliberate:

  Cheap by default.  --limit is 25 and the default run makes NO api call at all
                     (packets only). At roughly $0.004 a message, replaying all
                     45,046 would cost about $170 — so a big run has to be
                     asked for explicitly.

  Cannot send.       Nothing in this file imports a send path. Do not add one.
                     Grep for send_sms in the tests; that check is deliberate.

Usage:
    ./manage.py ai_replay                          # 25 packets, free
    ./manage.py ai_replay --live --limit 200       # 200 real drafts, ~$0.80
    ./manage.py ai_replay --live --category carseats --out scratch/seats.md
"""
import json

from django.core.management.base import BaseCommand, CommandError

from ai_assistant import context
from ghl_integration.models import LeadActivity

MAX_FREE_LIMIT = 500
COST_PER_MESSAGE = 0.004

# The buckets measured in the spec. Crude substring matching is fine — this is
# a sampling aid for a human reading output, not a classifier.
CATEGORIES = {
    "carseats": ("car seat", "carseat", "booster", "luggage", "suitcase", "stroller"),
    "flight": ("flight", "terminal", "baggage", "arriv", "landing"),
    "price": ("price", "expensive", "cheaper", "quote", "too high", "budget", "rate"),
    "softno": ("still deciding", "getting quotes", "all set", "shopping around",
               "pondering", "think about it", "no thank"),
    "booking": ("book it", "we'd like to book", "we would like to book", "confirm",
                "lets book", "let's book"),
    "payment": ("i paid", "already paid", "refund", "charged"),
}


class Command(BaseCommand):
    help = "Replay historical inbound messages through the assistant, offline."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=25)
        parser.add_argument("--category", choices=sorted(CATEGORIES), default=None)
        parser.add_argument("--lead-id", type=int, default=None)
        parser.add_argument("--out", default=None, help="Write to a file as well.")
        parser.add_argument(
            "--live", action="store_true",
            help="Actually call the model. Costs money. Off by default.",
        )
        parser.add_argument(
            "--i-know-it-costs", action="store_true",
            help=f"Required for a live run over {MAX_FREE_LIMIT} messages.",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        live = options["live"]

        if live and limit > MAX_FREE_LIMIT and not options["i_know_it_costs"]:
            raise CommandError(
                f"A live run of {limit:,} messages would cost roughly "
                f"${limit * COST_PER_MESSAGE:,.2f}. Re-run with "
                f"--i-know-it-costs if you mean it, or lower --limit."
            )

        rows = self._sample(options["category"], options["lead_id"], limit)
        if not rows:
            self.stdout.write("Nothing matched. Try a different --category.")
            return

        lines = []
        for activity in rows:
            lines.extend(self._one(activity, live))

        text = "\n".join(lines)
        self.stdout.write(text)
        if options["out"]:
            with open(options["out"], "w", encoding="utf-8") as fh:
                fh.write(text)
            self.stdout.write(self.style.SUCCESS(f"\nWritten to {options['out']}"))

        if live:
            self.stdout.write(
                f"\n~${len(rows) * COST_PER_MESSAGE:,.2f} spent on {len(rows)} messages."
            )

    def _sample(self, category, lead_id, limit):
        qs = (
            LeadActivity.objects
            .filter(activity_type=LeadActivity.ActivityType.REPLY_RECEIVED)
            .select_related("lead")
            .order_by("-id")
        )
        if lead_id:
            qs = qs.filter(lead_id=lead_id)

        needles = CATEGORIES.get(category) if category else None
        picked = []
        # Walk a bounded window rather than loading the whole table; the filter
        # is a substring test we cannot push into SQL portably.
        for activity in qs[: max(limit * 40, 2000)]:
            text = self._text(activity)
            if not text:
                continue
            if needles and not any(n in text.lower() for n in needles):
                continue
            picked.append(activity)
            if len(picked) >= limit:
                break
        return picked

    @staticmethod
    def _text(activity):
        meta = activity.metadata if isinstance(activity.metadata, dict) else {}
        return (meta.get("message_body") or meta.get("message_preview") or "").strip()

    def _one(self, activity, live):
        from ai_assistant import brain, guards

        text = self._text(activity)
        lead = activity.lead
        out = [
            "",
            "=" * 78,
            f"INBOUND  (lead #{lead.id}, {lead.first_name})",
            f"  {text}",
            f"TRIP     {lead.pickup_location or '?'} -> {lead.dropoff_location or '?'}"
            f"  |  {lead.pickup_date or 'no date'}  |  {lead.trip_type or '?'}"
            f"  |  quoted {lead.estimated_price or '-'}",
        ]

        try:
            packet = context.build_context_packet(lead, text)
        except Exception as exc:                      # noqa: BLE001 — report, continue
            out.append(f"PACKET   FAILED: {exc}")
            return out

        p = packet.get("pricing")
        out.append(
            "PACKET   price: "
            + (", ".join(f"{k} ${v['price']}" for k, v in p["by_vehicle"].items())
               if p else "NONE (will escalate)")
        )
        out.append(f"         missing: {', '.join(packet['missing']) or 'nothing'}")
        out.append(f"         history: {len(packet['conversation'])} messages")

        if not live:
            return out

        try:
            result = brain.draft_reply(packet)
        except Exception as exc:                      # noqa: BLE001
            out.append(f"DRAFT    FAILED: {exc}")
            return out

        verdict = guards.check(result, packet)
        out.append("")
        out.append(f"DRAFT    {result.reply_text or '(escalated, no reply written)'}")
        out.append(
            f"         confidence={result.confidence} "
            f"needs_human={result.needs_human} "
            f"tokens={result.input_tokens}/{result.output_tokens} "
            f"cached={result.cached_tokens}"
        )
        if result.reason:
            out.append(f"         reason: {result.reason}")
        if result.extracted:
            out.append(f"         extracted: {json.dumps(result.extracted)}")
        if verdict.flagged or verdict.escalate:
            out.append(f"  !!     GUARD: {verdict.detail}")
        return out
```

- [ ] **Step 4: Run the test and watch it pass**

Run: `./manage.py test ai_assistant.tests_replay`
Expected: PASS, 6 tests

- [ ] **Step 5: Run the whole suite**

Run: `./manage.py test ai_assistant ghl_integration reservations dispatching`
Expected: PASS — nothing existing is broken

- [ ] **Step 6: Try it for real against the snapshot, free**

Run: `./manage.py ai_replay --limit 10`
Expected: ten real messages with their packets, no API call, no cost. Read the
output and confirm the packets contain sensible prices and history.

- [ ] **Step 7: Commit**

```bash
git add ai_assistant/management/ ai_assistant/tests_replay.py
git commit -m "$(cat <<'EOF'
Replay real conversations before any customer sees one

The snapshot holds about 45,000 stored replies, each attached to the trip it
concerns, so a replay is the real input rather than a simulation. Read a few
hundred drafts, fix what is wrong, replay again — all of it local.

Cheap and inert by default: 25 messages, no api call, packets only. A live run
over 500 is refused unless you say you mean it, because replaying everything
would cost about $170. Nothing in the command can send, and a test greps the
source to keep it that way.

Release-Note: none

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Done when

- `./manage.py test ai_assistant` passes, making zero network calls.
- `./manage.py ai_replay --limit 10` prints real packets for free.
- `./manage.py ai_replay --live --limit 50 --out scratch/first-look.md` produces
  50 real drafts against real conversations for about twenty cents.
- Nothing is wired to the webhook. Nothing can send. Both flags still default off.

## What plan 2 covers

`tasks.process_turn`, the webhook branch, `sending.send_now` with the
`AI_ASSISTANT_LIVE_SEND` gate, `policy.can_auto_send`, the human-takeover lock
and supersession, enforcement of `AI_DAILY_SPEND_CAP_USD` (the setting lands in
Task 1 here but nothing reads it until there is a live pipeline), the approval
queue UI and knowledge editor, applying extracted details to the Lead,
`AiFeedback` capture, the metrics panel, `ai_retry_stalled`, and the release
note.
