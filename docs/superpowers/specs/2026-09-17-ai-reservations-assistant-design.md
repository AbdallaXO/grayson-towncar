# AI Reservations Assistant — Design

**Status:** approved design, not yet implemented
**Date:** 2026-09-17
**Audience:** whoever implements this. Assumes Django fluency, no prior knowledge of this codebase.

---

## 1. The problem

Grayson Towncar uses GoHighLevel's built-in Conversation AI to answer inbound
SMS. It gives customers incorrect information — wrong prices, invented policies,
availability it cannot know — and there is no practical way to constrain it.

We keep GHL as the customer inbox and CRM. We move the thinking into Django,
where the facts already live.

The governing requirement is narrower than "be a good assistant":

> The assistant must never state a price, policy, availability, or company fact
> that did not come out of this system.

Everything here serves that sentence. Where accuracy and capability conflict,
accuracy wins and the conversation goes to a human.

## 2. What the traffic actually looks like

Before designing, we measured. The local snapshot (`content/db.sqlite3`) holds
**45,046 inbound customer replies** — `ghl_integration_leadactivity` rows with
`activity_type='reply_received'`, message text in `metadata.message_body` or
`metadata.message_preview`, each joined to the `Lead` it concerns. Median
message length: 50 characters.

| Category | Share |
| --- | --- |
| Soft no — "still deciding", "getting quotes", "we're all good" | large part of the 62% uncategorised |
| Car seats, boosters, luggage | 11.3% |
| Flight / terminal / baggage-claim logistics | 7.9% |
| Price question or objection | 6.9% |
| Supplying trip details — "9 people 4 suitcases no seats" | inside uncategorised |
| Ready to book — "we'd like to book this", "Confirm" | inside uncategorised |
| Bare yes / ok | 3.3% |
| Bare no | 2.6% |
| Opt-out | 2.5% |
| Already booked elsewhere | 1.6% |
| Change / cancel | 1.2% |
| Asks for a call | 0.6% |

**Three conclusions that shaped this design.**

Pricing is not the highest-volume need. It is the highest-*risk* need, so it
gets the strictest treatment, but the assistant earns its keep on car seats,
luggage, airport logistics, and gracefully closing out customers who have
decided against booking.

The most common answerable questions have exact answers already in the
database. `rates.Vehicle` carries `capacity`, `luggage_capacity`,
`included_carseats`, `included_boosters`, `carseats_display`,
`extra_carseat_fee` and `extra_booster_fee`. "Can you fit 9 people and 4
suitcases?" is a lookup, not a judgement.

Messages are short and frequently contextless — real examples include "Just 2",
"Confirm", and "Or 5 suitcases.". The assistant is useless without the
preceding conversation and the Lead's trip details. Context assembly is the
product.

## 3. Scope

### In scope for v1

- Draft replies to inbound SMS **from senders that match an existing `Lead`**.
- A dispatcher approval screen: read the draft, send / edit and send / reject
  with a reason.
- Answers grounded in the Lead's own trip, the published rate card, vehicle
  capability and capacity, the cancellation policy, and an editable knowledge
  base.
- Escalation to a human whenever the assistant is unsure.
- A local replay harness for testing against historical messages.
- Capture of every dispatcher correction as structured feedback.

### Explicitly out of scope for v1

- **Auto-sending.** Every reply is reviewed by a human. The auto-send path is
  built and tested but gated off (§8).
- **Unknown senders.** Inbound from a phone with no matching `Lead` behaves
  exactly as it does today. New-inquiry handling is a later phase.
- **Off-rate-card pricing.** See §6.2.
- **Writing to `Reservation`.** The assistant never creates or modifies a
  booking. It may propose `Lead` updates, which a dispatcher applies.
- **Channels other than SMS.**
- **Model fine-tuning.** The feedback loop improves prompts and knowledge, not
  model weights (§9).

## 4. Architecture

A new app, `ai_assistant`. `ghl_integration` remains the transport layer — it
knows how to talk to GHL and does not learn to think.

