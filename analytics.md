# Analytics & Scheduling Optimization System

## Context

Grayson Towncar needs data-driven insights to optimize driver scheduling and maximize in-house driver coverage (minimizing affiliate usage). The primary challenge is **multi-leg optimization** - understanding how many jobs in-house drivers can realistically cover based on route timing patterns, demand fluctuations, and turnaround constraints.

**Key operational insight from user:**
- **Predictable**: Drive times between locations (MCO→Disney ≈ 30 min)
- **Unpredictable**: Airport dwell time (flight lands → guest in car with luggage)
- **Critical pattern**: 10 AM airport arrival → guest in car 10:30-10:50 AM → 30 min drive → can do 10:40 AM return from same area

**Current state**: Already tracking excellent data - LegStatus timestamps, flight times, driver assignments, trip types, and audit logs. Need to **analyze and surface** this data for scheduling decisions.

**Goal**: Build suggestion engine that recommends driver assignments, identifies batching opportunities, and warns when in-house capacity is exceeded (need affiliates).

---

## Phase 1: Core Analytics Foundation

### 1.1 New Database Models

Create models to cache computed metrics and historical patterns.

**File**: `reservations/models.py` (add after existing models)

#### RouteTimingMetric Model
```python
class RouteTimingMetric(models.Model):
    """
    Stores calculated average timing metrics for specific route patterns.
    Updated periodically (daily/weekly) via management command.
    """
    # Route identification
    trip_type = models.CharField(max_length=20)  # 'arrival', 'return', 'cruise', 'other'
    pickup_location_category = models.CharField(max_length=100)  # 'MCO', 'Disney Resort', 'Universal Resort', etc.
    dropoff_location_category = models.CharField(max_length=100)

    # Time-based segmentation
    time_of_day_category = models.CharField(max_length=20)  # 'early_morning', 'morning_rush', 'midday', 'afternoon', 'evening', 'night'
    day_type = models.CharField(max_length=20)  # 'weekday', 'weekend', 'holiday'

    # Calculated metrics (in minutes)
    avg_airport_dwell_time = models.IntegerField(null=True)  # Gate arrival → picked up (arrivals only)
    median_airport_dwell_time = models.IntegerField(null=True)
    p90_airport_dwell_time = models.IntegerField(null=True)  # 90th percentile for conservative estimates

    avg_drive_time = models.IntegerField(null=True)  # Picked up → completed
    median_drive_time = models.IntegerField(null=True)
    p90_drive_time = models.IntegerField(null=True)

    avg_total_time = models.IntegerField(null=True)  # Gate arrival → completed (arrivals) or pickup scheduled → completed (returns)

    # Sample size for confidence
    sample_count = models.IntegerField(default=0)
    last_calculated = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['trip_type', 'pickup_location_category', 'dropoff_location_category']),
            models.Index(fields=['time_of_day_category']),
        ]
        unique_together = [
            ['trip_type', 'pickup_location_category', 'dropoff_location_category', 'time_of_day_category', 'day_type']
        ]
```

#### DriverDailyCapacity Model
```python
class DriverDailyCapacity(models.Model):
    """
    Tracks historical driver performance to understand realistic daily capacity.
    """
    driver = models.ForeignKey('drivers.Driver', on_delete=models.CASCADE, related_name='daily_capacity_records')
    date = models.DateField()

    # Actual performance
    total_legs = models.IntegerField(default=0)
    total_revenue = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_active_hours = models.DecimalField(max_digits=5, decimal_places=2, null=True)  # First pickup → last dropoff

    # Efficiency metrics
    avg_turnaround_time = models.IntegerField(null=True)  # Minutes between jobs
    longest_gap_minutes = models.IntegerField(null=True)  # Longest idle period

    # Trip composition
    arrival_count = models.IntegerField(default=0)
    return_count = models.IntegerField(default=0)
    cruise_count = models.IntegerField(default=0)
    other_count = models.IntegerField(default=0)

    calculated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['driver', '-date']),
            models.Index(fields=['-date']),
        ]
        unique_together = ['driver', 'date']
```

