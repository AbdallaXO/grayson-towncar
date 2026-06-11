# Scheduler Logic Audit — How the In-House Assignment Engine Decides

**Date:** 2026-06-07 · **Scope:** read-only audit, no code changed.
**Subject:** the **in-house scheduler** — the engine that decides which in-house driver/vehicle gets
which leg when you press *Auto-Assign*. This is **NOT** the farm-out optimizer
(`dispatching/farmout_optimizer.py`), which is a separate retrospective "was that farm decision right?"
grading tool and is *never* invoked by the live scheduler.

**How to read this:** written so a dispatcher can follow it without opening code. Every claim cites
`file:line` and quotes the key logic. Each claim is marked **CONFIRMED** (read and quoted directly) or
**INFERRED** (deduced from the code but not stated outright). A plain-English answer leads each section;
the table at the end lists every scoring weight and its default.

**Files audited:** `dispatching/views.py` (orchestrator `auto_assign_drivers`),
`dispatching/scheduler.py` (the engine), `dispatching/feasibility_guards.py` (the rules),
`dispatching/swap_optimizer.py` (the cascade recovery), `dispatching/models.py` (the tunable weights).

---

## TL;DR — the one-paragraph answer

The scheduler is a **greedy, one-pass, leg-by-leg matcher**. It puts the day's legs in a fixed order
(scarcest first), then walks the list once and hands each leg to the single **highest-scoring driver
who can feasibly take it**. "Highest-scoring" is mostly about **giving the leg to the driver whose
existing day leaves an ideal-sized 20–30-minute gap for it**, with strong secondary pulls toward
**rare-vehicle protection**, **exact vehicle match**, **staying in the same area / chaining**, and
**not piling everything on one driver**. Once a leg is placed it is **never reconsidered** in the main
pass; two clean-up passes afterward (a swap-cascade to rescue would-be-farmed legs, and a gap-filler)
are the *only* things that move an already-placed leg. **What stays unassigned (and gets farmed) is
simply "whatever the greedy order couldn't fit" — there is no farm-cost or opportunity-cost reasoning
anywhere in the live engine.** That last point is the single biggest divergence from how the founder
describes farming by hand.

---

## Section 1 — Entry Points & Flow

**Plain English:** You press Auto-Assign → it (optionally) builds a few hand-picked drivers' full days
first, then runs one greedy pass over everything else, then tries to rescue legs that didn't fit by
shuffling existing jobs, then tidies up big idle gaps. Then it either previews or saves.

**Entry point** is `auto_assign_drivers` in the dispatch view, which runs in either *preview* mode
(suggest only) or *apply* mode (save to DB). **CONFIRMED** — `dispatching/views.py:8989`:

```python
@login_required
def auto_assign_drivers(request):
    """Auto-assign inhouse drivers to unassigned legs for a given date.
    Two modes controlled by `apply` flag:
      - apply=False (default): Preview ...
      - apply=True: Apply — run suggestions and save assignments to DB.
```

**The pipeline has four phases**, in this order (**CONFIRMED** — `dispatching/views.py:9155-9246`):

1. **Build-first seeding.** Drivers the dispatcher flags "Build first" get their *entire* day built
   before anything else, narrowest-window driver first, via `build_smart_schedule`
   (`dispatching/scheduler.py:1973`). Their legs are then **locked** so later steps can't move them.
2. **General greedy assignment.** `suggest_assignments_clustered` (`dispatching/scheduler.py:1061`)
   groups the remaining legs into time clusters, optionally pins drivers to clusters for shift
   coherence, then delegates to the core greedy engine `suggest_assignments`
   (`dispatching/scheduler.py:1118`). **CONFIRMED** — `dispatching/views.py:9197`:
   ```python
   suggestions = suggest_assignments_clustered(auto_unassigned, assign_board, target_date, ...)
   ```
3. **Pre-farm swap recovery.** `recover_residuals_via_swaps` (`dispatching/scheduler.py:1655`) tries to
   pull each would-be-farmed leg back in-house by *cascading* existing assignments (driver A takes the
   farmed leg, his bumped job moves to driver B, …). **CONFIRMED** — `dispatching/views.py:9229`.
