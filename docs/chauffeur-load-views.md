# Chauffeur Load & Utilisation — implementation plan

Two new pages built on one shared metrics core:

| | Route | Who | Purpose |
|---|---|---|---|
| **Dispatcher** | `/dispatching/chauffeur-load/` | `is_staff` | Weekly workload + coverage reference when building the schedule. No money. |
| **Admin / KPI** | `/dispatching/chauffeur-kpis/` | `is_superuser` | Monthly fairness + revenue review. Everything the dispatcher sees, plus money and the fairness reads. |

Naming mirrors the existing `staff-metrics/` (staff) vs `staff-kpis/` (admin) split.

Design reference: the published layout study — admin/dispatcher role switch, utilisation
distribution, trips-per-day, share-of-work, and the 21-day-back/7-day-forward day strip.

---

## 1. Audit findings that shape the plan

Checked against the codebase before writing this. Four things matter.

### `DriverDailyCapacity` is the perfect table and nothing populates it

`reservations/models.py:3762` defines exactly the fields we need — `total_legs`,
`total_revenue`, `total_active_hours`, `avg_turnaround_time`, `longest_gap_minutes`,
plus `arrival_count` / `return_count` / `cruise_count` / `other_count` per driver per day.

`dispatching/analytics.py:1202 update_daily_capacity_metrics()` populates it. **Nothing
calls that function.** `recalculate_route_metrics` (`views.py:13315`) only calls
`update_all_route_timing_metrics`, and there is no scheduled job for it. The GHL scheduler
(`ghl_integration/scheduler.py`) runs a 30-minute batch loop with a cycle counter but has no
capacity task.

**Decision: aggregate from `Leg` on the fly.** Do not build on a table with no writer.
`/dispatching/analytics/` (`views.py:10040`) already imports `DriverDailyCapacity` and then
ignores it, aggregating legs directly — we follow that precedent so our numbers agree with
the page that already exists. Backfilling `DriverDailyCapacity` via a nightly job is a
**later performance option**, not a prerequisite.

### The utilisation denominator is estimated, not measured

`inhouse_schedule` (`views.py:14608`) computes a driver's weekly hours from their
availability window — but for an **open/flex** day there is no window, so it uses
`max_hours` if set and otherwise **hardcodes 10 hours**. Most of the roster is open/flex.

That means utilisation % rests on an assumption, while **days worked** and **trips per
worked day** rest on counted facts. Consequence for the build order and the UI:

- Lead with trips-per-day and days-worked. They are robust.
- Utilisation stays, but the flex-day constant moves out of a view and into
  `SchedulerSettings` (or a module constant with one home), and the page footnotes it.
- Prefer day-denominated metrics over hour-denominated ones wherever both work.

### The vehicle column — answered

There are three distinct things, which is why the field felt ambiguous:

| Source | What it is |
|---|---|
| `Driver.preferred_vehicles` → `FleetVehicle` | *"their regular car (soft preference — informational)"* — the "always the same car" case |
| `DriverVehicleAssignment(driver, date, vehicle)` | The actual car that day. `unique_together (driver, date)`. Written by `day_setup.py`, read by `fold_advisor.py` |
| `Leg.vehicle` → `rates.Vehicle` | The vehicle **class** (Towncar / SUV / Van), not a physical car |

**Spec — one cell, two lines:**

- **Line 1 — class mix actually driven**, from legs in the window: `Towncar`, or
  `Towncar · SUV` when genuinely split. This is the operationally useful read (capability).
- **Line 2 — the car**: `preferred_vehicles` when set; otherwise the modal
  `DriverVehicleAssignment` over the window as `TC-04 (+2 others)`; otherwise `rotates`.

This covers both real patterns without pretending drivers have one fixed car.

### Scheduler-vs-human attribution is not currently possible

`assignment.py:126 set_leg_driver()` accepts `source=` (`"swap"`, `"takeback"`,
`"build_first"`, `"snapshot_restore"`) but for **live** writes only persists
`driver_assigned_by` — a User. Auto-assign runs as the clicking user, so a scheduler run and
a dispatcher hand-assigning 80 legs are indistinguishable in the data.

Fairness numbers are still valid — they prove the imbalance exists. They cannot say *who
caused it*. Closing that is Phase 5 and optional.

---

## 2. Metric contract

Both pages read identical numbers from one module. Definitions are fixed here so they
cannot drift.

