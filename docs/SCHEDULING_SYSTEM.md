# Grayson Towncar — Route Timing & Scheduling System

## What This System Does (Simple Terms)

This system answers one question: **"Which driver should we send, and when will they be done?"**

It learns from every completed trip — how long the airport pickup took, how long the drive was, how long until the driver was free for the next job. Over time, it builds a data-driven picture of how long each type of trip actually takes, and uses that to plan smarter schedules.

Think of it like Waze for dispatch — instead of guessing "MCO to Disney takes about 30 minutes," the system knows from 47 real trips that it takes **32 minutes on weekday mornings (P75)** and **38 minutes on holiday weekends (P75)**.

---

## How Data Flows Through the System

```
Driver completes a trip
       │
       ▼
LegStatus timestamps recorded
(on-the-way → on-location → picked-up → completed)
       │
       ▼
Analytics module calculates timing
(dwell time, drive time, turnaround time)
       │
       ▼
RouteTimingMetric updated
(aggregated stats by route/time/day)
       │
       ▼
Scheduler reads metrics for planning
(P75 drive times, dwell estimates, buffers)
       │
       ▼
Capacity Planner shows optimized schedule
(driver timelines, suggestions, batching)
```

---

## Part 1: Collecting the Data

### Location Categories

Every pickup and dropoff address is normalized into one of **9 standard categories**:

| Category | Examples |
|----------|----------|
| **MCO Terminal** | Orlando International Airport, MCO gates/terminals |
| **SFB Terminal** | Sanford International Airport |
| **Airport Hotel** | Hyatt Regency MCO, Marriott Airport |
| **Disney Resort** | All Disney property — Contemporary, Yacht Club, All-Stars, etc. |
| **Universal Resort** | Portofino Bay, Hard Rock Hotel, Royal Pacific, etc. |
| **Port Canaveral Area** | Cruise terminals, Cocoa Beach |
| **Other Hotel** | Non-Disney/Universal hotels |
| **Residential** | Home addresses (any street, road, avenue, lane) |
| **Other** | Anything that doesn't match above |

**Why?** Free-text addresses like "Disney's Contemporary Resort" and "Contemporary Resort Orlando" are the same place. Categorizing lets us group trips into meaningful buckets with enough sample data to be statistically useful.

### Time-of-Day Buckets

Each trip is also categorized by when it happens:

| Bucket | Hours | Why It Matters |
|--------|-------|----------------|
| **Early Morning** | 4–7 AM | Red-eye arrivals, light traffic |
| **Morning Rush** | 7–10 AM | Peak arrival window, I-4 congestion |
| **Midday** | 10 AM–2 PM | Moderate traffic, park transitions |
| **Afternoon** | 2–6 PM | Resort transitions, rush hour builds |
| **Evening** | 6–10 PM | Evening departures, moderate traffic |
| **Night** | 10 PM–4 AM | Late night, minimal traffic |

### Day Type

- **Weekday** — Monday through Friday (non-holiday)
- **Weekend** — Saturday and Sunday
- **Holiday** — US Federal holidays (detected automatically)

---

## Part 2: The Three Timing Calculations

### 1. Airport Dwell Time (Arrivals Only)

**What it measures:** How long from when the plane lands to when the passenger is in the car.

```
Flight gate arrival time → Passenger picked up
         (dwell time = this gap)
```

**Data sources:**
- Flight tracking API provides gate arrival time (actual → estimated → scheduled, best available)
- LegStatus `picked-up` timestamp from the driver

**Sanity checks:** Must be between 0 and 300 minutes. Anything outside that range is bad data and gets thrown out.

**Real-world meaning:** This captures baggage claim wait, customs (international), walking to the car, etc. A typical domestic arrival at MCO runs 30–50 minutes gate-to-car.

### 2. Drive Time (All Trip Types)

**What it measures:** Time from passenger pickup to trip completion.

```
Driver marks "picked-up" → Driver marks "completed"
              (drive time = this gap)
```

**Sanity checks:** Must be between 0 and 240 minutes.

**Real-world meaning:** Actual driving time including traffic. MCO to Disney is typically 25–35 minutes depending on time of day.