4. **Gap-compaction relocation.** `compact_gaps_via_relocation` (`dispatching/scheduler.py:1743`)
   relocates an already-covered leg from one driver onto another driver who has a big idle hole, when
   that heals more gap than it opens. **CONFIRMED** — `dispatching/views.py:9242`.

The four sources merge into one `final_assignments` map; manual + seeded legs are added to a
`locked_ids` set passed to both clean-up passes. **CONFIRMED** — `dispatching/views.py:9209-9220`.

**`build_smart_schedule` is the same engine, tuned differently.** It reuses the identical scoring math
but with "schedule-builder" weight variants (`sb_buffer_perfect`, `sb_flow_3rd_arrival`, …) and a
revenue bonus, and it scores legs against *one* driver in isolation rather than competing all drivers.
**CONFIRMED** — `dispatching/scheduler.py:1973`, `:2493-2595`; builder weights at
`dispatching/models.py:28-64,108-112`.

> **Top-level algorithm, plainly:** *Greedy, single-pass, leg-by-leg, with scarcity-first ordering and
> two post-build clean-up passes.* No global optimization, no capacity pre-check, no look-ahead beyond
> the local score.

---

## Section 2 — The Objective: what it actually optimizes FOR

**Plain English:** For each leg, every driver who *can* take it gets a numeric score; the leg goes to
the highest score. The score is built from ~13 factors. The **biggest single lever is buffer quality**
— it most wants the driver whose existing schedule leaves a *20–30-minute* gap for this leg. After that
it favors **giving rare jobs to the few who can do them**, **exact vehicle match**, **staying local /
chaining**, and it pushes back against **overloading one driver** or **stretching a day too long**.

Each candidate starts at `score = 0`, accumulates the terms below, and the **highest-scoring
non-reserved driver wins** (`best_score`). **CONFIRMED** — `dispatching/scheduler.py:1379`, `:1557`:

```python
score = 0
...
if score > best_score:
    best_score = score
    best_id = did
```

All weights are defaults on the `SchedulerSettings` singleton Django model (editable in admin; cached
in memory per run). **CONFIRMED** — `dispatching/models.py:9`, `get_settings()` at `:126-135`.

### The full scoring breakdown (auto-assign), ranked by default weight

| # | Factor | What it rewards/penalizes | Default weight(s) | Cite |
|---|--------|---------------------------|-------------------|------|
| 1 | **Buffer quality** | gap this leg leaves vs the neighboring jobs | perfect 20–30m **+120**, sweet 30–60m +100, good 60–120m +80, **tight 10–20m +70**, **loose 120m+ +50**, risky <10m +30 | `scheduler.py:1382-1394` · `models.py:21-26` |
| 2 | **Scarcity** | how few drivers can do this leg at all | 1 eligible **+80**, 2 +50, 3 +30, 4 +15 | `scheduler.py:1410-1418` · `models.py:43-46` |
| 3 | **Vehicle tier match** | driver's vehicle = leg's required vehicle | exact +60, 1-down +40, 2-down +25, 3-down +15, 4-down +10 | `scheduler.py:1396-1408` · `models.py:36-40` |
| 4 | **Trip-type preference** | leg matches driver's set preference | match +40 (**heavy ×2 = +80**), mismatch −10 (heavy −20); `only_*` is a HARD skip | `scheduler.py:1532-1546` · `models.py:109-110` |
| 5 | **Shift coherence** | leg is in driver's assigned time-cluster | +50 | `scheduler.py:1478-1483` · `models.py:97` |
| 6 | **Location proximity** | minimal repositioning from last dropoff | same-area +50, first-job +40, close ≤15m +30 | `scheduler.py:1420-1430` · `models.py:49-51` |
| 7 | **Chain (forward)** | other jobs cluster near this dropoff | 3+ +45, 2 +35, 1 +20 | `scheduler.py:1453-1460` · `models.py:70-72` |
| 8 | **Backward chain** | driver's last dropoff flows into this pickup | +40 (within 30m drive & 10–180m gap) | `scheduler.py:1462-1476` · `models.py:73-75,94` |
| 9 | **Flow (anti-arrival-streak)** | discourage back-to-back airport arrivals | 3rd+ arrival −40, 2nd −15; return/cruise break +30 | `scheduler.py:1432-1447` · `models.py:57-59` |
| 10 | **Retention** | keep returns/cruises in-house | +25 | `scheduler.py:1449-1451` · `models.py:67` |
| 11 | **Load balance** | penalize piling jobs on one driver | **−(10 × jobs^1.5)** | `scheduler.py:1485-1488` · `models.py:82-83` |
| 12 | **Idle-gap penalty** | penalize big holes in the day | −2 per minute over 120m | `scheduler.py:1490-1515` · `models.py:86-87` |
| 13 | **Span penalty** | penalize over-long driver days | −30 per hour over 13h | `scheduler.py:1517-1530` · `models.py:90-91` |
| 14 | **Reserved-vehicle mismatch** | protect a scarce vehicle from being "wasted" | −60 + segregated to fallback-only | `scheduler.py:1548-1551` · `models.py:78` |