```
window            = [start, end], default 30 days, selectable 7 / 30 / 90

legs              = Leg.objects.filter(driver=d, pickup_date in window,
                        status='completed')
                    .exclude(status='cancelled')
                    .exclude(reservation__status='cancelled')
                    -- same filter as views.py:10052 so pages agree

workedDays        = count(distinct pickup_date in legs)         # counted fact
availDays         = count(dates in window where resolve_effective_availability()
                        says available)                          # counted fact
spareDays         = availDays - workedDays

availHours        = sum over available dates of:
                      fixed window  -> (end_hour - start_hour), wrapping past midnight
                      open/flex     -> max_hours or FLEX_DAY_HOURS   # ESTIMATE
workedHours       = sum of actual leg durations                  # see note
utilisation       = workedHours / availHours                     # estimate-dependent

perWorkedDay      = legs / workedDays          # workload density  <- most robust
perAvailableDay   = legs / availDays           # folds idle days back in
perWeek           = legs / (window_days / 7)   # normalised rate
perMonth          = legs / (window_days / 30)  # normalised rate

shareOfWork       = (legs / fleet_legs) / (availHours / fleet_availHours)
                    1.00 = exactly the work their availability entitles them to
```

`workedHours`: prefer real durations. `LegStatus` timestamps give actuals where present but
are absent on older legs (noted in `analytics.py:1206`). Fall back to
`RouteTimingMetric.median_total_time` for the leg's route category, and only then to a flat
per-leg constant. Whichever tier was used must be reported, not silently averaged in.

**The three fairness reads are not redundant** — they catch different failures, and a driver
can be fine on two and bad on the third:

| Metric | Catches |
|---|---|
| `shareOfWork` | Getting more **days** than availability justifies |
| `perWorkedDay` | Days being **packed harder** than everyone else's |
| `utilisation` | Available hours **going unused** |

---

## 3. Architecture

### `dispatching/load_metrics.py` — new, the only place the maths lives

```python
def build_load_rows(start, end, *, with_money=False) -> list[LoadRow]
def build_fleet_summary(rows) -> FleetSummary
def build_day_cells(driver, back=21, forward=7) -> list[Cell]
```

**In-house drivers only** (`driver_type='inhouse'`, `is_active=True`). Affiliates are out of
scope for this work and handled separately — utilisation has no honest denominator for them
anyway, since we don't own their hours. The layout study's collapsed affiliate section is
therefore *not* part of the build.

- Availability comes **only** from `drivers/availability.py::resolve_effective_availability`.
  Never re-derive it here — same rule as turnaround labels reusing the feasibility engine.
- One grouped aggregate for legs (`values('driver', 'pickup_date').annotate(...)`), not a
  query per driver.
- Prefetch `weekly_schedule` + `date_overrides` before the resolver loop
  (19 drivers × 30 dates ≈ 570 resolver calls; `inhouse_schedule` already does this).
- Returns money fields as `None` when the caller is not entitled — see below.

### Role gating happens in the payload, not in CSS

The layout study hides admin columns with `display:none`. **Production must not do that.**
The dispatcher response must never *contain* revenue, cost, pay, or `shareOfWork` — not
hidden, absent. `build_load_rows(..., with_money=False)` omits the fields; the dispatcher
template has no branch that could render them.

### Two thin views

Both are ~40 lines: parse window, call the module, render. No maths in views.

- `chauffeur_load` — `is_staff`. Columns: chauffeur, vehicle, available, days worked, trips,
  per-day (basis switch), utilisation, day strip, next time off.
- `chauffeur_kpis` — `is_superuser`. The above plus revenue and share-of-work, plus the
  admin-only summary tiles.

---

## 4. Phases

### Phase 0 — verify before building — **DONE 2026-07-29, results in §7**

Cheap checks that change the design if they come back wrong:

1. `DriverDailyCapacity.objects.count()` — expected 0. Confirms the on-the-fly decision.
2. `DriverVehicleAssignment` coverage — what fraction of driver-days have a row? If sparse,
   line 2 of the vehicle cell leans on `preferred_vehicles` only.
3. `Leg.status='completed'` hygiene — are completed legs reliably marked, or do some finish
   in another status? Every number depends on this filter.