#### DemandPattern Model
```python
class DemandPattern(models.Model):
    """
    Aggregated demand patterns for capacity planning.
    """
    date = models.DateField()
    hour = models.IntegerField()  # 0-23
    day_of_week = models.IntegerField()  # 0=Monday, 6=Sunday

    # Volume by trip type
    arrival_legs = models.IntegerField(default=0)
    return_legs = models.IntegerField(default=0)
    cruise_legs = models.IntegerField(default=0)
    other_legs = models.IntegerField(default=0)

    # Total metrics
    total_legs = models.IntegerField(default=0)
    total_drivers_needed = models.IntegerField(null=True)  # Calculated based on timing constraints
    inhouse_drivers_used = models.IntegerField(default=0)
    affiliate_drivers_used = models.IntegerField(default=0)

    # Revenue
    total_revenue = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    calculated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['-date', 'hour']),
            models.Index(fields=['day_of_week', 'hour']),
        ]
        unique_together = ['date', 'hour']
```

### 1.2 Analytics Calculation Functions

**File**: `dispatching/analytics.py` (new file)

Key functions to implement:

```python
# Location categorization
def categorize_location(location_text: str) -> str:
    """
    Categorize free-text location into standard categories.
    Returns: 'MCO', 'SFB', 'Disney Resort', 'Universal Resort', 'Port Canaveral',
             'Other Hotel', 'Residential', 'Other'
    """

# Time categorization
def categorize_time_of_day(time_obj) -> str:
    """
    Categorize time into scheduling-relevant buckets.
    Returns: 'early_morning' (4-7am), 'morning_rush' (7-10am), 'midday' (10am-2pm),
             'afternoon' (2-6pm), 'evening' (6-10pm), 'night' (10pm-4am)
    """

def categorize_day_type(date_obj) -> str:
    """
    Categorize day as weekday/weekend/holiday.
    Returns: 'weekday', 'weekend', 'holiday'
    """

# Core metric calculations
def calculate_airport_dwell_time(leg: Leg) -> Optional[int]:
    """
    For arrival legs with flight info: calculate minutes from actual/estimated gate arrival
    to when status changed to 'picked-up'.

    Returns: minutes (int) or None if data incomplete
    """

def calculate_drive_time(leg: Leg) -> Optional[int]:
    """
    Calculate minutes from 'picked-up' status to 'completed' status.
    Uses LegStatus history.

    Returns: minutes (int) or None if data incomplete
    """

def calculate_turnaround_time(leg1: Leg, leg2: Leg) -> Optional[int]:
    """
    Calculate minutes from leg1 completion to leg2 pickup.
    Represents driver availability gap between jobs.

    Returns: minutes (int) or None if legs don't connect
    """

# Aggregation functions
def calculate_route_timing_metrics(trip_type: str, pickup_cat: str, dropoff_cat: str,
                                   time_of_day: str, day_type: str) -> dict:
    """
    Aggregate all historical legs matching criteria and calculate:
    - avg/median/p90 airport dwell time (arrivals only)
    - avg/median/p90 drive time
    - avg total time
    - sample count

    Returns: dict with calculated metrics
    """

def calculate_driver_daily_capacity_for_date(driver: Driver, date: date) -> dict:
    """
    Analyze all legs for a driver on specific date and calculate:
    - total legs, revenue, active hours
    - turnaround times between jobs
    - trip composition

    Returns: dict with calculated metrics
    """

def calculate_demand_pattern_for_hour(date: date, hour: int) -> dict:
    """
    Aggregate all legs with pickups in specified hour and calculate:
    - volume by trip type
    - drivers used (inhouse vs affiliate)
    - revenue

    Returns: dict with calculated metrics
    """

# Batch processing
def update_all_route_timing_metrics():
    """
    Recalculate all RouteTimingMetric records from historical data.
    Run as management command: python manage.py update_route_metrics
    """

def update_daily_capacity_metrics(start_date: date, end_date: date):
    """
    Calculate DriverDailyCapacity for all drivers in date range.
    Run as management command: python manage.py update_capacity_metrics
    """

def update_demand_patterns(start_date: date, end_date: date):
    """
    Calculate DemandPattern for date range.
    Run as management command: python manage.py update_demand_patterns
    """
```