### 3. Turnaround Time (Between Jobs)

**What it measures:** How long between finishing one job and starting the next.

```
Job 1 completed → Job 2 picked-up (or on-location)
         (turnaround = this gap)
```

**Real-world meaning:** Includes repositioning drive, bathroom breaks, fuel, waiting for next passenger. Used to understand how many jobs a driver can realistically handle per day.

---

## Part 3: Statistical Aggregation

### How Metrics Are Calculated

For each unique combination of **(trip type + pickup category + dropoff category + time of day + day type)**, the system:

1. Finds all completed legs matching that bucket
2. Calculates raw timing values for each leg
3. Filters outliers (dwell > 120 min, drive > 240 min, total > 360 min)
4. Computes statistics:

| Statistic | Minimum Samples Required | What It Tells You |
|-----------|-------------------------|-------------------|
| **Average (Mean)** | 1 | General ballpark, pulled by outliers |
| **Median (P50)** | 2 | "Typical" trip — half are faster, half slower |
| **P75 (75th Percentile)** | 4 | Conservative estimate — 75% of trips finish by this time |
| **P90 (90th Percentile)** | 10 | Very conservative — only 10% of trips take longer |

### Why P75 Is the Default for Scheduling

- **Average** is too optimistic — one fast trip skews the number down
- **P90** is too pessimistic — schedules become overly padded
- **P75** is the sweet spot — conservative enough to avoid most delays, realistic enough to keep drivers productive

### Confidence Levels

Based on how many trips we've seen for a route bucket:

| Sample Count | Confidence | Meaning |
|-------------|------------|---------|
| 20+ trips | **High** | Very reliable, safe to schedule tightly |
| 10–19 trips | **Medium** | Good estimate, small margin of error |
| 5–9 trips | **Low** | Rough estimate, pad with extra time |
| < 5 trips | **None** | Insufficient data, use hardcoded estimates |

---

## Part 4: The Scheduler

### Hardcoded Fallback Estimates

When there isn't enough historical data (< 5 samples), the scheduler falls back to pre-configured estimates for ~60 common routes:

| Route | Estimate |
|-------|----------|
| MCO ↔ Disney Resort | 30 min |
| MCO ↔ Universal Resort | 25 min |
| MCO ↔ Port Canaveral | 55 min |
| Disney ↔ Universal | 28 min |
| Disney ↔ Port Canaveral | 70 min |
| MCO ↔ Airport Hotel | 12 min |
| Unknown routes (default) | 35 min |

These get replaced automatically as real data accumulates.

### Inter-Job Buffer

**10 minutes** is always added between jobs. This accounts for:
- Walking passenger to the door / unloading luggage
- Quick bathroom break
- Repositioning to next pickup
- Minor delays

### How Drive Time Is Looked Up

When the scheduler needs "How long does MCO → Disney take?", it checks in this order:

1. **In-memory cache** (pre-loaded RouteTimingMetric records) → P75 drive time
2. **Database query** for RouteTimingMetric with sample_count ≥ 5 → P75 or P90 or average
3. **Hardcoded dictionary** of ~60 route estimates
4. **Default fallback** → 35 minutes

The cache is loaded once at the start of a scheduling session and cleared when done, avoiding hundreds of repeated database queries.

### Estimating When a Job Ends

**For airport arrivals:**
```
Job end = pickup_time + airport_dwell_time + drive_time
```

**For all other trips:**
```
Job end = pickup_time + drive_time
```

**When a driver is free for the next job:**
```
Available time = job_end + 10 min buffer
```

---

## Part 5: The Suggestion Engine

### How Auto-Assign Works

The suggestion engine uses a **greedy scoring algorithm** — it processes legs one at a time (earliest first) and assigns each to the best available driver.

#### Step 1: Sort all unassigned legs by pickup time

Earliest pickups get assigned first. This prevents later assignments from blocking morning jobs.

#### Step 2: For each leg, score every in-house driver

**Feasibility check first** — can this driver physically do this job?

- Does the job overlap with an existing assignment?
- Can the driver finish their previous job, reposition, and arrive in time?
- After this job, can they still make their next assignment?

