# Auto-Assign Deep Dive — How the Scheduler Actually Works

This document walks through every step of the auto-assign algorithm from the moment you click "Auto-Assign All" to when every job has a driver (or is marked for affiliate). It covers both **Auto-Assign** (`suggest_assignments`) and **Schedule Builder** (`build_smart_schedule`).

---

## The Big Picture (30 Seconds)

The scheduler is a **greedy algorithm with pre-scanning**. Before it assigns a single job, it gathers intelligence about the entire day: which vehicles are rare, where chains of follow-up work exist, and which drivers should be "saved" for matching jobs. Then it processes jobs one at a time, scoring every eligible driver, picking the best, simulating that assignment, and moving to the next job.

```
You click "Auto-Assign All"
         |
         v
  PHASE 1: Sort all unassigned jobs
         |
         v
  PHASE 2: Pre-scan the day (4 intelligence passes)
    2a. Load vehicle assignments (who drives what)
    2b. Compute scarcity map (how rare is each job)
    2c. Compute chain map (what follow-up work exists)
    2d. Compute vehicle reservation counts (who should be saved)
         |
         v
  PHASE 3: Main loop — for each job, score all drivers
         |
         v
  PHASE 4: Assign best driver, simulate it, update counts
         |
         v
  PHASE 5: Next job (repeat 3-4 until done)
```

---

## Phase 1: Sort All Unassigned Jobs (Two-Pass Processing)

**File**: `dispatching/scheduler.py`, inside `suggest_assignments()`

Before scoring anything, the algorithm decides **what order** to process jobs. This matters enormously — since each assignment changes what's available for the next one, processing order directly affects outcomes.

### Two-Pass Sorting

Jobs are sorted into **two passes**. Pass 1 legs are processed BEFORE Pass 2 legs. This ensures specialized drivers get their matching jobs first.

**Pass 1 — Truly Scarce Jobs**: A job goes to Pass 1 if BOTH conditions are met:
1. The number of drivers who ARE this exact vehicle type ≤ `reserve_max_scarcity` (default: 2)
2. The total number of drivers who CAN do this job ≤ half the fleet

**Pass 2 — Everything Else**: Jobs where many drivers are eligible.

**Why two conditions?** Consider mini_van: only 1 driver IS a mini_van, but ALL 8 drivers CAN do mini_van jobs. Processing mini_van first would consume SUV drivers as fallback, starving later towncar/SUV jobs. The "eligible ≤ half fleet" check prevents this.

### Within Each Pass

Jobs are sorted by **three keys**, in this priority:

1. **Hour** — Earlier hours first (8 AM before 9 AM)
2. **Trip type priority** — Within the same hour:
   - Returns first (priority 0)
   - Cruise transfers second (priority 1)
   - Other transfers third (priority 2)
   - Arrivals last (priority 3)
3. **Exact pickup time** — Within same type, earlier time first

```
Initial sort (before pre-scan data is available):
_TYPE_PRIORITY = {'return': 0, 'cruise': 1, 'other': 2, 'arrival': 3}
sort_key = (hour, type_priority, pickup_time)

Two-pass re-sort (applied AFTER pre-scan computes scarcity):
pass_priority = 0 if (exact_count ≤ 2 AND eligible ≤ half_fleet) else 1
final_sort_key = (pass_priority, hour, type_priority, pickup_time)
```

### Why Returns Before Arrivals (Within Each Pass)?

Returns are short (~25-30 min), predictable (no flight delays), and free the driver up quickly. Arrivals are long (~60-75 min) and risky (flight delays can cascade). By processing returns first:

- Drivers get their quick return jobs locked in first
- When arrivals are processed next, drivers already have a "base" of return work
- Returns don't get accidentally blocked by arrivals eating up capacity

### Real Example (Two-Pass)

Fleet: 4 SUVs, 1 Van, 2 Van(14 Pax), 1 MiniVan (8 drivers total, half = 4)

| Job | Type | Pickup | Vehicle | Exact Drivers | Eligible | Pass |
|-----|------|--------|---------|---------------|----------|------|
| A | Return | 9:00 AM | Van | 1 (Alex) | 3 | **1** |
| B | Return | 9:30 AM | Van | 1 | 3 | **1** |
| C | Arrival | 10:32 AM | Van(14 Pax) | 2 (David,Junaid) | 2 | **1** |
| D | Arrival | 8:05 AM | Towncar | 0 (nobody IS towncar) | 8 | 2 |
| E | Return | 8:00 AM | MiniVan | 1 (runer) | 8 | 2 |
| F | Arrival | 8:30 AM | SUV | 4 | 7 | 2 |

Processing order: **A → B → C** (Pass 1) → **D → E → F** (Pass 2)

Alex takes his Van returns (A, B) in Pass 1. By the time Pass 2 starts, Alex is busy with Van jobs and won't compete for the Towncar arrival (D).

---

## Phase 2: Pre-Scanning the Day

This is where the scheduler gathers intelligence BEFORE assigning anything. There are **four pre-computation passes**. Each one builds a lookup table that the main scoring loop uses.

### 2a. Load Vehicle Assignments

**What it does**: One database query to find out which vehicle each driver is assigned for this date.

```
Result: driver_vtypes = {
    driver_5: "van",
    driver_8: "suv",
    driver_12: "towncar",
    driver_15: "Van(14 Pax)",
    ...
}
```

**Why**: Every scoring decision that follows needs to know "what vehicle does this driver have?" Loading it once avoids dozens of repeated database queries.

**Also**: Any driver WITHOUT a vehicle assignment for this date is completely excluded. No vehicle = no jobs.

### 2b. Compute Scarcity Map

**What it does**: For every unassigned job, counts how many drivers have a vehicle that CAN do that job.

**How vehicle compatibility works**: Higher-tier vehicles can do all jobs at their tier and below.

```
Tier hierarchy:
  0: Towncar
  1: MiniVan
  2: SUV
  3: Van
  4: Van(14 Pax)

A Van(14 Pax) driver can do: Towncar, MiniVan, SUV, Van, Van(14 Pax) jobs
An SUV driver can do: Towncar, MiniVan, SUV jobs
A Towncar driver can do: Towncar jobs only
```

**Example fleet**: 2 Towncars, 2 SUVs, 1 Van, 1 Van(14 Pax)