All **CONFIRMED** by direct read of both the scoring loop and the model defaults.

### Why driver A beats driver B — in words

1. **A leaves a better-sized gap.** Buffer quality is the heaviest term (up to +120). The engine most
   wants the driver whose current schedule has an *ideal* 20–30-minute slot for this leg.
2. **A can do something few others can.** If only A's vehicle fits (or 1–2 do), scarcity adds up to +80
   and exact-tier match +60 — together able to outweigh buffer.
3. **A keeps it local / chains.** Same-area (+50), a 3+ chain (+45), backward-chain (+40), and
   cluster-coherence (+50) bias toward compact, low-deadhead days.
4. **A isn't already overloaded.** Each extra job on a driver costs `10 × jobs^1.5` (2 jobs ≈ −28,
   3 ≈ −52, 4 ≈ −82), and a >13-hour day or >2-hour holes cost more — so a fresh/light driver competes
   better as the day fills.

### A subtlety worth knowing: buffer scoring is a *hump*, not a ramp

**CONFIRMED** — `dispatching/scheduler.py:1382-1394`. Sorted by gap size the points go:
`<10m → 30`, `10–20m → 70`, `20–30m → 120`, `30–60m → 100`, `60–120m → 80`, `120m+ → 50`. It **peaks at
20–30 min and falls off for very loose gaps**. So the engine will prefer a *tight-but-feasible 15-minute
turn (+70)* over a driver who'd sit idle for two-plus hours (+50). **Idle time is treated as waste** —
which aligns with "min gaps," but means the engine sometimes *prefers* a tighter turn to a roomy one.

> A `time_scarcity_bonus` (default 30) is defined in settings (`models.py:101`), but in the scoring loop
> as read, hour-level time-scarcity influences leg **ordering** (Pass 1, see §3), not a direct score
> term. **INFERRED** (the field exists; no addend for it was found in `:1379-1563`).

---

## Section 3 — Order of Operations (the order *is* the strategy)

**Plain English:** In a greedy scheduler, the order you process legs largely decides the outcome,
because early picks eat capacity later picks need. This engine deliberately does the **scarcest,
hardest-to-place legs first**.

The legs are first sorted `(hour, trip_type_priority, pickup_time)` where
`_TYPE_PRIORITY = {'return': 0, 'cruise': 1, 'other': 2, 'arrival': 3}` — i.e. **returns/cruises are
offered before arrivals within the same hour**. **CONFIRMED** — `dispatching/scheduler.py:1164`.

Then a **three-pass re-sort** runs (**CONFIRMED** — `dispatching/scheduler.py:1302-1317`):

```python
def _multi_pass_sort_key(leg):
    ...
    if leg_vtype:
        exact_count = exact_type_driver_counts.get(str(leg_vtype), 0)
        eligible = scarcity_map.get(leg.id, len(working))
        if 0 < exact_count <= cfg.reserve_max_scarcity and eligible <= half_fleet:
            pass_priority = 0  # Pass 0 (vehicle-scarce)
    if pass_priority == 2 and time_scarcity_map.get(leg.id, 0) > 1.5:
        pass_priority = 1  # Pass 1 (time-scarce)
    return (pass_priority, leg.pickup_time.hour, _TYPE_PRIORITY_REF.get(trip_type, 2), leg.pickup_time)
```