4. Pull the **real** utilisation distribution over 90 days. This sets the healthy band. The
   50–82% in the mock is a placeholder and must not ship as one — that is
   `COVERAGE_TARGET = 14` all over again.
5. Count legs with usable `LegStatus` timestamps, to pick the `workedHours` tier.

### Phase 1 — shared core — **DONE 2026-07-29**

Shipped: `drivers/migrations/0044_driver_employment_type.py`,
`Driver.employment_type` (+ admin list/filter/fieldset so it can be labelled),
`dispatching/load_metrics.py`, `dispatching/tests_load_metrics.py` (22 tests, all pass).

Verified on real data: 26 rows in **8 queries, 0.15s**. Cross-checks against the
independent Phase 0 script (roberto 459 legs / 67 worked days / 83 available days). The
3-hour difference in available hours is the module correctly narrowing partial-day
exceptions, which the Phase 0 script did not do.

Two fixes the smoke test caught that the unit tests could not:

* **`Leg.vehicle` is set on 57 of 6,262 completed legs (0.9%)** — it is an override.
  `reservation.vehicle` is set on 100%. Reading `Leg.vehicle` alone rendered `—` for
  almost every driver, so the class mix coalesces leg → reservation.
* `_next_time_off` used `%-d`, which does not exist on Windows.

Still stubbed: `_hours_per_leg()` returns a flat 1.7. Replace with real `LegStatus`
durations (82% coverage) falling back to `RouteTimingMetric.median_total_time`. Until
then every row carries `utilisation_is_estimate = True`.

<details>
<summary>Original Phase 1 scope (for reference)</summary>

- `dispatching/load_metrics.py` per the contract above.
- Move the flex-day hours constant to one home; stop the view-local `10`.
- Tests: `dispatching/tests_load_metrics.py`
  - part-timer and full-timer with identical utilisation but different `perWorkedDay`
  - approved time off removed from `availDays` **and** `availHours`
  - a driver with zero legs (no rows in the leg aggregate) still appears, at 0
  - `shareOfWork` sums sensibly across the fleet
  - `with_money=False` omits keys entirely rather than nulling them
  - overnight availability window wrapping midnight
</details>

### Phases 2 & 3 — both views — **DONE 2026-07-29**

Shipped together rather than dispatcher-first, because one template serves both once the
payload is role-gated and the founder wanted to iterate on the real page.

* `chauffeur_load` / `chauffeur_kpis` + `_chauffeur_load_context()` in `dispatching/views.py`
* Routes `chauffeur-load/` (is_staff) and `chauffeur-kpis/` (is_superuser)
* One template `dispatching/templates/dispatching/chauffeur_load.html`
* Nav: *Drivers → Load & Availability*; *KPIs → Chauffeur KPIs* (that dropdown is already
  superuser-only)
* `load_metrics.serialize_rows()` — JSON-safe rows that preserve the money omission
* 32 tests pass (`dispatching/tests_load_metrics.py`)

Verified rendering against real data: 26 rows both pages, dispatcher payload has **no**
money keys and no money column headers; KPI payload has all four.

**Money gating is enforced three deep**, because the test caught the second and third:
1. `build_load_rows(with_money=False)` omits the keys
2. `serialize_rows` copies money keys only when present
3. The template's *JavaScript* is wrapped in `{% if with_money %}` — otherwise the string
   `driver_pay` shipped in dead render code on the dispatcher page

Follow-up: the dispatcher page took ~0.84s cold. `build_day_cells` calls
`resolve_effective_availability` for 28 more days per driver on top of the window loop
(~1,500 resolver calls for 26 drivers / 30 days). Memoising the resolver per driver+date
would roughly halve it. Not urgent at this roster size.

<details>
<summary>Original Phase 2 scope (for reference)</summary>

Cheaper half, and the one with a weekly use case attached. Route, template, nav entry,
sortable table, basis switch, day strip, distribution strip.

The day strip is the piece that answers "which days do they work" — 21 days back, a today
divider, 7 days forward. Solid = worked (height by trips), hollow = available with nothing
assigned, flat tick = scheduled day off, blue = approved time off.

</details>

### Phase 4 — admin extensions

In rough value order:

1. **Trip-type mix** per driver (arrival / return / cruise / other) — is one chauffeur
   getting all the easy returns?
2. **Revenue vs driver pay** margin per chauffeur. Read-only aggregate; the existing
   per-driver manual pay review stays exactly as it is.
