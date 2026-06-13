# Scheduler Scoring & Tuning Guide

All scoring values live in **`dispatching/scheduler.py`**. This doc explains every factor the scheduler uses to decide which driver gets which job, and how to tweak each one.

---

## How the Scheduler Works (Overview)

The scheduler has two modes:

1. **Auto-Assign** (`suggest_assignments`) — Takes ALL unassigned legs for a date and assigns each one to the best driver. Used in the capacity planner "Auto-Assign" button.
2. **Schedule Builder** (`build_smart_schedule`) — Builds the best schedule for ONE specific driver. Used when you click a driver in the capacity planner.

Both use the same scoring factors. The job with the highest total score wins.

---

## Processing Order (Auto-Assign Only)

Before scoring, the auto-assign sorts legs in this order:

| Priority | Trip Type | Why |
|----------|-----------|-----|
| 1st | Return | Short (~30 min), predictable, frees driver fast |
| 2nd | Cruise | High-value, time-sensitive |
| 3rd | Other | Resort-to-resort, misc transfers |
| 4th | Arrival | Longest jobs (~75 min), flight delay risk |

Within the same priority, legs are sorted by **founder value** (`leg_value()` — booked
vehicle class first, then trip type, then `revenue_share`, passenger count as the final
tiebreak), then pickup time. When two 10:00 AM arrivals tie on (hour, type), the
Van(14 Pax)-class booking reaches the V14 driver before the Van-class one, and the
3-pax towncar beats the 2-pax (founder rules R3 + R4).

**What this means**: Returns get assigned to drivers first, before arrivals eat up all the capacity. This prevents returns from being pushed to affiliates.

**To tweak**: The type priorities are **SchedulerSettings fields** (admin-tunable, no
code change): `type_priority_return` (0), `type_priority_cruise` (1),
`type_priority_other` (2), `type_priority_arrival` (3). Lower = processed first.
- Want cruise transfers assigned before returns? Set `type_priority_cruise=0, type_priority_return=1`
- Want everything equal (pure time-based)? Set all to `0`

---

## Scoring Factors

Every candidate job gets a score. Highest score wins. Here's every factor:

### 1. Buffer Quality

**What it is**: The gap (in minutes) between when the driver finishes their previous job and when the new job starts. Includes repositioning drive time + 10 min personal buffer.

**Example**: Driver finishes at Disney at 9:15 AM. New job picks up at MCO at 10:30 AM. Drive Disney→MCO is 30 min. Buffer = 10:30 - (9:15 + 30 min drive + 10 min buffer) = 35 min spare.

**Auto-Assign scoring**:
| Buffer (minutes) | Score | Why |
|-------------------|-------|-----|
| 20–60 min | **+100** | Sweet spot — tight but comfortable |
| 60–120 min | **+80** | Good, slightly loose |
| 10–20 min | **+70** | Tight but doable |
| 120+ min | **+50** | Big gap, driver sitting idle |
| Under 10 min | **+30** | Very tight, risky |

**Schedule Builder scoring**:
| Buffer (minutes) | Score | Why |
|-------------------|-------|-----|
| 20–60 min | **+30** | Sweet spot |
| 60–120 min | **+20** | Acceptable gap |
| 999 (first job) | **+25** | No prior job, always fits |
| 10–20 min | **+15** | Tight |
| Under 10 min | **+0** | No bonus |

**To tweak**: Search for `Score by buffer quality` (auto-assign) or `Buffer quality` (schedule builder).
- Schedules feel too tight? Increase the 20-60 range or make the 10-20 range score lower.
- Too much idle time between jobs? Reduce the 120+ score even further (e.g., +20).
- Want tighter schedules? Increase the 10-20 range score.

**Related constant**:
```python
INTER_JOB_BUFFER = 10  # minutes — personal break between jobs
```
Increasing this makes the scheduler leave more breathing room. Decreasing it packs jobs tighter. This affects feasibility (whether a job is even possible), not just scoring.

---

### 2. Vehicle Tier Match

**What it is**: How well the driver's vehicle matches the job's vehicle requirement. Exact match is best, downgrading (using a Van for a towncar job) is acceptable but not ideal.

**Tier hierarchy** (0 = lowest):
```
0: Towncar → 1: MiniVan → 2: SUV → 3: Van → 4: Van(14 Pax)
```

| Tier Difference | Score | Example |
|-----------------|-------|---------|
| 0 (exact match) | **+60** | SUV driver → SUV job |
| 1 (one down) | **+40** | SUV driver → MiniVan job |
| 2 (two down) | **+25** | Van driver → MiniVan job |
| 3 (three down) | **+15** | Van(14Pax) driver → MiniVan job |
| 4 (four down) | **+5 / +10** | Van(14Pax) driver → Towncar job |

