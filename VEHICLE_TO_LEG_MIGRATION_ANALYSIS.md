# Vehicle Migration from Reservation to Leg - Complexity Analysis

## Current Structure

**Reservation Model:**
- `vehicle` (ForeignKey) - Currently at reservation level
- `passenger_count`, `luggage_count`
- `need_carseats`, `rf_carseats`, `ff_carseats`, `booster_seats` - Carseat fields at reservation level
- `base_price`, `total_price` - Pricing at reservation level

**Leg Model:**
- `reservation` (ForeignKey) - Links to parent reservation
- `driver`, `pickup_location`, `dropoff_location`, `pickup_date`, `pickup_time`
- `driver_pay_amount`, `revenue_share`, `profit_estimate`
- **NO vehicle field currently**

## Proposed Change

Move `vehicle` from `Reservation` to `Leg`, allowing:
- Different vehicles for different legs (e.g., Van for leg 1, SUV for leg 2)
- Per-leg carseat requirements
- Per-leg vehicle capacity validation

## Complexity Assessment: **MEDIUM-HIGH** ⚠️

### 1. Database Changes (EASY)
- ✅ Add `vehicle` ForeignKey to `Leg` model
- ✅ Add carseat fields to `Leg` (or keep on Reservation for backward compatibility)
- ✅ Migration to copy existing `reservation.vehicle` to all `leg.vehicle` for 3k reservations
- ⚠️ Keep `reservation.vehicle` temporarily (nullable) for backward compatibility during transition

### 2. Code Changes (EXTENSIVE - ~20-30 files)

**Files that reference `reservation.vehicle`:**
- `reservations/models.py` - Model definition, validation
- `reservations/admin.py` - Admin displays (3+ places)
- `reservations/forms.py` - Form validation
- `reservations/views.py` - View logic
- `reservations/validator.py` - Vehicle capacity validation
- `reservations/utils.py` - Utility functions
- `dispatching/forms.py` - Dispatcher forms
- `dispatching/views.py` - Dispatcher booking creation
- `reservations/templates/` - Template displays
- `content/static/js/validate-vehicle-limits.js` - Frontend validation

**Key Changes Needed:**
1. **Validation Logic** - Move from reservation-level to leg-level
2. **Pricing Logic** - May need per-leg pricing if vehicles differ significantly
3. **Admin Interface** - Update displays to show vehicle per leg
4. **Forms** - Update booking forms to select vehicle per leg
5. **Templates** - Update all vehicle displays

### 3. Data Migration for 3k Reservations (MEDIUM)

**Strategy:**
```python
# Migration script
for reservation in Reservation.objects.all():
    vehicle = reservation.vehicle
    if vehicle:
        # Copy vehicle to all legs
        reservation.legs.update(vehicle=vehicle)
```

**Considerations:**
- ✅ Simple copy operation
- ⚠️ Need to handle reservations with no vehicle (set to None)
- ⚠️ Need to handle reservations with no legs (edge case)
- ✅ Can run during low-traffic period
- ⚠️ May take 1-5 minutes for 3k reservations

### 4. Business Logic Impact (MEDIUM)

**Pricing:**
- Currently: Reservation has one `total_price` based on one vehicle
- Challenge: If legs have different vehicles, pricing may need adjustment
- Solution: Keep reservation-level pricing, but validate per-leg vehicle capacity

**Carseats:**
- Currently: Reservation-level carseat requirements
- Challenge: Different vehicles may have different carseat capacities
- Solution: Move carseat fields to Leg, validate against leg's vehicle

**Validation:**
- Currently: `validate_vehicle_constraints()` checks reservation against vehicle
- Change: Need to validate each leg against its vehicle
- Impact: More complex validation logic

### 5. User Experience Changes (MEDIUM)

**Booking Form:**
- Currently: Select vehicle once for entire reservation
- Change: Select vehicle for each leg (or default to same vehicle)
- UX Impact: More complex form, but more flexible

**Admin Interface:**
- Currently: Vehicle shown at reservation level
- Change: Vehicle shown per leg in leg list
- Impact: Better visibility, but more data to display

## Migration Strategy

### Phase 1: Add Fields (Non-Breaking)
1. Add `vehicle` to `Leg` (nullable)
2. Add carseat fields to `Leg` (nullable)
3. Keep `reservation.vehicle` (for backward compatibility)
4. Update code to prefer `leg.vehicle` but fallback to `reservation.vehicle`

### Phase 2: Data Migration
1. Copy `reservation.vehicle` to all `leg.vehicle`
2. Copy carseat fields from reservation to legs
3. Verify data integrity

### Phase 3: Update Code
1. Update all references from `reservation.vehicle` to `leg.vehicle`
2. Update validation to check per-leg
3. Update forms and templates
4. Test thoroughly

### Phase 4: Cleanup (Optional)
1. Remove `reservation.vehicle` field (after all code updated)
2. Remove reservation-level carseat fields (if moved to leg)

## Risk Assessment

### Low Risk ✅
- Database migration (straightforward)
- Data migration (simple copy operation)

### Medium Risk ⚠️
- Code changes (many files, but straightforward)
- Validation logic updates
- Template updates

### High Risk 🔴
- **Pricing logic** - If different vehicles have different rates, pricing becomes complex
- **User confusion** - More complex booking form
- **Edge cases** - Reservations with no legs, legs with no vehicle, etc.

## Estimated Effort

- **Database Migration:** 1-2 hours
- **Code Updates:** 8-16 hours (depending on test coverage)
- **Testing:** 4-8 hours
- **Data Migration:** 1 hour (including verification)
- **Total:** 14-27 hours (2-4 days)

## Recommendations

### ✅ DO IT IF:
- You frequently need different vehicles for different legs
- You want per-leg carseat management
- You have time for thorough testing
- You can handle the complexity in booking forms

### ⚠️ CONSIDER ALTERNATIVES IF:
- Most reservations use the same vehicle for all legs
- You want to minimize code changes
- You're close to a major release

### 🔴 DON'T DO IT IF:
- You can't afford downtime for migration
- You don't have time for thorough testing
- The business case isn't strong enough

## Alternative Approach: Hybrid Model

Keep vehicle on Reservation as "default" but allow override on Leg:
- Reservation.vehicle = default vehicle for all legs
- Leg.vehicle = override (nullable, defaults to reservation.vehicle)
- Simpler migration, less code change
- Still allows per-leg vehicles when needed

## Questions to Answer First

1. **How often do you need different vehicles per leg?** (If <10%, maybe not worth it)
2. **Do different vehicles have different pricing?** (If yes, pricing logic becomes complex)
3. **Can you handle more complex booking forms?** (User experience impact)
4. **Do you have time for thorough testing?** (Critical for 3k reservations)

## Conclusion

**Complexity: MEDIUM-HIGH**
**Risk: MEDIUM**
**Effort: 2-4 days**
**Value: HIGH (if you need the flexibility)**

The migration is **feasible** but requires careful planning and thorough testing. The biggest challenges are:
1. Updating all code references
2. Handling pricing if vehicles differ
3. User experience changes in booking flow

Recommendation: **Proceed with Phase 1 (add fields) first**, then evaluate if you need the full migration based on actual usage patterns.