3. **Fairness trend by month** — is the spread narrowing?
4. **Vehicle-class utilisation** — a class where everyone runs heavy is a class we're short on.

### Phase 5 — attribution (optional)

Persist `source` on the Leg so auto-assign is distinguishable from hand-assignment. Small
migration + threading it through `set_leg_driver`. Only worth it if "is the *scheduler*
fair" is a question you want answered, versus "is the *outcome* fair."

---

## 5. Decisions taken (2026-07-29)

1. **Flex day = 12 hours** (`max_hours` where set, else 12).
2. **Revenue and driver pay shown side by side** on the KPI page — read-only aggregate. The
   existing per-driver manual pay review is untouched.
3. **Full-time / part-time flag is required** — see §6. No such field exists today.
4. **Healthy band** deferred until availability means "committed" — see §7 finding 3.

## 6. Full-time / part-time (new requirement)

Founder's definition:

> Full-time means they need to work every day they are available. Other drivers can be
> available certain days but don't need to work.

No field exists — `Driver.driver_type` is only inhouse/affiliate. Needs
`Driver.employment_type` (`full_time` / `part_time`), defaulting to unset rather than
guessing, because guessing wrong inverts the meaning of every number below.

This is not cosmetic. It changes what the metrics *mean*:

| | Full-time | Part-time |
|---|---|---|
| An available day is | a **commitment** | an **offer** |
| A day available but not worked | a finding — they expected work and got none | normal, not a problem |
| `daysWorked / daysAvailable` should be | ≈ 100% | whatever it is |
| Flagging "under-used" | meaningful | misleading |

Consequences for the build:

- **"Under-used" only fires for full-timers.** For part-timers, spare days are information.
- **Fairness is computed within cohort**, not across the fleet. Comparing a part-timer's
  share-of-work against a full-timer's using availability as the denominator makes the
  part-timer look starved when they are simply part-time.
- The roster **groups by cohort** rather than mixing them in one ranked list.

## 7. Phase 0 results — run 2026-07-29 against `content/db.sqlite3`, 90-day window
(2026-04-30 → 2026-07-28). Read-only queries, no writes.

**26 active in-house drivers** (not 19), 9 active affiliates.

1. **`DriverDailyCapacity` = 0 rows.** Confirmed dead. Aggregating `Leg` on the fly is correct.
2. **17.3% of legs in the window are still `in-progress`** — 1,366 of 7,908, on dates already
   past. Only 6,262 (79.2%) are `completed`. A past-dated `in-progress` leg was almost
   certainly driven and never closed out, so `status='completed'` **under-counts real work**,
   unevenly across drivers. This is its own data-hygiene problem and it distorts every metric
   on the page. Options: count `completed + in-progress` for past dates, or fix the close-out
   gap first. Must be decided before the numbers can be trusted.
3. **The availability denominator records willingness, not commitment.** 25 of 26 drivers have
   a full 7-row weekly pattern, every one of them `full_day` + `flexible`. Seven are marked
   available **7 days a week**. Against that: neuma is available 90 of 90 days and worked 23;
   Lev 89 available, worked 8; ernesto 83 available, worked 22. Nobody works 7 days a week, so
   "available" is being used to mean *would accept work* — exactly the distinction in §6.
   Until an available day means a committed day for full-timers, utilisation % and coverage %
   are not interpretable, and no healthy band can be set from them.
4. **Day density is fair — this is good news.** Trips per worked day: p25 **5.0**, median
   **5.6**, p75 **6.4** (min 2.6, max 7.2). The scheduler is *not* packing one chauffeur's day
   harder than another's.
5. **The spread is in days, not density.** `daysWorked / daysAvailable`: median **72%**, and
   **20 of 26 drivers below 80%** — tail down to 7% (Charlie), 9% (Lev), 16% (AldoH), 26%
   (neuma). This is where the imbalance lives, but finding 3 means we cannot yet say whether
   it is starvation or just part-time availability.
6. **Utilisation: median 48%, p25 21%, p75 66%, max 78%.** The mock's 50–82% "healthy" band
   would have painted more than half the roster as under-used. Placeholder correctly withheld.
7. Two data defects worth a ticket: **Charlie** has no `DriverWeeklySchedule` rows at all, so
   the `entry.is_available if entry else True` default marks them available 7 days a week.
   **Julio** shows 0 available days but 2 worked days / 13 legs.
