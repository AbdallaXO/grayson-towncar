# Recovery Advisor — the dispatcher-facing redesign

**Status:** uncommitted, not yet exercised in a browser by a human.
**Scope:** a presentation layer over the existing advisor, plus three deliberate
engine behaviour changes (§7). Detection, plan generation and the apply/snooze/
file-task write paths are otherwise untouched.

---

## 1. What this is

The Recovery Advisor engine (`dispatching/conflict_advisor.py`) was already
correct and auditable. Its problem was that it spoke scheduler:

> Untangle D1's 4:00 AM turn — 110 min short
> Chain math (same formula as auto-assign): previous job's clear + reposition +
> turnaround leaves -110 min against the 4:00 AM pickup.

A dispatcher reading that at 4 a.m. has to know what a chain clear is, what a
reposition is, and that `SFB` is Sanford. This redesign puts a plain-language
surface on top of it and draws the problem instead of describing it:

> **george will be 13 minutes late for the 12:30 PM pickup**
> george's 11:00 AM Orlando Airport run doesn't drop off until 12:15 PM, and
> after driving back george isn't ready again until about 12:43 PM — 13 minutes
> after the 12:30 PM Stella Nova pickup george is also on.

The engine's own words are not deleted. They sit, verbatim, under a per-plan
**"Show the math"** expander. That is the whole reason the rewrite is safe to
make: the audit trail survives, so a dispatcher who distrusts the summary can
always check the working.

---

## 2. Architecture

```
conflict_advisor.py                     advisor_display.py
  detect_disruptions()  ─── Disruption ────► card_display(board, d)
  generate_plans()      ─── CandidatePlan ─► plan_display(board, d, plan, rank)
  _serialize_plan()                              │
        │                                        │  JSON-safe dict
        └──────────► card["display"] ◄───────────┘  (percentages, not datetimes)
                          │
            ┌─────────────┴──────────────┐
            ▼                            ▼
  _recovery_advisor.html        _advisor_plans.html
  (dispatch board rail,          (conflict task page,
   client-side JS)                server-side Django)
            └──────────┬─────────────────┘
                       ▼
        _recovery_advisor_styles.html  (one shared stylesheet)
```

**Two hooks into the engine, both additive:**

| Where | What |
|---|---|
| `_advisor_state()` | `card["display"] = safe_display(card_display, board, d)` |
| `_serialize_plan()` | `out["display"] = safe_display(plan_display, board, d, plan, rank)` |

Every pre-existing field (`headline`, `narrative`, `basis`, `why`, `risks`,
`moves`, `apply`, `score`, `key`, `rank`) is left byte-for-byte as it was. The
apply payload still carries ids only; `plan.title` still lands in the task
resolution note; the anti-flap hysteresis still hangs off `key` and `score`.

### Why geometry is computed on the server

Every block, marker, bracket and band arrives at the template as a **percentage**
(`left_pct`, `width_pct`). The renderers place; they never calculate.

The board rail renders client-side from polled JSON. The conflict-task page
renders server-side from the same dict. If each computed its own layout, the two
would drift the moment either changed — and a timeline that disagrees with
itself across two screens is worse than no timeline. One `Axis` class, one set of
percentages, two dumb renderers.

---

## 3. The `display` contract

### On a card