| Job Vehicle Type | Who Can Do It | Scarcity Count |
|------------------|--------------|----------------|
| Towncar | All 6 drivers | 6 |
| MiniVan | 2 SUVs + 1 Van + 1 Van(14 Pax) | 4 |
| SUV | 2 SUVs + 1 Van + 1 Van(14 Pax) | 4 |
| Van | 1 Van + 1 Van(14 Pax) | 2 |
| Van(14 Pax) | 1 Van(14 Pax) only | 1 |

```
Result: scarcity_map = {
    leg_101: 6,     # Towncar job, any driver
    leg_102: 4,     # SUV job
    leg_103: 1,     # Van(14 Pax) job — only 1 driver!
    leg_104: 2,     # Van job — only 2 drivers
    ...
}
```

**Why this matters**: During scoring, a job with scarcity=1 gets +80 points. Scarcity=2 gets +50. This pushes the scheduler to assign rare-vehicle jobs first and to the right driver.

### 2c. Compute Chain Map

**What it does**: For every unassigned job, looks at ALL other unassigned jobs and asks: "If a driver finishes THIS job, are there other jobs nearby they could do next?"

**How "nearby" is determined**: Two conditions must BOTH be true:

1. **Close enough**: The other job's pickup is within `chain_drive_threshold` minutes drive of this job's dropoff (default: 30 min). Same-area = 0 min drive.
2. **Right timing**: The other job starts between `chain_time_min` (default: 10 min) and `chain_time_max` (default: 180 min = 3 hours) after this job ends.

**Step-by-step for one job**:

```
Job A: 8:00 AM Arrival MCO → Disney (estimated end: 9:15 AM, dropoff at Disney)

Check every other unassigned job:
  Job B: 9:30 AM Return Disney → MCO
    - Drive Disney → Disney = 0 min (same area) ✓ under 30 min threshold
    - Gap: 9:30 AM - 9:15 AM = 15 min ✓ between 10-180 min
    - COUNT IT! chain_count = 1

  Job C: 10:00 AM Return Disney → MCO
    - Drive Disney → Disney = 0 min ✓
    - Gap: 10:00 AM - 9:15 AM = 45 min ✓
    - COUNT IT! chain_count = 2

  Job D: 10:30 AM Arrival MCO → Universal
    - Drive Disney → MCO = 30 min ✓ exactly at threshold
    - Gap: 10:30 AM - 9:15 AM = 75 min ✓
    - COUNT IT! chain_count = 3

  Job E: 2:00 PM Arrival SFB → Port Canaveral
    - Drive Disney → SFB = 60 min ✗ over 30 min threshold
    - SKIP

Result: chain_map[Job A] = 3
```

```
Result: chain_map = {
    leg_A: 3,   # 3 follow-up jobs near Disney after this one
    leg_B: 2,   # 2 follow-ups near MCO
    leg_C: 1,   # 1 follow-up
    leg_D: 0,   # dead end — nothing near Universal afterwards
    ...
}
```

**Why this matters**: During scoring, a job with chain_count=3 gets +45 points. Chain=0 gets +0. This steers drivers toward productive chains rather than dead-end locations.

### 2d. Compute Vehicle Reservation Counts

**THIS IS THE NEW PRE-SCAN** that solves the "Van driver doing Towncar jobs while Van jobs wait" problem.

**What it does**: For each driver, counts how many unassigned jobs:
1. Match their EXACT vehicle type (not just compatible — exact match), AND
2. Are "rare" — the number of drivers with that **exact** vehicle type is ≤ `reserve_max_scarcity` (default: 2)

**Why "exact type count" not "compatible count"**: The general scarcity map counts all drivers who CAN do the job (including higher-tier vehicles). For example, a Van job might show scarcity=3 because 1 Van + 2 Van(14 Pax) can all do it. But the reservation system needs to ask: "How many drivers actually ARE Vans?" Answer: just 1. That lone Van driver should be saved for Van jobs, even though Van(14 Pax) drivers could theoretically help.

**Step 1: Count exact types across fleet**:
```
Fleet:
  Alex: Van              → exact_type_counts["Van"] = 1
  Maria: SUV             → exact_type_counts["SUV"] = 1
  David: Van(14 Pax)     → exact_type_counts["Van(14 Pax)"] = 2
  Junaid: Van(14 Pax)    ↗

Result: {"Van": 1, "SUV": 1, "Van(14 Pax)": 2}
```

**Step 2: For each driver, count matching jobs where exact_type_count ≤ threshold**:
```
For Alex (Van driver, exact Van count = 1, threshold = 2):
  - Job 1: Towncar ≠ Van → skip
  - Job 2: Van == Van ✓, exact_count=1 ≤ 2 ✓ → count!
  - Job 3: Van(14 Pax) ≠ Van → skip
  - Job 4: Towncar ≠ Van → skip
  - Job 5: Van == Van ✓, exact_count=1 ≤ 2 ✓ → count!
  Alex reserved_count = 2

For Maria (SUV driver, exact SUV count = 1):
  - No jobs are SUV type → reserved_count = 0

For David (Van 14 Pax driver, exact Van(14 Pax) count = 2):
  - Job 3: Van(14 Pax) == Van(14 Pax) ✓, exact_count=2 ≤ 2 ✓ → count!
  David reserved_count = 1
```

```
Result: driver_reserved_count = {
    Alex: 2,     # 2 Van jobs waiting, only 1 Van driver exists
    Maria: 0,    # no scarce exact-match jobs
    David: 1,    # 1 Van(14 Pax) job waiting, only 2 Van(14 Pax) drivers exist
    Junaid: 1,   # same as David
}
```

**How this affects scoring — HARD SKIP with FALLBACK**:

When a driver with reserved_count > 0 is being scored for a job that DOESN'T match their vehicle (they'd be "downgrading"), they are **hard-skipped** — they go into a separate fallback pool, not the primary candidate pool.

The algorithm tracks TWO sets of candidates per job:
1. **Primary pool**: Non-reserved drivers (or drivers whose exact type matches the job)
2. **Fallback pool**: Reserved-mismatch drivers (with -60 penalty applied for ranking)

If ANY primary candidate exists → they win (even if a fallback scores higher).
If NO primary candidate exists → best fallback is used (prevents jobs going unassigned).

**Example**: Carlos (Van, reserved=2) is being scored for a Towncar arrival:
- Carlos's vehicle tier = 3 (Van)
- Job's vehicle tier = 0 (Towncar)
- Tier difference: 3 > 0 (driver is downgrading)
- Reserved count: 2 > 0 (Van jobs are waiting)
- **Result**: Carlos goes to FALLBACK pool (not primary)

