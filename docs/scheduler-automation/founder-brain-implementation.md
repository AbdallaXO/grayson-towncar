# Implement: value-aware auto-assign ("founder brain")

Goal: the auto-assigner must produce the schedule the founder would build by hand, so he stops
post-editing every day. Four rules, a full human answer-key schedule, and a precise code map
below — everything was verified against the code in June 2026; do NOT re-discover it, but DO
re-check line numbers before editing (they drift).

## The four rules

**R1 — Crunch rule: departures in-house, arrivals are the farm-out currency.**
When demand exceeds driver supply in a time window, in-house drivers should be doing
DEPARTURES/returns (hotel→MCO: ~30 driver-minutes, fixed pickup time, driver ends at MCO — the
demand hub). ARRIVALS (MCO→resort: ~75 driver-minutes including the 45-min dwell, flight-variable,
driver ends stranded at a resort) are what gets farmed. Affiliates do MCO meet-and-greets fine
(commodity); a farmed fixed-time hotel pickup that no-shows means a missed flight. The farm-out
optimizer already hard-protects true departures (`is_departure()`, never farmed) — auto-assign
must align with that policy instead of fighting it.

**R2 — Eviction: an assigned leg is not sacred.**
If farming an assigned arrival frees a driver to cover an unassigned departure (or any
higher-value leg), evict the arrival and farm it. The founder accepts farming a 4-pax arrival to
cover a 2-pax departure — the metric is value-per-driver-minute plus farmability, NOT passenger
count.