### 1.3 Management Commands

**File**: `dispatching/management/commands/update_analytics.py`

```python
# Command to run all analytics updates
python manage.py update_analytics --days=90  # Update last 90 days
python manage.py update_analytics --all      # Update all historical data
python manage.py update_analytics --daily    # Quick daily update
```

---

## Phase 2: Analytics Dashboard UI

### 2.1 New Dashboard Views

**File**: `dispatching/views.py`

```python
@login_required
def analytics_dashboard(request):
    """
    Main analytics dashboard showing key scheduling metrics.
    """
    # Display route timing reference table
    # Show driver capacity trends
    # Demand patterns visualization
    # Peak hours by route type

@login_required
def route_timing_reference(request):
    """
    Quick lookup table: route → average timings
    Filterable by time of day, day type
    """

@login_required
def driver_capacity_analysis(request):
    """
    Historical driver performance analysis.
    Shows max capacity per driver, efficiency metrics, trends.
    """

@login_required
def demand_forecast(request):
    """
    Predict busy periods based on historical patterns.
    Show upcoming days/hours with high demand.
    """
```

### 2.2 Templates

**File**: `dispatching/templates/dispatching/analytics_dashboard.html`

Key sections:
- **Route Timing Reference Card**: Quick lookup table showing average times for common routes
- **Peak Demand Chart**: Bar/line chart showing legs per hour by trip type
- **Driver Capacity Summary**: Table of drivers with avg daily capacity, max capacity, efficiency
- **Batching Opportunities**: List of upcoming jobs that could be paired for same driver

---

## Phase 3: Scheduling Suggestion Engine

### 3.1 Feasibility Checker

**File**: `dispatching/scheduler.py` (new file)

```python
class DriverJobFeasibility:
    """
    Determines if a driver can realistically do job B after job A.
    """

    @staticmethod
    def can_driver_do_both_jobs(job1: Leg, job2: Leg, driver: Driver) -> dict:
        """
        Check if driver can complete job1 and still make job2 on time.

        Returns:
            {
                'feasible': bool,
                'confidence': 'high' | 'medium' | 'low',
                'gap_minutes': int,  # Time between jobs
                'buffer_minutes': int,  # Extra time beyond minimum needed
                'warnings': [str],  # e.g., "Tight timing", "Rush hour traffic"
            }
        """

    @staticmethod
    def calculate_min_turnaround_needed(job1: Leg, job2: Leg) -> int:
        """
        Calculate minimum minutes needed between job1 completion and job2 pickup.
        Considers:
        - Drive distance between dropoff1 and pickup2
        - Traffic patterns for time of day
        - Prep time buffer (5-10 min)
        """

class DriverScheduleSuggester:
    """
    Suggests optimal driver assignments for a set of jobs.
    """

    def __init__(self, date: date):
        self.date = date
        self.available_drivers = self._get_available_drivers()
        self.unassigned_jobs = self._get_unassigned_jobs()

    def suggest_assignments(self) -> dict:
        """
        Generate driver assignment suggestions prioritizing in-house coverage.

        Returns:
            {
                'assignments': [
                    {
                        'driver': Driver,
                        'jobs': [Leg, Leg, ...],
                        'utilization': float,  # 0-1
                        'total_revenue': Decimal,
                    }
                ],
                'unassigned': [Leg, ...],  # Jobs that need affiliates
                'inhouse_coverage_pct': float,
                'warnings': [str],
            }
        """

    def optimize_for_inhouse_coverage(self):
        """
        Maximize jobs covered by in-house drivers.
        Algorithm:
        1. Sort jobs by pickup time
        2. For each job, find in-house driver with most compatible schedule
        3. If no in-house driver feasible, mark for affiliate
        4. Track utilization to avoid overloading drivers
        """
```

### 3.2 Capacity Planner View

**File**: `dispatching/views.py`