If Maria (SUV, reserved=0) is also feasible → Maria wins from primary pool (Carlos never competes).
If Maria is NOT feasible (busy) → Carlos used from fallback pool (better than leaving job unassigned).

**Why hard skip instead of just a penalty?** Because within a busy hour, ALL non-reserved drivers may be consumed by earlier jobs (returns/other process before arrivals). If only reserved drivers remain, the -60 penalty is irrelevant — they all have it, so the highest-scored reserved driver still wins. The hard skip ensures they're truly a last resort.

**Dynamic updating**: After each assignment in the main loop, if the assigned job was a scarce job, ALL drivers whose vehicle matches that job type get their reserved_count decremented by 1. This keeps the counts accurate as jobs get assigned.

```
Example flow:
1. Job 2 (Van, scarcity=2) gets assigned to Carlos
2. System checks: Was this a scarce job? scarcity=2 ≤ 2 → yes
3. For all drivers with Van type: Carlos reserved_count 2→1
4. Next scoring round: Carlos now has reserved_count=1 (still penalized for non-Van jobs, but less so)
5. Job 5 (Van, scarcity=2) gets assigned to Carlos
6. Carlos reserved_count 1→0
7. Now Carlos can take Towncar jobs without penalty — his Van jobs are done
```

---

## Phase 3: Main Scoring Loop

Now the algorithm processes each job (in the sorted order from Phase 1) and scores every eligible driver.

### For Each Unassigned Job:

```
for each job in sorted_legs:    # (Pass 1 legs first, then Pass 2)
    best_score = -1
    best_driver = None
    best_reserved_score = -1    # fallback pool
    best_reserved_driver = None

    for each driver in working_schedules:

        1. VEHICLE COMPATIBILITY CHECK — Can this driver's vehicle handle this job?
           If not → skip this driver entirely

        2. FEASIBILITY CHECK — Can this driver physically fit this job?
           - Does it overlap with existing jobs?
           - Is there enough time after the previous job (drive + buffer)?
           - Is there enough time before the next job?
           If not feasible → skip this driver

        3. RESERVED MISMATCH CHECK — Is this driver's type > job type AND reserved_count > 0?
           If yes → mark as reserved_mismatch (goes to fallback pool)

        4. SCORE CALCULATION — Add up all factors:

           a. Buffer Quality         (how much spare time between jobs)
           b. Vehicle Tier Match     (exact match vs downgrading)
           c. Scarcity Bonus         (how rare is this job)
           d. Location Proximity     (is driver already nearby)
           e. Schedule Flow          (arrival stacking penalty)
           f. In-House Retention     (bonus for returns/cruise)
           g. Chain Bonus            (follow-up work available)
           h. Load Balance           (penalty for already-busy drivers)

        5. If reserved_mismatch:
               score += reserve_penalty (-60)
               Track in fallback pool (best_reserved_driver)
           Else:
               Track in primary pool (best_driver)

    # Prefer primary pool; use fallback only if no primary candidate
    if best_driver:
        → Assign job to best_driver (simulated)
    elif best_reserved_driver:
        → Assign job to best_reserved_driver (fallback)
    else:
        → Mark job as "No in-house driver available" (goes to affiliate)

    → Update driver's schedule
    → Update vehicle reservation counts
```

### The 11 Scoring Factors (In Detail)

#### a. Buffer Quality (+30 to +120 points)

The gap between the driver's previous job ending and this job's pickup time, minus repositioning drive time and the inter-job buffer.

```
Buffer = pickup_time - (previous_job_end + reposition_drive + inter_job_buffer)

Example:
  Previous job ends: 8:30 AM at Disney
  This job picks up: 9:45 AM at MCO
  Reposition drive: Disney → MCO = 30 min
  Inter-job buffer: 5 min
  Buffer = 9:45 - (8:30 + 0:30 + 0:05) = 9:45 - 9:05 = 40 min
```

| Buffer Range | Auto-Assign Points | Schedule Builder Points |
|---|---|---|
| 20-30 min (perfect) | +120 | +35 |
| 30-60 min (comfortable) | +100 | +30 |
| 10-20 min (tight) | +70 | +15 |
| 60-120 min (loose) | +80 | +20 |
| 120+ min (very loose) | +50 | — |
| Under 10 min (risky) | +30 | — |
| First job (no prior) | — | +25 |

#### b. Vehicle Tier Match (+10 to +60 points)

How closely the driver's vehicle matches the job's required vehicle.

| Tier Difference | Points |
|---|---|
| 0 (exact match) | +60 |
| 1 down | +40 |
| 2 down | +25 |
| 3 down | +15 |
| 4 down | +10 |

```
Example: Van driver (tier 3) doing a Towncar job (tier 0) = 3 tiers down = +15 points
         SUV driver (tier 2) doing an SUV job (tier 2) = exact match = +60 points
```

#### c. Scarcity Bonus (+0 to +80 points)

How many drivers could do this job. Fewer eligible = higher bonus.

| Eligible Drivers | Points |
|---|---|
| 1 | +80 |
| 2 | +50 |
| 3 | +30 |
| 4 | +15 |
| 5+ | +0 |

#### d. Location Proximity (+0 to +50 points)

Where the driver currently is relative to the job's pickup.

| Situation | Points |
|---|---|
| Same area (last dropoff = this pickup area) | +50 |
| Close (reposition drive ≤ 15 min) | +30 |
| First job (no prior location) | +40 |
| Far away | +0 |

#### e. Schedule Flow (-40 to +30 points)

Prevents stacking multiple arrivals back-to-back. Rewards alternating between arrivals and returns.

The algorithm counts consecutive arrivals at the END of the driver's current schedule:

```
Driver's schedule: [Return, Arrival, Arrival]
                                ↑        ↑
                         consecutive_arrivals = 2

Adding another arrival? → -40 penalty (3rd in a row)
Adding a return instead? → +30 bonus (breaks the streak)
```

| Situation | Points |
|---|---|
| 3rd+ arrival in a row | -40 |
| 2nd arrival in a row | -15 |
| Return/cruise breaking arrival streak | +30 |

#### f. In-House Retention (+0 or +25 points)

Flat bonus for returns and cruise transfers. These are predictable, short jobs that should stay in-house rather than being farmed to affiliates.

| Trip Type | Points |
|---|---|
| Return | +25 |
| Cruise | +25 |
| Arrival | +0 |
| Other | +0 |

#### g. Chain Bonus (+0 to +45 points)

Uses the pre-computed chain map from Phase 2c. How many follow-up jobs exist near this job's dropoff?