**To tweak**: Search for `Vehicle tier preference` (auto-assign) or `Vehicle tier scoring` (schedule builder).
- Want stricter tier matching (don't waste big vehicles on small jobs)? Increase the exact match bonus (+60 → +80) and reduce the downgrade scores.
- Want more flexible assignment? Flatten the scores (make them closer together).

---

### 3. Scarcity (How Many Other Drivers Could Do This Job)

**What it is**: Counts how many drivers on this date have a vehicle capable of handling this job. If only 1-2 drivers can do it, the job is "scarce" and should be prioritized over jobs any driver could take.

| Eligible Drivers | Score | Meaning |
|------------------|-------|---------|
| 1 (only this driver) | **+80** | Must take it — nobody else can |
| 2 drivers | **+50** | Very scarce |
| 3 drivers | **+30** | Somewhat scarce |
| 4 drivers | **+15** | Mild scarcity |
| 5+ drivers | **+0** | Plenty of options, no bonus |

**Real example**: You have 1 Van(14 Pax) and 3 SUVs. A 14-pax job has scarcity=1 (only the Van driver can do it → +80). An SUV job has scarcity=4 (3 SUV drivers + the Van driver → +15). The scheduler will prefer giving the Van driver their 14-pax jobs first.

**To tweak**: Search for `Scarcity bonus` in both functions.
- If rare vehicle jobs are still being missed, increase the 1-driver bonus (+80 → +100).
- If you want the scheduler to care less about scarcity, reduce all values.

---

### 4. Location Proximity

**What it is**: Whether the driver is already near the next job's pickup location. Checks the dropoff location of their last completed job.

**Auto-Assign scoring**:
| Situation | Score |
|-----------|-------|
| Same area (e.g., last dropoff = Disney, next pickup = Disney) | **+50** |
| Close area (repositioning drive ≤ 15 min) | **+30** |
| First job (no prior location) | **+40** |
| Far away | **+0** |

**Schedule Builder scoring**:
| Situation | Score |
|-----------|-------|
| Same area | **+35** |
| Far away | **+0** |

**To tweak**: Search for `Location proximity bonus`.
- Want location to matter more? Increase +50/+35 to +70/+50. This will make the scheduler strongly prefer keeping drivers in one area.
- Want location to matter less (prioritize other factors)? Reduce to +25/+15.

---

### 5. Schedule Flow (Arrival Stacking Prevention)

**What it is**: Looks at the last few jobs on a driver's schedule. Penalizes adding another arrival if they already have arrivals stacked up. Rewards adding a return after arrivals (natural alternating flow).

**How it counts**: Walks backward from the end of the driver's schedule, counting consecutive arrivals.

**Auto-Assign scoring**:
| Situation | Score |
|-----------|-------|
| 3rd+ arrival in a row | **-40** |
| 2nd arrival in a row | **-15** |
| Return/cruise breaking an arrival streak | **+30** |

**Schedule Builder scoring**:
| Situation | Score |
|-----------|-------|
| 3rd+ arrival in a row | **-35** |
| 2nd arrival in a row | **-10** |
| Return/cruise breaking an arrival streak | **+25** |

**Why this matters**: 3 arrivals in a row means 3 flights that could each delay 30-60 min. One delay cascades into the next. A return between arrivals acts as a "reset" — it's short, predictable, and gets the driver back to the airport area.

**To tweak**: Search for `Schedule flow`.
- Schedules still too arrival-heavy? Increase the penalties (-40 → -60, -15 → -30).
- Want to allow 2 arrivals in a row without penalty? Remove the `consecutive_arrivals == 1` penalty.
- Want even stronger preference for alternating? Increase the flow break bonus (+30 → +50).

---

### 6. In-House Retention (Returns & Cruise)

**What it is**: A flat bonus for returns and cruise transfers when being scored for any in-house driver. Makes the scheduler try harder to keep these jobs in-house instead of farming them to affiliates.

| Trip Type | Bonus |
|-----------|-------|
| Return | **+25** |
| Cruise | **+25** |
| Arrival | +0 |
| Other | +0 |

**Why**: Returns are 25-30 min and predictable. Arrivals are 60-75 min with flight delay risk. If you have to farm one out, it's smarter to farm the arrival — it ties up an affiliate for longer and the delay risk is on them.

**To tweak**: Search for `In-house retention bonus`.
- Still seeing returns farmed out? Increase to +40 or +50.
- Want to also retain arrivals? Add a smaller bonus for arrivals (e.g., +10).
- Want cruise transfers prioritized even higher? Give them a separate, larger bonus.

---

### 7. Trip Type Preference (Schedule Builder Only)

**What it is**: When building a schedule for one driver, the dispatcher can set a "preferred trip type" (e.g., "arrivals only" or "cruise"). Jobs matching the preference get a bonus.

| Situation | Score |
|-----------|-------|
| Matches preferred type | **+40** |
| Doesn't match preferred type | **-10** |
| No preference set | +0 |

**To tweak**: Search for `Trip type preference bonus`.

---

### 8. Chain Awareness (Look-Ahead)

**What it is**: Before scoring a job, the scheduler checks: "If this driver takes this job, will they end up near other unassigned jobs?" If yes, the job gets a bonus because it opens up follow-up work — creating a 2-3 job chain instead of an isolated trip.

**How it works**: For each candidate leg, the scheduler looks at all other unassigned legs and checks:
1. Is the other leg's pickup within 30 min drive of this leg's dropoff? (near enough to chain)
2. Does the other leg start 10-180 min after this leg ends? (realistic time window)

If both conditions are met, that's a "chainable follow-up." The more follow-ups, the bigger the bonus.

| Follow-up Jobs Near Dropoff | Score |
|-----------------------------|-------|
| 3+ jobs | **+45** |
| 2 jobs | **+35** |
| 1 job | **+20** |
| 0 jobs | +0 |

**Why this matters**: Without chain awareness, the scheduler picks jobs in isolation. It might give Driver A a job that drops them at Port Canaveral (dead end — no follow-up work nearby), when a different job would drop them at MCO (3 more pickups waiting). Chain awareness sees that the MCO-dropoff job leads to more work and scores it higher.

**Real example — your exact scenario**:
```
Driver finishes: Disney → MCO, done 12:30 PM. Driver is at MCO.

Available jobs:
  Job A: 1:00 PM Arrival MCO → Disney (~75 min, done ~2:15)
  Job B: 1:00 PM Arrival MCO → Port Canaveral (~100 min, done ~2:40)
  Job C: 3:00 PM Return Disney → MCO (~30 min)
```

Without chain awareness, Jobs A and B score similarly (both at MCO, same buffer). The scheduler might pick Job B.

With chain awareness:
- **Job A** (MCO → Disney): After this job, driver is at Disney by ~2:15. Job C picks up from Disney at 3:00. Gap = 45 min, same area. **Chain count = 1 → +20 bonus.**
- **Job B** (MCO → Port Canaveral): After this job, driver is at Port Canaveral by ~2:40. Job C is at Disney, 70 min drive away. Too far to chain. **Chain count = 0 → +0.**

Job A wins. Driver does Job A (1 PM) → Job C (3 PM). Two jobs instead of one.

**Another example — MCO morning rush**:
```
Available:
  8:00 AM  Arrival MCO → Disney      (done ~9:15 at Disney)
  8:15 AM  Arrival MCO → Universal   (done ~9:20 at Universal)
  9:30 AM  Return Disney → MCO       (pickup at Disney)
  10:00 AM Return Universal → MCO    (pickup at Universal)
  10:30 AM Arrival MCO → Disney      (pickup at MCO)
```

Chain scores:
- 8:00 MCO→Disney: Drops at Disney. 9:30 Disney return is 15 min later in same area, 10:30 MCO arrival is 75 min after (would need to chain through the 9:30 return). **Chain = 1 (+20)**
- 8:15 MCO→Universal: Drops at Universal. 10:00 Universal return is 40 min later in same area. **Chain = 1 (+20)**

Both have chains. Now combine with location proximity: if the driver is at MCO, both pickups are at MCO (+50 location). The tie-breaker becomes other factors (buffer, tier, flow). But the key point: **neither job is a dead end** — both lead to follow-up work, and the scheduler knows it.

**To tweak**: Search for `Chain bonus` in both functions.
- Jobs at dead-end locations still getting assigned? Increase the 1-chain bonus (+20 → +35).
- Chain awareness too aggressive (overriding better immediate matches)? Reduce all values by 10-15.
- Want to extend the look-ahead window beyond 3 hours? Change `180` in the gap check.
- Want chains only for closer locations? Reduce the `30` min drive threshold to `15`.

---

### 9. Revenue Bonus (Schedule Builder Only)

**What it is**: Higher-paying jobs get a small bonus. Caps at +20 to prevent revenue from dominating other factors.

```
Bonus = min(revenue_share / 10, 20)
```

| Revenue | Bonus |
|---------|-------|
| $50 | +5 |
| $100 | +10 |
| $150 | +15 |
| $200+ | +20 (cap) |

**To tweak**: Search for `Revenue bonus`.
- Want revenue to matter more? Change the divisor (10 → 5) or raise the cap (20 → 40).
- Want revenue to not matter at all? Remove the section.

---

### 10. Load Balance

**What it is**: Drivers with fewer jobs get slightly higher scores, preventing one driver from being overloaded while others sit idle.

```
Penalty = number_of_existing_legs × 5
```

| Existing Legs | Penalty |
|---------------|---------|
| 0 | -0 |
| 1 | -5 |
| 2 | -10 |
| 3 | -15 |
| 4 | -20 |
| 5 | -25 |

**To tweak**: Search for `Load balance`.
- Want more even distribution? Increase the multiplier (5 → 10).
- Want to pack drivers full before moving to the next? Decrease (5 → 2) or remove.

---

## Founder Brain: Value Rules, Class Guard, Evict-to-Farm (2026-06)

Design record: `docs/scheduler-automation/founder-brain-implementation.md`. Four founder
rules now shape the build; everything below is flag-gated and tunable.

### Leg value (`leg_value()`, scheduler.py)

The founder value of keeping a leg in-house. Band widths guarantee the priority order
can never invert: **booked vehicle class** (10,000/tier) › **trip type** (return 3,000,
cruise 2,000, other 1,000, arrival 0) › **revenue_share** (clamped ≤ 999) › **passenger
count** (×0.01, final tiebreak). A Van(14 Pax) booking with ONE passenger outranks a
Van booking with eight — revenue and the coverage obligation follow the BOOKED class.
Used to (a) order legs inside each (pass, hour, type) bucket, (b) rank evict-to-farm
targets, (c) feed the `auto_assign_value_weight` scoring term.

### New SchedulerSettings knobs (migration 0009)

| Knob | Default | Effect |
|------|---------|--------|
| `auto_assign_value_weight` | 1 | Weight of the leg-value scoring term (one class step ≈ 10×weight points). 0 disables. |
| `displacement_min_value_gain` | 500 | Evict-to-farm: min `leg_value(residual) − leg_value(evicted arrival)`. 1,000 ≈ one trip-type step; 10,000 ≈ one class step. Raise to make eviction rarer. |
| `max_displacements_per_run` | 10 | Evict-to-farm: eviction cap per auto-assign run. |
| `type_priority_*` | 0/1/2/3 | Greedy type ordering (see Processing Order above). |

### New module flags (scheduler.py)

| Flag | Default | Effect |
|------|---------|--------|
| `AUTO_EVICT_TO_FARM_PASS` | True | The R1+R2 pass: a residual leg may displace an engine-proposed ARRIVAL when strictly more valuable. True departures (`farmout_optimizer.is_departure`) are never evicted; manual/seeded/pre-existing are locked; every move re-validates the whole chain. Runs after the swap pass, before the span rescue (which re-seats evicted arrivals elsewhere). A second `free_insert_only` sweep runs after trim/gap so no leg stays farmed that fits the final board as-is. |
| `CLASS_MATCH_FIRST` | True | R3 downward: a feasible EXACT-class driver wins the leg outright; higher-class drivers stay as fallback. Pushes towncar work onto the towncar, keeping vans/SUVs free for their own class. |
| `CLASS_MATCH_GUARD` | True | R3 upward, UNCONDITIONAL: a driver is hard-skipped for a lower-class leg when one of his own class's pending jobs would be pushed off his board by it. No "someone else can cover it" escape — that test is optimistic at decision time and stranded the 9:15 V14 port cruise on 6/14. |
| `CHAIN_STATIC_TIMING` | True | Chain feasibility runs on the founder's static planning model: clear = pickup + 45-min dwell + category-table drive (+ live flight DELAY only), reposition = category table. The p75 metric path stays for display estimates but over-priced chains ~10-20 min (MCO→Disney p75 43 vs the founder's 30) and rejected back-to-back days he builds by hand. |
| `ARRIVAL_CLEAR_STATIC_FLOOR` | True | Fallback when `CHAIN_STATIC_TIMING` is off: an arrival's chain clear time can never undercut the static model (the sereen 6:01→7:00 admission). |