```
ai_assistant/
  models.py     AiConversationTurn, AiDraft, KnowledgeItem, AiFeedback
  context.py    build_context_packet(lead, inbound_text, history) -> dict
  pricing.py    thin wrapper over dispatching.quote_engine
  vehicles.py   capacity, car seat and luggage facts from rates.Vehicle
  brain.py      the single Claude call: packet in, structured result out
  policy.py     can_auto_send(draft) -> bool — the gate flipped in a later phase
  sending.py    send_now(draft) — the ONE send path
  views.py      approval queue, send, reject, knowledge editor
  tasks.py      process_turn(turn_id), retry_stalled_turns()
  prompts/      versioned system prompts, plain text files
  management/commands/
      ai_replay.py         offline backtest against historical messages
      ai_retry_stalled.py  requeue turns stranded by a restart
```

**The load-bearing boundary:** `brain.py` accepts a plain dict and returns a
dataclass. It never imports models and never touches the ORM. Every test of
"does the assistant say the right thing" is therefore a fixture, and the test
suite makes no network calls.

### 4.1 Why grounding works here

The assistant is given facts. It is not given the ability to fetch them. There
is no tool call, no database handle, no retrieval step it controls. Django
decides what is true before the model runs, and the model's only job is to turn
those facts into a sentence a customer can read.

This is the difference between "we told it to use the tool" and "it has no
other option". GHL's assistant fails at the former.

## 5. Data model

```python
class AiConversationTurn(models.Model):
    """One inbound message. The unit of work."""
    lead            = FK(reservations.Lead, on_delete=CASCADE, related_name="ai_turns")
    ghl_contact_id  = CharField(max_length=100, blank=True, db_index=True)
    inbound_text    = TextField()
    received_at     = DateTimeField(default=timezone.now, db_index=True)
    status          = CharField(choices=[pending, drafted, escalated, failed, skipped, superseded])
    skip_reason     = CharField(blank=True)   # opted_out | human_active | disabled | duplicate
    context_packet  = JSONField(default=dict) # exactly what the model was given
    extracted       = JSONField(default=dict) # trip details found in the message — §6.5
    extracted_applied_at = DateTimeField(null=True)  # set when a dispatcher saves them
    error           = TextField(blank=True)
    attempts        = PositiveSmallIntegerField(default=0)

class AiDraft(models.Model):
    turn            = OneToOneField(AiConversationTurn, on_delete=CASCADE, related_name="draft")
    reply_text      = TextField()
    needs_human     = BooleanField(default=False)
    reason          = TextField(blank=True)    # why it wants a human, in its own words
    confidence      = CharField(choices=[high, medium, low])
    facts_used      = JSONField(default=list)  # packet fact ids — see §6.4
    flagged         = BooleanField(default=False)  # tripped a §7 guard
    flag_detail     = TextField(blank=True)
    model_id        = CharField(max_length=60)
    prompt_version  = CharField(max_length=20)
    input_tokens    = PositiveIntegerField(default=0)
    cached_tokens   = PositiveIntegerField(default=0)
    output_tokens   = PositiveIntegerField(default=0)
    latency_ms      = PositiveIntegerField(default=0)
    status          = CharField(choices=[awaiting_review, sent, rejected, superseded])
    sent_text       = TextField(blank=True)    # what actually went out
    reviewed_by     = FK(User, null=True, on_delete=SET_NULL)
    reviewed_at     = DateTimeField(null=True)
    auto_sent       = BooleanField(default=False)

class KnowledgeItem(models.Model):
    topic           = CharField(max_length=80, db_index=True)
    trigger_hint    = CharField(max_length=200, blank=True)  # guidance, not matching
    answer          = TextField()
    is_active       = BooleanField(default=True)
    sort_order      = PositiveSmallIntegerField(default=100)
    history         = HistoricalRecords()      # simple_history, already installed

class AiFeedback(models.Model):
    draft           = FK(AiDraft, on_delete=CASCADE, related_name="feedback")
    kind            = CharField(choices=[approved, edited, rejected])
    reason_code     = CharField(blank=True, choices=REJECT_REASONS)
    original_text   = TextField()
    final_text      = TextField(blank=True)
    note            = TextField(blank=True)
    created_by      = FK(User, null=True, on_delete=SET_NULL)
    created_at      = DateTimeField(default=timezone.now, db_index=True)
```

