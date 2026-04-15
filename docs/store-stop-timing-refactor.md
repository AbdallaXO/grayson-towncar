# Refactor Store-Stop Route Timing to Segmented Trip Model

## Context

Airport arrival trips with a Publix grocery stop currently calculate timing incorrectly. The system adds a flat 25-minute constant (`PUBLIX_STOP_MINUTES`) on top of the **direct** Airport→Destination drive time. But the real trip is Airport→Publix→Destination — a different route with different total drive time.

**Current formula (inaccurate):**
```
dwell + drive(Airport → Destination) + 25 min flat
```

**Correct formula:**
```
dwell + drive(Airport → Publix) + Publix dwell + drive(Publix → Destination)
```

**Why this matters:** Depending on destination direction relative to Publix, the current approach can significantly underestimate trip duration. Example:
- MCO → Airport Hotel (near MCO, opposite direction from Publix): Current = 45 dwell + 12 drive + 25 flat = **82 min**. Real = 45 dwell + 20 drive-to-Publix + 18 Publix-dwell + 18 drive-from-Publix = **101 min** (19 min underestimate → scheduling conflict risk).

---

## Plan

### 1. Add Publix drive time estimates to `DRIVE_TIME_ESTIMATES`
**File:** `dispatching/scheduler.py` lines 20-69

Add entries for `'Publix Store'` as a location category (used only as an intermediate waypoint, never as a real leg pickup/dropoff). No change to `categorize_location()` needed — Publix is never a leg endpoint.

```python
# Publix Store (Lake Cay Commons, 9930 Universal Blvd, Orlando)
('MCO Terminal', 'Publix Store'): 20,
('Publix Store', 'MCO Terminal'): 20,
('SFB Terminal', 'Publix Store'): 50,
('Publix Store', 'SFB Terminal'): 50,
('Publix Store', 'Disney Resort'): 25,
('Publix Store', 'Universal Resort'): 8,
('Publix Store', 'Port Canaveral Area'): 60,
('Publix Store', 'Other Hotel'): 15,
('Publix Store', 'Residential'): 25,
('Publix Store', 'Airport Hotel'): 18,
('Publix Store', 'Other'): 20,
```

### 2. Add configurable `publix_stop_dwell_minutes` to SchedulerSettings
**File:** `dispatching/models.py` line 104

Add field in the "Global" section, next to `inter_job_buffer` and `arrival_grace_minutes`:
```python
publix_stop_dwell_minutes = models.IntegerField(
    default=18,
    help_text="Minutes at Publix during a store stop (shopping time, not drive time)"
)
```
Then run `makemigrations` + `migrate`.

### 3. Update constant in scheduler.py
**File:** `dispatching/scheduler.py` line 452

Replace `PUBLIX_STOP_MINUTES = 25` with:
```python
PUBLIX_STOP_DWELL_MINUTES = 18  # Fallback: time inside Publix (not drive time)
PUBLIX_LOCATION_CATEGORY = 'Publix Store'  # Intermediate waypoint category
```

### 4. Refactor `estimate_job_end_time()` — core fix
**File:** `dispatching/scheduler.py` lines 499-508

Replace the arrival store-stop block with segmented routing:
- If `store_stop`: compute `drive(pickup_cat → 'Publix Store')` + `publix_dwell` (from SchedulerSettings) + `drive('Publix Store' → dropoff_cat)`
- If no store_stop: keep existing `dwell + drive(pickup → dropoff)` unchanged

### 5. Refactor `get_clearing_breakdown()` — diagnostic formula
**File:** `dispatching/scheduler.py` lines 546-568

Update the arrival section to show the segmented formula when store_stop is true:
```
"3:00 PM (flight) + 45min dwell + 20min drive→Publix + 18min Publix stop + 25min drive→dest = 4:48 PM"
```
Add new breakdown keys: `drive_to_publix`, `publix_dwell_minutes`, `drive_from_publix`. Keep `store_stop_minutes` key for backward compat.

### 6. No changes needed elsewhere
- **`check_feasibility()`** (`scheduler.py` line 605): Already calls `estimate_job_end_time()` — gets the fix automatically.
- **Analytics exclusion** (`analytics.py` lines 655-658): Store-stop legs already excluded from route timing metrics. No change.
- **Templates**: Only use `store_stop` as a boolean for display badges. No template references the breakdown dict keys.
- **`categorize_location()`**: Not modified — Publix is never a leg pickup/dropoff, only an intermediate waypoint constant.

---

## Files Modified
| File | Change |
|------|--------|
| `dispatching/scheduler.py` | Add Publix drive time estimates, refactor timing logic |
| `dispatching/models.py` | Add `publix_stop_dwell_minutes` field to SchedulerSettings |
| New migration | Auto-generated for the new field |

## Verification
1. Run `python manage.py makemigrations dispatching && python manage.py migrate`
2. Check the clearing breakdown for an arrival+store-stop leg shows the segmented formula
3. Compare old vs new estimates for several destination types:
   - Airport Hotel (near MCO, opposite Publix) — should increase significantly
   - Disney Resort — should change moderately
   - Universal Resort (near Publix) — should decrease slightly
4. Verify feasibility checks still work by testing driver assignment on the planner board
5. Confirm `publix_stop_dwell_minutes` appears in SchedulerSettings admin and can be tuned