If not feasible → skip this driver.

**If feasible, calculate a score:**

| Factor | Points | Logic |
|--------|--------|-------|
| **Buffer quality** | +100 | Sweet spot: 20–60 minutes of spare time between jobs |
| **Location proximity** | +50 | Driver's last dropoff is same area as this pickup |
| **Close proximity** | +30 | Driver's last dropoff is < 15 min drive from this pickup |
| **Load balance** | -5 per existing leg | Prevents overloading one driver while others sit idle |

#### Step 3: Assign to highest-scoring driver

The driver with the highest score gets the job. Their schedule is updated (simulated), and the next unassigned leg is processed.

#### Step 4: Mark remaining legs for affiliate

Any leg that no in-house driver can handle gets flagged as "needs affiliate driver."

### Example Scoring

```
Leg: MCO → Disney, 9:00 AM pickup

Driver John (last job ends 8:15 AM at Disney):
  - Reposition: Disney → MCO = 30 min → arrives 8:45 AM
  - Buffer: 9:00 - 8:45 = 15 min ✓ (feasible but tight)
  - Score: 80 (buffer ok) + 0 (different area) - 10 (2 existing legs) = 70

Driver Mary (last job ends 8:30 AM at MCO):
  - Reposition: MCO → MCO = 0 min → available 8:40 AM
  - Buffer: 9:00 - 8:40 = 20 min ✓ (sweet spot)
  - Score: 100 (great buffer) + 50 (same area!) - 5 (1 existing leg) = 145

→ Mary gets the job (score 145 vs 70)
```

---

## Part 6: The Capacity Planner UI

### What It Shows

The capacity planner (`/dispatching/capacity-planner/`) is the visual interface for all of this:

1. **Driver Timelines** — Horizontal bars showing each driver's day
   - Colored blocks for each assigned job
   - Gaps between jobs (yellow = tight, red = critical)
   - Estimated end times for each job

2. **Unassigned Legs** — Jobs that need drivers
   - Each shows a suggested driver with reasoning
   - "Assign" button to accept suggestion
   - "Auto-Assign All" to accept all suggestions at once

3. **Batching Opportunities** — Groups of jobs in the same area within 30 minutes
   - "3 pickups at Disney between 8:00–8:30 AM"
   - Helps dispatchers consolidate trips

4. **Coverage Stats** — How well the day is covered
   - In-house vs affiliate vs unassigned percentages
   - Revenue breakdown by driver type

### Schedule Snapshots

Before any auto-assign or schedule reset, the system automatically saves a snapshot of the current assignments. Snapshots can also be saved manually with optional notes. This allows:
- **Undo** if auto-assign produces bad results
- **Compare** different scheduling approaches
- **Audit** who changed what and when

---

## Part 7: Recalculating Metrics

### When Metrics Update

- **Automatically** — When a leg is marked as completed, its specific route bucket gets recalculated
- **Manually** — From the Route Timing Reference page, dispatchers can trigger a full recalculation with date range filters:
  - Last 30 days (recent trends)
  - Last 90 days (seasonal patterns)
  - Last 6 months (broad baseline)
  - All time (complete history)

### Route Timing Reference Page

Located at `/dispatching/route-timing-reference/`, this page shows:
- All route timing metrics in a filterable table
- Sample counts and confidence badges
- Hardcoded fallback estimates for comparison
- Recalculate buttons for different time ranges

---

## Part 8: Data Models Reference

### RouteTimingMetric

One record per unique combination of route characteristics:

| Field | Type | Description |
|-------|------|-------------|
| `trip_type` | CharField | arrival, return, cruise, other |
| `pickup_location_category` | CharField | One of the 9 location categories |
| `dropoff_location_category` | CharField | One of the 9 location categories |
| `time_of_day_category` | CharField | One of the 6 time buckets |
| `day_type` | CharField | weekday, weekend, holiday |
| `avg_airport_dwell_time` | int (min) | Mean dwell time (arrivals only) |
| `median_airport_dwell_time` | int (min) | 50th percentile dwell |
| `p75_airport_dwell_time` | int (min) | 75th percentile dwell |
| `p90_airport_dwell_time` | int (min) | 90th percentile dwell |
| `avg_drive_time` | int (min) | Mean drive time |
| `median_drive_time` | int (min) | 50th percentile drive |
| `p75_drive_time` | int (min) | 75th percentile drive — **used by scheduler** |
| `p90_drive_time` | int (min) | 90th percentile drive |
| `avg_total_time` | int (min) | Mean total (dwell + drive) |
| `median_total_time` | int (min) | 50th percentile total |
| `p75_total_time` | int (min) | 75th percentile total |
| `p90_total_time` | int (min) | 90th percentile total |
| `sample_count` | int | Number of trips in this bucket |
| `last_calculated` | datetime | When this was last updated |

### DriverDailyCapacity

One record per driver per date:

| Field | Type | Description |
|-------|------|-------------|
| `driver` | FK → Driver | Which driver |
| `date` | DateField | Which day |
| `total_legs` | int | Completed legs that day |
| `total_revenue` | Decimal | Revenue earned |
| `total_active_hours` | Decimal | First pickup to last dropoff |
| `avg_turnaround_time` | int (min) | Average gap between jobs |
| `longest_gap_minutes` | int (min) | Biggest idle period |
| `arrival_count` | int | Airport pickup legs |
| `return_count` | int | Airport return legs |
| `cruise_count` | int | Cruise transfer legs |
| `other_count` | int | Other legs |

### DemandPattern

One record per date per hour:

| Field | Type | Description |
|-------|------|-------------|
| `date` | DateField | Which day |
| `hour` | int (0–23) | Which hour |
| `day_of_week` | int (0–6) | Monday=0, Sunday=6 |
| `arrival_legs` | int | Arrivals in this hour |
| `return_legs` | int | Returns in this hour |
| `cruise_legs` | int | Cruise transfers in this hour |
| `other_legs` | int | Other legs in this hour |
| `total_legs` | int | All legs in this hour |
| `inhouse_drivers_used` | int | In-house drivers active |
| `affiliate_drivers_used` | int | Affiliate drivers active |
| `total_drivers_needed` | int | Estimated drivers needed |
| `total_revenue` | Decimal | Revenue in this hour |

---

## File Reference

| File | Lines | Purpose |
|------|-------|---------|
| `dispatching/analytics.py` | ~950 | Timing calculations, metric aggregation, batch updates |
| `dispatching/scheduler.py` | ~910 | Feasibility checking, scoring engine, schedule building |
| `reservations/models.py` | Models | RouteTimingMetric, DriverDailyCapacity, DemandPattern |
| `dispatching/views.py` | Views | capacity_planner, auto_assign_drivers, route_timing_reference |
| `dispatching/templates/dispatching/daily_capacity_planner.html` | Template | Interactive capacity planner UI |
| `dispatching/templates/dispatching/route_timing_reference.html` | Template | Metrics dashboard and recalculation |
| `dispatching/templates/dispatching/analytics_dashboard.html` | Template | Analytics overview with trends |

---

## Design Principles

1. **P75 over averages** — Conservative scheduling that accounts for real-world variability without being overly pessimistic.

2. **Composite bucketing** — Metrics are segmented by route, time of day, and day type because "MCO to Disney at 8 AM on a Monday" is fundamentally different from "MCO to Disney at 10 PM on Christmas."

3. **Minimum sample thresholds** — Statistics are only reported when enough data exists to be meaningful. No P90 from 3 trips.

4. **Cache-first scheduling** — All timing lookups during a scheduling session use an in-memory cache loaded with one query, not 200 individual lookups.

5. **Graceful degradation** — P75 data → hardcoded estimates → sensible defaults. The system always has an answer.

6. **Greedy with simulation** — The suggestion engine assigns one leg at a time, updating the simulated schedule after each assignment so future suggestions account for it.

7. **Snapshot safety net** — Every destructive operation auto-saves a snapshot first. You can always undo.

8. **Transparency** — Every suggestion includes a reason string ("Mary: 20 min buffer, same area as last job") so dispatchers understand and trust the system's logic.