**R3 — Booked-class matching (NOT passenger count), in BOTH directions.**
A vehicle serves its own booked class first. A Van(14 Pax)-class booking with ONE passenger still
outranks a Van-class booking with eight passengers for the 14-pax van: revenue and the coverage
obligation follow the BOOKED class, and no lower class can cover that job. Never let the
highest-class vehicle run a lower-class job while a same-class job at a conflicting time goes
unassigned. This must be UNCONDITIONAL — not gated behind the existing scarcity/reservation logic
(on 6/14 the V14 type wasn't "scarce" by the Pass-0 rule, so nothing protected the sprinter job).
Downward too: push the LOWEST-class jobs onto the lowest-class vehicle (towncar arrivals belong on
the towncar, not on SUVs), keeping higher-class vehicles free for their own class. In the founder's
manual pass he rebuilt the towncar driver's whole afternoon out of towncar arrivals for exactly
this reason. The existing `tier_exact` bonus points in this direction but is too weak to matter.

**R4 — Same-slot value swap.**
When two jobs compete for the same driver-slot (same/overlapping time, both feasible), keep the
higher booked class first, then the higher passenger count, and farm the smaller. Founder examples
from 6/14: ken keeps the 2:41 PM Mini Van 4-pax arrival and farms the 2:41 PM towncar 2-pax;
rizwan keeps the 10:49 3-pax towncar arrival and farms the 10:51 2-pax. This is the tiebreak layer
under R3 — class first, pax second, never the reverse.

## Regression fixtures (Sunday 2026-06-14)

Data: `.analysis/legs_sunday_after_autoassign.csv` (the auto-assign output the founder corrected).
Timing model used to verify: `.analysis/analyze_sunday.py` and `.analysis/check_moves.py`
(mirrors the scheduler's category drive table, 45-min dwell, 15-min deplaning grace).

The five manual corrections the implementation must reproduce (all verified feasible):

| # | Driver | Evict (→ farm) | Assign instead | Rule |
|---|--------|----------------|----------------|------|
| M1 | ken | leg 11527, 9:27 AM arrival (7 pax) | leg 13256, 10:30 AM Universal→MCO departure (6 pax) | R1+R2 |
| M2 | sereen | leg 20670, 9:30 AM arrival (6 pax) | leg 22551, 10:00 AM Art of Animation→MCO departure (4 pax) | R1+R2 |
| M3 | runer | leg 23223, 10:00 AM arrival (4 pax) | leg 22907, 11:00 AM AK Lodge→MCO departure (2 pax) | R1+R2 |
| M4 | Aftab | leg 23846, 10:15 AM arrival (4 pax) | leg 23282, 11:00 AM departure (2 pax) — pax DROPS and it is still correct | R1+R2 |
| M5 | Raymond (V14 vehicle) | leg 20100, 10:00 AM Van-class (7 pax) | leg 13398, 10:00 AM Van(14 Pax)-class (13 pax), same neighborhood | R3 |

### Full human answer key

`.analysis/legs_sunday_manual.csv` is the founder's COMPLETE manual rework of the same day
(37 edits vs the auto CSV; `.analysis/diff_schedules.py` prints the diff and rule scorecard).
Outcome vs auto: departures farmed 8→3, V14 dropped 3→2, +11 pax covered, guest-continuity split
fixed (res 11774 both legs on one driver), window violations fixed. Treat it as the target
distribution of decisions — but it is NOT perfect. Known flaws (do NOT learn these; the
implementation should beat them):

1. **Aftab is double-booked**: he holds BOTH leg 23846 (10:15 AM arrival, clears ~11:30) and leg
   22907 (11:00 AM departure) — a −42 min overlap. Founder's stated intent: farm 23846, keep
   22907. The engine must never produce this (the guards already forbid it).
2. **Missed R3 trade**: leg 22305 (9:15 AM Van(14 Pax)-class, 13 pax, cruise from port) stayed
   farmed while roberto (V14 driver) ran the 9:30 AM Van-class 10-pax (20620) from the same port.
   If roberto's paired vehicle is V14-class, the engine should make this swap.
3. **Missed free insertions**: leg 23857 (7:00 AM MV) fits george (+3 min); leg 20799 (8:30 AM MV
   arrival) fits Raymond (+3 min). Both stayed farmed.
4. **Load dumping**: runer went 8→10 jobs, 14.1h span, last clear 7:04 PM vs his 7 PM window —
   solving one driver's window violation by overloading another. Load-balance/span scoring should
   resist this.

## Code map (verified June 2026)

- **Greedy build**: `suggest_assignments*` in `dispatching/scheduler.py`. Leg order =
  `(pass, hour, _TYPE_PRIORITY, pickup_time)`; `_TYPE_PRIORITY = {return:0, cruise:1, other:2,
  arrival:3}` hardcoded ~line 1205. Pass 0 = vehicle-scarce (exact_count ≤ reserve_max_scarcity
  AND eligible ≤ half fleet), Pass 1 = time-scarce (demand/supply > 1.5), Pass 2 = rest
  (~1204-1358). Main loop ~1367-1634 is greedy with NO cross-hour lookahead — a 9:27 arrival
  consumes a driver before any hour-10 leg is examined. That is the root cause of M1-M4.
- **Scoring** ~1425-1634: 14 factors (buffer quality, tier match, scarcity, location proximity,
  arrival-streak flow penalties, retention_bonus +25 for return/cruise, chain bonuses, load
  balance, idle gap, span, trip pref, reserve penalty). NO passenger-count, NO revenue, NO
  MCO-positioning term. M5 happened because legs 13398/20100 tied on (hour, type) and value never
  entered.
- **Swap pass**: `dispatching/swap_optimizer.py` `find_swaps` (~239+) — iterative-deepening
  displacement chains, but coverage-preserving by construction: displaced legs may only be
  re-homed on in-house drivers (~293-306, 495-559). It can pull farmed legs IN
  (`recover_residuals_via_swaps`, scheduler.py ~1713, called from views.py ~10210 under
  `AUTO_PREFARM_SWAP_PASS`), it can never evict OUT to farm. R2 is a missing move type.
- **Gap compaction** (`AUTO_GAP_COMPACT_PASS`, scheduler.py ~102-108, ~2273): pure relocation,
  never unassigns. Leave intact; run the new eviction pass before it or after it — decide and
  document.
- **Farm-out**: `dispatching/farmout_optimizer.py` — read-only grader. recovered_margin =
  farm_cost − inhouse cost (~670, 1042, 1077). Hard rule: true departures never farmed/displaced
  (`is_departure` ~262-273, `policy_departure_rescue` ~1150-1161). REUSE `is_departure()` as the
  eviction protection.
- **Value data already on the model**: `Leg.revenue_share`, `Leg.leg_base_price`,
  `Leg.profit_estimate` (reservations/models.py ~955-961, 1142-1155) — currently unused by
  auto-assign. Builder-only knobs `revenue_divisor`/`revenue_cap` exist as a pattern to follow.
- **Settings**: `SchedulerSettings` singleton (dispatching/models.py ~9-174, ~72 numeric knobs,
  module-level cached — follow the existing cache-bust pattern). New knobs need a migration.
- **Guards**: `check_feasibility` (scheduler.py ~761-880) + `feasibility_guards.py` (turnaround
  with 15-min deplaning grace ~149-172; window_check CLEAR_BY + max-hours ~255-327). EVERY move
  any new pass makes must re-validate through these.

## Implementation — three changes, in order

**C1 — Value-aware ordering and scoring.**
Define `leg_value(leg)`: booked vehicle-class tier is the PRIMARY term (R3), then trip type
(departure premium per R1), then `revenue_share` when populated, passenger count only as the final
tiebreak. Use it (a) to sort legs within each (pass, hour, type) bucket, and (b) as a new scoring
term gated by a new knob `auto_assign_value_weight`. Add a class-match guard: when a driver's
paired class is C and an unassigned class-C leg conflicts in time with a candidate lower-class
leg, the class-C leg wins — unconditionally (makes M5 deterministic).

**C2 — Evict-to-farm pass (the core, fixes M1-M4).**
New guard-safe pass AFTER `recover_residuals_via_swaps` (hook at views.py ~10213). For each
unassigned leg U in descending `leg_value`: find drivers where U fits if exactly ONE assigned leg
A is removed. Requirements: (i) A is farmable — trip_type == 'arrival', never `is_departure(A)`,
never locked; (ii) `leg_value(U) − leg_value(A) ≥ displacement_min_value_gain` (new knob);
(iii) the modified chain re-passes `check_feasibility` end to end. Evict A to the unassigned/farm
pool, assign U. Bound the pass with `max_displacements_per_run` (new knob). Log every move with a
human-readable reason, following the existing provenance patterns.

**C3 — Expose `_TYPE_PRIORITY` as SchedulerSettings fields**
(`type_priority_return/cruise/other/arrival`) so the ordering is tunable without code changes.

**C4 — Correctness quick-fixes (verified bugs; do these first, they make C1/C2 math trustworthy):**
- `DRIVE_TIME_ESTIMATES` has NO entries for ('Airport Hotel','Port Canaveral Area') or
  ('Other Hotel','Port Canaveral Area') — hotel→port cruise runs fall back to the 35-min default
  vs ~55 real, so every to-port chain is scored optimistically (on 6/14 this made David's
  9:00→10:00 port chain look +15 when it is realistically −5). Add both directions at ~55 min.
- Investigate how sereen's 6:01 AM arrival → 7:00 AM departure pair (legs 23348→20423, buffer −16)
  got assigned: `check_feasibility` computes a negative buffer and should reject it. Either a
  code path skips the guard or RouteTimingMetric p75 values mask it — find and close the hole.

Skip cross-hour lookahead in the greedy — C2 achieves the same outcome reactively and every move
re-runs the existing guards, which is far safer to validate.

## Constraints / gotchas

- `auto_assign_drivers()` only processes UNASSIGNED legs; "Reset Schedule" first when testing
  full runs.
- Sandbox/draft no-leak invariant: never `leg.save()` on draft paths until publish.
- Respect locked legs in all passes.
- Windows; tests need `ENABLE_DEBUG_TOOLBAR=0`; system python, no venv.
- Update `docs/scheduler-tuning-guide.md` and `docs/auto-assign-deep-dive.md` with the new
  passes and knobs.

## Acceptance

1. Unit tests for `leg_value` ordering, the class-match guard (both directions, R3), the same-slot
   tiebreak (R4), and the eviction pass — encode M1-M5 as fixtures (synthetic minimal legs
   mirroring the CSV rows are fine).
2. End to end against the answer key: run auto-assign on the 6/14 data (or a synthetic
   equivalent); the output must match or beat `legs_sunday_manual.csv` on the founder scorecard
   (departures farmed ≤ 3, V14-class legs dropped ≤ 2 — ≤ 1 if roberto's vehicle is V14, covered
   pax ≥ 483), must additionally capture the answer key's four known misses listed above, must
   produce ZERO guard violations (no Aftab-style overlaps, no window/span breaches), and must not
   recreate the load-dumping pattern (no driver over their max_hours/span cap to fix another's
   violation).
3. Use `.analysis/analyze_sunday.py <csv>` and `.analysis/diff_schedules.py` to score any run —
   they implement the founder scorecard (coverage by class/trip-type, chain buffers, insertion
   test, hourly walls).