```jsonc
{
  "headline": "george will be 13 minutes late for the 12:30 PM pickup",
  "story":    "george's 11:00 AM Orlando Airport run doesn't drop off until …",
  "scope":    {"text": "Going by the clock",
               "detail": "worked out from the schedule, not from live tracking"},
  "why_nobody": "",                    // plain rewrite of the swap diagnostic
  "conflict": {                        // null when there is no real geometry
    "title": "Tomorrow afternoon as it stands",
    "subtitle": "Both runs are on george, back to back. …",
    "axis":    {"ticks": [{"label": "11 AM", "left_pct": 25.0, "hour": true}, …]},
    "lanes":   [{"driver": "george", "initial": "G", "role": "driver",
                 "blocks": [{"type": "run",   "trip": "arrival", "time": "11:00 AM",
                             "label": "Orlando Airport → Beach Club",
                             "left_pct": 25.0, "width_pct": 20.8, "minutes": 75},
                            {"type": "reset", "label": "drive back + reset", …}]},
                {"driver": "", "role": "conflict",
                 "blocks": [{"type": "conflict", "label": "… — george can't make it"}]}],
    "markers": [{"kind": "pickup", "left_pct": 66.7, "label": "Stella Nova pickup · 12:30 PM"},
                {"kind": "free",   "left_pct": 88.9, "label": "george free · ~12:43 PM"}],
    "bracket": {"tone": "red", "label": "13 min short",
                "detail": "the first run doesn't clear in time",
                "left_pct": 66.7, "width_pct": 22.2, "mid_pct": 77.8},
    "legend":  [{"cls": "trip-arrival", "label": "airport arrival"}, …]
  }
}
```

### On a plan

```jsonc
{
  "headline":     "Move the 12:30 PM Stella Nova → Orlando Airport run to Seline",
  "price_label":  "$120",              // null unless it is a farm plan
  "action_label": "Apply this move",
  "outcome":      "george keeps the rest of the day. Seline covers the 12:30 PM …",
  "warnings":     [{"tone": "amber", "text": "Check with Seline first — no set shift …"}],
  "after": {                           // null for monitor plans — nothing moves
    "axis":  {"ticks": [ … ]},
    "lanes": [
      {"driver": "george", "role": "from", "cleared": true, "tag": null,
       "blocks": [{"type": "keep",  …},
                  {"type": "ghost", "label": "↓ to Seline", …}]},
      {"driver": "Seline", "role": "to",   "cleared": false,
       "tag":    {"kind": "moved", "left_pct": 50.0, "text": "moved from george"},
       "blocks": [{"type": "other", …}, {"type": "reset", "label": "drive back"},
                  {"type": "moved", …}],
       "gaps":   [{"kind": "room", "side": "after", "minutes": 65, "slack_min": 65,
                   "label": "1 h 5 m free before Seline's next run",
                   "left_pct": 61.1, "width_pct": 13.9}]}]
  },
  "math": {                            // the engine, verbatim — the audit trail
    "title": "…", "why": [ … ], "risks": [ … ], "narrative": "…",
    "facts": ["detection slack: -13 min", "Seline spare after move: 65 min …",
              "issue class: flight", "plan: reassign (tier 2, rank #1, score 970)"]
  }
}
```

**Block types:** `run` · `reset` · `conflict` · `tight` · `keep` · `other` ·
`moved` · `ghost`.
**Gap kinds:** `room` (green, dashed) · `tight` (red sliver).

---

## 4. How the conflict picture is derived

For an overlap card the engine has already computed three numbers. The drawing
is built **from those and nothing else**, so it cannot disagree with the
detector:

| Element | Derivation |
|---|---|
| committed run | leg's pickup → its `chain_clear_dt` |
| drive back + reset | `chain_clear_dt` → `ready` |
| **`ready`** | **`impact_dt − details["slack"]`** ← the engine's own arithmetic |
| uncovered run | `impact_dt` → that leg's `chain_clear_dt` |
| bracket | `impact_dt` ↔ `ready`, labelled `duration(slack)` |

`ready` is *defined* as `impact − slack`. There is no second opinion about how
short the driver is; if the engine says −110, the bracket is 110 minutes wide.

Card kinds map onto the same shape:

| kind | committed run | the pickup at risk | shortfall |
|---|---|---|---|
| `overlap` | `leg_ids[0]` | `leg_ids[1]` | `details["slack"]` |
| `late_cascade` / `overrun` | `anchor_leg_id` | **first** entry of `details["breaks"]` | that break's slack |
| `flight_change` | `leg_ids[0]` | `leg_ids[1]` | `details["slack_out"]` (only when < 0) |
| `unassigned` | — none — | the leg itself | — none — |
| hygiene / abstain | no picture — text only | | |

**The first break, not the worst.** Later breaks in a cascade were measured
against the carried-forward clear of the ones before them, so pairing the anchor
with the worst break drew a reset block over the job that actually breaks first,
and named a different pickup than the card's own countdown.