| Follow-up Jobs | Points |
|---|---|
| 3+ | +45 |
| 2 | +35 |
| 1 | +20 |
| 0 | +0 |

#### h. Vehicle Reservation — Hard Skip + Fallback

Uses the pre-computed reservation counts from Phase 2d. When BOTH conditions are true:
- The driver's vehicle tier > the job's vehicle tier (they're downgrading)
- The driver has reserved_count > 0 (scarce matching jobs are waiting)

The driver is **moved to the fallback pool** instead of competing with primary candidates. They still get scored (with -60 penalty for ranking among fallbacks), but they can only win if NO non-reserved driver is feasible.

```
If (driver_tier > job_tier) AND (driver_reserved_count > 0):
    → Driver goes to fallback pool (not primary)
    → Score gets reserve_penalty (-60) for fallback ranking
    → Only used if no primary candidate found
```

#### i. Load Balance (-5 per existing job)

Simple penalty that increases with each job already on the driver's schedule.

```
Penalty = existing_leg_count × load_balance_multiplier

3 existing jobs × 5 = -15 points
```

---

## Phase 4: Assignment Simulation

After the best driver is found for a job, the algorithm doesn't just record the assignment — it **simulates** it by updating the working copy of the driver's schedule. This is critical because each assignment changes what's feasible for the next job.

### What Gets Updated:

1. **Driver's schedule** — A simulated ScheduleSlot is added with the job's pickup time, locations, estimated end time. The slots are re-sorted by pickup time.

2. **Vehicle reservation counts** — If the job just assigned was a scarce job (scarcity ≤ `reserve_max_scarcity`), all drivers whose vehicle type matches that job get their reserved_count decremented by 1.

```
Example simulation:

Before assignment:
  Carlos: [8:00 AM MCO→Disney, 9:30 AM Disney→MCO]  (2 slots)
  Maria: [8:15 AM MCO→Universal]  (1 slot)

Job being assigned: 10:30 AM MCO→Disney (Towncar, scarcity=6)
Best driver: Maria (score 245)

After assignment:
  Carlos: [8:00 AM MCO→Disney, 9:30 AM Disney→MCO]  (2 slots, unchanged)
  Maria: [8:15 AM MCO→Universal, 10:30 AM MCO→Disney]  (2 slots now!)

Next job scored: The feasibility check for Maria now includes her 10:30 job.
If the next job is at 10:45 AM, Maria might not be feasible anymore.
```

### Why Simulation Matters

Without simulation, the scheduler would think every driver has their original schedule and might assign 5 jobs to the same driver without realizing they overlap. The simulated schedule is what makes the greedy algorithm work — each assignment is immediately reflected.

---

## Phase 5: Repeat Until Done

The loop continues through every unassigned job in the sorted order. Jobs where no driver scores above -1 are marked as "No in-house driver available" and flagged for affiliate assignment.

---

## Complete Walk-Through: Real Scenario

Let's trace through the exact scenario that caused the Van/Towncar bug (before the vehicle reservation fix).

### Fleet
| Driver | Vehicle | Tier |
|--------|---------|------|
| Carlos | Van | 3 |
| Maria | SUV | 2 |
| John | Towncar | 0 |

### Unassigned Jobs (sorted)
| Order | Job | Time | Type | Vehicle | Scarcity |
|-------|-----|------|------|---------|----------|
| 1 | B | 9:00 AM | Return | Van | 2 (Carlos, John*) |
| 2 | D | 9:30 AM | Return | Van | 2 |
| 3 | A | 8:05 AM | Arrival | Towncar | 3 (all) |
| 4 | C | 8:55 AM | Arrival | MiniVan | 2 (Maria, Carlos) |

*Note: Within hour 8, returns sort before arrivals. But Job B/D are hour 9, so they actually come after hour 8 jobs. Let me re-sort properly:

**Correct sort order**:
1. Hour 8, Arrival priority=3, 8:05 AM → Job A (Towncar Arrival 8:05 AM)
2. Hour 8, Arrival priority=3, 8:55 AM → Job C (MiniVan Arrival 8:55 AM)
3. Hour 9, Return priority=0, 9:00 AM → Job B (Van Return 9:00 AM)
4. Hour 9, Return priority=0, 9:30 AM → Job D (Van Return 9:30 AM)

### Pre-Scan Results

**Scarcity map**:
- Job A (Towncar): 3 eligible (all drivers)
- Job B (Van): 2 eligible (Carlos + John can Van-tier, Maria can't)

Wait — let me recalculate. Van(tier 3) includes Towncar, MiniVan, SUV, Van. SUV(tier 2) includes Towncar, MiniVan, SUV. Towncar(tier 0) includes Towncar only.

- Job A (Towncar): All 3 can do Towncar → scarcity=3
- Job B (Van): Carlos(Van) can, Maria(SUV) can't (SUV tier 2 < Van tier 3), John(Towncar) can't → scarcity=1
- Job C (MiniVan): Carlos(Van) can, Maria(SUV) can, John(Towncar) can't → scarcity=2
- Job D (Van): Same as Job B → scarcity=1

**Chain map** (simplified):
- Job A (8:05 Arrival → done ~9:20 at Disney): Jobs B (9:00 Van Return at Disney) is 0 drive, gap=-20 min (before end, doesn't count). Job D (9:30 at Disney) gap=10 min ✓ → chain=1
- Job B (9:00 Return → done ~9:30 at MCO): Job D (9:30 at Disney) drive MCO→Disney=30 min, gap=0 (too soon) → chain=0
- etc.

**Vehicle reservation counts**:
- Carlos (Van): Job B (Van, scarcity=1 ≤ 2) ✓, Job D (Van, scarcity=1 ≤ 2) ✓ → reserved=2
- Maria (SUV): No SUV jobs → reserved=0
- John (Towncar): No Towncar jobs with scarcity ≤ 2 → reserved=0 (Job A is Towncar but scarcity=3 > 2)

### Main Loop

**Processing Job A** (8:05 AM Towncar Arrival):

| Driver | Feasible? | Buffer | Tier Match | Scarcity | Location | Flow | Retention | Chain | Reserve Penalty | Load Bal | Total |
|--------|-----------|--------|-----------|----------|----------|------|-----------|-------|-----------------|----------|-------|
| Carlos | Yes | +50 (loose) | +15 (3 down) | +30 (3 eligible) | +40 (first job) | +0 | +0 | +20 (1 chain) | **-60** (Van reserved=2, downgrading) | -0 | **95** |
| Maria | Yes | +50 (loose) | +25 (2 down) | +30 | +40 | +0 | +0 | +20 | +0 (reserved=0) | -0 | **165** |
| John | Yes | +50 (loose) | +60 (exact) | +30 | +40 | +0 | +0 | +20 | +0 (reserved=0) | -0 | **200** |

**John wins (200)** — exact vehicle match, no reservation penalty. Carlos gets -60 because his Van jobs are waiting.

**Without the reservation system**, Carlos would have scored 155 (no -60) and might have beaten John if other factors differed slightly. That's how the Van driver used to end up doing Towncar arrivals.

**Processing Job C** (8:55 AM MiniVan Arrival):
- Carlos: Still has reserved=2, MiniVan tier=1 < Van tier=3 → -60 penalty
- Maria: SUV doing MiniVan = 1 tier down, reserved=0 → no penalty, gets +40 tier

**Maria wins** — Carlos is still being saved for Van jobs.

**Processing Job B** (9:00 AM Van Return):
- Carlos: Van job = exact match! No reservation penalty (tier diff = 0). Gets +60 tier + +80 scarcity(1) + +25 retention
- Maria: Can't do Van jobs (SUV tier < Van tier) → skipped
- John: Can't do Van jobs (Towncar tier < Van tier) → skipped

**Carlos wins** — only eligible driver. reserved_count decrements: 2→1.

**Processing Job D** (9:30 AM Van Return):
- Carlos: Same as above. reserved_count is now 1, still exact match so no penalty.

**Carlos wins** again. reserved_count: 1→0. Now Carlos has no more reserved Van jobs.

### Final Result

| Driver | Jobs | Correct? |
|--------|------|----------|
| John (Towncar) | 8:05 AM Towncar Arrival | Yes — matched vehicle |
| Maria (SUV) | 8:55 AM MiniVan Arrival | Yes — closest match |
| Carlos (Van) | 9:00 AM Van Return, 9:30 AM Van Return | Yes — saved for his Van jobs! |

---

## Schedule Builder — How It Differs

The Schedule Builder (`build_smart_schedule`) does the same thing but for **one driver at a time**. A dispatcher clicks a driver and says "build me the best schedule for Carlos."

### Key Differences

| Aspect | Auto-Assign | Schedule Builder |
|--------|-------------|-----------------|
| **Scope** | All drivers, all jobs | One driver, all available jobs |
| **Processing order** | Returns before arrivals | Highest-tier vehicle jobs first |
| **Scarcity** | How many drivers total | How many OTHER drivers (excludes this driver) |
| **Extra factors** | — | Trip type preference, revenue bonus |
| **Pinned legs** | Not supported | Can pin specific legs as "must include" |
| **Time window** | Full day | Configurable start/end hours |

### Builder Processing Order

Instead of returns-first, the builder sorts jobs by **vehicle tier descending**:

```
def _leg_tier_sort_key(leg):
    tier = get_vehicle_tier(leg.vehicle_type)
    return (-tier, leg.pickup_time)  # highest tier first
```

This means: Van(14 Pax) jobs → Van jobs → SUV jobs → MiniVan jobs → Towncar jobs.

**Why**: When building for a specific driver, you want to fill their schedule with the highest-value vehicle matches first, then backfill with lower-tier jobs.

### Builder Scoring

The builder uses the same 11 factors but with different point values (prefixed `sb_` in the tuning panel). The builder also adds:

- **Trip type preference**: +40 if job matches, -10 if not
- **Revenue bonus**: min(revenue / 10, 20) — higher-paying jobs get a small boost
- **Base score**: Every job starts at +50 (auto-assign starts at 0)

### Builder Vehicle Reservation

The builder also pre-computes reservation count, but only for THIS driver:

```
reserved_count = 0
for each unassigned job:
    if job.vehicle_type == driver's vehicle type:
        if scarcity for this job ≤ reserve_max_scarcity:
            reserved_count += 1
```

If the builder is considering a lower-tier job for this driver and reserved_count > 0, the -60 penalty applies — steering the builder toward the driver's matching jobs first.

---

## How Feasibility Checking Works

Before any scoring happens, the scheduler checks: "Can this driver physically do this job?"

### What It Checks

1. **No existing jobs** → Always feasible (buffer = 999, "Available - no jobs yet")

2. **Has existing jobs** → Find the preceding slot (last job before this pickup time) and following slot (first job after):

```
Driver's schedule: [Job at 8:00, Job at 10:00, Job at 1:00 PM]
New job: 11:30 AM

Preceding: Job at 10:00 (the one right before 11:30)
Following: Job at 1:00 PM (the one right after 11:30)
```

3. **Check against preceding job**:
```
reposition_drive = drive_time(preceding.dropoff → new.pickup)
earliest_available = preceding.end_time + reposition_drive + inter_job_buffer
buffer = new.pickup_time - earliest_available

If buffer < 0 → NOT FEASIBLE ("Needs X more minutes")
If buffer < 15 → Warning: "Tight schedule"
```

4. **Check against following job**:
```
reposition_drive = drive_time(new.dropoff → following.pickup)
earliest_for_next = new.end_time + reposition_drive + inter_job_buffer
following_buffer = following.pickup_time - earliest_for_next

If following_buffer < 0 → NOT FEASIBLE ("Conflicts with next job")
```

5. **Final buffer** = minimum of preceding buffer and following buffer

### Example

```
Driver schedule:
  8:00 AM: MCO → Disney (ends ~8:50 at Disney)
  11:00 AM: Disney → MCO (starts at 11:00)

New job: 9:30 AM pickup at MCO

Against preceding (8:00 job):
  Reposition: Disney → MCO = 30 min
  Earliest: 8:50 + 30 + 5 = 9:25 AM
  Buffer: 9:30 - 9:25 = 5 min ← tight but feasible

Against following (11:00 job):
  New job ends: 9:30 + 30 (MCO→Disney drive) = 10:00 AM
  Reposition: Disney → Disney = 12 min
  Earliest for next: 10:00 + 12 + 5 = 10:17 AM
  Following buffer: 11:00 - 10:17 = 43 min ← comfortable

Final buffer: min(5, 43) = 5 min
Result: Feasible with warning "Tight: 5min after previous job"
```

---

## How Job End Times Are Estimated

The scheduler needs to know "when will this job be done?" to calculate buffers.

### For Arrivals (Airport Pickups)
```
end_time = pickup_time + airport_dwell_time + drive_time

Airport dwell = time from flight landing to passenger in car
              = baggage claim + customs + walking + loading
              ≈ 45 min default, or P75 from historical data

Example: 8:00 AM arrival MCO → Disney
  Dwell: 45 min (baggage claim etc.)
  Drive: 30 min
  End: 8:00 + 45 + 30 = 9:15 AM
```

### For All Other Trips (Returns, Cruise, Other)
```
end_time = pickup_time + drive_time

Example: 9:30 AM return Disney → MCO
  Drive: 30 min
  End: 9:30 + 30 = 10:00 AM
```

### Drive Time Lookup Priority
1. In-memory cache (pre-loaded RouteTimingMetric P75 values)
2. Database query for RouteTimingMetric with ≥ 5 samples
3. Hardcoded DRIVE_TIME_ESTIMATES dictionary (~35 Orlando-area routes)
4. Default fallback: 35 minutes

---

## Tuning Parameter Reference

All parameters are configurable from the Tuning panel in the Capacity Planner (gear icon). They're stored in the `SchedulerSettings` singleton model.

### Buffer Quality (Auto-Assign)
| Parameter | Default | What It Does |
|-----------|---------|-------------|
| `buffer_perfect` | 120 | Points for 20-30 min buffer |
| `buffer_sweet_spot` | 100 | Points for 30-60 min buffer |
| `buffer_good` | 80 | Points for 60-120 min buffer |
| `buffer_tight` | 70 | Points for 10-20 min buffer |
| `buffer_loose` | 50 | Points for 120+ min buffer |
| `buffer_risky` | 30 | Points for under 10 min buffer |

### Buffer Quality (Schedule Builder)
| Parameter | Default | What It Does |
|-----------|---------|-------------|
| `sb_buffer_perfect` | 35 | Points for 20-30 min buffer |
| `sb_buffer_sweet_spot` | 30 | Points for 30-60 min buffer |
| `sb_buffer_good` | 20 | Points for 60-120 min buffer |
| `sb_buffer_first_job` | 25 | Points for first job (no prior) |
| `sb_buffer_tight` | 15 | Points for 10-20 min buffer |

### Vehicle Tier Match
| Parameter | Default | What It Does |
|-----------|---------|-------------|
| `tier_exact` | 60 | Points for exact vehicle match |
| `tier_1_down` | 40 | Points for 1 tier below |
| `tier_2_down` | 25 | Points for 2 tiers below |
| `tier_3_down` | 15 | Points for 3 tiers below |
| `tier_4_down` | 10 | Points for 4 tiers below |

### Scarcity
| Parameter | Default | What It Does |
|-----------|---------|-------------|
| `scarcity_1` | 80 | Points when only 1 driver can do job |
| `scarcity_2` | 50 | Points when 2 drivers eligible |
| `scarcity_3` | 30 | Points when 3 drivers eligible |
| `scarcity_4` | 15 | Points when 4 drivers eligible |

### Location Proximity (Auto-Assign)
| Parameter | Default | What It Does |
|-----------|---------|-------------|
| `loc_same_area` | 50 | Points when last dropoff = next pickup |
| `loc_close` | 30 | Points when reposition ≤ 15 min |
| `loc_first_job` | 40 | Points for driver's first job |

### Location Proximity (Schedule Builder)
| Parameter | Default | What It Does |
|-----------|---------|-------------|
| `sb_loc_same_area` | 35 | Points when same area |

### Schedule Flow (Auto-Assign)
| Parameter | Default | What It Does |
|-----------|---------|-------------|
| `flow_3rd_arrival` | -40 | Penalty for 3rd+ arrival in a row |
| `flow_2nd_arrival` | -15 | Penalty for 2nd arrival in a row |
| `flow_break_bonus` | 30 | Bonus for breaking arrival streak |

### Schedule Flow (Schedule Builder)
| Parameter | Default | What It Does |
|-----------|---------|-------------|
| `sb_flow_3rd_arrival` | -35 | Penalty for 3rd+ arrival in a row |
| `sb_flow_2nd_arrival` | -10 | Penalty for 2nd arrival in a row |
| `sb_flow_break_bonus` | 25 | Bonus for breaking arrival streak |

### In-House Retention
| Parameter | Default | What It Does |
|-----------|---------|-------------|
| `retention_bonus` | 25 | Bonus for return/cruise jobs |

### Chain Awareness
| Parameter | Default | What It Does |
|-----------|---------|-------------|
| `chain_3_plus` | 45 | Points for 3+ follow-up jobs nearby |
| `chain_2` | 35 | Points for 2 follow-up jobs |
| `chain_1` | 20 | Points for 1 follow-up job |
| `chain_drive_threshold` | 30 | Max drive minutes to count as "nearby" |
| `chain_time_min` | 10 | Minimum gap minutes for chain |
| `chain_time_max` | 180 | Maximum gap minutes for chain (3 hours) |

### Vehicle Reservation
| Parameter | Default | What It Does |
|-----------|---------|-------------|
| `reserve_penalty` | -60 | Penalty when rare vehicle takes mismatched job |
| `reserve_max_scarcity` | 2 | Max eligible drivers for job to count as "needs saving" |

### Load Balance
| Parameter | Default | What It Does |
|-----------|---------|-------------|
| `load_balance_multiplier` | 5 | Penalty per existing job on driver |

### Global
| Parameter | Default | What It Does |
|-----------|---------|-------------|
| `inter_job_buffer` | 5 | Minutes between jobs (buffer + break) |

### Builder Extras
| Parameter | Default | What It Does |
|-----------|---------|-------------|
| `base_score` | 50 | Starting score for each candidate |
| `trip_pref_match` | 40 | Bonus when job matches preferred type |
| `trip_pref_mismatch` | -10 | Penalty when job doesn't match |
| `revenue_divisor` | 10 | Revenue / this = bonus points |
| `revenue_cap` | 20 | Max revenue bonus points |

---

## Common Scenarios and What the Scheduler Does

### Scenario 1: Morning Rush with Mixed Vehicles

**Fleet**: 1 Van, 2 SUVs, 1 Towncar
**Jobs**: 3 Towncar arrivals (8-9 AM), 2 Van returns (9-9:30 AM), 1 SUV arrival (8:30 AM)

**What happens**:
1. Pre-scan: Van driver gets reserved_count=2 (2 Van returns, scarcity=1)
2. Towncar arrivals process first (hour 8, arrivals). Van driver gets -60 for each, Towncar/SUV drivers preferred
3. Van returns process next (hour 9, returns). Van driver is the only one eligible → gets them
4. Result: Towncar/SUV drivers handle arrivals, Van driver does Van returns

### Scenario 2: One Driver At MCO, Three Disney Pickups

**Jobs**: 9:00, 9:15, 9:30 — all arrivals at MCO → Disney

**What happens**:
1. Driver at MCO gets massive location bonus (+50) for first job
2. After first assignment, driver is simulated at Disney (done ~10:15)
3. For second MCO job, driver needs Disney → MCO = 30 min reposition. Might not be feasible if buffer < 0
4. If feasible, gets arrival stacking penalty (-15 for 2nd, -40 for 3rd)
5. Likely: Driver gets 1-2 MCO jobs, the rest go to other drivers or affiliates

### Scenario 3: Dead-End Location

**Driver A** can do: MCO → Port Canaveral (ends at Port Canaveral, nothing nearby for 3 hours)
**Driver A** can also do: MCO → Disney (ends at Disney, 2 returns waiting)

**What happens**:
- MCO → Disney gets chain_count=2 → +35 bonus
- MCO → Port Canaveral gets chain_count=0 → +0
- Chain bonus steers Driver A to the Disney job, keeping them productive
- Port Canaveral job goes to another driver or affiliate

---
**VAN ISSUE FIX**
# Plan: Fix Vehicle Reservation — COMPLETED

## Problem

Alex (the only Van driver) was being assigned to 8:05 AM Towncar arrivals while 3 Van returns at 9:00-9:30 AM went unassigned. Result: 11 unassigned legs.

## Root Causes (Two Issues)

### Issue 1: Reservation used wrong scarcity metric
The reservation pre-scan used `scarcity_map` (counts all COMPATIBLE drivers). Van scarcity = 3 (Alex+David+Junaid), which exceeded `reserve_max_scarcity` of 2, so reservation never triggered.

**Fix**: Use `exact_type_driver_counts` (how many drivers ARE that exact type). Van exact = 1 (only Alex IS a Van). 1 ≤ 2 → reservation triggers.

### Issue 2: Reservation penalty was too soft + processing order consumed SUV drivers first
Even with the exact-type fix, the -60 penalty wasn't enough. Within each hour, returns/other are processed BEFORE arrivals. By the time the 8:05 arrival was scored, all 4 SUV drivers were already consumed by hour-8 returns/other jobs. Only reserved drivers remained, so the penalty was irrelevant (all candidates had it).

**Fix (two parts)**:

**a) Hard skip with fallback**: Instead of just applying a -60 penalty, reserved-mismatch drivers are SEPARATED into a fallback pool. Non-reserved drivers are always preferred. Fallback drivers are only used if no non-reserved driver is feasible.

**b) Two-pass processing order**: Legs with truly scarce vehicle types (Van, Van14) are processed FIRST, before general legs. This ensures specialized drivers get their matching jobs before being consumed. A leg is "truly scarce" if:
- Exact-type driver count ≤ `reserve_max_scarcity` (few drivers ARE this type)
- Total eligible drivers ≤ half the fleet (few drivers CAN do this type)

This correctly excludes mini_van from Pass 1 (1 exact driver, but ALL 8 drivers are eligible).

## Changes Made (dispatching/scheduler.py)

### 1. Exact-type reservation counting (~line 614)
Pre-counts `exact_type_driver_counts` from `driver_vtypes`. Used for both reservation triggering and two-pass sort.

### 2. Hard skip with fallback (~lines 642-778)
- Added `best_reserved_*` tracking variables alongside `best_*` primary candidates
- Added `is_reserved_mismatch` detection after feasibility check
- Reserved-mismatch drivers go to fallback pool; non-reserved go to primary pool
- After scoring all drivers: use primary if available, fallback only if no primary exists

### 3. Two-pass processing order (~line 638-672)
- Re-sorts `sorted_legs` after computing scarcity and exact counts
- Pass 1: scarce exact-type legs (van, Van14) processed first
- Pass 2: general legs (suv, towncar, mini_van) processed normally
- Within each pass, original sort order preserved (hour, type priority, time)

### 4. Post-assignment reservation decrement (~line 817)
Uses `exact_type_driver_counts` instead of scarcity_map for decrement threshold.

### 5. build_smart_schedule() (~line 1047)
Same exact-type counting pattern for the single-driver schedule builder.

## Verified Results (2/11 Schedule Simulation)

| Metric | Before | After |
|--------|--------|-------|
| Assigned | 57 | **58** |
| Unassigned | 11 | **10** |
| Van returns assigned | 0/3 | **3/3** |
| Alex's exact Van matches | 0 | **4** |
| Van14 exact matches | 1/3 | **2/3** |

Alex's new schedule: 9:00 Van return [EXACT], 11:20 Van arrival [EXACT], 1:30 Van arrival [EXACT], 3:59 Van arrival [EXACT], plus 3 general jobs.

## Verification Steps

1. **Restart the server** to pick up code changes
2. **Reset** the 2/11 schedule
3. **Re-run Auto-Assign**
4. Confirm Alex gets Van returns at 9:00, not Towncar arrivals at 8:05


## File Reference

| File | Function | Purpose |
|------|----------|---------|
| `dispatching/scheduler.py` | `suggest_assignments()` | Main auto-assign algorithm |
| `dispatching/scheduler.py` | `build_smart_schedule()` | Single-driver schedule builder |
| `dispatching/scheduler.py` | `check_feasibility()` | Can a driver fit this job? |
| `dispatching/scheduler.py` | `estimate_job_end_time()` | When will a job finish? |
| `dispatching/scheduler.py` | `compute_leg_scarcity()` | How many drivers per job? |
| `dispatching/scheduler.py` | `get_drive_time()` | Drive time between locations |
| `dispatching/scheduler.py` | `load_all_driver_vtypes()` | All driver vehicles for a date |
| `dispatching/scheduler.py` | `_score_leg_for_smart_schedule()` | Builder scoring function |
| `dispatching/models.py` | `SchedulerSettings` | All tunable parameters |
| `dispatching/views.py` | `auto_assign_drivers()` | View that calls suggest_assignments |
| `dispatching/views.py` | `smart_schedule_builder()` | View that calls build_smart_schedule |

---

## Founder Brain (2026-06): Value Rules, Class Guard, Evict-to-Farm, Static Chain Timing

Design record: `docs/scheduler-automation/founder-brain-implementation.md`. Validated
end-to-end against the founder's complete manual rework of Sunday 2026-06-14
(`.analysis/legs_sunday_manual.csv`, scored by `.analysis/analyze_sunday.py` +
`.analysis/diff_schedules.py`): the engine now covers 526 pax vs the answer key's 483,
farms 2 departures vs 3, drops 0 V14-class legs vs 2, captures all four of the answer
key's known misses, with zero buffer/window violations.

### The four founder rules

* **R1 — Crunch rule**: when demand beats driver supply, in-house drivers belong on
  DEPARTURES/returns (fixed pickup, ~30 driver-min, driver ends at the MCO demand hub);
  ARRIVALS are the farm-out currency (flight-variable, ~75 driver-min, driver ends
  stranded at a resort). Affiliates do MCO meet-and-greets fine; a farmed fixed-time
  hotel pickup that no-shows means a missed flight.
* **R2 — Eviction**: an assigned leg is not sacred. Farming an assigned arrival to free
  a driver for an unassigned higher-value leg is correct — value-per-driver-minute plus
  farmability, NOT passenger count.
* **R3 — Booked-class matching, both directions**: a vehicle serves its own booked
  class first. Never let the highest-class vehicle run a lower-class job while a
  same-class job at a conflicting time goes unassigned (upward); push the lowest-class
  jobs onto the lowest-class vehicle (downward).
* **R4 — Same-slot value swap**: when two jobs compete for one driver-slot, keep the
  higher booked class, then the higher passenger count — never the reverse.

### Where each rule lives

1. **`leg_value(leg)`** — banded scalar: booked class tier (10,000/step) › trip type
   (3,000/2,000/1,000/0) › revenue_share (≤999) › pax (×0.01). Orders legs inside each
   (pass, hour, type) bucket — R4 emerges from the ordering — and prices evictions.
   A scoring term gated by `SchedulerSettings.auto_assign_value_weight` exposes it in
   candidate scores.
2. **Class-match-first banding** (`CLASS_MATCH_FIRST`) — in the main loop, feasible
   candidates split into three bands: exact-class › other non-reserved › reserved
   fallback. An exact-class driver wins the leg outright (R3 downward).
3. **Class-match guard** (`CLASS_MATCH_GUARD`) — a driver whose paired class is C is
   HARD-skipped for a lower-class leg when one of his own class's pending legs would be
   pushed off his board by it. UNCONDITIONAL: there is deliberately no "another class-C
   driver still looks free" escape — that test is optimistic at decision time (each
   class-C driver gets released in turn and the class-C job strands; this is exactly
   how the 9:15 AM V14 port cruise was lost on 6/14, because the Pass-0 scarcity rule
   does not fire when 4 drivers hold the type). A driver who cannot serve the class-C
   leg anyway is not barred.
