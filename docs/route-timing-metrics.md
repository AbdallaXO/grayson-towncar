# Route Timing Metrics — How It Works

## What Is It?

Route Timing Metrics is a system that **learns how long each type of trip actually takes** based on your completed trips. Instead of guessing "MCO to Disney is about 30 minutes", the system looks at every completed trip on that route and gives you real numbers.

It tracks two things per trip:

- **Dwell Time** (arrivals only) — How long from the flight landing to the passenger being picked up. This includes walking through the terminal, getting bags, meeting the driver, etc.
- **Drive Time** — How long from picking up the passenger to completing the trip (dropping them off).

---

## Where Does the Data Come From?

Every trip goes through status updates that drivers tap in the app:

```
Confirmed → On the Way → On Location → Picked Up → Completed
```

The system measures the gaps between these timestamps:

| Measurement | How It's Calculated |
|---|---|
| **Dwell Time** | Flight gate arrival time (from AeroAPI) → driver taps "Picked Up" |
| **Drive Time** | Driver taps "Picked Up" → driver taps "Completed" |
| **Total Time** | Dwell + Drive (arrivals) or just Drive (returns/cruise/other) |

**Important:** The flight gate arrival time comes from the AeroAPI flight tracking system — this is very accurate. The "Picked Up" and "Completed" times depend on drivers tapping the buttons promptly, which is why we have outlier filtering (more on that below).

---

## How Are Routes Categorized?

The system doesn't track every individual address. Instead, it groups locations into categories:

| Category | Examples |
|---|---|
| **MCO Terminal** | Orlando International Airport terminals, baggage claim, airline names |
| **SFB Terminal** | Orlando Sanford Airport |
| **Airport Hotel** | Hyatt Regency MCO, Marriott Airport, etc. |
| **Disney Resort** | All Disney hotels + parks (Contemporary, Polynesian, Magic Kingdom, etc.) |
| **Universal Resort** | All Universal hotels + parks (Hard Rock, Portofino, CityWalk, etc.) |
| **Port Canaveral Area** | Cruise terminals, Cocoa Beach, Cape Canaveral |
| **Other Hotel** | Any non-Disney, non-Universal, non-airport hotel |
| **Residential** | Street addresses, homes, apartments |
| **Other** | Anything that doesn't match the above |

So a trip from "MCO Terminal A, Baggage Claim" to "Disney's Contemporary Resort" becomes **MCO Terminal → Disney Resort**.

### Time Buckets

Each route is further split by when the trip happens:

| Time Period | Hours |
|---|---|
| Early Morning | 4 AM – 7 AM |
| Morning Rush | 7 AM – 10 AM |
| Midday | 10 AM – 2 PM |
| Afternoon | 2 PM – 6 PM |
| Evening | 6 PM – 10 PM |
| Night | 10 PM – 4 AM |

### Day Type

| Type | When |
|---|---|
| Weekday | Monday – Friday (non-holiday) |
| Weekend | Saturday – Sunday |
| Holiday | US federal holidays |

This means the system knows that "MCO → Disney on a weekday morning rush" might take longer than "MCO → Disney on a weekend midday" because of I-4 traffic.

---

## How Does It Clean Bad Data?

Drivers sometimes forget to tap buttons on time, or tap them late. A 30-minute drive might show as 90 minutes because the driver completed it late. The system uses **three layers of protection**:

### Layer 1: Hard Thresholds

Any value that's clearly wrong gets thrown out immediately:

- Dwell time > 2 hours → discarded
- Drive time > 4 hours → discarded
- Total time > 6 hours → discarded

### Layer 2: Adaptive Per-Route Max

Once the system has 10+ trips on a route, it looks at the 90th percentile (P90) and sets a tighter ceiling at P90 x 1.5.

**Example:** If the MCO → Disney route has a P90 drive time of 38 minutes, the adaptive max becomes 57 minutes. A value of 65 minutes would be thrown out even though it's under the 4-hour hard threshold.

### Layer 3: IQR Outlier Filtering

The standard statistical method for finding outliers. Once there are 5+ data points:

1. Find Q1 (25th percentile) and Q3 (75th percentile)
2. Calculate IQR = Q3 - Q1
3. Remove anything below Q1 - 1.5 x IQR or above Q3 + 1.5 x IQR

**Example:** If MCO → Disney drive times are [28, 29, 30, 31, 32, 33, 85], the IQR method correctly identifies 85 as an outlier and removes it. The median stays accurate at ~31 min instead of being pulled up to 33.

---

## What Numbers Does It Produce?

For each route + time period + day type combination, the system calculates:

