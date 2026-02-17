# Grayson Towncar: Scheduling & Dispatch System Analysis

## Context

Grayson Towncar operates 6-8 inhouse drivers serving MCO airport transfers, Disney/Universal resorts, and Port Canaveral cruise transfers in Orlando, FL. The owner manually achieves 93-97% job coverage by reshuffling the auto-assign output and using affiliates as a last resort. The automated system currently hits 85-90%. The primary pain points are: (1) flight delay cascading requires manual analysis of each driver's downstream schedule, and (2) mid-day gap coverage with no automated swap suggestions.

This document audits the current system, identifies what's working, maps the gaps, and proposes a Smart Dynamic Dispatcher with a phased implementation roadmap.

---

## 1. Current State Audit

### 1.1 Data Flow: Trip Lifecycle

```
BOOKING                           ASSIGNMENT                        EXECUTION
Customer/Dispatcher               Auto-Assign or Manual             Driver Day-Of
        |                                |                               |
        v                                v                               v
Customer -> Reservation -> Leg(s)   suggest_assignments()          Status updates:
         -> Flight (optional)       scores all drivers per leg     in-progress -> confirmed
         -> Cruise (optional)       greedy best-score wins         -> on-the-way -> on-location
                                    OR manual dropdown pick        -> picked-up -> completed
                                         |                               |
                                         v                               v
                                    Leg.driver = Driver             LegStatus audit trail
                                    Leg.driver_assigned_at          Route timing metrics updated
                                    Leg.driver_assigned_by          Reservation auto-completed
```

**Key models**: `Reservation` (reservations/models.py:56), `Leg` (reservations/models.py:537), `Flight` (reservations/models.py:1030), `Driver` (drivers/models.py:7), `DriverVehicleAssignment` (drivers/models.py:199)

### 1.2 Auto-Assign Pipeline (The Core Algorithm)

**Entry point**: `auto_assign_drivers()` in dispatching/views.py:5204

**Step-by-step flow**:

| Step | What Happens | Where |
|------|-------------|-------|
| 1. Load data | Fetch all legs for date, inhouse drivers with vehicle assignments | views.py:5245-5262 |
| 2. Build schedules | Create `DriverDaySchedule` for each driver from already-assigned legs | scheduler.py:485-541 |
| 3. Filter unassigned | Separate manual overrides from auto-assign pool | views.py:5289-5303 |
| 4. **Run `suggest_assignments()`** | Core algorithm - scores drivers for each unassigned leg | scheduler.py:544-907 |
| 5. Merge manual + auto | Manual overrides take precedence | views.py:5305-5320 |
| 6. Preview or Apply | Preview returns JSON for UI; Apply saves to DB with snapshot | views.py:5321-5472 |

**Critical**: `auto_assign_drivers()` only processes **UNASSIGNED** legs. Must call `reset_schedule()` (views.py:5475) first to re-run on already-assigned legs.

### 1.3 The Scoring Algorithm: `suggest_assignments()`

**File**: dispatching/scheduler.py:544-907

**Three stages**:

#### Stage 1: Preparation (lines 560-649)

1. **Leg sorting**: `(hour, type_priority, pickup_time)` where returns=0, cruises=1, other=2, arrivals=3. Returns/cruises process first within each hour.

2. **Vehicle reservation counting**: Count "exact-type" drivers (not general compatibility). Van with 1 exact driver triggers reservation; mini_van with 1 exact driver but 8 eligible does NOT.

3. **Chain opportunity detection**: For each leg, count follow-up jobs within 30-min drive and 10-180 min gap.

#### Stage 2: Two-Pass Processing Order (lines 650-685)

- **Pass 1**: Truly scarce types where `exact_count <= 2 AND eligible <= half_fleet`
- **Pass 2**: Everything else
- Within each pass: sorted by hour, type priority, pickup time

#### Stage 3: Greedy Scoring (lines 687-842)

For each leg, iterate all inhouse drivers and score:

| Factor | Score Range | What It Measures |
|--------|------------|-----------------|
| Buffer quality | 30 to 120 pts | How much spare time between jobs (20-30 min = perfect = 120 pts) |
| Vehicle tier match | 10 to 60 pts | Exact match = 60, one tier down = 40, two = 25, etc. |
| Scarcity bonus | 0 to 80 pts | Fewer eligible drivers = higher bonus (1 driver = 80 pts) |
| Location proximity | 0 to 50 pts | Same area = 50, close (<15 min) = 30, first job = 40 |
| Schedule flow | -40 to +30 pts | 3rd consecutive arrival = -40, break streak = +30 |
| In-house retention | 0 to 25 pts | Return/cruise trips get +25 to keep revenue inhouse |
| Chain bonus | 0 to 45 pts | Follow-up jobs nearby: 3+ = 45, 2 = 35, 1 = 20 |
| Load balance | -5 per leg | Penalizes overloading one driver |
| Trip preference | -20 to +80 pts | Matches driver's stated preference (heavy = 2x) |

**Reserved-mismatch handling**: If a rare-vehicle driver would take a non-matching job while matching jobs exist, they go to a FALLBACK pool. Non-reserved drivers always preferred. Fallback only used as last resort (with -60 penalty).

**After scoring**: Highest-scoring driver wins. Assignment is **simulated** into the working schedule. Reservation counts are decremented. Move to next leg. **No backtracking**.

### 1.4 Feasibility Checking

**Function**: `check_feasibility()` in scheduler.py:400-482

**Checks**:
1. Preceding job: `preceding.estimated_end_time + reposition_drive + inter_job_buffer <= new_pickup_time`
2. Following job: `new_estimated_end + reposition_drive + inter_job_buffer <= following_pickup_time`
3. Buffer < 15 min = warning, buffer < 0 = infeasible

**Drive time sources** (in priority order):
1. In-memory cache of `RouteTimingMetric` P75 values (sample_count >= 5)
2. DB query for P75, then average
3. Hardcoded `DRIVE_TIME_ESTIMATES` dict (~50 routes, scheduler.py:20-69)
4. `DEFAULT_DRIVE_TIME = 35 min`

**Job end time estimation** (scheduler.py:361-389):
- Arrivals: `pickup_time + airport_dwell(~45 min) + drive_time + store_stop(+25 min if applicable)`
- Cruise from airport: `pickup_time + airport_dwell + drive_time`
- Everything else: `pickup_time + drive_time`

### 1.5 Smart Schedule Builder (Single-Driver Optimizer)

**Function**: `build_smart_schedule()` in scheduler.py:964-1237

Used by the planner UI to build/rebuild one driver's schedule with:
- Pinned legs (must-include)
- Time windows (start_hour to end_hour)
- Trip type preferences (prefer/heavy/only)
- Excluded legs

Processes: pinned first (verify feasibility), then optional legs sorted by preference mode, scored and inserted if score > 0.

### 1.6 Flight Tracking

**Service**: `AeroAPIService` in dispatching/aeroapi_service.py (665 lines)
- Fetches from FlightAware AeroAPI: status, scheduled/estimated/actual times, terminal, gate, baggage claim
- Converts UTC to Eastern time
- Rate limiting handled (HTTP 429 + Retry-After)

**Views for flight ops** (dispatching/views.py:2129-2930):
- `refresh_flight_data()`: Single leg refresh
- `refresh_all_flights()`: Bulk refresh via ThreadPoolExecutor (batch of 5)
- `match_leg_time_to_flight()`: Sync leg pickup_time to flight arrival
- `match_all_leg_times_to_flight()`: Bulk sync all arrivals for a date

### 1.7 Real-Time Monitoring

**Dispatch flags**: `detect_leg_flags()` in dispatching/utils.py:588-654
- "Not confirmed" (status still 'in-progress')
- "Not on the way" (within 20-min lead time, 50 min for cruises)
- "Not picked up" (past pickup time, non-arrivals only)

**Automated alerts**: `dispatch_alerts.py` management command
- Runs via Windows Task Scheduler every 5-10 min
- Sends ntfy push notifications for danger-level flags
- 60-minute cooldown prevents duplicate alerts
- Detects flight time mismatches via `leg.get_flight_time_mismatch_display()`

### 1.8 Schedule Snapshots

**Models**: `ScheduleSnapshot` + `ScheduleSnapshotEntry` (reservations/models.py:1680-1725)
- Auto-saved before reset and before auto-assign
- Stores leg-to-driver mappings for a date
- Can be listed, viewed, and restored

---

## 2. What's Working Well