```python
@login_required
def daily_capacity_planner(request):
    """
    Interactive capacity planning for specific date.

    Features:
    - Shows all jobs for date grouped by time
    - Displays available in-house drivers
    - Suggests assignments with feasibility indicators
    - Highlights when affiliate drivers needed
    - Allows manual override/adjustment
    - Shows coverage percentage (in-house vs affiliate)
    """
```

**File**: `dispatching/templates/dispatching/daily_capacity_planner.html`

Interface:
```
┌─────────────────────────────────────────────────────────┐
│  📅 Daily Capacity Planner - Feb 15, 2026              │
├─────────────────────────────────────────────────────────┤
│  Total Jobs: 47    In-House: 38 (81%)  Affiliate: 9    │
│                                                          │
│  ⚠️  Peak Hours: 9-11 AM (18 arrivals)                  │
│  ✅  In-house coverage: 38/47 jobs (81%)                │
│  💡  Recommendation: Hire 1 more driver for 9-11 AM     │
└─────────────────────────────────────────────────────────┘

📊 Timeline View:
8:00 AM ─────────────────────────────────────
  ✈️ Arrival MCO → Disney (10 jobs)
     ✅ Driver: Mike (3 jobs) - 100% capacity
     ✅ Driver: Sarah (2 jobs) - 67% capacity
     ⚠️  Driver: John (3 jobs) - TIGHT - 95% capacity
     🔴 NEED AFFILIATE: 2 jobs (no drivers available)

  🏨 Return Disney → MCO (3 jobs)
     ✅ Driver: Lisa (2 jobs) - 80% capacity
     💡 BATCHING: Jobs #234, #235 (same resort, 20 min apart)

[View Detailed Schedule] [Export CSV] [Send to Drivers]
```

---

## Phase 4: Integration & Automation

### 4.1 Real-Time Updates

Update analytics incrementally when new legs completed:

**File**: `dispatching/signals.py` (new file)

```python
from django.db.models.signals import post_save
from reservations.models import LegStatus

@receiver(post_save, sender=LegStatus)
def update_metrics_on_status_change(sender, instance, created, **kwargs):
    """
    When leg status changes to 'completed', trigger incremental analytics update.
    """
    if instance.status == 'completed':
        # Update route timing metric for this route
        # Update driver daily capacity for today
        # Check if demand pattern needs recalculation
```

### 4.2 Daily Automation

**File**: `dispatching/management/commands/daily_analytics_update.py`

Cron job (run at 1 AM daily):
```bash
python manage.py daily_analytics_update
```

Updates:
- Previous day's completed legs → update route metrics
- Previous day's driver performance → update capacity records
- Previous day's demand pattern
- Rolling 90-day averages

### 4.3 API Endpoints for Scheduling UI

**File**: `dispatching/urls.py`

```python
path('api/check-driver-feasibility/', views.api_check_driver_feasibility, name='api_check_driver_feasibility'),
path('api/suggest-assignments/<str:date>/', views.api_suggest_assignments, name='api_suggest_assignments'),
path('api/route-timing/<str:route_key>/', views.api_get_route_timing, name='api_get_route_timing'),
```

---

## Phase 5: Future Enhancements

### Machine Learning Integration (Phase 2+)
- Predict flight delay probability based on airline, route, time, weather
- Predict no-show probability based on customer history, payment status
- Optimize driver assignments with reinforcement learning
- Demand forecasting with seasonal patterns, events, holidays

### Real-Time Tracking Integration (Phase 2+)
- GPS tracking of drivers → actual drive times vs. estimated
- Live traffic integration → dynamic turnaround adjustments
- Customer ETA updates based on live position

### Mobile Driver App Integration (Phase 2+)
- Push suggested next jobs to drivers
- Drivers can accept/decline suggestions
- Auto-update availability based on job completion

---

## Implementation Priority

### Week 1-2: Foundation
1. Create new models (RouteTimingMetric, DriverDailyCapacity, DemandPattern)
2. Migration files
3. Core calculation functions in analytics.py
4. Management command to calculate all historical data