- **Pass 0 — vehicle-scarce:** legs needing a vehicle only ≤2 drivers *are* (`reserve_max_scarcity`)
  **and** that ≤ half the fleet *can* serve. Placed first.
- **Pass 1 — time-scarce:** legs in an hour whose demand/supply ratio > 1.5 (more jobs than drivers).
- **Pass 2 — everything else.** Within each pass the original `(hour, type, time)` order holds.

**Does it ever revisit a pick?** No — not inside the core pass. Once the winning driver is found, the
leg is appended to that driver's working schedule and re-sorted, and the loop moves on. **CONFIRMED** —
`dispatching/scheduler.py:1628-1629`:

```python
working[best_id].slots.append(sim_slot)
working[best_id].slots.sort(key=lambda s: s.pickup_time)
```

The **only** things that move an already-placed leg are the two post-build passes (§1 phases 3–4),
which run after the greedy pass completes. **CONFIRMED** — `dispatching/views.py:9196-9246`.

---

## Section 4 — Constraints Enforced (hard gates vs soft penalties)

**Plain English:** Some rules *disqualify* a driver outright (you simply can't give him the leg); the
rest are *score adjustments* that make a driver better or worse but never block him.

### Hard blocks — a driver is skipped entirely (`continue`)

All in the candidate loop, **CONFIRMED**:

1. **Outside the driver's time window** (unless he's flexible) — `scheduler.py:1342-1346`.
2. **Already at his max-hours span** — `scheduler.py:1349-1355`.
3. **Vehicle too small** — the leg's required tier is above the driver's vehicle tier —
   `scheduler.py:1357-1362`, using `get_compatible_vehicle_types` (`:153-158`). Tiers are
   `towncar < mini_van < suv < van < Van(14 Pax)`; a driver can serve his tier **and everything below**.
4. **`check_feasibility` fails** (overlap / turnaround / window) — `scheduler.py:1364-1367`.
5. **Trip-type preference set to `only_*`** and the leg doesn't match — `scheduler.py:1532-1538`
   (this is the one *preference* that becomes a hard block).

**Reserved-vehicle mismatch** is a soft-hard hybrid: a driver in a *higher* tier who still has scarce
matching jobs waiting is **segregated to a fallback pool** and only used if no normal driver fits.
**CONFIRMED** — `scheduler.py:1369-1377`, `:1548-1555`, `:1568-1571`.

### `check_feasibility` — the timing gate

**CONFIRMED** — `dispatching/scheduler.py:726-841`. It checks **no overlap**, then a
**context-dependent turnaround** (Guard B) to both the preceding and following job, then the
**per-driver window** (Guard C). If the computed buffer to a neighbor is negative, the driver is
infeasible:

```python
earliest_available = preceding.estimated_end_time + timedelta(minutes=req)
buffer_minutes = int((new_pickup_dt - earliest_available).total_seconds() / 60)
if buffer_minutes < 0:
    return FeasibilityResult(feasible=False, ...)
```

**Guard B — turnaround** (**CONFIRMED** — `feasibility_guards.py:96-118`): for an **airport-arrival
pickup**, the driver only needs to reach the curb by gate-arrival + `DEPLANING_GRACE_MIN` (15 min), and
a **same-terminal** hop charges *zero* drive — so the required turnaround can go **negative** (you may
grab a 1:34 MCO arrival right after dropping a 1:35 MCO return). For any **non-arrival**, the **full
real drive time** is required with no grace. A global `SAFETY_PAD_MIN = 0` means no extra slack is
added — *"we are never late"* is handled by live dispatch, not by padding the engine:

```python
if next_is_airport_arrival:
    base = (0 if same_terminal else reposition_drive_min) - dg   # may be < 0
else:
    base = reposition_drive_min
return base + pad
```