**Two clocks, kept apart** (the engine's guard 1). The conflict picture uses the
**detection** clock — a recorded pickup re-anchors the clear via
`chain_clear_dt_from_actual`, because that is the clock the slack was measured
on. Every *after-the-move* picture uses the **planning** clock (never
optimistic), because that is the board `validate_post_move_board` blessed.

---

## 5. How the after-the-move picture is derived

Per affected driver, `_post_move_jobs` rebuilds the day: their own jobs, minus
what leaves, plus what lands (via `scheduler._make_sim_slot`, the same simulated
slot the validator used), with any retimed run redrawn at its new time.

Between two adjacent jobs, `board_validation.turn_slack_minutes` — the one
shared formula the assignment engine and the board pill also use — decides what
gets drawn:

```
[ moved run ]──[ drive back ]──[ green: free time ]──[ receiver's real next job ]
                               └── or a red sliver, when the engine calls it tight
```

Three rules make this honest:

1. **The drive back is drawn, not folded in.** The clock gap between two jobs is
   not all free time. If the green band covered the whole gap while labelled with
   usable spare, a dispatcher eyeballing it would read free time the driver does
   not have. The band spans only the slack; the hatched drive-back explains the
   rest.
2. **Tight vs roomy is the engine's call**, from `TURN_TIGHT_SLACK_MIN`, never
   from how wide the gap happens to look.
3. **The receiver's real next job is on the lane.** Spare time anchored to
   nothing is a number; anchored to the 2:00 PM Saratoga Springs run, it is
   something the dispatcher can go and check.

The donor's lane shows a dashed **ghost** where the run used to be, directly
above the block that now holds it — a reassignment changes the driver, not the
clock, so the two line up and the receiving lane's tag draws a stem up to meet
it.

---

## 6. Language rules

- **Name the person and the consequence.** "george will be 13 minutes late for
  the 12:30 PM pickup" — not "the turn out goes 13 min short".
- **Prose spells units out** (`duration_words` → "1 hour 50 minutes"); tight
  drawings abbreviate (`duration` → "1 h 50 m"). Past two days it reads in days.
- **Locations as spoken** (`plain_place`): address tail dropped, airport codes
  expanded, chain prefix and generic property noun trimmed —
  `Disney's Port Orleans Resort, Orlando, FL` → `Port Orleans`.
  Guarded both ways: a chain word is only dropped when two or more words
  survive (`Disney Springs` stays `Disney Springs`), and a bare code only counts
  when it opens the label followed by airport vocabulary, or is ALL-CAPS inside
  a venue name (`Brightline MCO` → `Brightline Orlando Airport`, while
  `Cafe Mia` is left alone).
- **"Double-booked" means simultaneous.** A driver whose 11:00 run overruns his
  own 12:30 job is *late*, not double-booked.
- **Claims we cannot make, we don't.** `basis` records which signal found the
  problem — detection never inspects the car, the licence or the booking. The
  scope chip says "Going by the clock", never "vehicle and licensing check out".
- Job colours come from the dispatch board's own trip palette
  (`legs_filter.html` `tripColors`): arrival `#1565c0`, return `#2e7d32`,
  cruise `#e65100`, other `#6a1b9a`. Role is carried by outline and opacity so
  the two codings never fight.

---

## 7. Engine behaviour changes (deliberate — read this before pushing)

These are **not** presentation. They change what dispatchers see.

### 7.1 Farming is a last resort

Previously an in-house reassign that created a tight turn was demoted to tier 3
— the same tier as farming — and then score decided, so a clean affiliate quote
could outrank keeping the work in-house.

Now:

- The farm tiers **only run when no in-house plan survives validation**. If the
  work can stay in-house, no affiliate is priced and no farm card is built.
- Swaps are searched when every direct taker would create a tight turn, not only
  when nobody can take it — shuffling the board is still keeping it in-house.
- `bool(p.farm_out)` leads the sort key, so no score can ever invert this.

**Consequence to be aware of:** when the only in-house option is a bad one (a
reassign leaving a 3-minute turn), you now see that option and *no* priced farm
alternative beside it.

### 7.2 One card per problem

A flight card reporting that the turn out of its leg is broken now claims that
pair whatever moved it — a delayed plane *or* an unacknowledged pickup-time
change. Previously it claimed only on a late flight, so the retime case produced
two cards with the same headline, the same legs and the same single fix.

### 7.3 `no_internal_solution` no longer tests tiers

It tested `tier in (1, 2)`, so a tight-turn reassign (demoted to tier 3) made
the card announce "No clean in-house fix exists for this one" while displaying
that very reassign. It now tests for the presence of a non-farm plan.

### 7.4 Cache keys are versioned

`RA_CARD_SHAPE_V` is baked into both advisor cache keys. The board fingerprint
tracks the *board*, not the code, so without this a deploy would keep serving
pre-redesign cards until the TTL drained. **Bump it whenever the card or plan
dict gains, loses or redefines a field.**

---

## 7.5 The Swap Tester draws with the same builder

`/swap-tester/` used to list its solutions as text ("Move Leg #14751 from Angel
→ george"). It now renders each solution as the board **after** that swap —
identical lanes, colours, ghosts and spare-time bands as a recovery plan.

There is no second renderer. Two seams make it work:

```python
# dispatching/advisor_display.py
swap_board(schedules, legs_by_id, target_date)   # wrap what the page already has
swap_timeline(board, moves, target_leg_id)       # → the same `after` dict
```

`find_swap_suggestions` builds the board **once** (the planning-clock sweep
caches onto it, so N solutions cost one sweep) and attaches `timeline` to each
solution. The client calls `RATimeline.buildAfterTimeline(sol.timeline)`.

`window.RATimeline` (`includes/_timeline_renderer.html`) is now the single
client-side renderer, shared by the advisor rail and the swap tester; the
conflict-task page renders the same structures server-side. Three surfaces, one
drawing, one stylesheet.

**One deliberate difference:** on the swap tester the disruption is synthesised
with `leg_ids=[]`, so no lane is tagged "conflict cleared" — nothing was in
conflict there. The donor's dashed ghost still shows the run leaving, which is
the true statement.

The engine's move list is preserved verbatim under **"Show the moves"**, the
same audit-trail contract the advisor has.

---

## 8. Files

| File | Role |
|---|---|
| `dispatching/advisor_display.py` | **new** — the whole presentation layer |
| `dispatching/tests_advisor_display.py` | **new** — 63 tests |
| `dispatching/conflict_advisor.py` | 2 display hooks + §7.1–7.3 |
| `dispatching/advisor_views.py` | `RA_CARD_SHAPE_V` |
| `ops/views.py` | `RA_CARD_SHAPE_V` in the task-card cache key |
| `…/includes/_recovery_advisor.html` | rail renderer (JS) |
| `…/includes/_advisor_plans.html` | conflict-task renderer (Django) |
| `…/includes/_recovery_advisor_styles.html` | shared stylesheet |
| `…/conflict_task_detail.html` | includes the shared stylesheet |
| `…/includes/_timeline_renderer.html` | **new** — `window.RATimeline`, the one client renderer |
| `…/swap_tester.html` | solutions drawn as timelines (§7.5) |
| `dispatching/views.py` | `find_swap_suggestions` attaches `timeline` per solution |

---

## 9. Safety contracts

- **A display failure never costs a card.** Every builder runs through
  `safe_display`, which returns `None`; both renderers fall back to the engine's
  raw text, and the Apply button is untouched. On the degraded path the task page
  shows `plan.risks` as visible chips rather than burying them in the collapsed
  expander.
- **Nothing is invented.** Where a fact does not exist — an unassigned leg has no
  old driver, a farmed leg's receiver has no schedule we hold — the element is
  omitted, not guessed. An affiliate lane draws no free time around the run,
  because we do not have their day.
- **No safety line is ever dropped.** Recognised engine risks are rewritten;
  anything unrecognised is shown verbatim. Every worsened turn gets its own chip.
- **Read-only.** The display layer makes zero queries and zero writes (measured,
  §10). It never mutates the board, the disruption or the plan.
- **Presentation time is not charged against the analysis budget**, so a slower
  drawing can never shrink a later card's swap search.

---

## 10. What has been verified — and what has not

### Verified

| Check | Result |
|---|---|
| Automated suite (`dispatching` + `ops`) | **1403 passing** |
| New display-layer tests | 68, incl. a sweep asserting no engine vocabulary or bare airport code reaches any dispatcher-visible string |
| Adversarial multi-agent review (4 lenses, findings independently refuted) | 38 raised → **25 confirmed** → all fixed but §11.1 |
| Live database sweep: 80 busiest days, 405 cards, 50 plans | 0 display failures, 0 jargon leaks, JSON-safe throughout |
| Display layer cost on real boards | **+0 queries**, no measurable time (worst full compute 95 ms on a 161-card day, unchanged with display off) |
| Rail + swap-tester + shared-renderer JS syntax | parses clean |
| Swap Tester rendered on 6 real days; endpoint exercised on real swap solutions | timelines built, per-lane donors correct, no "conflict cleared" mislabel |

### NOT verified — this is the gap

- **Nobody has opened either page in a browser since the last round of changes.**
  Everything above is server-side data and template rendering. The visual layer —
  trip colours, the ghost + connector arrow, the amber "tight" block, the
  relocated "conflict cleared" flag, overflow on narrow blocks — has only been
  seen in your screenshots, which predate those changes.
- **Nobody has clicked Apply.** The apply *flow* is unchanged and covered by
  existing tests, but the confirm modal's rendering changed (title, warning
  source, price label, the ±999 sentinel).
- **No load test.** 95 ms is a single-process measurement on a dev database.

### Suggested 10-minute manual pass before you push

1. Open the dispatch board on a day with a live conflict. Check the rail renders,
   colours match the board's timeline, and the ghost + arrow line up.
2. Collapse/expand the rail; reload — the collapsed state should persist.
3. Open "Show the math" on a plan and confirm the engine's own lines are there.
4. Open the matching conflict task page and confirm it draws the *same* card.
5. Apply one low-stakes move, then Undo. Confirm the board matches expectation.
   (See §11.1 first if the plan involves a pickup-time change.)
6. Snooze a card; confirm it disappears and stays gone on the next poll.
7. Open `/swap-tester/`, search swaps on a no-fit leg, and confirm the solution
   timelines draw and the hover-highlight into the driver schedule still works.

For a deeper audit than this, `/code-review ultra` runs a multi-agent cloud
review of the branch — that one is yours to trigger.

---

## 11. Known gaps and follow-ups

### 11.1 Undo does not restore a moved pickup time *(pre-existing, unfixed)*

Confirmed empirically against the real apply path. `ScheduleSnapshotEntry`
(`reservations/models.py`) stores `leg`, `driver`, `driver_assigned_by`,
`driver_assigned_at` — **no `pickup_time`** — and `restore_schedule_snapshot`
restores driver assignments only. So after applying a `match_flight` plan, Undo
puts the drivers back and silently leaves the pickup time moved.

This predates the redesign and fixing it means changing the snapshot model and
the undo path, which is outside a presentation change. **Until it is fixed,
treat Undo on any plan containing a retime as partial.**

### 11.2 Forced re-tick dropped when a poll is in flight *(pre-existing, minor)*

`tick()` checks `if (ticking) return;` before the `forced` branch, so the 409
re-tick can be swallowed. `fp` is already reset to `null`, so the next 60-second
poll recovers — a delay, not a stall.

### 11.3 Legal same-terminal turns draw touching blocks

`required_turnaround` is **negative** for a same-terminal airport turn (the
10-minute deplaning grace), so the next pickup can legally begin before the
previous job clears. The sliver is clamped so it can never render inverted, but
the two run blocks genuinely abut on the clock. That is the truth about the
board; it just looks tight because it is.

### 11.4 The conflict-task page loads a second Google Fonts stylesheet

It already loaded one for its own type; the shared advisor stylesheet adds
JetBrains Mono + Inter. One extra render-blocking request on that page.