| Metric | What It Means | When to Use It |
|---|---|---|
| **Median** | The middle value — half of trips are faster, half are slower | Best single number for "how long does this usually take" |
| **P75** | 75% of trips finish within this time | Good for planning buffer time, what the scheduler uses |
| **Average** | Mathematical average of all clean data points | Can be skewed by a few long trips, less reliable than median |
| **P90** | 90% of trips finish within this time | Conservative estimate, worst-case planning |
| **Sample Count** | How many completed trips this is based on | More samples = more confidence in the numbers |

### Confidence Levels

| Samples | Confidence | What It Means |
|---|---|---|
| 20+ | High | Very reliable, enough data to trust |
| 10–19 | Medium | Decent estimate, could shift with more data |
| 5–9 | Low | Rough estimate, use with caution |
| < 5 | None | Not enough data yet, treat as a guess |

---

## How Does the Scheduler Use This?

The auto-assign schedule builder uses route timing metrics to estimate how long each leg will take. This is critical for:

1. **Knowing when a driver will be free** — If a driver picks up at MCO at 2:00 PM and the route to Disney takes ~35 min (P75), the scheduler knows the driver will be available around 2:35 PM.

2. **Avoiding schedule overlaps** — Won't assign a driver a 2:30 PM pickup at Universal if their Disney dropoff won't finish until 2:35 PM.

3. **Airport dwell time** — For arrivals, the scheduler adds dwell time (gate → pickup) to the estimate. A flight landing at 1:00 PM with 25 min median dwell means pickup around 1:25 PM, then drive time on top of that.

The scheduler prefers **P75 values** when available (covers 75% of cases with buffer), falls back to **average**, and if no data exists at all, uses **hardcoded estimates** like:

| Route | Hardcoded Estimate |
|---|---|
| MCO → Disney Resort | 30 min |
| MCO → Universal Resort | 25 min |
| MCO → Port Canaveral | 55 min |
| Disney → Port Canaveral | 70 min |
| MCO → Airport Hotel | 12 min |
| MCO → Residential | 30 min |

As more trips are completed, the data-driven P75 values replace these hardcoded guesses.

---

## How Are Metrics Updated?

### Automatic (Incremental)

Every time a driver marks a leg as **"Completed"** on the dispatching dashboard, the system automatically recalculates the metrics for that specific route bucket. This happens in the background — no action needed.

### Manual (Batch Recalculation)

For a full recalculation of all route metrics from scratch, run via Django shell:

```python
from dispatching.analytics import update_all_route_timing_metrics

# Recalculate from ALL completed legs
update_all_route_timing_metrics()

# Or only from the last 90 days
update_all_route_timing_metrics(recent_days=90)
```

---

## Using the Route Timing Page

Go to **Dispatching → Route Timing** (`/dispatching/route-timing/`).

### Default View (Pre-Computed)

Shows all stored route metrics grouped by route cards. Each card shows the route (e.g., "MCO Terminal → Disney Resort") with rows for each time period and day type.

**Columns:**
- **Time Period** — When (Early Morning, Midday, etc.)
- **Type** — Trip type (Arrival, Return, Cruise)
- **Samples** — How many completed trips
- **Dwell** — Median and P75 airport wait time (arrivals only)
- **Drive** — Median and P75 drive time
- **Total** — Median and P75 total time

### Filtered View (Live Computation)

When you apply any of these filters, the system computes metrics **on-the-fly** from raw completed legs (shown with a "Live" badge):

- **Driver** — See timing metrics for a specific driver. Useful for comparing driver performance.
- **Team** — Filter to in-house team only.
- **Date From / Date To** — Look at a specific time range. Useful for seeing if times have improved/worsened.
- **Trip Type** — Only arrivals, only returns, etc.
- **Pickup / Dropoff** — Filter to specific location categories.
- **Min Samples** — Only show routes with at least N completed trips.

### Practical Uses

**For dispatching:**
- Know how long each route really takes so you can plan driver schedules better
- Identify which time periods have longer drive times (rush hour impact)
- Set realistic pickup time expectations for customers

**For driver performance:**
- Filter by a specific driver to see their average times vs the team
- Identify if a driver consistently takes longer on certain routes

**For pricing and quoting:**
- Use median total time to validate your pricing for different routes
- Port Canaveral trips take 55+ min drive time — price accordingly

**For improving accuracy over time:**
- The more completed trips with proper status updates, the better the data gets
- Encourage drivers to tap "Picked Up" and "Completed" promptly
- The IQR filtering automatically handles occasional late taps

---

## Key Files

| File | What It Does |
|---|---|
| `dispatching/analytics.py` | Core calculation functions — dwell time, drive time, outlier filtering, batch updates |
| `dispatching/scheduler.py` | Uses metrics for schedule building (drive time estimates, dwell time estimates) |
| `dispatching/views.py` | `route_timing_reference` view — handles filtering and on-the-fly computation |
| `dispatching/templates/dispatching/route_timing_reference.html` | The route timing page UI |
| `reservations/models.py` | `RouteTimingMetric` model — stores pre-computed metrics in the database |