### Window semantics change

`get_effective_window` now merges the dispatcher's typed modal window with the stub:
the TIGHTER start/end wins (typed 6-18 beats stub 6-20 — "the modal is authoritative"),
exactly like max_hours.

---

## Global Constants

These affect the entire scheduler, not just scoring:

| Constant | Value | Location | Effect |
|----------|-------|----------|--------|
| `INTER_JOB_BUFFER` | 10 min | Line 74 | Time gap between jobs (break + uncertainty). Increase = more breathing room but fewer jobs per driver. |
| `DEFAULT_DRIVE_TIME` | 35 min | Line 71 | Fallback when no route data exists. Increase = more conservative estimates. |
| `DRIVE_TIME_ESTIMATES` | dict | Lines 20-69 | Hardcoded drive times between location categories. These are fallbacks when route metrics have insufficient data. Includes the hotel↔Port Canaveral pairs at 55 min (added 2026-06 — they previously fell to the 35-min default and scored every to-port chain ~20 min optimistic). |

---

## Score Ranges (What Wins What)

Typical total scores for a well-matched job:

| Scenario | Approximate Score |
|----------|-------------------|
| Perfect fit (same area, right tier, good buffer, scarce, chains) | 350-450 |
| Good fit (close area, right tier, OK buffer, some chains) | 200-350 |
| Acceptable fit (different area, downgraded tier) | 100-200 |
| Poor fit (far away, heavily downgraded, tight buffer) | 50-100 |
| Terrible fit (arrival stacked, far away, loose buffer, dead end) | Under 50 |