`REJECT_REASONS` is a fixed list: `wrong_price`, `wrong_policy`, `wrong_facts`,
`wrong_tone`, `missed_the_question`, `should_have_escalated`, `other`. Fixed
codes are countable; free text alone yields anecdotes instead of numbers.

Two fields carry most of the diagnostic value.

`context_packet` is stored in full on every turn. When a draft is wrong you must
be able to tell whether the model reasoned badly or was handed a bad fact. Those
have completely different fixes and cannot be distinguished after the fact
without the packet.

`sent_text` next to `reply_text` is the feedback loop. The diff between what the
assistant wrote and what the dispatcher actually sent is the highest-value
signal in the system, and it costs nothing to record.

## 6. The context packet

`build_context_packet(lead, inbound_text, history) -> dict` is the heart of the
system. It returns a JSON-serialisable dict with a stable key order, because
that stability is what makes prompt caching work (§10.2).

```python
{
  "lead": {
      "first_name", "pickup_location", "dropoff_location",
      "pickup_date", "trip_type", "quoted_price", "vehicle",
      "status", "has_replied", "created_at",
  },
  "conversation": [                      # last N turns, oldest first
      {"from": "customer" | "us", "text": ..., "at": ...},
  ],
  "pricing": {...},                      # §6.2 — may be absent
  "vehicles": [                          # §6.3
      {"label", "capacity", "luggage_capacity", "carseats_display",
       "included_carseats", "included_boosters",
       "extra_carseat_fee", "extra_booster_fee"},
  ],
  "policies": {
      "cancellation": CANCELLATION_POLICY_SENTENCE,   # verbatim, never paraphrased
  },
  "knowledge": [ {"id", "topic", "answer"} ],
  "internal": {...},                     # dispatcher-only, must not reach a customer
}
```

### 6.1 Conversation history

Read from `LeadActivity`: `reply_received` rows give the customer side
(`metadata.message_body`, falling back to `metadata.message_preview`), and
`sms_sent` rows give ours. Cap at the last 10 exchanges. Median message length
is 50 characters, so this is cheap.

This matters more than anything else in the packet. "Just 2" and "Or 5
suitcases." are real messages from the archive and are meaningless alone.

### 6.2 Pricing — the strict part

`ai_assistant/pricing.py` is a thin wrapper over
[`dispatching/quote_engine.py`](../../../dispatching/quote_engine.py), which
describes itself as "the single source of truth for dispatcher price estimates"
and encodes founder decisions: the published rate card beats the formula
outright, gratuity is always a separate line, airport fees never apply to a card
price. It is already guarded by `dispatching/tests_quote_engine.py` against the
published rate card.

The assistant quotes what a dispatcher would quote because it is the same
function. No new pricing code is written. None may be.

```python
pickup_loc, _  = quote_engine.match_location(lead.pickup_location, locations)
dropoff_loc, _ = quote_engine.match_location(lead.dropoff_location, locations)

if not (pickup_loc and dropoff_loc):
    return None            # -> packet has no "pricing" key -> assistant escalates

results = quote_engine.quote_all_vehicles(
    trip_type=..., pickup_location=pickup_loc, dropoff_location=dropoff_loc,
)
results = [r for r in results if r.is_rate_card]
if not results:
    return None            # off-card: needs a distance lookup -> escalate
```

**v1 answers rate-card routes only.** When both ends match a `Location`, the
card price needs no distance and the answer is exact. When an end does not
match, pricing falls to the mileage formula, which needs a Google Maps distance
lookup — more moving parts, more ways to be wrong, and an external dependency in
the reply path. Those escalate.

This is a measured decision, not a permanent one. Every escalation records its
reason, so after a few weeks the off-card rate is a number rather than a guess.

**Only `QuoteResult.price` may reach a customer.** The dataclass docstring is
explicit that `breakdown` and `notes` are internal; one of the notes reads
*"Quote this, not a custom price."* Those belong in `packet["internal"]`, and
§7 guards against them leaking.

### 6.3 Vehicle capability