### Week 3-4: Dashboard
1. Analytics dashboard UI
2. Route timing reference view
3. Driver capacity analysis view
4. Basic demand pattern visualization

### Week 5-6: Suggestion Engine
1. Feasibility checker logic
2. Daily capacity planner view
3. Assignment suggestion algorithm
4. Interactive UI for manual override

### Week 7+: Automation & Refinement
1. Real-time metric updates (signals)
2. Daily cron job
3. API endpoints
4. Fine-tune algorithms based on real usage

---

## Critical Files to Modify

### New Files
- `dispatching/analytics.py` - Core analytics functions
- `dispatching/scheduler.py` - Scheduling suggestion engine
- `dispatching/signals.py` - Real-time updates
- `dispatching/management/commands/update_analytics.py`
- `dispatching/management/commands/daily_analytics_update.py`
- `dispatching/templates/dispatching/analytics_dashboard.html`
- `dispatching/templates/dispatching/route_timing_reference.html`
- `dispatching/templates/dispatching/daily_capacity_planner.html`

### Modified Files
- `reservations/models.py` - Add RouteTimingMetric, DriverDailyCapacity, DemandPattern models
- `dispatching/views.py` - Add analytics and capacity planner views
- `dispatching/urls.py` - Add new routes
- `dispatching/utils.py` - May reuse some existing statistics functions

---

## Verification Plan

### 1. Data Accuracy Testing
```bash
# Run analytics on known date with verified data
python manage.py update_analytics --date=2026-01-15

# Verify calculations match manual counts:
# - Count legs for specific route on specific date
# - Calculate average times manually
# - Compare with RouteTimingMetric records
```

### 2. Feasibility Checker Testing
```python
# Test with real scenarios:
job1 = Leg.objects.get(id=123)  # 10 AM MCO arrival → Disney
job2 = Leg.objects.get(id=124)  # 10:45 AM Disney return → MCO
result = DriverJobFeasibility.can_driver_do_both_jobs(job1, job2, driver)
# Should return feasible=True, high confidence, ~40 min gap
```

### 3. Suggestion Engine Testing
```python
# Test assignment suggestions for busy day:
suggester = DriverScheduleSuggester(date='2026-02-15')
suggestions = suggester.suggest_assignments()

# Verify:
# - All jobs assigned or marked for affiliate
# - No driver overbooked
# - In-house coverage maximized
# - No impossible turnarounds
```

### 4. UI/Dashboard Testing
- Load analytics dashboard → verify charts render correctly
- Load capacity planner for specific date → verify job groupings
- Test route timing lookup → verify accurate averages displayed
- Test demand forecast → verify predictions reasonable

### 5. Performance Testing
- Run analytics update on all historical data → measure time
- Load dashboard with 1000+ legs → verify query performance
- Check for N+1 queries in capacity planner view

---

## Success Metrics

After implementation, measure:
1. **In-house coverage increase**: % of jobs covered by in-house drivers (target: 80%+)
2. **Driver utilization**: Average legs per driver per day (target: 4-6)
3. **Planning time reduction**: Time to create daily schedule (target: < 30 min)
4. **Scheduling accuracy**: % of suggested assignments accepted without changes (target: 90%+)
5. **Affiliate cost reduction**: Monthly spend on affiliate drivers (target: -20%)

---

## Questions for User Approval

Before implementation, confirm:

1. **Model design**: Are the proposed models (RouteTimingMetric, DriverDailyCapacity, DemandPattern) capturing the right data?

2. **Time granularity**: Is hourly demand tracking sufficient, or need 15/30 minute buckets?

3. **Categorization**: Are the proposed location categories (MCO, Disney Resort, Universal Resort, Port Canaveral, etc.) comprehensive for your routes?

4. **Buffer times**: What minimum buffer between jobs is safe? (Currently assuming 10 min prep + drive time)

5. **UI priorities**: Which dashboard is most critical first - analytics overview, capacity planner, or route timing reference?

6. **Affiliate integration**: Should system automatically notify/book affiliates for uncovered jobs, or just flag them?