### 2.1 Vehicle Reservation System
The three-part vehicle reservation logic (exact-type scarcity, hard skip with fallback, two-pass processing) is well-engineered and solves a real problem. It correctly prevents the single Van driver from being consumed by Towncar arrivals while still allowing fallback when needed. The distinction between "exact-type count" and "general eligible count" is a key insight.

### 2.2 Multi-Factor Scoring
The 9-factor scoring system captures real scheduling intelligence: buffer quality (not just feasibility), location proximity (geographic clustering), schedule flow (avoid driver fatigue from consecutive arrivals), and chain awareness (batch opportunities). These factors align well with best practices in fleet dispatch.

### 2.3 Route Timing Analytics
Using P75 historical drive times (with IQR outlier filtering and minimum sample counts) is significantly better than hardcoded estimates alone. The fallback chain (cache -> DB P75 -> DB avg -> hardcoded -> default) is robust. The `RouteTimingMetric` model captures the right dimensions (route, time of day, day type).

### 2.4 Configurable Weights via SchedulerSettings
Making all scoring weights configurable via a singleton model (with module-level caching) allows tuning without code changes. This is production-friendly and enables iterative improvement.

### 2.5 Schedule Snapshots
Auto-saving snapshots before reset/auto-assign provides an undo mechanism and audit trail. This is a critical safety feature.

### 2.6 Airport Dwell Time Modeling
Separating "airport dwell time" (gate arrival -> passenger pickup) from "drive time" (pickup -> dropoff) is accurate for arrival trips. The 45-minute default accounts for landing, taxi, deplaning, baggage claim, and walk to car.

### 2.7 Smart Builder for Single-Driver Optimization
The `build_smart_schedule()` with pinned legs, time windows, and trip preferences gives the dispatcher granular control. The timing details with "reasoning" strings make the algorithm's decisions transparent.

---

## 3. Gaps & Improvement Opportunities

### 3.1 Greedy Algorithm Cannot See Global Optimum (PRIMARY GAP)

**Impact**: This is the main reason for the 85-90% vs 93-97% coverage gap.

The greedy algorithm assigns legs one at a time, left to right. Once Driver A is assigned to Trip 1, that assignment is never reconsidered even if it blocks the only solution for Trip 5. The owner's manual reshuffling (moving 2-3 assignments) unlocks coverage the greedy approach misses.

**Example**: Trip 1 (2:00 PM return, 5 eligible drivers) gets assigned to Driver A. Trip 5 (2:30 PM Van arrival, only Driver A eligible) can't be assigned. The system says "no driver available." The owner sees this, moves Trip 1 to Driver B, and assigns Trip 5 to Driver A. Coverage goes from 85% to 93%.

**Root cause**: No backtracking, no swap logic, no look-ahead.

### 3.2 No Flight Delay Cascading

**Impact**: Owner spends significant time manually tracing cascading effects of delays.

The system detects flight time mismatches and sends alerts, but does NOT:
- Analyze which downstream legs are affected
- Calculate buffer deficits for each affected leg
- Suggest which drivers to swap or which legs to reassign
- Show the full cascade impact visually

**Current workflow**: Owner gets alert -> manually opens driver's schedule -> checks each subsequent job -> decides whether to swap, absorb, or use affiliate. This is a 5-10 minute manual process per delay.

### 3.3 No Swap/Shuffle Logic

**Impact**: When a trip becomes unassigned mid-day, the system can only check if any single driver is free. It cannot discover multi-step solutions.

Missing: "Move Driver A from Trip X to Trip Y, which frees Driver B for the unassigned trip." These 2-3 step swap chains are exactly what the owner does manually.

### 3.4 Inter-Job Buffer May Be Too Tight

The `INTER_JOB_BUFFER = 5 minutes` is the hard minimum added after reposition time. In practice, drivers need:
- Time to park, walk to terminal (arrivals)
- Bathroom/fuel stops between jobs
- Unexpected traffic or passenger delays

The P75 drive time estimates help absorb some of this, but the 5-minute buffer between "predicted available" and "next pickup" is aggressive. The scoring does penalize tight buffers (< 20 min gets lower scores), but the feasibility check allows assignments with only 5 min buffer.

### 3.5 No Live Traffic / Google Maps Integration

All drive time estimates are historical or hardcoded. The system cannot account for:
- I-4 construction delays (a constant in Orlando)
- Event traffic (Disney fireworks, Universal events, conventions)
- Accidents or weather
- Real-time ETA calculation

### 3.6 No Driver Location Tracking