8. `DriverVehicleAssignment`: 991 rows across 76 of 90 dates — good enough for line 2 of the
   vehicle cell. Only **4 of 26** drivers have `preferred_vehicles` set, so the modal-car
   fallback carries most rows.
9. `LegStatus` rows exist for 6,519 of 7,908 legs (82%) — real durations viable for most legs,
   with the `RouteTimingMetric` fallback covering the rest.

### What this changes

- **Lead with counted facts.** `daysWorked / daysAvailable` and `trips per worked day` are
  countable. Utilisation % rests on both the 12-hour assumption *and* a denominator that
  currently means the wrong thing.
- **Utilisation stays admin-only and labelled an estimate.** It does not belong on the
  dispatcher view as a headline number.
- **Findings 2 and 3 are prerequisites**, not polish. Neither the healthy band nor the
  fairness read can be trusted until leg close-out and the FT/PT flag are sorted.

## 8. Revision — 2026-07-29 (second pass, same day)

The founder reworked the KPI page around three rules: no money, no jargon, more insight.
Everything below supersedes the matching parts of §2–3; the history above is kept as-is.

* **Money removed entirely.** `with_money`, revenue/pay/margin/share-of-work and
  `_attach_share_of_work` were deleted from `load_metrics.py` (git history preserves
  them). They will return on a separate *Driver economics* page — the KPI header carries
  a "coming soon" placeholder only. Both pages now render identical numbers.
* **Vocabulary.** "Gap" → **idle days** (`idle_days` everywhere, including the JSON
  payload). The Available column shows days only — `avail_hours`, `worked_hours` and
  utilisation % no longer exist on rows (`available_hours_for` survives, tested, for
  Driver economics). The glossary, the utilisation footnote and the healthy-zone essay
  moved into SOP-003's methodology appendix.
* **Insight.** New `dispatching/load_insights.py`: pure rules over the rows produce
  plain-sentence **findings** (both pages) and a **"Worth a conversation" exceptions
  list** (KPI page only; one entry per driver, priority no-day-off-streak → never-drove
  → mostly-idle → days-packed-harder). Every rule pairs a relative condition with an
  absolute floor that scales across the 7/30/90 windows; thresholds are documented in
  SOP-003. The roster's idle-count highlight is now "is on the exceptions list"
  (server-decided) instead of a hardcoded ≥ 8.
* **Prior-window comparisons.** The context builder computes the preceding equal-length
  window via `build_load_rows(lite=True)` (skips day cells, vehicle queries and time
  off) for the tile comparisons and the worked-share trend finding.
* **Token guarantee.** A view test asserts the rendered HTML of BOTH pages never
  contains `$`, `utilisation`/`utilization`, `share of work` or the word `gap` —
  case-insensitive, whole response. This is why the template's CSS spaces flex children
  with margins rather than the flexbox `gap` property.
* Tests: `tests_load_metrics.py` reworked; new `tests_load_insights.py` covers each
  rule's fire and boundary-silent cases.
* **Handled / dismiss (added same day).** `ChauffeurExceptionDismissal` (migrations
  0012–0013) lets a superuser mark an exception handled with an optional note. Episode
  semantics: suppressed while the (driver, rule) keeps firing; spent (cleared_at) only
  by a render of the window it was dismissed on, with the driver present in the roster
  — so browsing other windows, deactivating a driver, or an empty roster can never
  discard one. Outranked/collapsed dismissals render a "still applies" fallback row so
  they stay undo-able. Endpoints `chauffeur-kpis/handled/` (+`undo/`), POST + superuser
  only. An adversarial review workflow (19 agents) found and led to fixes for the
  roster-absence mass-spend, the cross-window spend, the invisible-outranked case, and
  malformed-id 500s.

## 9. Explicitly out of scope

- **"Who can take this leg tomorrow at 4pm?"** This is the moment-of-assignment question and
  neither page answers it. That is the separate date-resolved view. The day strip's forward
  week is a reference, not a substitute.
- Quality metrics — complaints, on-time, guest feedback. Volume is not quality.
- Anything that acts on a driver automatically. Both pages are read-only.
- Any warning or nudge aimed at dispatchers. Advisory only — a tiebreaker when the
  operational answer is already a tie, never something that argues with tight-turn, rest, or
  feasibility checks. If it starts warning at people it has gone wrong.