**Guard C — per-driver window** (**CONFIRMED** — `feasibility_guards.py:149-195`): a **start** bound, a
**clear-by end** bound (`END_HOUR_MODE = "CLEAR_BY"` — the driver must *finish*, not just pick up, by
his end hour, correctly handling after-midnight clears), and a **max-hours** day-span cap. Flexible
drivers bypass start *and* end (`FLEXIBLE_RESPECTS_CLEAR_BY = False`) but **still respect max-hours**.
Windows currently come from observed-history **stubs** (`USE_STUB_WINDOWS = True`), not configured
schedules.

**Guard A — physical capacity (party / luggage / car-seats) was intentionally REMOVED.** Booking-time
validation already enforces fit against the booked vehicle, and an assignment-time check fired false
positives off stale seat-count data. **CONFIRMED** — `feasibility_guards.py:86-90`.

### Everything else is a soft score term

Buffer quality, vehicle-tier *preference* (beyond the hard "too small" block), scarcity, proximity,
chains, flow, retention, load-balance, idle-gap, span, and trip-preference (in `prefer`/`heavy` modes)
are all additive — they shape ranking, never disqualify. **CONFIRMED** — `scheduler.py:1379-1551`.

---

## Section 5 — What It Optimizes vs What It Ignores

### What it genuinely optimizes
- **Rare-vehicle protection** — Pass-0 ordering + reservation segregation keep the only-van/only-SUV
  driver available for the jobs only he can do. (`scheduler.py:1302-1311`, `:1369-1377`)
- **Constrained-hour coverage** — Pass-1 fills the over-subscribed hours before slack hours.
  (`scheduler.py:1313-1314`)
- **On-time feasibility with a realistic buffer** — Guard B/C + the buffer hump. (`scheduler.py:726-841`)
- **In-house retention of returns/cruises** — explicit +25 and flow bonuses. (`scheduler.py:1449-1451`)
- **Local, chained, cluster-coherent days** — proximity + chain + shift-coherence bonuses, a *proxy*
  for low deadhead. (`scheduler.py:1420-1483`)
- **Not overloading / not over-running a driver** — load-balance, idle-gap, span penalties.
  (`scheduler.py:1485-1530`)

### What it ignores or handles weakly (each mapped to a founder goal)

1. **Farm cost / opportunity cost when leaving a leg out — completely absent.** When no driver fits, the
   leg just becomes "No in-house driver available." Nothing ranks the leftovers by how expensive or
   out-of-pattern they'd be to farm. **CONFIRMED** — `scheduler.py:1640-1648` (see §6). *Maps to the
   founder's "shed out-of-pattern legs first" rule, which the engine does not implement.*
2. **Look-ahead is myopic.** `chain_map` is computed once over the *unassigned* pool before the loop; an
   early pick that destroys a better later chain is never reconsidered, and the chain credit isn't
   recomputed as the board fills. **CONFIRMED** — `scheduler.py:1192-1221`, `:1610-1629`. *The engine
   optimizes the leg in front of it, not the day as a whole.*
3. **Empty deadhead is never a direct objective.** Only *same-area / ≤15-min* proximity bonuses exist;
   crucially, `loc_first_job` (+40) is awarded **regardless of how far the first pickup is from base** —
   a 5-minute and a 60-minute first leg score identically. **CONFIRMED** — `scheduler.py:1420-1430`.
   *Maps to "min empty deadhead / build paid round-trips," which is only indirectly served.*
4. **Round-trips aren't paired explicitly.** A customer's departure+return scores the same as two
   unrelated chained legs — the chain bonus doesn't know they belong together. **INFERRED** from the
   chain logic — `scheduler.py:1453-1476`.
5. **Load-balance can fight slow-day consolidation.** The `−10 × jobs^1.5` penalty grows fast (4 jobs
   ≈ −82), which *spreads* work across drivers — the opposite of "use fewer cars on slow days."
   **CONFIRMED** — `scheduler.py:1485-1488`. (The retention/proximity/coherence bonuses partly offset
   this, but the tension is real and untuned to a stated target.)