In auto-assign, if no driver scores above 0, the job goes to affiliate.

---

## Quick Reference: What to Change for Common Issues

| Problem | What to Adjust | Direction |
|---------|---------------|-----------|
| Returns still going to affiliates | Retention bonus (+25) | Increase to +40-50 |
| Too many arrivals stacked on one driver | Arrival stacking penalties (-40/-15) | Increase to -60/-30 |
| Drivers sitting idle with big gaps | Buffer 120+ score (+50) | Decrease to +20-30 |
| Schedules too tight, drivers rushing | INTER_JOB_BUFFER (10 min) | Increase to 15-20 |
| Big vehicles doing small jobs too often | Tier downgrade scores (+40/+25) | Decrease to +20/+10 |
| Rare vehicle jobs being missed | Scarcity bonus for 1 driver (+80) | Increase to +100 |
| One driver overloaded, others empty | Load balance multiplier (×5) | Increase to ×10 |
| Drivers criss-crossing the map | Location proximity bonus (+50/+35) | Increase to +70/+50 |
| Drivers sent to dead-end locations | Chain bonus (+20/+35/+45) | Increase to +30/+50/+65 |
| Driver gets 1 job when 2-3 were possible | Chain bonus + location proximity | Increase both |

---

## Full Scoring Walkthrough (Example)