The largest answerable category (11.3%) and pure lookup. Include every active
`Vehicle` so the assistant can answer "what about an SUV?" and "will 9 people
and 4 suitcases fit?" without a second round trip, and so a capacity question is
answered by comparison rather than recall.

### 6.4 Fact ids

Every fact in the packet carries a stable string id, assigned by
`build_context_packet` — for example `pricing.towncar.oneway`,
`vehicle.suv.capacity`, `policy.cancellation`, `knowledge.12`, `lead.pickup_date`.

The model is asked to list the ids it relied on in `facts_used`. Two things
depend on this:

- The money guard (§7, guard 1) resolves quoted amounts against the pricing
  facts the reply claims to have used.
- A wrong draft can be traced to the specific fact that misled it, which is what
  separates a context bug from a wording problem (§9).

Ids must be stable across turns for the same lead, or the audit trail is
worthless. They are derived from the fact's position and key, never from a
counter or a UUID.

### 6.5 What the Lead does not know — slot filling

`Lead` is thin. It holds name, pickup, dropoff, **one** date, trip type, a
vehicle and a price. It does not hold:

| Missing | Why it matters |
| --- | --- |
| Return date | A round trip has two. Only the first is stored. |
| Passenger count | Decides the vehicle, which decides the price. |
| Luggage count | Same — `Vehicle.luggage_capacity` can bind before seats do. |
| Car seats / boosters | 11.3% of inbound traffic asks about these. |
| Flight number | Drives pickup timing and baggage-claim meetup. |

So the assistant cannot only answer — it must **collect**. Two additions:

**The packet states its own gaps.** Alongside `lead`, it carries:

```python
"known":   {"passenger_count": 3, "luggage_count": 2},
"missing": ["return_date", "car_seats"],
```

`known` is merged from prior turns' `extracted` (most recent wins). `missing` is
the difference against what the trip type requires — a round trip missing its
return date is incomplete in a way a one-way is not. The prompt instructs the
assistant to ask for **one** missing item at a time, never a form.

**Extraction is proposed, never applied.** When a message supplies a detail
("9 people 4 suitcases no seats"), the model returns it in `extracted`. It is
stored on the turn and shown on the approval screen as a suggestion the
dispatcher taps to save onto the `Lead`; `extracted_applied_at` records that.
The assistant never writes to `Lead` on its own — a bad extraction would
silently corrupt lead data and nobody would know which turn did it.

### 6.6 Re-quoting when the vehicle changes

If `known` passenger or luggage counts exceed the capacity of the vehicle on
the Lead, the packet's pricing section is built for the **smallest vehicle that
actually fits**, not the one originally quoted, and flags the change:

```python
"pricing": {
    "quoted_vehicle": "towncar", "quoted_price": "140.00",
    "required_vehicle": "van", "required_reason": "9 passengers exceeds towncar capacity of 4",
    "required_price": "...",        # rate card, same route
}
```

This stays inside the grounding rule. The van's price for that route is on the
published card, so it is a lookup like any other — the assistant is not
inventing an upcharge, it is reading a different row. It must state plainly
that the vehicle changed and why.

If no vehicle on the card fits the party, or the route has no card entry for
the required vehicle, pricing is omitted and the turn escalates.

## 7. Grounding guards

Enforced in code, after the model returns and before the draft reaches the
queue. A prompt instruction is a request; these are conditions.

1. **Money must trace.** Extract every currency amount from `reply_text`. Any
   amount not present in the packet's pricing facts sets `flagged=True` with
   detail. Architecturally the assistant should not be able to invent a price;
   this catches the case where it does anyway.
2. **No availability claims.** The assistant has no access to the schedule, so
   it must never say a date, time, or specific vehicle is free. Unlike the other
   guards this one cannot be checked exactly — it is semantic, not textual — so
   it is enforced in two overlapping ways: a phrase list (`we have`, `is
   available`, `we can fit you in`, `that time works`, `still open`) flags the
   draft, and the system prompt instructs the model to set `needs_human` for any
   availability question. Treat the phrase list as a net with holes and review
   what the queue catches; this is the guard most likely to need tuning from
   real transcripts.
