# Rest Advisor — overnight rest awareness for auto-assign

**Status: DESIGNED, NOT IMPLEMENTED** (investigation done 2026-06-12; line numbers below are from that date)

> Naming note: `dispatching/shift_advisor.py` is ALREADY TAKEN by the Second-Shift
> Advisor (Span Governor Phase 5 — "this day needs another driver"). This feature
> is the **Rest Advisor**, new module `dispatching/rest_advisor.py`.

## Problem

Auto-assign has **zero awareness of the previous day**. Robert (Van driver) can
finish his last leg at 10:30 PM and still be handed a 5 AM Van leg the next
morning, even when another Van driver who went home at 8 PM is available. Nothing
in `dispatching/scheduler.py` queries `target_date - 1` (verified by grep — no
reference to the prior day anywhere in the pipeline or post-passes).

Telling artifact: `DriverWakeupCheck` (`drivers/models.py` ~line 660) already
exists to SMS/call drivers with early first pickups — the business treats "early
start after late night" as a real failure mode, but the optimizer doesn't.

## Investigation findings (how early legs are assigned today)

Pipeline: `auto_assign_drivers` (`dispatching/views.py:9914`) → builds candidate
pool (in-house drivers with a `DriverVehicleAssignment` for the date, filtered by
the modal's `driver_hours` payload) → `build_driver_schedules` →
`suggest_assignments_clustered` → `suggest_assignments` (`scheduler.py:1158`) →
swap/rescue/trim/gap post-passes.

- **Leg order** (`_multi_pass_sort_key`, `scheduler.py:1343`): Pass 0
  vehicle-scarce legs, Pass 1 time-scarce hours, Pass 2 rest; within a pass by
  (hour, returns→cruise→other→arrivals, pickup_time). A 5 AM Van leg is among the
  first legs the greedy loop sees, while all simulated schedules are still empty.
- **For the first leg of the day, all compatible flexible drivers score
  IDENTICALLY.** Every score component is a property of the leg or of the
  driver's existing schedule — and the schedules are all empty. Empty schedule →
  buffer 999 (`check_feasibility`, `scheduler.py:811-812`) → same `buffer_loose`,
  same `loc_first_job`; no flow/load/idle/span terms. Only per-driver
  differentiators: vehicle tier vs leg, trip-type preference, reserved-mismatch
  status, cluster hints.
- **Tie-break is arbitrary**: `if score > best_score` (strictly greater,
  `scheduler.py:1615`) means the first driver in dict iteration order wins. That
  order = `Driver.objects.filter(...)` order, and `Driver` has **no
  `Meta.ordering`** → DB default (in practice primary-key order). So the 5–6 AM
  legs go to whichever compatible driver has the lower ID. Cluster hints
  (`assign_drivers_to_clusters`, `scheduler.py:1008`) tie-break on the same
  arbitrary order for all-flexible drivers.
- After the first leg lands, chain/location/backward-chain bonuses stack — the
  whole morning snowballs from that arbitrary seed.

## Design

### 1. Data source — previous day's last drop-off

One query per run (in `auto_assign_drivers`, alongside the other preloads):

```python
prev_date = target_date - timedelta(days=1)
prev_legs = (Leg.objects.filter(pickup_date=prev_date, driver__isnull=False,
                                driver_id__in=working_ids)
             .exclude(reservation__status="cancelled").exclude(status="cancelled"))
prev_end_by_driver = {}  # {driver_id: datetime of last estimated drop-off}
for leg in prev_legs:
    end = estimate_job_end_time(leg, prev_date)
    if end > prev_end_by_driver.get(leg.driver_id, datetime.min):
        prev_end_by_driver[leg.driver_id] = end
```

- Uses `estimate_job_end_time` — the same end-time machinery the scheduler trusts
  everywhere else. Handles past-midnight ends naturally (datetime, not time).
- Drivers with no legs yesterday → absent from the map → fully rested, no penalty.
- Remember the gotcha: query `pickup_date=prev_date` directly on Leg, NOT
  `reservation__pickup_date`.
- v2 (out of scope): prefer actual `LegStatus` 'completed' timestamps when present.

### 2. Config — two new `SchedulerSettings` fields (+ migration)

`dispatching/models.py`, following existing field conventions:

```python
# === Rest Advisor (overnight rest gap) ===
rest_min_gap_hours = models.FloatField(
    default=10.0, help_text="Min hours between last drop-off (prev day) and first "
    "pickup (next day). 0 disables rest scoring + advisories.")
rest_penalty_per_hour = models.IntegerField(
    default=40, help_text="Score penalty per hour of rest deficit when a leg "
    "would become a driver's first pickup of the day")
```

Defaults rationale: a 3 h deficit → −120, which decisively beats the per-driver
deltas that currently decide ties (tier ±20, shift coherence +50, reserve −60),
but stays soft. `SchedulerSettings` is a singleton with module-level caching —
needs a migration; check admin fieldsets for where to surface the two fields.

### 3. Scoring — soft marginal penalty in `suggest_assignments`

**Soft, not a hard block.** If Robert is the only Van driver, the leg must still
get covered in-house rather than stranded to affiliates.

- Pass `prev_end_by_driver` into `suggest_assignments_clustered` →
  `suggest_assignments` (new optional kwarg, default None → feature off, keeps
  other callers at `views.py:8927` and `views.py:14353` unchanged until opted in).
- In the candidate loop (near the span penalty block, `scheduler.py:~1563`),
  charge ONLY when this leg would become the driver's **first pickup of the day**
  (marginal — mirrors `marginal_span_penalty` philosophy; mid-day legs unaffected):

```python
if rest_cfg_on and (not sched.slots
                    or leg.pickup_time < min(s.pickup_time for s in sched.slots)):
    prev_end = prev_end_by_driver.get(did)
    if prev_end is not None:
        rest_h = (datetime.combine(target_date, leg.pickup_time) - prev_end).total_seconds() / 3600
        deficit = cfg.rest_min_gap_hours - rest_h
        if deficit > 0:
            score -= int(deficit * cfg.rest_penalty_per_hour)
```

Result: between two empty same-type drivers competing for the 5 AM leg, the
better-rested one wins; the tired driver's day naturally starts later. Above the
threshold there is deliberately NO preference (avoids permanently loading early
work on whoever goes home earliest).

### 4. UI — two surfaces

**(a) Auto-Assign modal hint (most actionable spot).** Next to each driver's
Start/End row show: `ended 10:30 PM yest → rested by 8:30 AM` (prev end +
`rest_min_gap_hours`). The dispatcher can set a later start BEFORE building.
Requires the preview/modal endpoint to ship `prev_end_by_driver` (string-formatted)
per driver — find where the modal's driver rows are populated in
`daily_capacity_planner.html`.

**(b) Advisor cards in the preview.** New module `dispatching/rest_advisor.py`
with `build_rest_advisories(target_date, proposed, prev_end_by_driver, cfg, ...)`,
appended to `advisor_proposals` in `auto_assign_drivers` (`views.py:~10590`,
alongside Second-Shift / Fold-Out / Rebalance — same try/except advisory-only
contract: an exception must never break the preview).

- Fires for any driver whose **final-board** first pickup violates the minimum —
  catches violations regardless of which pass (or manual/locked assignment)
  produced them. Scoring prevents most; the advisor verifies the end state.
- Card kind `rest`, signature `_rest<driver_id>`, dismissible like the others.
- Copy: *"Robert ended yesterday 10:30 PM — 5:30 AM start is 7.0h rest (min 10h).
  Alternative: Marcus (Van, ended 8:00 PM). Or push Robert's first leg to
  8:30 AM+."*
- When NO rested same-type alternative exists, say so explicitly ("no rested Van
  alternative — accept, or farm the early leg") instead of staying silent.
- Frontend: extend the card router at `daily_capacity_planner.html:~3380`
  (`advisorAll.filter(...)`) with a `kind === 'rest'` group (signature prefix
  `_rest`, mirroring `_fold` / `_rebal` handling).

### 5. Tests — `dispatching/tests_rest_advisor.py`

- Two Van drivers, one ended late yesterday → early leg goes to the rested one
  (this is the headline behavior; without the feature the lower-ID driver wins).
- Tired driver is the ONLY compatible driver → leg still assigned (soft penalty).
- No previous-day legs → no penalty, no card.
- `rest_min_gap_hours = 0` → feature fully disabled (no scoring, no cards).
- Advisor card emitted for a locked/manual assignment that violates rest
  (scoring never saw it; the end-state check must catch it).
- Previous-day leg ending after midnight → deficit computed correctly.
- Card lists a rested same-type alternative when one exists; explicit "no
  alternative" text when not.

Run with `ENABLE_DEBUG_TOOLBAR=0` (local .env breaks endpoint tests otherwise);
system python, no venv.

## Implementation order

1. `SchedulerSettings` fields + migration (+ admin fieldset).
2. `prev_end_by_driver` preload in `auto_assign_drivers`; thread through
   `suggest_assignments_clustered` → `suggest_assignments` as optional kwarg.
3. Scoring penalty in the candidate loop.
4. `dispatching/rest_advisor.py` + hookup in the preview response.
5. Frontend: advisor card group + modal row hint in `daily_capacity_planner.html`.
6. Tests.

## Out of scope (v1)

- Applying the penalty inside the swap/trim/gap relocation passes — the advisor
  card verifies the end state instead; those passes rarely touch dawn legs.
- Persisting rest stats (computed live each run).
- Actual completion times from `LegStatus` (v2 refinement).
- Hard-blocking assignments on rest violations.

## Open questions for the founder

- Default minimum rest: 10 h assumed — confirm.
- Should the modal hint also appear in the Day Setup page, or only the
  Auto-Assign modal?
