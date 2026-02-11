# Vehicle Reservation Fix — How & Why

## The Problem

On 2/11, the auto-assign scheduler gave Alex (the only Van driver) an 8:05 AM Towncar arrival instead of saving him for 9:00 AM Van returns. Result: 3 Van returns went unassigned, 11 total unassigned legs.

**Why it happened** — three interacting bugs:

### Bug 1: Wrong scarcity metric for reservation

The reservation pre-scan counted "compatible" drivers (anyone whose tier is high enough to do Van jobs). Van scarcity = 3 (Alex + David + Junaid can all do Van jobs). Since 3 > `reserve_max_scarcity` (2), reservation never triggered for Alex.

**Fix**: Use `exact_type_driver_counts` — how many drivers ARE a Van (answer: 1, just Alex). 1 ≤ 2 triggers reservation.

**Code**: `dispatching/scheduler.py`, line ~614:
```python
exact_type_driver_counts = {}
for dvtype in driver_vtypes.values():
    if dvtype:
        exact_type_driver_counts[dvtype] = exact_type_driver_counts.get(dvtype, 0) + 1
```

### Bug 2: Penalty was too soft

Even with the reservation triggered, the -60 penalty wasn't enough. Here's why:

Within hour 8, the processing order is: returns → other → arrivals. By the time the 8:05 AM arrival was scored (9th job in the hour), all 4 SUV drivers were already consumed:

```
Hour 8 processing order:
1. 8:00 return  → Neuma  (SUV consumed)
2. 8:30 return  → Julio  (SUV consumed)
3. 8:15 other   → Robert (SUV consumed)
4. 8:30 other   → runer  (MiniVan consumed)
5. 8:05 arrival → Only Alex, David, Junaid, Angel left
                   (Angel infeasible — busy with 7:45 return)
                   All remaining drivers are RESERVED
                   → -60 penalty is irrelevant (all have it)
                   → Highest scored reserved driver wins → Alex
```

**Fix**: Changed from a score penalty to a **hard skip with fallback**. Reserved-mismatch drivers are separated into a fallback pool and only used if NO non-reserved driver is feasible.

**Code**: `dispatching/scheduler.py`, lines ~758-778:
```python
if is_reserved_mismatch:
    # Reserved driver — track as fallback only
    score += cfg.reserve_penalty
    if score > best_reserved_score:
        best_reserved_id = did
        best_reserved_score = score
else:
    # Normal candidate — track as primary
    if score > best_score:
        best_id = did
        best_score = score

# After all drivers scored:
if not best_id and best_reserved_id:
    best_id = best_reserved_id  # use fallback only if no primary
```

### Bug 3: Processing order consumed all flexible drivers first

Even with hard skip, the processing order (returns/other before arrivals) consumed all SUV drivers before arrivals were even scored. So the hard skip kept Alex out of the 8:05 arrival, but then nobody else could take it either.

**Fix**: Two-pass processing order. Scarce vehicle types (Van, Van14) are processed FIRST, before general jobs. This ensures Alex gets his Van returns early, leaving SUV drivers free for later arrivals.

**Code**: `dispatching/scheduler.py`, lines ~638-672:
```python
half_fleet = max(len(working) // 2, 3)

def _two_pass_sort_key(leg):
    leg_vtype = getattr(...)
    pass_priority = 1  # Pass 2 (normal)
    if leg_vtype:
        exact_count = exact_type_driver_counts.get(str(leg_vtype), 0)
        eligible = scarcity_map.get(leg.id, len(working))
        if 0 < exact_count <= cfg.reserve_max_scarcity and eligible <= half_fleet:
            pass_priority = 0  # Pass 1 (truly scarce)
    return (pass_priority, hour, type_priority, pickup_time)

sorted_legs = sorted(sorted_legs, key=_two_pass_sort_key)
```

**Why the "eligible ≤ half fleet" check?** Without it, mini_van legs (1 exact driver but ALL 8 eligible) would go to Pass 1, consuming SUV drivers as fallback before Pass 2 even starts. The check ensures only TRULY scarce types (Van: 3 eligible, Van14: 2 eligible) go to Pass 1, while broadly-compatible types (mini_van: 8 eligible) stay in Pass 2.

---

## Results