Here's a complete scoring example so you can see how all 10 factors combine.

**Setup**: Tomorrow's date, 5 drivers assigned. You click "Auto-Assign" for 12 unassigned legs.

**The leg being scored**: 1:00 PM Arrival, MCO → Disney, SUV reservation, $85 revenue share.

**Driver being evaluated**: Carlos, drives an SUV, currently has 2 jobs:
- 8:00 AM Arrival MCO → Universal (done ~9:20 at Universal)
- 9:45 AM Return Universal → MCO (done ~10:15 at MCO)

Last dropoff: MCO. Next pickup: MCO. Buffer: 1:00 PM - (10:15 + 0 drive + 10 buffer) = **155 min**.

| Factor | Score | Reasoning |
|--------|-------|-----------|
| Buffer quality | +50 | 155 min = big gap (120+ range) |
| Vehicle tier | +60 | SUV driver → SUV job = exact match |
| Scarcity | +15 | 4 drivers can handle SUV jobs (3 SUVs + 1 Van) |
| Location proximity | +50 | Last dropoff = MCO, pickup = MCO (same area) |
| Schedule flow | +0 | Last job was a return, no arrival streak |
| Retention bonus | +0 | This is an arrival, not return/cruise |
| Chain bonus | +35 | 2 unassigned legs near Disney in next 3 hours |
| Load balance | -10 | 2 existing legs × 5 = -10 |
| **Total** | **200** | |

**Compare against Driver B** (Maria, SUV, at Disney, 1 existing leg):

| Factor | Score | Reasoning |
|--------|-------|-----------|
| Buffer quality | +80 | 75 min buffer (60-120 range) |
| Vehicle tier | +60 | Exact match |
| Scarcity | +15 | Same 4 eligible drivers |
| Location proximity | +0 | At Disney, pickup is MCO (30 min away) |
| Schedule flow | +0 | No arrival streak |
| Retention bonus | +0 | Arrival |
| Chain bonus | +35 | Same 2 follow-ups near Disney |
| Load balance | -5 | 1 existing leg |
| **Total** | **185** | |

**Carlos wins (200 vs 185)** — even though Maria has a better buffer and fewer legs, Carlos is already at MCO (pickup location) which gives him the edge. After this job, both would be at Disney with chain opportunities.