4. **Evict-to-farm pass** (`evict_to_farm_for_value`, `AUTO_EVICT_TO_FARM_PASS`) — runs
   in `auto_assign_drivers` AFTER `recover_residuals_via_swaps` (cheaper cascades
   first), BEFORE `rescue_span_blocked_residuals` (so the rescue re-seats evicted
   arrivals anywhere they still fit) and before the trim/gap passes. For each residual
   in descending `leg_value`: try a free insertion; else find a driver where it fits if
   exactly ONE engine-proposed leg is removed, requiring (i) the victim is a farmable
   arrival — `trip_type == 'arrival'`, never `farmout_optimizer.is_departure()`, never
   locked/manual/seeded/pre-existing; (ii) value gain ≥
   `SchedulerSettings.displacement_min_value_gain`; (iii) the modified day re-passes
   the guards end to end (`_chain_ok`: every turnaround + window/max-hours), plus the
   greedy-parity gates (modal hours, tier compatibility, shared-car occupancy).
   Bounded by `max_displacements_per_run`; every move is logged with a human-readable
   reason and returned as `evict_moves` in both the preview and apply responses.
5. **Final free-insertion sweep** — the same pass re-runs with `free_insert_only=True`
   AFTER gap compaction: trim/gap relocations open seats that did not exist when
   coverage was settled, and no leg may stay farmed that fits the final board as-is
   (the answer key itself missed two such insertions on 6/14).