| Metric | Before | After |
|--------|--------|-------|
| Assigned | 57 | **58** |
| Unassigned | 11 | **10** |
| Van returns at 9:00-9:30 | 0/3 assigned | **3/3 assigned** |
| Alex's exact Van matches | 0 | **4** |
| Van14 exact matches | 1/3 | **2/3** |

### Alex's schedule comparison

**Before**: 8:05 AM Towncar arrival → busy until ~10:00 AM → missed ALL Van returns

**After**: 9:00 AM Van return [EXACT] → 11:20 AM Van arrival [EXACT] → 1:30 PM Van arrival [EXACT] → 3:59 PM Van arrival [EXACT] → plus 3 general jobs

### Where the 10 remaining unassigned legs are

| Time | Type | Vehicle | Why |
|------|------|---------|-----|
| 10:00 AM | other | Van(14 Pax) | David/Junaid busy at 10:00 (just finished 9:30 returns) |
| 8:30 AM | arrival | mini_van | Hour-8 driver crunch (all consumed by returns/other/arrivals) |
| 8:45 AM | arrival | towncar | Same crunch |
| 8:55 AM | arrival | mini_van | Same crunch |
| 9:00 AM | return | suv | All drivers busy (just took hour-8 jobs) |
| 9:30 AM | cruise | suv | Same |
| 10:41 AM | arrival | towncar | Timing conflict |
| 11:20 AM | arrival | suv | No driver available in window |
| 2:25 PM | arrival | suv | No driver available |
| 9:03 PM | arrival | towncar | Evening gap |

---

## How to Tune This Further

### If you want MORE jobs in Pass 1

Increase `reserve_max_scarcity` in the Scheduler Settings tuning panel. Default is 2. Setting to 3 would include vehicle types with up to 3 exact-type drivers.

### If you want the fallback to be stricter

Increase `reserve_penalty` magnitude (e.g., -100 instead of -60). This pushes fallback candidates lower in ranking but doesn't change when they're used (they're still last resort).

### If you want to completely prevent fallback

Set `reserve_penalty` to a very large negative number (e.g., -9999). The fallback driver would score so low that even "no in-house driver available" might be preferred. But this risks more jobs going to affiliate.

### If a new vehicle type is added

The system adapts automatically. When a new driver with a new vehicle type is added:
- `exact_type_driver_counts` recalculates (1 new type = 1 exact driver)
- If the new type has ≤ 2 exact drivers AND ≤ half fleet eligible, it goes to Pass 1
- The new driver gets reserved for matching jobs automatically

---

## File Reference

| File | Lines | What Changed |
|------|-------|-------------|
| `dispatching/scheduler.py` | ~614-617 | `exact_type_driver_counts` pre-computation |
| `dispatching/scheduler.py` | ~619-636 | Reservation counting uses exact-type, not general scarcity |
| `dispatching/scheduler.py` | ~638-672 | Two-pass sort (scarce types first) |
| `dispatching/scheduler.py` | ~662-670 | `is_reserved_mismatch` detection |
| `dispatching/scheduler.py` | ~758-778 | Hard skip + fallback pool logic |
| `dispatching/scheduler.py` | ~817-822 | Post-assignment decrement uses exact-type |
| `dispatching/scheduler.py` | ~1047-1050 | `build_smart_schedule()` exact-type counting |
| `docs/auto-assign-deep-dive.md` | Phase 1,2d,3 | Updated documentation |

## Key Concepts for Future Reference

**Exact-type count vs compatible count**: `exact_type_driver_counts` asks "how many drivers ARE this type?" while `scarcity_map` asks "how many drivers CAN DO this type?" For reservation, exact-type is correct because you want to save the Van driver for Van jobs even though Van14 drivers could theoretically help.

**Hard skip vs penalty**: A penalty reduces a driver's score but they can still win if they're the only option. A hard skip removes them from the primary competition entirely, making them last-resort only. This prevents the "all remaining drivers are penalized so penalty is irrelevant" problem.

**Two-pass processing**: Ensures specialized drivers are assigned their matching jobs BEFORE general jobs consume them. Without it, returns/other in hour 8 would consume all SUV drivers, leaving only specialized drivers for arrivals.

**Half-fleet threshold**: Prevents broadly-compatible types (like mini_van, which ALL drivers can do) from going to Pass 1. Only types with genuinely limited eligible drivers (Van: 3/8, Van14: 2/8) qualify.