`detect_leg_flags()` uses time-based heuristics ("not on the way" if within 20 min of pickup and status not updated). Without GPS/location data, the system can't determine:
- Where the driver actually is
- Whether they'll make the next pickup on time
- Optimal routing between jobs

### 3.7 No Real-Time Dashboard Updates

The dispatch board requires manual page refresh. The `dispatch_alerts.py` runs every 5-10 min via Windows Task Scheduler. There are no WebSockets or live-updating panels.

### 3.8 No Automated Conflict Prevention

The system detects conflicts (via `detect_leg_flags`) but only after they happen or are imminent. There is no forward-looking analysis that says "based on current status, Driver Mike will be 15 minutes late to his 3:30 PM pickup."

### 3.9 Processing Order Within Hours

Within each hour, returns/other process before arrivals. This can consume SUV drivers before arrivals are scored, making the -60 reserve penalty irrelevant. The type priority ordering (returns first) is generally good but can produce suboptimal results when arrivals have fewer eligible drivers.

### 3.10 No Priority Tiers for Trip Types

All trips are treated equally in scoring. In reality:
- Cruise port pickups are non-negotiable (ship won't wait)
- Airport arrivals have natural buffer (dwell time)
- Hotel returns have more flexibility (customer can be told "5 min")

The scoring system doesn't capture these operational priority differences.

---

## 4. Real-Time Conflict Resolution

### 4.1 Architecture: Conflict Engine

**New file**: `dispatching/conflict_engine.py`

Three core functions:

```
conflict_engine.py
  +-- detect_cascading_conflicts(leg, new_pickup_time, target_date, schedules)
  |     -> CascadeResult (impacts + suggestions)
  |
  +-- find_driver_substitutes(leg, target_date, schedules, driver_vtypes)
  |     -> List[SubstituteOption] (ranked replacement drivers)
  |
  +-- find_swap_chains(unassigned_leg, target_date, schedules, driver_vtypes, max_depth=3)
        -> List[SwapChain] (multi-step reassignment sequences)
```

### 4.2 Flight Delay Cascading

**Algorithm for `detect_cascading_conflicts()`**:

1. Find the delayed leg's assigned driver
2. Get their schedule sorted by pickup_time
3. Recompute delayed leg's end time with new pickup
4. Walk forward through subsequent legs:
   - Compute reposition time (existing `get_drive_time()`)
   - Check `new_end + reposition + buffer <= next_pickup`
   - If deficit < 0: record overlap, cascade further (the next leg also shifts)
   - If deficit 0-15 min: record tight buffer warning
   - If deficit >= 15 min: stop (sufficient buffer absorbs the delay)
5. Generate fix suggestions based on severity:

| Deficit | Suggestion |
|---------|-----------|
| < 15 min | "Absorb - tight but feasible" |
| 15-45 min | "Swap the NEXT leg to another driver" (run `find_driver_substitutes`) |
| > 45 min | "Swap the DELAYED leg to another driver" or "use affiliate" |
| Multiple legs affected | "Swap chain needed" (run `find_swap_chains`) |

**Integration points**:
- `dispatch_alerts.py`: After detecting flight mismatch, include cascade details in ntfy notification
- `refresh_flight_data()` view (views.py:2132): Return cascade analysis in JSON response
- New endpoint `POST /dispatching/analyze-conflict/`: Accept leg_id + hypothetical new time, return analysis

### 4.3 Driver Running Late

**Detection**: When a driver's status is "picked-up" (en route to dropoff) but their estimated end time is later than expected, OR when their status hasn't progressed as expected.

**Algorithm for `find_driver_substitutes()`**:

1. Get the at-risk leg (the driver's next job)
2. For each other inhouse driver:
   - Check vehicle compatibility
   - Check time feasibility (can they reach the pickup?)
   - Score: buffer quality + vehicle match + proximity to pickup location
3. Rank substitutes by score
4. Include "use affiliate" as final option

**Key reuse**: This calls existing `check_feasibility()` and scoring infrastructure from scheduler.py.

### 4.4 Unassigned Trips Mid-Day

This is the hardest problem. When no single driver is free, we need swap chains.

**Algorithm for `find_swap_chains()`**:

```
For unassigned leg U:
  For each compatible driver D:
    Can D take U? -> Direct assign (no swap needed)
    If not, find which of D's legs block U (the "blockers")
    For each blocker B:
      For each other driver E:
        Can E take B? (vehicle compat + feasibility)
        If yes: Remove B from D, give B to E, assign U to D
        Result: SwapChain with 2 steps, net score = feasibility of both moves

  If max_depth >= 3 and no 2-step chain found:
    Recurse: Try moving E's blocker to a third driver F
    Result: SwapChain with 3 steps

  Sort chains by: fewest steps first, then highest net score
  Return top 5
```

**Performance**: With 8 drivers and ~30 legs, the search space is:
- 2-step: 8 drivers * ~4 blockers * 7 other drivers = ~224 evaluations
- 3-step: 224 * ~4 * 6 = ~5,376 evaluations
- Each evaluation: 2 `check_feasibility()` calls (~50us each) = ~0.5ms total for 2-step, ~5ms for 3-step

This is trivially fast for the fleet size.

### 4.5 Presenting Suggestions to the Dispatcher

Suggestions should be displayed as actionable cards:

```
[!] Flight DL1691 delayed 45 min (was 2:30 PM, now 3:15 PM)
    Driver: Mike | Leg #234 (MCO -> Disney)

    CASCADE: Mike's next job (Leg #267, 4:00 PM) will have only 8 min buffer

    SUGGESTED FIX:
    [1] Absorb it - tight but feasible (8 min buffer)     [Accept]
    [2] Move Leg #267 to Alex (32 min buffer)              [Apply Swap]
    [3] Send Leg #267 to affiliate                         [Assign Affiliate]
```

Each suggestion has an "Apply" button that calls the existing `update_leg_assignment()` endpoint.

---

## 5. Smart Dynamic Dispatcher Design

### 5.1 Post-Assignment Optimizer

**Add to**: `dispatching/scheduler.py`

**Function**: `optimize_assignments()` - runs AFTER the greedy `suggest_assignments()` and applies local search improvements.

**Two improvement operators**:

**Operator 1: 2-Opt Swap**
For each pair of assignments (legA on driverX, legB on driverY):
- Can driverX take legB AND driverY take legA?
- If yes and total score improves: swap them

**Operator 2: Eject-and-Reassign**
For each unassigned leg U:
- For each assigned leg A on driver D:
  - Could D take U if A were removed?
  - Could any other driver take A?
  - If both yes: eject A, move A to other driver, assign U to D
  - Net result: one more leg covered

**Evaluation function**: `coverage * 1000 + sum(buffer_scores) + sum(tier_scores) - imbalance_penalty`

Coverage dominates all other factors (assigning one more leg = +1000 points).

**Runtime**: With 8 drivers and 30 legs, 2-opt evaluates ~450 pairs per iteration, eject evaluates ~240 per iteration. 50 iterations = < 200ms. Acceptable for an operation that runs once per scheduling session.

### 5.2 Regret-Based Priority Ordering

**Modify**: The sort key in `suggest_assignments()` (scheduler.py ~line 685)

**Current sort**: `(pass_priority, hour, type_priority, pickup_time)`

**Proposed sort**: `(pass_priority, -regret_score, hour, type_priority, pickup_time)`

**Regret** = `score_of_best_driver - score_of_second_best_driver`

High regret means this leg MUST go to a specific driver (only one good option). Low regret means many drivers are equally good. Process high-regret legs first to ensure constrained legs get their best match before flexible legs consume options.

**Cost**: Requires pre-scoring all legs (one pass through all drivers per leg). With 8 drivers * 30 legs = 240 evaluations. Trivially fast.

### 5.3 Provisional Assignment with Look-Ahead

**Insert at**: scheduler.py ~line 845, before "Simulate the assignment"

Before committing an assignment, check: does this block any scarce future legs? If yes, and a second-best driver is available for the current leg, use the second-best to preserve the best for the scarce future leg.

### 5.4 Real-Time Situation Monitor

**New file**: `dispatching/realtime.py`

**Function**: `get_dispatch_situation(target_date)` -> `DispatchSituation`

Aggregates:
- `detect_leg_flags()` for each active leg
- Flight mismatch detection
- Cascade analysis for any delayed flights (30+ min)
- Driver position inference from LegStatus history
- Prioritized "suggested actions" list

**Consumed by**: HTMX-powered panel on the capacity planner page, polling every 60 seconds.

### 5.5 Priority Tier System

**Proposed tier weights** (added to scoring):

| Trip Type | Priority Tier | Bonus |
|-----------|--------------|-------|
| Cruise (to port) | Non-negotiable | +100 (ensures inhouse assignment) |
| Airport arrival (with flight) | High (has natural dwell buffer) | +20 |
| Airport return (departure) | High (customer has flight to catch) | +30 |
| Hotel transfer | Normal | +0 |
| Other | Flexible | -10 |

Add to SchedulerSettings for configurability.

---

## 6. Implementation Roadmap

### Phase 1: Quick Wins (Week 1) - Immediately improve auto-assign quality

| # | Task | Files | Effort | Impact |
|---|------|-------|--------|--------|
| 1 | **Regret-based sort** | scheduler.py | Small (0.5 day) | High - processes constrained legs first |
| 2 | **Provisional look-ahead** | scheduler.py | Small (1 day) | Medium - prevents greedy blocking |
| 3 | **Priority tier scoring** | scheduler.py, models.py | Small (0.5 day) | Medium - cruise/return priority |
| 4 | **Increase inter_job_buffer default** to 10 min | models.py | Trivial | Low - safer schedules |

**Expected result**: Auto-assign should improve from 85-90% to ~90-93% coverage by processing legs in smarter order and avoiding greedy traps.

### Phase 2: Conflict Engine (Week 2) - Automate the owner's cascade workflow

| # | Task | Files | Effort | Impact |
|---|------|-------|--------|--------|
| 5 | **Cascade detection engine** | conflict_engine.py (new) | Medium (2 days) | **Very High** - core pain point |
| 6 | **Substitute finder** | conflict_engine.py | Small (1 day) | High - "who can cover?" |
| 7 | **Enhanced dispatch alerts** | dispatch_alerts.py | Small (0.5 day) | High - cascade in notifications |
| 8 | **Conflict analysis API endpoint** | views.py, urls.py | Small (0.5 day) | Medium - powers UI |

**Expected result**: Flight delay = instant cascade analysis + suggested fixes delivered via push notification and available in the planner UI.

### Phase 3: Post-Assignment Optimizer (Week 3) - Close the coverage gap

| # | Task | Files | Effort | Impact |
|---|------|-------|--------|--------|
| 9 | **2-Opt swap optimizer** | scheduler.py | Medium (1.5 days) | **Very High** - the reshuffling the owner does |
| 10 | **Eject-and-reassign** | scheduler.py | Medium (1.5 days) | **Very High** - unlocks blocked assignments |
| 11 | **Wire into auto_assign_drivers** | views.py | Small (0.5 day) | Required for #9-10 to work |
| 12 | **SchedulerSettings flags** | models.py, admin | Small (0.5 day) | Safety - feature flags for new logic |

**Expected result**: Auto-assign should reach 93-95%+ coverage, matching or exceeding manual scheduling.

### Phase 4: Dashboard & Swap Chains (Week 4) - Real-time operations

| # | Task | Files | Effort | Impact |
|---|------|-------|--------|--------|
| 13 | **Real-time situation module** | realtime.py (new) | Medium (2 days) | High - aggregated awareness |
| 14 | **HTMX alerts panel** | templates, views.py, urls.py | Medium (2 days) | High - live dispatch board |
| 15 | **Swap chain finder** | conflict_engine.py | Medium (1.5 days) | High - multi-step solutions |
| 16 | **Apply-swap UI** | templates, views.py | Medium (1 day) | High - one-click fixes |

**Expected result**: The capacity planner becomes a live operations dashboard with automated conflict detection and one-click resolution.

### Phase 5: Future Enhancements (Weeks 5+)

| # | Task | Files | Effort | Impact |
|---|------|-------|--------|--------|
| 17 | Google Maps API for live drive times | scheduler.py, new service | Large | Medium-High |
| 18 | Hour-block permutation optimization | scheduler.py | Medium | Medium |
| 19 | Driver mobile app / GPS tracking | New Django app | Very Large | High |
| 20 | WebSocket live updates (django-channels) | New infrastructure | Large | Medium |
| 21 | Multi-day schedule optimization | scheduler.py | Large | Medium |
| 22 | Demand forecasting from DemandPattern data | analytics.py | Medium | Medium |

### Pros/Cons Summary

| Proposal | Pros | Cons |
|----------|------|------|
| Regret sort | Trivial to implement, safe, immediately effective | Minor: changes assignment order, needs testing |
| Post-optimizer (2-opt + eject) | Directly automates what owner does manually; bounded runtime | Adds ~150 lines to scheduler.py; need to rebuild working schedules per swap |
| Cascade detection | Addresses #1 pain point; reuses all existing infrastructure | Requires UI work to display meaningfully |
| Swap chains | Solves "no single driver" problem that owner solves manually | BFS can miss optimal chains; heuristic not provably optimal |
| Real-time dashboard | HTMX keeps it simple (no WebSocket infra needed) | 60-second polling delay; adds dashboard complexity |
| Google Maps integration | Live traffic awareness | API costs; added latency; 35-min default works well enough for most routes |
| Driver mobile app | GPS tracking, in-app confirmations | Very large scope; requires native or hybrid app development |

---

## 7. Files That Would Change

### New Files

| File | Purpose |
|------|---------|
| `dispatching/conflict_engine.py` | Cascade detection, swap chains, substitute finding |
| `dispatching/realtime.py` | Real-time situation aggregation |
| `dispatching/templates/dispatching/partials/realtime_alerts.html` | HTMX alert panel |
| `dispatching/templates/dispatching/partials/cascade_detail.html` | Cascade visualization |

### Modified Files

| File | Changes |
|------|---------|
| `dispatching/scheduler.py` | Add `optimize_assignments()`, regret sort, provisional look-ahead, priority tiers |
| `dispatching/models.py` | New SchedulerSettings fields: `enable_post_optimization`, `enable_regret_sort`, `cruise_priority_bonus`, etc. |
| `dispatching/views.py` | New endpoints: `realtime_alerts`, `analyze_conflict`, `apply_swap_suggestion`; wire optimizer into `auto_assign_drivers` |
| `dispatching/urls.py` | 3 new URL patterns |
| `dispatching/management/commands/dispatch_alerts.py` | Enhanced cascade details in notifications |
| `dispatching/templates/dispatching/daily_capacity_planner.html` | HTMX integration for live alerts panel |

---

## 8. Verification Plan

### Testing the Optimizer
1. Run auto-assign on a historical date where manual coverage was 95%+ but auto-assign was 85-90%
2. Compare: did the optimizer find the same swaps the owner made?
3. Check: did coverage improve to 93%+?
4. Verify: no feasibility violations (all buffers positive)

### Testing Cascade Detection
1. Take a real flight delay scenario (e.g., 2-hour delay on a mid-afternoon arrival)
2. Run `detect_cascading_conflicts()` and verify it identifies the correct downstream legs
3. Verify suggestions are actionable (substitute drivers are actually feasible)

### Testing Swap Chains
1. Create a scenario where no single driver is free but a 2-step swap unlocks coverage
2. Run `find_swap_chains()` and verify it finds the solution
3. Apply the swap and verify all feasibility constraints are maintained

### Regression Testing
1. Run the full auto-assign pipeline on 10 historical dates
2. Compare: coverage should be >= original for all dates
3. Verify: no new feasibility violations introduced
4. Check: scoring weights still produce sensible assignments

---

## 9. Clarifying Questions

1. **Affiliate workflow**: When you send a trip to an affiliate, how does that work in the system today? Is there an affiliate driver model, or do you just unassign it and handle it outside the system?

2. **Buffer comfort level**: The system allows 5-minute buffers. What's the minimum buffer YOU feel comfortable with? (15 min? 20 min? Depends on the route?)

3. **Cruise priority**: You mentioned cruise port pickups are non-negotiable. Does that mean you'd rather leave a hotel transfer unassigned than miss a cruise pickup? How far does this priority extend?

4. **Daily volume**: On a typical busy day, how many total legs are there? (Helps calibrate optimizer performance expectations.)

5. **Driver preferences**: How rigid are driver preferences in practice? If a driver prefers arrivals but assigning them a return would unlock 2 more covered legs, should the system override the preference?

6. **Store stop behavior**: Store stops add 25 min to arrivals. How common are these? Do they significantly impact scheduling?

7. **Affiliate as scoring option**: Would you want the system to include "assign to affiliate" as a scored option in the optimizer (with a penalty), so it can proactively suggest affiliates when inhouse coverage would require too many compromises?

8. **Historical data availability**: How many months of completed leg data do you have? This affects how much we can backtest the optimizer improvements.