3. **Policy is quoted, not paraphrased.** `CANCELLATION_POLICY_SENTENCE` is
   inserted verbatim when relevant. A reply discussing refunds without
   containing that sentence is flagged.
4. **No internal text.** Anything from `packet["internal"]` appearing in
   `reply_text` is flagged. This is what stops "Quote this, not a custom price"
   reaching a customer.
5. **Never confirm payment.** Messages asserting payment ("I paid") escalate
   unconditionally. The assistant has no payment state and must never appear to
   confirm money movement.
6. **Multi-leg escalates.** Requests describing more than one trip ("three
   different reservations", "airport to hotel, then hotel to the port")
   escalate. These are common enough in the archive to name explicitly.

A flagged draft still reaches the queue — visibly marked, never auto-sendable.
Hiding flagged drafts would hide the failure mode you most need to see.

Two further controls:

**Kill switch.** `AI_ASSISTANT_ENABLED` (env var, read per-request, not cached)
stops the assistant everywhere without a deploy. You will want this at an
inconvenient hour.

**Human-takeover lock.** If a `sms_sent` activity exists for the lead within
`AI_HUMAN_TAKEOVER_MINUTES` (default 15), the turn is skipped with
`skip_reason="human_active"`. Without this, the assistant and a dispatcher
answer the same customer at the same time, which is worse than no assistant.

## 8. Request flow

```
GHL inbound SMS
  |
  v
ghl_webhook  (ghl_integration/views.py:76 — extended, not replaced)
  |  all existing behaviour runs FIRST and unchanged:
  |  opt-out, lead matching, sibling sequence cancellation,
  |  LeadActivity, lifecycle tags, ntfy
  |
  |  then, inside a try/except that can only log:
  |     if AI_ASSISTANT_ENABLED and primary_lead and message_body:
  |         turn = AiConversationTurn.objects.create(status="pending")   # sync, fast
  |         run_in_background(process_turn, turn.id)
  v
  returns 200 immediately
                |
                v
        process_turn(turn_id)
          1. re-check guards: opted out / human active / disabled / superseded
          2. packet = build_context_packet(lead, inbound_text, history)
          3. result = brain.draft_reply(packet)
          4. apply §7 guards -> AiDraft
          5. needs_human or flagged -> turn.status = "escalated" + ntfy
          6. if policy.can_auto_send(draft): sending.send_now(draft)   # False in v1
                |
                v
        Approval queue (Django, branded per docs/claude.md)
          Send | Edit & Send | Reject + reason
                |
                v
        sending.send_now(draft)
          -> GoHighLevelService.send_sms()   (existing opt-out choke point)
          -> AiFeedback row, always
```

Three properties to preserve.

**The webhook stays additive.** Everything at
[`ghl_integration/views.py:76`](../../../ghl_integration/views.py) keeps working
exactly as it does today. The assistant is a branch at the end, wrapped so no
failure in it can break reply tracking or opt-out compliance. A broken assistant
must never cost a STOP request.

**One send path.** `sending.send_now(draft)` is called by the dispatcher's Send
button now and by `policy.can_auto_send()` later. Enabling autonomy changes what
`can_auto_send` returns — never how sending works. The path that eventually runs
unattended is the one that spent months under human supervision.

**Supersession.** If a customer sends a second message before the first draft is
reviewed, the earlier turn and draft are marked `superseded` and a fresh turn is
created. Replying to a stale message is a visible failure.

## 9. The feedback loop

Every review outcome writes an `AiFeedback` row. Nothing is optional; an
approval is as informative as a rejection.

Corrections resolve into one of three fixes, and classifying them correctly is
most of the value:

| Symptom | Fix | Cost |
| --- | --- | --- |
| Assistant lacked a fact it needed | Write a `KnowledgeItem` | ~30 seconds, no deploy |
| Had the fact, worded it badly | Edit the prompt, bump `prompt_version` | a deploy |
| Was handed something wrong | Bug in `context.py` | a deploy, and the serious one |

`prompt_version` is stored on every draft so quality can be compared across
prompt changes rather than argued about.

**What this is not.** This is curated correction feeding better instructions and
better reference material. It is not model retraining. Retraining is a far
larger undertaking and is not what moves accuracy at this scale — fixing the
facts and the instructions is.

### 9.1 Where knowledge is edited

`KnowledgeItem` is edited in the **branded dispatcher UI**, alongside the
approval queue, not in Django admin. Admin is Jazzmin-themed and remains
available as a power-user fallback; `simple_history` supplies the version trail
under either door.

Seed the initial items from the measured categories, in order: car seats and
boosters, luggage allowances, baggage-claim meetup, grocery stops, flight
tracking and wait time, what is included in a quote, gratuity.

## 10. The Claude call

### 10.1 Configuration

- Model: `claude-haiku-4-5`, from a setting so it can change without a deploy.
- No `output_config.effort` — Haiku 4.5 rejects it.
- No `thinking` block. This is a short, grounded writing task.
- `max_tokens`: 1024. Replies are SMS-length; a long reply is itself a defect.
- Structured output via `output_config.format` with a JSON schema, read back
  with `client.messages.parse()` so the shape is validated rather than trusted:

```json
{ "reply_text": "string",
  "needs_human": "boolean",
  "reason":      "string",
  "confidence":  "high | medium | low",
  "facts_used":  ["string"],
  "extracted":   { "passenger_count": "int|null", "luggage_count": "int|null",
                   "car_seats": "int|null", "booster_seats": "int|null",
                   "return_date": "date|null", "flight_number": "string|null" } }
```

`extracted` is proposed detail only (§6.5). Every field is nullable and the
default is null — the model must not fill a slot the customer did not mention.

Structured output is what makes escalation a field rather than a phrase to be
parsed out of prose.

### 10.2 Caching

The system prompt, policies, vehicle table and knowledge base are identical
across turns and form the bulk of the tokens. Put them first with
`cache_control: {"type": "ephemeral"}`; put the lead's details, conversation
history and inbound message after the breakpoint.

Verify with `usage.cache_read_input_tokens` rather than assuming — the minimum
cacheable prefix is model-dependent and a short prefix silently will not cache.
Store the value in `AiDraft.cached_tokens` so a regression is visible.

### 10.3 Cost

At roughly 3K input and 150 output tokens per turn, `claude-haiku-4-5`
($1/$5 per million) costs about $0.004 per turn before caching — near $40 per
month at 10,000 turns, materially less with caching working.

`AI_DAILY_SPEND_CAP_USD` (default 10) halts the assistant for the day when
exceeded. A runaway loop must not bill overnight.

## 11. Failure handling

| Failure | Behaviour |
| --- | --- |
| Claude API down or rate limited | SDK retries; then turn stays `pending`, `attempts += 1`, error stored |
| Deploy kills the thread mid-flight | Turn stays `pending`; `ai_retry_stalled` requeues it |
| Malformed model output | Schema validation fails → `status="failed"`, escalate, notify |
| `quote_engine` raises | Caught; packet omits pricing; assistant escalates |
| Guard trips (§7) | Draft reaches the queue flagged, never auto-sendable |
| Spend cap hit | Assistant disabled for the day, ntfy sent |
| Anything unexpected in the webhook branch | Logged and swallowed; webhook returns 200 |

`ai_retry_stalled` runs from the existing scheduler alongside the other
`ghl_integration` commands. Turns older than 24 hours are marked `failed` rather
than retried forever — a day-old reply is a human's problem.

There is no Celery worker in this deployment.
[`ghl_integration/runner.py`](../../../ghl_integration/runner.py) runs background
work in daemon threads inside Gunicorn (3 workers × 4 threads, single Railway
replica), closing DB connections on exit. This design uses that same mechanism
and tolerates its one weakness — killed threads — by persisting the turn
synchronously before dispatching the work.

## 12. Local replay harness

The first thing built, not an afterthought. It is how the design is validated
before a customer is involved.

```
python manage.py ai_replay --limit 200 --category carseats --out scratch/replay.md
```

For each historical `reply_received` activity it reconstructs the Lead's state,
builds the real context packet, calls the real `brain.draft_reply`, and writes
the inbound message, a packet summary, the draft, its confidence, any escalation
reason and any guard flags to a readable file.

Hard requirements:

- **Sending is impossible.** `sending.send_now` refuses unless
  `AI_ASSISTANT_LIVE_SEND` is true, which is never set locally. Belt and braces:
  the command never calls it.
- `--dry-run` (default) makes no API calls at all and prints only the packets,
  so context assembly can be debugged for free.
- `--category` filters by the §2 buckets so one failure mode can be worked at a
  time.
- Output is a file for reading, not a pass/fail. Early on, judgement is the
  measurement.

45,046 historical replies means the sample is never the constraint. Cost is,
mildly — so replay in slices, never the whole archive:

| Messages replayed | Approximate cost |
| --- | --- |
| 200 | $0.80 |
| 1,000 | $4 |
| all 45,046 | $170, or ~$90 once caching is working |

200–500 per category is enough to see a failure mode. Expect the whole testing
phase to cost under $25.

## 13. Testing

`manage.py test` must make no network calls. `brain.draft_reply` takes a dict,
so it is stubbed at that boundary.

**Context tests** — `context.py` against real `Lead`, `Rate`, `Route`,
`Location` and `Vehicle` fixtures. This is where grounding is proven: a
rate-card route yields the card price; an unmatched location yields no pricing
key; internal notes land in `internal` and nowhere else.

**Guard tests** — one per §7 rule, each asserting the negative. A draft quoting
$150 when the packet holds no such figure must be flagged.

**Escalation tests** — the ones that would have caught the current GHL
behaviour. Given a packet with no pricing, the assistant must escalate rather
than quote. Given a multi-leg request, it must escalate. Given "I paid", it must
escalate.

**Flow tests** — supersession, takeover lock, opt-out re-check, kill switch, and
`send_now` refusing without `AI_ASSISTANT_LIVE_SEND`.

**Existing webhook tests must pass untouched.** That is the proof the assistant
did not break compliance.

A separate, explicitly-marked suite may call the real API for prompt regression.
It never runs in CI.

## 14. What gets measured

Surfaced on the approval screen, because a metric nobody sees does not change
behaviour:

- Escalation rate, broken down by reason.
- Share of drafts sent clean / edited / rejected, and rejection reason mix.
- Median and 90th-percentile time a draft waits in the queue.
- Cost per conversation, and cache hit rate.
- Guard trip rate by guard.

These answer the two questions deliberately left open: whether rate-card-only
pricing is too narrow, and whether a standalone approval page gets used. Both
get answered with evidence rather than prediction.

## 15. Settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | `""` | credentials |
| `AI_ASSISTANT_ENABLED` | `False` | master kill switch |
| `AI_ASSISTANT_LIVE_SEND` | `False` | allows any outbound send at all |
| `AI_ASSISTANT_MODEL` | `claude-haiku-4-5` | model id |
| `AI_PROMPT_VERSION` | `v1` | stamped on every draft |
| `AI_HUMAN_TAKEOVER_MINUTES` | `15` | takeover lock window |
| `AI_HISTORY_TURNS` | `10` | conversation turns in the packet |
| `AI_DAILY_SPEND_CAP_USD` | `10` | runaway guard |

Both enable flags default off. Installing this code changes nothing until
someone deliberately turns it on.

## 16. Expansion path

Deliberately staged, each step gated on evidence from the one before.

1. **Auto-send a narrow whitelist.** `policy.can_auto_send` returns true for
   proven-safe categories once the queue shows a sustained clean-send rate.
   Soft-no acknowledgements are the natural first candidate: high volume,
   near-zero risk.
2. **Off-card pricing**, if the measured escalation rate justifies the Google
   Maps dependency.
3. **Unknown senders.** Requires rewriting the webhook's 404 path to create a
   Lead. Higher value, more failure modes.
4. **Tool calling.** When pre-assembly stops being sufficient, the functions in
   `context.py` and `pricing.py` become tool implementations with little
   rewriting. That seam is intentional.
5. **Writing to Lead and Reservation.**

## 17. Release note

Dispatchers get a new screen, so per [CLAUDE.md](../../../CLAUDE.md) this ships
with a release note written on the commit that makes it live. Intermediate
commits that ship nothing visible carry `Release-Note: none`.