6. **Drive-time is coarse.** `USE_LIVE_DISTANCE` is **OFF** by default (a perf hotfix), so reposition
   times come from a category table; intra-resort hops are billed at a flat ~12-min average even when
   the true hop is 0–20 min. This can produce **false "impossible"** (farming a tight turn that's
   actually fine) and **false "feasible"** (accepting a turn that's actually tight). **CONFIRMED** —
   `scheduler.py:74-94`, `:469-496`. *Directly affects "we are never late" and "don't farm real turns."*
7. **No global optimality.** Greedy + scarcity ordering is a heuristic. The swap pass compensates only
   partially and is bounded by depth (`swap_max_depth=5`) and time (`swap_time_limit_ms=5000`).
   **CONFIRMED** — `models.py:115-116`.

**A concrete suboptimal scenario** (illustrative, INFERRED from the greedy structure): one van driver,
three towncar drivers; a van leg at 6 AM and another van leg at 1 PM, plus a towncar leg at 7 AM. If the
6 AM van leg consumes the van driver in a way that blocks the 1 PM van leg, the 1 PM van leg has no
compatible driver and is farmed — even though a human would have kept the van free for both van legs and
let a towncar driver absorb the flexible work. The swap pass *may* repair this if a feasible cascade
exists within its depth/time budget, but it isn't guaranteed.

---

## Section 6 — How It Decides What to Leave Unassigned (the farm set)

**This is the most important section for farm quality, because the leftover set is exactly what you
then farm by hand — and the engine does not optimize that set at all.**

**Plain English:** A leg is left unassigned for one reason only: *no driver could feasibly take it* by
the time the greedy order reached it. There is **no comparison** of "is this one cheaper to farm than
that one." The farm set is a byproduct of the processing order plus capacity.

**CONFIRMED** — `dispatching/scheduler.py:1640-1648`:

```python
else:
    suggestion = AssignmentSuggestion(
        leg_id=leg.id,
        suggested_driver_id=None,
        suggested_driver_name=None,
        feasibility=None,
        reason="No in-house driver available",
        priority=0,
    )
```

The final unassigned list is just "legs not in `final_assignments`" assembled in the orchestrator after
all passes. **CONFIRMED** (path) — `dispatching/views.py:9248-9249` (`remaining = len(unassigned) -
assigned_count`) and the unassigned set surfaced to the UI later in the view.

**The pre-farm swap pass rescues by REVENUE, not farm cost.** When it tries to claw back would-be-farmed
legs, it processes them **highest-revenue-first** — a value proxy, *not* the cost/opportunity-cost the
founder uses. **CONFIRMED** — `dispatching/scheduler.py:1690`:

```python
farmed.sort(key=lambda l: -float(getattr(l, "revenue_share", 0) or 0))  # highest value first
```

This is the **opposite** of "shed the most out-of-pattern (far-from-hub, high-opportunity-cost) leg
first." A high-value arrival far from the hub could be kept while a cheap-to-farm near-hub return is
let go.

**Gap-compaction never changes the farm set** — it only relocates already-covered legs; coverage is
fixed before it runs. **CONFIRMED** — `dispatching/scheduler.py:1749` ("Coverage is preserved: a leg
only changes driver, never gets farmed.").

**The cost comparison the founder makes by hand exists in code — but is never used live.**
`dispatching/farmout_optimizer.py` can answer *"was it cheaper to farm this leg or keep it and farm
something else?"*, but it is a **read-only retrospective** tool, not called by `auto_assign_drivers`.
**CONFIRMED** — `farmout_optimizer.py:1-54`.

> **Bottom line:** the quality of the leftover set you farm is determined by the *scarcity/time sort
> order + greedy capacity*, then nudged only by *revenue* in the swap pass. Farm cost / opportunity cost
> is not a factor at any step of the live engine.

---

## Section 7 — Known Limitations / Gaps, and Questions for the Founder

"Optimize like me" is a human standard only the founder can define, so each gap below ends with a
question. Gaps are code-grounded; confidence is marked.

**G1 — No cost-aware farming decision. (CONFIRMED)**
The engine farms "whatever didn't fit," and the rescue pass prioritizes *revenue*, not farm cost
(`scheduler.py:1640-1648`, `:1690`).
→ *When you must farm, do you shed the most out-of-pattern (far-from-hub, high-opportunity-cost) leg, or
the cheapest-to-farm one? Should the engine rank leftovers by opportunity cost (distance-from-hub ×
farm-premium) instead of leaving them in greedy order?*

**G2 — Myopic, single-pass greedy. (CONFIRMED)**
No backtracking; chain credit is computed once over the unassigned pool and never refreshed
(`scheduler.py:1192-1221`, `:1610-1629`). The swap pass only triggers on *farmed* legs, not on
already-placed legs that a later leg could chain with better.
→ *Should the engine revisit an early pick when a later leg would chain better — or is single-pass +
swap-rescue good enough for your day?*

**G3 — Load-balance may fight consolidation. (CONFIRMED)**
`−10 × jobs^1.5` pushes work to spread across more drivers (`scheduler.py:1485-1488`).
→ *On a slow 20-leg day, do you want those on 3–4 cars (consolidated) or 6–8 (spread)? Should the
load-balance penalty be softened (or disabled) on slow days?*

**G4 — Deadhead / depot positioning not modeled. (CONFIRMED for proximity; INFERRED for depot)**
Proximity bonuses exist, but `loc_first_job` (+40) ignores how far the first pickup is from base
(`scheduler.py:1420-1430`). Empty repositioning is never a direct cost.
→ *Should the first-job bonus shrink with distance from your base? Should empty miles be an explicit
cost the engine minimizes?*

**G5 — Coarse drive times. (CONFIRMED)**
Category table (live distance OFF); intra-resort hops billed flat (`scheduler.py:74-94`, `:469-496`),
causing both false-impossible and false-feasible turns.
→ *Would a nightly-refreshed cached distance matrix (no per-request network) be worth it, or is the
table accurate enough for your tolerance?*

**G6 — Round-trip pairing not explicit. (INFERRED)**
A departure+return pair scores like any two chained legs (`scheduler.py:1453-1476`).
→ *Should a matched departure→return pair earn a bigger bonus so it stays on one driver (a paid
round-trip), versus today's generic chain credit?*

**G7 — Reservation-count bookkeeping is type-scoped but leg-driven. (INFERRED; NOT a clear bug)**
When a scarce-vehicle leg is assigned, the reserved count is decremented only for drivers whose vehicle
type **matches the leg's required type** — the `dvtype_r == assigned_vtype` guard is present
(`scheduler.py:1631-1639`), so the earlier "possible bug" concern is largely unfounded. The subtle part:
it keys off the *leg's required* type, regardless of which driver/tier actually took it.
→ *Confirm intent: when a scarce-van leg is covered by a higher-tier fallback driver, should the van
drivers' "reserved" counts still decrement? (Today they do.)*

**G8 — Buffer scoring ignores the arrival grace. (INFERRED)**
Buffer-quality bands apply uniformly, but airport arrivals get a 15-min deplaning grace — so a "tight"
10–20-min gap to an *arrival* is genuinely roomier than the same gap to a *non-arrival*, yet both score
+70 (`scheduler.py:1382-1394` vs `feasibility_guards.py:96-118`).
→ *Should buffer scoring be context-aware (treat a tight gap to an arrival as better than the same gap
to a curbside departure)?*

**G9 — `only_*` trip preference is a hard block. (CONFIRMED)**
A driver set to `only_arrival` is skipped for every non-arrival even if that leg would otherwise be
farmed (`scheduler.py:1532-1538`).
→ *Should `only` be a strong penalty rather than an absolute block, so a scarce job can still be covered
in a pinch? Or suppressed for Pass-0 scarce legs?*

**G10 — Driver windows are provisional stubs. (CONFIRMED)**
Start/end/max-hours come from observed-history stubs marked "optimistic … captures what a driver did,
not hard limits," including a `placeholder` id 6 (`feasibility_guards.py:45-83`).
→ *When do real configured driver windows replace the stubs? Until then, are the stub windows safe to
schedule against, or should a few (e.g. id 6) be excluded?*

### Open questions surfaced during the read
- Is `reserve_max_scarcity = 2` plus the *half-fleet* eligibility test the right trigger for "this
  vehicle needs protecting" (`scheduler.py:1306-1311`)?
- Is the chain window `chain_time_max = 180 min` right for your market — should two same-area jobs 5
  hours apart still count as a chainable pair on a consolidation day (`models.py:75`)?
- Are flexible drivers' span/idle penalties aggressive enough to keep their days compact, given they
  bypass start/end bounds (`feasibility_guards.py:30,176-177`)?
- How much do the swap optimizer's own weights (`swap_depth_penalty=150`, `swap_buffer_weight=2`,
  `swap_revenue_weight=10`, `swap_tier_bonus=20`) actually change which rescues win (`models.py:118-121`)?

---

## Appendix — Full tunable-weight reference (`SchedulerSettings`, `dispatching/models.py:9-124`)

All defaults below are **CONFIRMED** by direct read.

**Buffer quality (auto):** perfect=120, sweet_spot=100, good=80, tight=70, loose=50, risky=30 (`:21-26`)
**Buffer quality (builder):** sb_perfect=35, sb_sweet=30, sb_good=20, sb_first_job=25, sb_tight=15 (`:29-33`)
**Vehicle tier:** exact=60, 1_down=40, 2_down=25, 3_down=15, 4_down=10 (`:36-40`)
**Scarcity:** 1=80, 2=50, 3=30, 4=15 (`:43-46`)
**Location (auto):** same_area=50, close=30, first_job=40 (`:49-51`) · **(builder)** sb_same_area=35 (`:54`)
**Flow (auto):** 3rd_arrival=−40, 2nd_arrival=−15, break_bonus=30 (`:57-59`) · **(builder)** −35 / −10 / 25 (`:62-64`)
**Retention:** 25 (`:67`)
**Chain:** 3_plus=45, 2=35, 1=20; drive_threshold=30m, time_min=10m, time_max=180m (`:70-75`)
**Reservation:** penalty=−60, max_scarcity=2 (`:78-79`)
**Load balance:** multiplier=10, exponent=1.5 (`:82-83`)
**Idle gap:** threshold=120m, penalty_per_min=2 (`:86-87`)
**Span:** threshold=13h, penalty_per_hour=30 (`:90-91`)
**Backward chain:** 40 (`:94`)
**Cluster/coherence:** shift_coherence_bonus=50, cluster_gap_minutes=120 (`:97-98`)
**Time scarcity:** time_scarcity_bonus=30 (`:101`) *(ordering driver; direct-score use not observed)*
**Global:** inter_job_buffer=5m, arrival_grace_minutes=15 (`:104-105`)
**Builder extras:** base_score=50, trip_pref_match=40, trip_pref_mismatch=−10, revenue_divisor=10, revenue_cap=20 (`:108-112`)
**Swap optimizer:** max_depth=5, time_limit_ms=5000, max_iterations=5000, depth_penalty=150, buffer_weight=2, revenue_weight=10, tier_bonus=20 (`:115-121`)

**Feasibility-guard flags (`dispatching/feasibility_guards.py`):** DEPLANING_GRACE_MIN=15 (`:38`),
SAFETY_PAD_MIN=0 (`:43`), END_HOUR_MODE="CLEAR_BY" (`:25`), FLEXIBLE_RESPECTS_CLEAR_BY=False (`:30`),
USE_STUB_WINDOWS=True (`:46`).

**Engine flags (`dispatching/scheduler.py`):** USE_LIVE_DISTANCE=0/off (`:87`),
AUTO_PREFARM_SWAP_PASS=True, AUTO_GAP_COMPACT_PASS=True, GAP_COMPACT_MIN_GAP=120,
GAP_COMPACT_MIN_NET_GAIN=60, GAP_COMPACT_PROTECT_DONOR_MAX_JOBS=3, GAP_COMPACT_MAX_MOVES=25.

---

*Methodology: read-only audit. Findings produced by a 7-agent parallel read of the scheduler, then
cross-checked by direct reads of `scheduler.py:1302-1317,1379-1652`, `:726-841`, `models.py:9-135`,
`feasibility_guards.py:1-196`, and `views.py:8989,9196-9249`. No source code was modified.*