### Chain timing now uses the founder's static planning model

`CHAIN_STATIC_TIMING` (default True): chain feasibility — `check_feasibility`'s
preceding/following checks, `_chain_ok`, and every pass that goes through them — runs
on `chain_clear_dt()`: pickup + 45-min dwell (arrivals and airport-pickup cruises) +
category-table drive (+ Publix stop), with the anchor pushed LATER by a live flight
delay but never earlier. Repositioning between jobs uses `chain_repo_minutes()`
(same-address 0, else the category table; live distance only for unplaceable
endpoints). Each `ScheduleSlot` carries the precomputed value in `chain_clear_dt`.

Why: the RouteTimingMetric p75 path remains the right tool for DISPLAY clearing
estimates, but as chain math it both (a) admitted chains on optimistic decision-time
data — an early-trending flight ETA + a thin p75 bucket let a 7:00 AM fixed-time
departure chain at zero real slack off a 6:01 arrival the static model says clears
7:16 (the sereen pair, C4) — and (b) rejected chains the founder builds by hand,
because p75 of observed in-job drives (MCO→Disney 43 min vs his 30) silently taxed
every MCO round-trip 10–20 minutes of slack ("tight turns that work in reality must
NOT be farmed as impossible"). `.analysis/analyze_sunday.py` scores with exactly this
static model.

`ARRIVAL_CLEAR_STATIC_FLOOR` survives as the fallback when static chain timing is off:
an arrival's chain clear time is floored at the static model.

### Window semantics

`feasibility_guards.get_effective_window` now merges a typed/configured window with the
stub by taking the TIGHTER start/end ("the modal is authoritative" — typed 6-18 beats
the stub's 6-20, which had let auto-assign seat a job clearing 19:15 past a driver's
clear-by). The stub still tightens a looser configured window, mirroring max_hours.

### Tests and regression fixtures

`dispatching/tests_founder_brain.py` (30 tests) encodes M1–M5 from the 6/14 board as
synthetic fixtures: leg_value ordering (R3/R4), the class guard both directions with
flag-off pins, the evict pass (M1 displacement, min-gain churn gate, lock/departure
protection, chain revalidation, free insertion, bounds), the arrival static floor
(the sereen pair in both insertion orders), and the type-priority knobs. The
end-to-end harness is `scratch/founder_brain_0614.py` (seeds the fixture board,
runs the real `/dispatching/auto-assign-drivers/` apply, exports the board in the
fixture schema, scores it against the founder scorecard, restores the DB byte-for-byte).
