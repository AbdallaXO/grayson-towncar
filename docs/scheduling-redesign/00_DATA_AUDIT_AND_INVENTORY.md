# 00 — Data Audit and Platform Inventory

**Phase 1, deliverable 1 of 5. This is the go/no-go gate for the rest of the engagement.**

| | |
|---|---|
| Produced | 2026-08-21 |
| Source | `content/db.sqlite3`, opened read-only (`file:...?mode=ro`) throughout |
| Prior art reconciled | [`docs/operational-data-audit.md`](../operational-data-audit.md) (2026-07-31) |
| Scripts | [`analysis/`](analysis/) — seven self-contained scripts, no arguments, runnable from the repo root |
| Method | 15 independent readers/probes over the code and the snapshot, then one completeness critic and one adversarial verifier that re-computed the six highest-stakes numbers by different queries. Numbers below are the reconciled values; where the two passes disagreed, both are shown. |

Every figure is labelled **[measured]** (computed from the snapshot), **[inferred]** (derived under a
stated assumption), **[modeled]** (output of a model), or **[unavailable]** (the data does not exist).

---

## VERDICT

**GO, with a hard scope correction and one blocking test.**

1. **The snapshot is a production cut from 2026-07-11, not from today.** "Present" for this
   engagement is 2026-07-11. Six weeks of trading — including the entire period since the Day Setup
   redesign shipped — are not in this file. Everything below is scoped to that.
2. **Three of the brief's premises are wrong and the plan has to change around them** (§C2): the Rest
   Advisor is ON, not off; the Fleet Capacity Intelligence buy/hire/farm engine does not exist as code;
   and there is no base-location concept anywhere, so the prescribed handoff chain cannot be validated
   as written.
3. **Day Setup already computes a demand-derived required-driver number**, and the codebase has already
   measured, rejected and demoted a "peak concurrency = required drivers" warning for firing falsely on
   9 of 23 days. The new diagnostic is not building the first such thing — it is building the fourth
   (§B1). Reconciling with the incumbent is the central design constraint, not an afterthought.
4. **One assumption must be tested before the deep analysis starts** (§C3): that booked `pickup_time`
   is a usable proxy for when a car is actually occupied. On airport-origin legs — 48.5% of the window
   [measured] — the repo's own prior audit puts the real boarding event ~35 minutes after the booked
   time. Every demand curve, peak, shift boundary and staffing number in this engagement rests on it.

---

# PART A — DATA

## A1. Snapshot provenance — the single most consequential finding

`content/db.sqlite3` has a file mtime of today. **Its contents stop on 2026-07-11.** Four unrelated
write streams agree [measured]:

| Stream | Last production value | Rows after 2026-07-12 |
|---|---|---|
| `reservations_reservation.created_at` | 2026-07-11 19:33:49 | **0** |
| `reservations_quote.created_at` | 2026-07-11 20:34:45 | **0** |
| `reservations_lead.created_at` | 2026-07-11 20:34:45 | **0** |
| `payment_payment.created_at` | 2026-07-11 20:12:32 | **0** |
| `drivers_legpayment.updated_at` | 2026-07-07 18:57:44 | **0** |
| `auth_user.last_login` (last non-dev login) | 2026-07-11 20:00:57 (affiliate `Cheapolimo`) | 3 accounts, all local |
| `reservations_legstatus.timestamp` | 2026-07-11 (564 taps that day) | **7**, on 3 days, all `updated_by_id=2` |

There is a **37-day hole with zero rows** between 2026-07-11 20:37 and 2026-08-17 20:32. Three
independent forensics confirm that what follows the hole is local development, not trading:

- **Microsecond fingerprint** [measured] — 69,150 of 69,219 `legstatus` timestamps (99.90%) end in
  `.###000`, the millisecond truncation of the Postgres→SQLite export. All 7 post-hole rows carry full
  6-digit microseconds, i.e. a live Django process wrote them against this file. (A third regime exists:
  62 rows carry no fractional seconds at all.)
- **Single author** — all 7 late taps and all 194 late `driver_assigned_at` writes are `updated_by_id = 2`,
  in bursts on 07-18 (102 legs), 07-24 (10), 07-31 (76).
- **Tables that post-date the export** — `reservations_auditlog` (275 rows) and
  `reservations_historicalleg` (223 rows) are *entirely* post-2026-07-18; their migration was applied
  locally after the cut.

Two corroborating checks from outside the database: the 2026-08-09 handoff note
[`docs/scheduler-automation/day-setup-redesign-handoff.md`](../scheduler-automation/day-setup-redesign-handoff.md)
records "prod has 17 cars; this snapshot has 13" — and the snapshot still shows 13 [measured]. The same
note records DVA rows for 2026-07-11 disappearing mid-session. **This file is a writable dev database
that the local app has been driven against; it is neither current nor pristine.**

### A1.1 Why forward dates look empty — and why that matters to the product

Because every reservation in the file was created on or before 2026-07-11, a pickup date *K* days past
the cut can only contain legs booked at least *K* days in advance. Measured on fully-observed dates
(2026-02-01 .. 2026-06-30, n = 12,884 legs) [measured]:

| Days before pickup | H-0 | H-3 | H-7 | H-14 | H-21 | H-30 | H-45 | H-60 | H-90 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **Share of final demand already booked** | 100% | 91% | 79% | **61%** | 49% | 37% | 23% | 14% | 4% |

Booking lead time: P10 3d · P25 8d · **P50 20d** · P75 41d · P90 67d · P95 87d.

**Consequence for the data.** August 2026 in this file is 836 live legs over 21 days (39.8/day) against
June's 91.0/day — 44%. That is 100% truncation. Every falsification test fails to save it [measured]:
August's booking-lead median is 58 days against a 20-day norm; **not one August leg was booked after
2026-07-11**; August's *cancellation* rate is lower than June's (post-cut cancellations were never
recorded either); and re-imposing the same K-day freeze on complete June days predicts the observed
August level to within 9–26% with **zero seasonality terms**, always under-predicting. Nothing dated
2026-07-12 or later may enter any aggregate, chart axis, percentile or replay.

**Consequence for the product — this is a finding in its own right.** `peak_concurrency`
([day_setup.py:100](../../dispatching/day_setup.py#L100)) runs on *booked* legs with no lead-time
correction. A dispatcher opening Day Setup 14 days out is being sized against ~61% of that day's
eventual demand; 21 days out, ~49%. The further ahead the day is built, the more the roster is
structurally under-sized. This is measurable, it is not currently modelled anywhere, and it belongs in
the Phase 2 spec.

## A2. The analysis window

The brief asks for "the most recent 6–8 months". The growth curve says the business was not one
business for that period. Trailing-28-day mean legs/day [measured, `analysis/00_snapshot_provenance.py`]:

```
2025-06-30   2.3      2025-12-29  37.7      2026-04-06  90.8
2025-08-11   6.9      2026-01-26  45.5      2026-05-04  89.1
2025-10-06  19.1      2026-02-23  67.6      2026-06-01  88.6
2025-11-17  26.0      2026-03-23  81.0      2026-06-29  92.5
```

**5.8× in seven months, then flat.** The ramp ends around 2026-03-23 and the mean never leaves
88–93 legs/day through the cut. So the window is split by *purpose*, not chosen as one range:

| Purpose | Window | Days | Live legs | legs/day | Why |
|---|---|---:|---:|---:|---|
| **PRIMARY** — shift shapes, staffing levels, capacity, replay | **2026-03-01 .. 2026-07-11** | 133 | **11,881** | 89.3 | 1.3% below the plateau mean for +28% sample. 102 days gives only 14 of each weekday — too thin for a P90 claim on a `dow × hour` cell. |
| *strict alternative* | 2026-04-01 .. 2026-07-11 | 102 | 9,278 | 91.0 | The only regime stationary to ±2%. Use if a level claim must be defended to the tightest standard. |
| **SHAPE** — `dow × hour`, class mix, lane mix | 2026-01-01 .. 2026-07-11 | 192 | 15,315 | 79.8 | Shape is stationary against the plateau (day-of-week TVD 0.020, hour TVD 0.045, `dow × hour` TVD 0.086). **Normalised shares only — never raw counts.** |
| **ACTUALS** — durations, dwell, turnaround, discipline | 2026-02-08 .. 2026-07-10 | 153 | 13,294 | 86.9 | `reservations_legstatus` has no row before 2026-02-08. End a day early: the cut is mid-afternoon on the 11th, so that evening has bookings but no taps. |
| **REJECTED** | anything reaching into 2025 | | | | A 2–16 legs/day business with a 0.0% recorded cancellation rate (the flag was not in use). Different company. |

**Two limits that cannot be fixed with more analysis** [unavailable]: season and growth are perfectly
confounded — leg data starts 2025-04-26 at ~2 legs/day, so no prior-year Apr–Jul comparison exists, and
templates cut on this window are *summer* templates. And the 91 legs/day plateau may be supply-limited
rather than demand-limited; 91 legs across 25 in-house drivers is 3.6 legs/driver-day, and leg counts
cannot separate a demand ceiling from a capacity ceiling. Only the farm-out analysis can.

## A3. Reconciliation with `docs/operational-data-audit.md`

The prior audit's window was 2026-02-08 → 2026-07-11. **That is the snapshot's entire extent.** Extending
to 2026-08-21 adds 2,044 eligible legs, of which 78 carry any tap and **0** carry a full ladder — so
every timing figure recomputes bit-identically. The audit is not stale; it is the complete and final
statement of what this file contains.

**Holds, re-verified**

- Timezone rules and the 2026-03-08 DST flip — confirmed on **29 of 29 consecutive days**, 100%
  agreement, on a larger n than the audit used. UTC−5 before, UTC−4 after.
- Gold-cohort durations — *every* published figure reproduces to the minute, n within 1
  (approach 6,403 vs 6,404; dwell 2,443 vs 2,442; all eight ride-time lanes exact).
- The fabricating-driver cohort — **9 drivers, membership unchanged**, no additions, no drops.
  `exclude_from_timing` now catches all 9 and flags zero false negatives. The remediation held.
- True dwell, "28% of the time the driver is on location before the plane docks" — confirmed at 28.1%.
- Flight delay — P10 −23 / P50 −6 / P90 +34, unchanged to the minute; every airline's P10/P50/P90
  within ±2 min.
- Impossible ride times still present at the same rates; still unfixed.

**Needs revision**

| Audit claim | Corrected |
|---|---|
| "Coverage improved 60% → 85%, unaided" | February's 60% is a denominator artefact (whole calendar month vs a system that started on the 8th). Real curve **73.5% → 85.3%** — and it is **in-house only**. Affiliate ladder coverage *fell* every month: 53.9% → **35.3%** [measured]. |
| "Duplicate status rows 3.7–5.9%" | True through April. On the audit's own definition it **tripled to 17.1% (May) and 17.5% (June)** [measured], driven by driver-unassign auto-resets rising 36 → 1,007/month. Anything using `.first()` under `LegStatus.Meta.ordering` is now 3× more wrong; `analytics.first_status_times` (`MIN` per status) is immune. |
| "Double-tapping on 36% of full-ladder legs" | Now **42.4%**. |
| "Corrupt pickup dates, year 3220 and 2029" | **Exactly 2 rows** — legs 9210 and 9211, one junk booking. The other 188 post-2026 legs are legitimate 2027 advance bookings. Filter with an explicit `BETWEEN`, never `MAX()`; the brief inherited "3220-03-06" as the data's endpoint precisely because of this. |
| Gold cohort share | **49.5%** of the tap window (59.4% of in-house legs), not 44%. The lower figure used a denominator running to 2026-08-21, silently including 2,044 legs from the period with no taps at all. |
| `exclude_from_timing` is correct | One gap remains: `rizwan` (468 legs, 20% instant) is excluded despite being a *sparse* driver, which the audit's own rule says must not be excluded. Costs ~70 honest full-ladder legs. |
| Gold cohort membership | Should gain **`Seline`** (521 legs, passes every gate — the largest omission) and lose `sereen` / `Charlie` / `oualid`. A data-driven re-vet yields 19 drivers / 7,410 legs. |
| Affiliates excluded wholesale | Deserves a per-driver decision. `anthony` (715 legs, 100% instant, 100% collapsed ladder) is the worst data source in the system and is **unflagged**; `hany` / `martin` / `wael` / `Cheapolimo` / `oualid` are cleaner than several in-house drivers. |

**Audit sections nothing in this bundle re-derived, and that the deep analysis must read before
re-opening any of them**: §7 (Disney resort granularity — *tested and rejected*, all 19 resorts inside an
~8-minute band), §10 (predicted clear time vs actual, per lane, computed with the production
`chain_clear_dt` — concludes the prediction is "broadly sound" for Port Canaveral, with
`Disney → Port Canaveral` the one named under-buffered lane at 71% finishing late), §11 (shipped-constant
vs measured-reality table, including two more duration constants: `ops/tasks.py:31
FALLBACK_TRIP_DURATION_MINUTES = 75` and a second hard-coded 60-minute fallback at `ops/views.py:1761`),
§12.5 (cruise data is two strings — no port, no sail date), §12.6 (**nothing records what the scheduler
predicted, so estimate accuracy can never be tracked** — the single biggest structural gap for anything
that wants to learn), §12.7.

## A4. Source-by-source reliability

| Source | Grade | Trustworthy from | Notes |
|---|---|---|---|
| Leg demand (planned `pickup_date` / `pickup_time`) | **A** | 2025-04-26 | 99.9% populated, 2 corrupt rows. But see §C3 — *planned* is not *when the car is busy*. |
| Driver status taps (`legstatus`) | **B+** | 2026-02-08 | Full-ladder coverage 73.5% → 85.3%; 90.7% for in-house `exclude_from_timing=0`, **48.4% for affiliates**. |
| Farm-out identification | **A** | 2026-02-08 | One signal only: `driver.driver_type = 'affiliate'`. Agrees with the independent payment ledger on 99.98% of paid legs. The whole `operator_*` column family is 0% populated — that portal shipped 2026-08-17, after the cut, and `Leg.save()` blanks those fields on reassignment anyway, so they can never describe history. |
| Driver pay | **A** | 2025 | The cleanest data in the file. `driver_pay_amount` equals `base + gratuity + additional` on **every single row**, both arms; booked pay vs payroll ledger disagree on 8 of 12,180 legs (0.07%). |
| Leg revenue | **C** | — | `leg_base_price` is a **dead column** (1 usable value in 13,897). `profit_estimate` swings 12.5% → 99.0% (a backfill, not organic). `revenue_share` is zero on 46%. Only usable answer: `reservation.total_price ÷ leg count` — and **81.6% of legs sit on a multi-leg reservation**, so that is an even split of a round-trip price. |
| Roster — who *worked* | **A** | 2025-10-01 | ≥97% of legs carry a `driver_id` every month. |
| Roster — who was *rostered* in-house (DVA) | **A−** | **2026-01-18** | 176 contiguous covered dates; precision 99.5%, recall 94.9% (96–99% from April), median Jaccard 1.00. See the caveat in assumption A9. |
| Which car ran | **A−** | 2026-01-18 | DVA is the **only** record — `leg.vehicle_id` is populated on 95 of 24,124 legs (0.39%). |
| Roster — affiliates | **F** | never | DVA has no affiliate row by construction; affiliates carried 12–40% of legs. |
| *When* anyone was on duty | **F** [unavailable] | never | `planned_start_hour` / `planned_end_hour` populated on **0 of 2,001** DVA rows. Capacity can only be modelled as bodies-per-day, never as shift windows. |
| Declared weekly availability at a past date | **F** [unavailable] | never | `drivers_driverweeklyschedule` has no timestamp column — snapshot state only, overwritten in place. 80% of rows are the model default `6–23` copied out; `preference` has **never once been set** on any of 231 rows. |
| Date overrides (time off) | **C** | 2026-04-22 | Real and respected — of 131 approved full-day-OFF driver-days the driver drove anyway on 5 (4%) — but only ~2.7 months old and ~2 driver-days per touched date. |
| Fleet size over time | **F** [unavailable] | never | `in_service_since` populated on **0 of 14** rows; no `created_at`, no history table. Only DVA first-appearance, floored at 2026-01-18. |
| Flight arrival anchors | **A−** | 2026-04 | 99.5% of airport-pickup legs resolve a flight; **85.0%** have a hard `actual_gate_arrival` (61.5% in Feb, stable ~91% from April). |
| `RouteTimingMetric` (lane timing cache) | **D** | — | 456 rows, but only **115 clear `sample_count >= 5`** — and of the 185 rows carrying a *pre-cut* `last_calculated`, only **15** do. The newest production recalculation is 2026-06-11, a month stale at the cut. Over-partitioned: 6,093 samples shattered across `trip_type × pickup × dropoff × time_of_day × day_type`. |
| `RouteDistanceCache` | **F** [unavailable] | never | All 118 rows were written **after** the cut (first 2026-07-31). In production this table was empty for the whole window. |
| Service / cost / mileage | **F** [unavailable] | never | 0 service records, 0 service schedules, 0 faults; `VehicleDayReading` has 11 non-null `miles_driven` values across 66 rows. |
| KEOI as a risk label | **F** [unavailable] | never | 5 rows total, **all created after the cut**, 4 still open. Cannot be a validation signal or a training label. |
| Sandbox schedule drafts | **B** | — | 20 drafts / 923 `DraftAssignment` rows / 551 events — unlike KEOI, real usage. |

## A5. Assumptions register

Every downstream deliverable inherits these. Numbered so they can be cited and challenged.

| # | Assumption | Basis | If wrong |
|---|---|---|---|
| **A1** | Booked `pickup_time` is a usable proxy for when a driver becomes occupied. | Not tested. | **Invalidates the demand curve, the peak, every shift boundary and every staffing number.** See §C3 — this is the blocking test. |
| A2 | Production data ends 2026-07-11; everything after is local development. | Four independent write streams + microsecond fingerprint + single author + post-cut migrations. | Nothing — this is as well established as anything in the file. |
| A3 | `driver_type = 'affiliate'` on the assigned driver identifies a farm-out. | 99.98% agreement with the payment ledger; the application itself uses it in five places. | Small. `driver_type` is a *current* flag with no history table, so a driver who ever switched arms retro-relabels their past. An all-driver pay-step test found **0** drivers with a regime change, which bounds but cannot eliminate the risk. |
| A4 | `is_active` / `driver_type` / `exclude_from_timing` are current-state flags applied retroactively. | No history table exists for any of them. | Material and one-directional. **7 in-house drivers flagged inactive today drove 1,871 legs inside the window**; a replay filtered on `is_active` erases a fifth of the crew that actually worked. A further 2,545 in-house window legs sit under `exclude_from_timing = 1` — a July remediation decision applied to February behaviour. **Never filter a replay on these flags.** |
| A5 | The gold cohort generalises to the fleet. | It does not, and the gap is measured. On identical metrics, dwell P75 is 47 min (gold) vs **54** (all drivers) and P90 64 vs **77**. Only 81.0% of window legs carry an `on-the-way` tap, and which ones do is decided by driver discipline, not at random. | A buffer or turnaround gate fitted to gold and deployed fleet-wide is **+8 min tight at P75 and +13 min at P90**, in exactly the tail where it fails. Use the all-driver row for anything that must hold fleet-wide. |
| A6 | Demand *shape* is stationary across the window even though *level* is not. | TVD 0.020 (day-of-week), 0.045 (hour), 0.086 (`dow × hour`) between the ramp and the plateau. | Low. Licenses the dual window in §A2. |
| A7 | The farm-out premium is stable; the volume is not. | Premium range across six months $64.76–$71.80 (±5%). Volume range 716 → 137 legs/month. | The per-leg premium is safe. **Annualising the window average over-states today by about a third** — quote a recent run rate, not the window mean. |
| A8 | Idle-vehicle carrying cost must be a founder-supplied parameter. | There is no acquisition cost, no lease, no insurance premium, no maintenance spend, and under three weeks of partial mileage anywhere in the schema. | It must be carried as an explicit **[assumed]** parameter with a sensitivity range, and **must never be blended into a figure that also contains [measured] farm-out dollars.** Note `Leg.calculate_profit()` is already revenue-share minus driver pay only — "profit" in this codebase means contribution *before* vehicle cost. |
| A9 | DVA is the roster record from 2026-01-18. | Precision 99.5%, recall 94.9%. | **Qualify it.** Of 1,936 in-house DVA driver-days, only **9** are not already derivable from `leg.driver_id` — the table is a near-mirror of the assignment table, so 99.5% agreement is circularity, not corroboration. It is an **A for "which car"**, a **D for "who was rostered"**, an **F for "who was available"**. |
| A10 | `reservation.total_price ÷ leg count` is per-leg revenue. | Only usable option; 99.9% populated. | Wrong on asymmetric round trips, and 81.6% of legs are on multi-leg reservations. Never make it the load-bearing term in a premium estimate — the route×class match and the rate-table check exist for that. |
| A11 | Cancellations before 2026-01 read as ~0% because the flag was not in use, not because nothing was cancelled. | 0.0–0.1% for 2025 vs 1.9–3.8% from 2026-01. | **The cancellation series must never be trended.** |

## A6. Non-negotiable filters

```sql
WHERE l.pickup_date BETWEEN '2026-03-01' AND '2026-07-11'   -- never MIN()/MAX()
  AND l.status <> 'cancelled'
  AND r.status NOT IN ('cancelled','canceled')              -- BOTH spellings exist
```

- **`'canceled'` (one L) is real** — 5 reservations, 1 leg. `day_setup.py:122-123` excludes only the
  two-L spelling. A 1-leg bug today that will grow silently.
- **Do NOT filter `exclude_from_analytics`** for demand. All 32 rows are `completed`, all in Feb–Mar,
  all artefacts of the July timing audit. It is a timing-quality flag, not a demand flag.
- **Do NOT exclude `status = 'in-progress'`** — it is the Django model default for a new Leg
  (`reservations/models.py:1069`), meaning "not started". Excluding it deletes 4,934 legs.
- Anything reading `driver_assigned_at`, `legstatus`, `ops_operationaltask`, `reservations_auditlog` or
  `reservations_historicalleg` needs an **additional** `<= 2026-07-11` filter. The 194 locally-written
  driver assignments all fall on pickup dates in 2026-07/08, so the `pickup_date` cut removes them for
  free; `ops_operationaltask` must be cut on `created_at`. Those 194 assignments were made by the local
  scheduler and are **100% in-house** — ingesting them fabricates a 0% farm-out rate.
- Exclude legs 9210 and 9211 (dates 2029-09-09 and 3220-03-06). Keep the 188 legitimate 2027 bookings.

---

# PART B — INVENTORY

## B1. The headline: this is the fourth demand-vs-staffing signal, not the first

Day Setup already answers "how many drivers does this day need", and the codebase has already tried and
rejected the obvious version of the new feature.

| Question | Answered today at | The number |
|---|---|---|
| How many bodies does the day need? | [day_setup.py:443](../../dispatching/day_setup.py#L443) | `peak_concurrency(date)["overall"][0] + DAY_SETUP_PEAK_BUFFER` — peak concurrent in-flight legs, **+1** |
| How many cars must physically go out? | [day_setup.py:199](../../dispatching/day_setup.py#L199) `parkable_units` | `len(must_run)` — Hall's condition over the per-tier cumulative peak, floored by the overall peak |
| Are we short? | [day_setup.py:945](../../dispatching/day_setup.py#L945) | `final_checked < len(must_run)` → *"The busiest moment needs N cars out but only M drivers are ticked — add K more from the bench, **or the gap farms out**."* |
| What does the day look like hour by hour? | [day_setup.py:158](../../dispatching/day_setup.py#L158) `concurrency_series` | 30-minute grid, split by exact vehicle type |
| Does this leftover work need another body? | `shift_advisor.py` | residual legs + over-span drivers → a concrete named driver and car |
| Can I release a body? | `fold_advisor.py` | whole-day-or-nothing, validated through a six-gate simulation |
| Is the work spread fairly? | `rebalance_advisor.py` | the founder's relative-balance rule |
| Is the driver day understaffed? | [schedule_risk.py:46](../../dispatching/schedule_risk.py#L46) | a **flat `COVERAGE_TARGET_DEFAULT = 14`**, seven days a week, all year |

**Four traps this creates for Phase 3.**

1. **The raw peak has already been demoted.** The comment at
   [day_setup.py:941](../../dispatching/day_setup.py#L941) records why: comparing ticked drivers to the
   raw peak *"counts bookings we farm out and **fired on 9 of 23 days**"*. The shipped rule compares
   against `must_run` — a *car* count after Hall's condition — instead. A new panel that says "peak 13 vs
   11 drivers → 2 short" contradicts the sentence rendered directly below it. Corroborating: on
   2026-07-12 `peak_concurrency` read **25 concurrent** where in-house has never covered more than **13**
   at once.
2. **Per-size shortfalls were deliberately removed.** [day_setup.py:596](../../dispatching/day_setup.py#L596)
   caps per-tier `need` at fleet capability specifically because "needs 19 towncar-capable units" on a
   13-car fleet was unactionable and fired 24 times across 11 days. A demand-vs-staffing panel reporting
   per-class gaps re-introduces exactly the finding that was deleted.
3. **Two demand denominators are already in play.** `peak_concurrency` counts unpaid legs deliberately
   ("staffing for a maybe-paid leg errs safe"); the Second-Shift Advisor strips them when
   `exclude_unpaid` is on, and the code records that one skipped unpaid leg *"was enough to suppress
   every Fold-Out card"*. A new panel must pick a side and say which, or it will report "you need 14" on
   a day the advisor reports fully covered.
4. **Timing and sovereignty — the highest-probability contradiction in the design space.** All three
   staffing advisors run **post-build**; Day Setup runs **pre-build**. If a diagnostic says "you need 14,
   you have 11" and the build then covers everything with 11 — which it can, because the peak is
   explicitly documented as a *lower* bound — the dispatcher sees it contradicted five seconds later by a
   zero-residual preview and a Fold-Out card telling them to *remove* a driver.

**The `+1` is a fudge factor for unmodelled turnaround.** `estimate_job_end_time`
([scheduler.py:824](../../dispatching/scheduler.py#L824)) models drive time plus airport dwell and nothing
else — no reposition, no wash/fuel, no base return. `DAY_SETUP_PEAK_BUFFER = 1` is a flat body added to
compensate. The principled replacement is to inflate each leg's occupancy interval by measured
turnaround rather than adding a constant body, which is precisely what this engagement is for.

## B2. Module inventory

Everything below is read-only unless marked. Grades are for *reuse in the new feature*.

### The three staffing advisors — the closest prior art

| | Second-Shift (`shift_advisor.py`, 261) | Fold-Out (`fold_advisor.py`, 317) | Rebalance (`rebalance_advisor.py`, 430) |
|---|---|---|---|
| Fires when | a residual leg exists, **or** a working driver's effective span > 13.5 h with a peelable tail | `residual_count == 0` **and** a working car-holder has ≤ 3 jobs whose whole day re-seats | `residual_count == 0`, ≥ 2 working drivers, job spread ≥ 3, and someone at ≤ `max(1, floor(mean × 0.5))` |
| Demand notion | residual **leg count** + residual `revenue_share` + over-target span hours | jobs per driver + receiver span hours | jobs per driver (unweighted) + raw span + max internal gap |
| Granularity | leg → cluster (180-min split, 20-min chain pad) | per driver, whole day | per driver, whole day |
| Produces a required-driver number? | **No** | No | No |
| Gate stack | **none** — defers to Apply plus the rebuild | `idle → window → tier → occupancy → feasibility → span` | the same six, **hand-copied** |

**All three are ON in production, unconditionally, for every `is_staff` user** — governed by six
module-level Python constants across three files. There is no feature flag, no `SchedulerSettings` field
(the singleton's 74 fields contain no advisor toggle [measured]), and no per-user gate. They run only
inside `views.auto_assign_drivers` when a human clicks Build Schedule; there is no background execution
anywhere. Each call is wrapped in a bare `try/except` that logs and continues, so a crash silently
yields no cards.

**None of the three ever reads the day's booking volume, revenue total, hourly concurrency, or
vehicle-tier demand mix.** All three read only the *output* of a build that already happened. That is the
gap this engagement fills — and also why the new pre-build diagnostic can contradict them (§B1, trap 4).

The founder's relative-balance rule, exactly as coded: *a driver is thin when
`jobs <= max(1, floor(mean_jobs × 0.5))` **and** the day's `max(jobs) − min(jobs) >= 3`; a donor may give
only while `jobs − 1 >= ceil(mean_jobs)` **and** `jobs − 1 >= thin_jobs + 1`; fill stops at
`floor(mean_jobs)` or three moves; the card ships only if `spread_after <= spread_before`; and no move may
leave either side hollow (`raw span >= 10 h with a >= 240 min hole`).*

**There is no shared gate helper** — `fold_advisor._simulate` and `rebalance_advisor._gate_receiver` are
two hand-maintained copies of the same six gates. **Extracting one shared `gate_receiver()` should be a
prerequisite of this feature, not a follow-up**; a third copy is the point of no return.

### `dispatching/day_setup.py` (993) — what Phase 3 extends

Read §B1 first. Additional facts a builder needs:

- **The "pure function of (date, DB), no writes" contract is false transitively.** Directly true — no
  `.save()`, `.create()`, `.delete()` or `cache.set()` in the module, and a test pins determinism. But
  `suggest_day_setup → peak_concurrency → estimate_job_end_time → resolve_drive_minutes →
  route_distance.cached_drive_minutes` can, on a cache miss for an `Other` / `Residential` /
  `Other Hotel` or intra-cluster route, **INSERT a `RouteDistanceCache` row and spawn a daemon thread
  that calls the billed Google Distance Matrix API**. It also mutates the process-global
  `scheduler._timing_cache`. *Scope correction:* all 118 cache rows in the snapshot are post-cut, and
  those categories are 1.3% of legs — so this path almost never fired in production. It is still a real
  contract violation, and Phase 2 must either fix it or restate the contract honestly rather than
  repeating the docstring.
- **A locked DVA row bypasses the availability hard gate entirely.** The founder's "make sure they are
  physically available" rule is not enforced for anyone who already has a car row — including a driver
  whose approved time-off was filed after the row was created.
- **Availability is used as one bit.** `end_hour`, `max_hours`, `flexible`, the `available_until` /
  `available_after` / `available_window` times and `preferred_shift` are all discarded. A driver
  available 4 p.m.–8 p.m. is pre-checked, given a car, and counted by the peak cap as a body covering a
  09:30 peak they cannot physically serve.
- **Every threshold is hard-coded** — no `SchedulerSettings` field, no Django setting, no admin surface.
  Retuning `DAY_SETUP_PEAK_BUFFER` needs a deploy.
- **Apply never deletes.** Unticking a driver removes them from the payload; it does not clear their DVA
  row. Apply is purely additive and idempotent (re-posting writes nothing).
- **Load-bearing UI contracts** the panel must not disturb: `swaps` strings are **regex-parsed** by the
  template, `reason` strings are pattern-matched, `hint` must keep an `N/M` substring, `vehicle_label`
  must stay `"#NNN <type>"`, and any injected DOM must not match `.ds-row` / `.ds-check` / `.ds-veh` or
  `dsCollect()` will read it as an Apply pair. Adding *new keys* to the payload is safe — the JS reads by
  name and never enumerates.
- **Latency budget:** ~11–12 baseline queries plus a `can_drive` N+1 (`.filter()` bypasses the prefetch;
  ~338 calls on a full `unit_options` pass). An additive panel is effectively free *if* it reuses the
  already-materialised `legs` list and the spans already computed, rather than calling
  `estimate_job_end_time` a third time per leg.

### `feasibility_guards.py` (432) + `pickup_policy.py` (375) — the constraint layer

`pickup_policy` is the single definition of "late" for the whole board, and every band reads **slack**,
never raw clock distance. Constants, each verified by `grep -n`:

| Constant | Value | Line |
|---|---:|---|
| `ARRIVAL_MEET_GRACE_MIN` — driver at the **in-terminal** meet point by gate + this | 10 | [pickup_policy.py:46](../../dispatching/pickup_policy.py#L46) |
| `ARRIVAL_DWELL_MIN` — when the pickup should have *happened* | 45 | [:63](../../dispatching/pickup_policy.py#L63) |
| `WATCH_SLACK_MIN` | 10 | [:66](../../dispatching/pickup_policy.py#L66) |
| `TURN_TIGHT_SLACK_MIN` | 15 | [:87](../../dispatching/pickup_policy.py#L87) |
| `DEPLANING_GRACE_MIN` | 10 | [feasibility_guards.py:39](../../dispatching/feasibility_guards.py#L39) |
| `SAFETY_PAD_MIN` | **0** | [:44](../../dispatching/feasibility_guards.py#L44) |
| `MIN_TURN_BUFFER_DEFAULT` | 5 | [:64](../../dispatching/feasibility_guards.py#L64) |
| `BUFFER_MODES` aggressive / standard / relaxed | 0 / 5 / 10 | [:67](../../dispatching/feasibility_guards.py#L67) |
| `SPAN_SOFT_EFFECTIVE_HOURS` / `SPAN_HARD_HOURS_DEFAULT` | 13.5 / 15.0 | [:109](../../dispatching/feasibility_guards.py#L109), [:97](../../dispatching/feasibility_guards.py#L97) |
| `VEHICLE_SHARE_PAD_MIN` | 60 | [scheduler.py:138](../../dispatching/scheduler.py#L138) |
| `STATIC_FLOOR_DWELL_MIN` | 45 | [scheduler.py:195](../../dispatching/scheduler.py#L195) |
| `DEFAULT_DRIVE_TIME` (unknown lane) | 35 | [scheduler.py:83](../../dispatching/scheduler.py#L83) |

**The existing turn-band vocabulary has three values, not five.** `pickup_policy.turn_band(slack)`
returns `''` (healthy) / `'tight'` (< 15 min) / `'critical'` (< 0). The separate live-risk ladder is
`on_time` / `watch` / `at_risk` / `late` / `unknown`. The brief asks for
INVALID / CRITICAL / TIGHT / HEALTHY / EXCESSIVE. **INVALID and CRITICAL both collapse onto the shipped
`critical`; EXCESSIVE has no equivalent at all.** Phase 2 must either extend `turn_band` in place or
justify a parallel vocabulary — silently renaming existing bands would break the one-definition-of-late
property this module exists to hold.

**`required_turnaround` models a driver-continues turn, not a vehicle handoff**
([feasibility_guards.py:184](../../dispatching/feasibility_guards.py#L184)). It returns
`-DEPLANING_GRACE_MIN` when the next pickup is an airport arrival and the driver is already at that
terminal, otherwise the full reposition drive, plus `SAFETY_PAD_MIN = 0`. There is **no wash/fuel step
and no base return** anywhere in it. The brief's chain (clear → wash/fuel → base → possession →
reposition → pickup) is therefore genuinely new modelling. It must not contradict `required_turnaround`
for the same-driver case, and it must be presented as an addition, not a re-derivation. **And it cannot
be validated as specified** — see §C2.

### `dispatching/scheduler.py` (4,195) — the live engine

**There is no capacity model anywhere.** "This day cannot be covered in-house" is an emergent residue of
six hard gates in order: (1) the driver has no DVA row for the date — *a Day Setup decision, not a
scheduler one*; (2) not in the modal payload; (3) vehicle tier; (4) turnaround; (5) window/span;
(6) turn buffer. Survivors are retried through six recovery passes; whatever is left lands in
`still_unassigned` and the dispatcher farms it manually. **The engine never says "we are N drivers
short" — it says "here are M legs nobody could take."**

The only demand-vs-supply *ratio* anywhere is `time_scarcity_map` = `hour_demand / hour_supply`, where
supply counts a driver as supplying **every hour of their window** even while they are on a three-hour
cruise run. It is used only to reorder legs. The `SchedulerSettings` field named for it,
`time_scarcity_bonus`, is **never read by any code** — as are `span_threshold_hours` and
`span_penalty_per_hour`. Three dead, UI-exposed fields are available for repurposing rather than adding
new ones.

`SchedulerSettings` is a singleton with **74 tunable fields** [measured — 75 columns including `id`; the
brief's "~90" is high]. The live row reads `rest_min_gap_minutes = 510`, `rest_penalty_per_hour = 40`,
`min_turn_buffer = 5`, `arrival_grace_minutes = 10`, `reserve_max_scarcity = 2`, `reserve_penalty = -60`,
`idle_gap_threshold = 120`, `cluster_gap_minutes = 120`.

### `rest_advisor.py` (140) — **ON in production, correcting the brief**

The brief describes the Rest Advisor as "built but currently off". [measured] **Both halves are live.**
The `SchedulerSettings` singleton carries `rest_min_gap_minutes = 510` and `rest_penalty_per_hour = 40`,
so the scorer arms itself (`scheduler.py:1963`) *and* the cards arm (`rest_advisor.py:43`) and render in
a teal panel in the planner template. The rule:
`rest_hours = first_pickup_today − estimated_last_dropoff_yesterday`; a card fires below
`510/60 − 15/60` hours, capped at four cards.

This is the **only** module expressing a human-rest constraint for drivers, so it is the natural home for
any shift architecture's minimum inter-shift gap. A new rest minimum that does not read
`rest_min_gap_minutes` forks the rule. Weaknesses: rest is measured pickup-to-dropoff, not door-to-door,
so real rest is materially less than reported; the alternative-driver search ignores feasibility and can
propose a swap the engine would reject; and if the previous-day query block raises, the whole feature
silently disables with no signal. [inferred, on a generous measure] **26 of 634 consecutive-day pairs
(4.1%) fall under 8.5 h; 9 under 6 h; 2 under 2 h.**

### `ops/scheduling.py` + `ops/coverage.py` — the Staffing Board is **office-only**

Verified: the roster selector `ops/staff.py:21 office_staff_qs()` explicitly *excludes* drivers, and both
files contain **zero** references to `Driver`, `Leg`, `Reservation` or `Vehicle` [measured — the only
import from `drivers` is a string formatter]. The models are a parallel family (`StaffWeeklySchedule` vs
`DriverWeeklySchedule`). Scale: 10 office users, 35 staff schedule rows, 0 on-call rows, 0 extra-shift
rows — a nearly-empty system beside a populated driver side (33 drivers, 231 rows, 90 overrides).

**The structural finding: nothing in the file named `coverage.py` consumes demand.** Its target is a
hard-coded literal 2 in 9 a.m.–8 p.m. If the premise of this engagement is "compare demand to staffing",
it is building the *first* such thing in this codebase. The reuse candidates are the pure minute-math
helpers and the resolver *shape*, not the coverage judgement.

### Existing "shift template" vocabulary — there is nothing working to preserve

Four overlapping enums exist and none functions:

- `DriverWeeklySchedule.SHIFT_TYPE_CHOICES` — 7 values (`morning` / `midday` / `evening` / `night` /
  `split` / `full_day` / `custom`). The help text claims named types auto-set hours; **no such preset
  table exists anywhere** [measured]. Only 2 of 7 values are in use (187 `full_day`, 44 `custom`).
- `PREFERRED_SHIFT_CHOICES` — 5 values, consumed only for a soft label. 219 of 231 rows are blank.
- `PREFERENCE_CHOICES` (trip type) — 10 values, of which `only_*` are a hard skip in the scorer. **0 rows
  use any of them.**
- A second, undeclared **display** vocabulary at `views.py:15121` remaps the 7 shift types onto
  `SHIFT_BUCKETS = ("morning","midday","evening","night","split","flex","set")`, which
  `schedule_risk.py` then reasons over.

**Consequence worth flagging loudly** [inferred, from measured data plus `schedule_risk.py:203-213`]:
since every real row is `full_day → flex` or `custom → set`, `flex_covering > 0` on any day anyone works,
so `shift_gaps` is **always empty** and the essential-shift escalation to "critical" can **never fire in
production**. The named-shift machinery is not merely unused — it makes a live risk rule vacuous.

The live editor writes `shift_type` from the `flexible` checkbox alone
(`flexible ? 'full_day' : 'custom'`), hard-coding `default_start_hour: 6`, `default_end_hour: 23` for
every driver. The new architecture can replace `SHIFT_TYPE_CHOICES` outright, but must migrate the 44
`custom` rows (the only ones carrying real window information) and update `DESIGN_BUCKET` and
`schedule_risk.py:55-65` **together**, or the essential-shift rule flips from always-silent to
always-firing.

### `farmout_optimizer.py` (1,485) + `farmout_report.py` + `farmout_actions.py`

Answers "cheaper to farm this leg, or keep it and farm something else instead", pricing from real
`DriverPayRate` cards through `drivers/pay_calc`. It carries a `WaterfallLedger` that prevents the
marginal-vs-total double count — **the pattern any "+1 driver recaptures N legs" number must use**,
because per-leg recoverability is *not additive*. It abstains rather than inventing a price for an
uncarded affiliate. Hard-coded operational identities not to inherit silently: `OUALID_DRIVER_ID = 7`,
`ANTHONY_DRIVER_ID = 29`, `ANTHONY_MAX_LEGS_PER_DAY = 12`, `DEFAULT_MIN_SAVINGS = $20.00`.

**Its capacity model runs on an unvalidated parameter.** It consumes `AffiliateProfile.daily_cap`, with a
hard-coded ~12/day fallback. [measured] Only **4 of 18** affiliate rows have a profile at all, and 13 of
the 15 affiliates who actually took work have no cap configured. Of the two that do, **`Cheapolimo`
exceeded its cap of 12 on 21% of the days it worked (max 21)** and `anthony` on 6% (max 22 vs 15).
`daily_cap` does not describe what happened.

### Fleet Capacity Intelligence — **the buy/hire engine does not exist**

The brief instructs reuse of "a config-driven margin engine" for buy-vehicle vs hire-driver vs
keep-farming. [measured] **That engine is documentation only.** `VehicleTypeCostProfile`,
`simulate_plus_one_vehicle` and `fleet_simulation.py` do not exist in code or schema; a repo-wide grep
for `carrying_cost|monthly_cost|depreciation` hits only doc prose. What *does* exist is a per-leg
categorical **label** (`ACT_BUY` / `ACT_HIRE` / `ACT_PREVENTABLE` / …) derived from a feasibility replay
against the day's actual board — no break-even, no cost input, no formula.

**Reusable, and genuinely worth reusing** — three Decimal-dollars-per-leg functions where `None` means
uncomputable and never zero: `fleet_intel.affiliate_base_cost(leg)`,
`fleet_intel.inhouse_counterfactual_cost(leg)`, `fleet_intel.recovered_margin(leg)`.

**Not reusable, must be assumed** — vehicle carrying cost. 0 service records, 0 service schedules, 0
faults, `in_service_since` NULL on all 14 vehicles, no purchase price or premium column anywhere. See
assumption **A8**.

**Critique to carry forward:** `recovered_margin` is positive on **2,837 of 2,839** signed legs — an
estimator that cannot produce its own falsifying case, because it compares a real affiliate payment to
19 flat `route.inhouse_base_pay` numbers that have never been checked against realised in-house pay.
And 7.6% of farmed legs sit on days with **zero DVA rows** and are auto-labelled `ACT_BUY` — that is
evidence about data entry, not about the fleet.

### Modules the brief named or implied that also matter

- **`board_validation.py` (454)** — `turn_slack_minutes()` is *the* slack formula, including the
  recorded-pickup re-anchor; `board_turn_bands` sweeps it and bands via `pickup_policy.turn_band`. It
  exists because three copies of this arithmetic lived inline in `views.py` and were promoted so "the
  advisor and the apply path can never disagree at the threshold." **Any new diagnostic that renders
  slack must call it.**
- **`conflict_advisor.py` (2,943) — the Recovery Advisor engine.** The second-largest scheduling module
  in the repo. Its **two-clock policy** is directly relevant and appears nowhere else: the detection
  clock re-anchors on recorded pickups, the planning clock is
  `max(chain_clear_dt, chain_clear_dt_from_actual)` and is *never* optimistic, and
  `estimate_job_end_time` is **banned** from feasibility there. It is also the codebase's only worked
  example of the operational discipline this feature needs — a one-line release switch, a per-(date,
  fingerprint) shared compute cache with a shape version in the key, a presentation layer separated from
  the engine, and a `safe_display` degradation path. **Build on this pattern, not on the staffing
  advisors' "module-level `True` plus inline JS in a 4,000-line template".**
- **`samsara_scheduler.py` (385) — the only background loop in the system.** One daemon thread from
  `AppConfig.ready()`, guarded by a Postgres advisory lock; 3-minute GPS poll, 6-minute paid Google ETA
  refresh. `fleet_sync.py` rides in the same thread for the nightly reconcile. **There is no cron, no
  Procfile entry, no Celery worker** — which is why `update_demand_patterns` and
  `update_daily_capacity_metrics` are empty tables, and why this thread is the only place a new periodic
  job could live.
- **`load_insights.py` (349)** — carries the **threshold doctrine** that is the direct precedent here and
  is quoted nowhere else: *"each outlier rule needs BOTH a relative condition (versus cohort peers) and
  an absolute floor. A purely self-calibrating cutoff always manufactures an outlier … a purely absolute
  cutoff is the `COVERAGE_TARGET = 14` mistake."* The codebase's own written verdict on the number this
  engagement is about to replace.
- **`analytics.py` (1,418)** — the taxonomy and the metric writer. Holds the outlier discipline
  (`MAX_DWELL_MINUTES=120`, `MAX_DRIVE_MINUTES=180`, IQR ×1.5 at n ≥ 5), the status-chain gate
  (`REQUIRED_ANALYTICS_STATUSES = {on-the-way, picked-up, completed}` — note **`on-location` is not in
  the production full-chain test**), and `first_status_times` (correctly `MIN` per status, immune to the
  duplicate-tap inflation). **This is the module that would have to change to make the redesign
  learnable.**
- **`ScheduleDraft` / `DraftAssignment` / `ScheduleDraftEvent`** — per-date hold → edit → review →
  publish, with `DraftAssignment` as a delta overlay (row with driver = assigns; row with NULL =
  unassigns; no row = no opinion). 20 drafts / 923 rows / 551 events of real use. **Any propose-and-apply
  feature must route through `assignment.set_leg_driver` — and `auto_assign_drivers`' apply path already
  bypasses it, so there are already two implementations of one gate.**

### `COVERAGE_TARGET = 14` — the number being replaced

It exists **twice**: `schedule_risk.py:46` and inline at `views.py:15117`, used at `:15448-15451` and
passed back into `schedule_risk` at `:15515`. Flat, seven days a week, all year, with no per-date
override path — against a measured demand shape where **Saturday runs 2.2× Tuesday** and Fri+Sat+Sun
carry 55.4% of volume on 43% of the days.

## B3. Where the new feature's config belongs

| Concept | Home | Note |
|---|---|---|
| Turnaround / handoff feasibility | `fg.required_turnaround()` — **a function, not a number** | Do not mint a parallel knob. Reuse the engine. |
| Planning turn buffer | `min_turn_buffer` + `Driver.default_min_turn_buffer` + `fg.BUFFER_MODES` | A four-level most-specific-wins resolver already exists. |
| Overnight rest | `rest_min_gap_minutes` / `rest_penalty_per_hour` | Live. A diagnostic must honour 0 = disabled. |
| Idle-hole definition | `idle_gap_threshold` = 120 min | Three places already agree on 120. Do not mint a fourth. |
| Day-length limits | `SPAN_SOFT_FREE_HOURS 12 / SPAN_SOFT_EFFECTIVE_HOURS 13.5 / SPAN_HARD_HOURS_DEFAULT 15` | Calibrated against 39 hand-built founder driver-days. |
| **Staffing target / headcount** | **nothing fits** | The single most important new config value. Needs a real per-date or per-weekday home, not a constant. |
| **Percentiles** | **nothing fits** | The engine has exactly one percentile — p75 — hard-coded as a preference order with a hard-coded `sample_count >= 5` floor. |
| **Concurrency / peak-in-flight** | **nothing fits** | No concept exists in settings. |
| **Shift templates themselves** | **nothing fits** | See §B2 — the existing enums are non-functional. |
| Available for repurposing | `time_scarcity_bonus`, `span_threshold_hours`, `span_penalty_per_hour` | Live, UI-exposed, **never read by any code**. Repurposing beats adding. |

---

# PART C — GO/NO-GO

## C1. What this snapshot can and cannot support

| Phase 1 deliverable | Feasible? | Constraint |
|---|---|---|
| `DEMAND_AND_UTILIZATION.md` | **Yes** | Conditional on the §C3 test. Shape from the 192-day window (normalised), levels from the 133-day primary. |
| `SHIFT_ARCHITECTURE.md` | **Yes, with a named gap** | Templates are *summer* templates — season and growth are confounded and cannot be separated [unavailable]. There is no historical record of when anyone was on duty, so supply can only be modelled as bodies-per-day. |
| `FARMOUT_RECAPTURE.md` | **Yes** | Premium is solid to ±5%; the volume term carries ±33%. Must be quoted on a recent run rate, must be labelled **gross premium on all farm-out** (not avoidable cost), and the +1/+2/+3 estimate must use a sequential capacity-consuming replay (`WaterfallLedger`), never a count of legs that individually pass feasibility. |
| `REPLAY_AND_EVIDENCE.md` | **Yes, on 2026-04-01 .. 2026-07-12** | 102 days at 100% DVA coverage, 100% leg assignment, median-perfect roster match. A harness already exists: `suggest_day_setup(ignore_existing=True)` backtested 23 days in August. **Never filter the replay on `is_active` / `driver_type` / `exclude_from_timing`** (assumption A4). |
| Idle-vehicle carrying cost, in dollars | **No** [unavailable] | Founder-supplied parameter with a sensitivity range, never blended with measured dollars. |
| Seasonality of any kind | **No** [unavailable] | Twelve months of data, of which the first eight are a materially smaller company. |
| Anything about 2026-07-12 → today | **No** [unavailable] | Including the entire period the redesigned Day Setup has been in use. |

**Farm-out, the headline numbers** [measured, primary window]: **20.5–21.2%** of assigned legs are farmed
— but the share has **fallen from 40% (Oct 2025) to 12.5% (Jul 2026)** and is still falling, so a
window-average baseline over-states today by about a third. Concentration is extreme and is the clearest
staffing signal in the file: **Fri–Sun carry 75.8% of all farm-outs** on 55.8% of legs (Sat 30.6%,
Sun 28.7%, Tue 7.0%), and **86.1% of farm-outs are committed within 24 h of pickup**, 79.6% inside the
6–24 h band — exactly when the day-before schedule is built. Per-leg premium by class: towncar $58–61,
mini_van $68–71, suv $69–73, van $111–127, Van(14 Pax) $126–134.

**Overall the estimators disagree by about 12%** — $65.00 (within-reservation matched pair, median) /
$67.79 (same, mean) / $68.82 (dollars-correct counterfactual) / $73.96 (route × class, per-stratum
medians). The spread is a real methodological question, not noise: gratuity is 26.6% of in-house driver
dollars but only 9.1% of affiliate dollars, so any per-stratum *median* systematically under-prices the
in-house side. **Resolving this is the first task of `FARMOUT_RECAPTURE.md`**; until then, quote the
range. For scale, total driver-pay dollars in the window are **affiliate $306,844 on 2,859 legs
($107.33/leg) vs in-house $407,684 on 10,629 legs ($38.36/leg)** — 21% of legs consume 43% of driver-pay
dollars.

## C2. Three premises in the brief that are wrong

| Brief says | Actually |
|---|---|
| Rest Advisor is "built but currently off, default `rest_min_gap_minutes=510`" | **It is ON.** The live singleton carries 510 *and* `rest_penalty_per_hour = 40`, so both the scorer penalty and the cards are armed and rendering. Any new inter-shift minimum must read this field, not fork it. |
| Fleet Capacity Intelligence "already has a config-driven margin engine" for buy vs hire vs farm | **The margin functions exist; the decision engine does not.** `VehicleTypeCostProfile` and `simulate_plus_one_vehicle` are documented and unbuilt; `fleet_simulation.py` does not exist. There is no cost input anywhere in the schema. Reuse `recovered_margin` for the dollar side, and treat buy-vs-hire as out of scope or as an explicitly-assumed model. |
| The handoff chain: "return to base (**MCO ↔ base ≈ 12 min** default)" | **There is no base.** No base address, no coordinates, no depot `Location` row, no per-vehicle home yard [measured, by exhaustive grep]. The only `BASE_LOCATION` literal is a *pricing* reference point in `quote_engine.py` that never enters scheduling. The scheduler says so itself at `scheduler.py:133-139`: a geography-aware split *"needs a base-location concept the engine doesn't have yet"* — which is why `VEHICLE_SHARE_PAD_MIN = 60` is a flat stand-in for the entire chain. The "12 min" coincides with `DRIVE_TIME_ESTIMATES[('MCO Terminal','Airport Hotel')] = 12`, a category constant, not a measurement. |

**What follows for the handoff model.** Decomposing the chain into
`clear → drive_to_base + service + drive_out → pickup` cannot be validated: `clear → base` is
[unavailable], and wash / fuel / inspection have **no event, no field and no table** anywhere. Writing it
that way buys false precision — invented numbers wearing a measurement's clothes. What *is* validatable
end to end:

- **Same-driver reposition** [measured, n = 6,250 in-house-trustworthy consecutive gaps]:
  `completed(N) → on-location(N+1)` at **P25 27 / P50 49 / P75 86 / P90 132** min. Per lane the
  directional asymmetry is large and real: `MCO → MCO` P75 72 vs `Disney → MCO` P75 **122**.
- **Real observed vehicle handoffs** [measured, small n]: 80 shared car-days, 60 with both drivers'
  taps; AM-clear → PM-first-move at P25 71 / **P50 133** / P75 207. **10.0% are negative** and
  **18.3% fall under the `VEHICLE_SHARE_PAD_MIN = 60` the engine enforces** — so the pad is optimistic
  on roughly one handoff in five.

**Recommendation:** keep the handoff as **one tunable constant covering the whole base round trip plus
service**, tuned against those two measured distributions, and say plainly that its components are
unobservable. Also note that the `completed → on-the-way` gap (P50 2–8 min, 3.8% negative) is **tap
latency, not turnaround** — drivers close A and open B in the same moment. `completed → on-location` is
the physically meaningful clock.

*One reconciliation to carry into the deep analysis:* ordering consecutive legs by **actual first tap**
rather than scheduled `pickup_time` moves the turnaround P90 from 131 to **140 min**, because
**301 of 2,223 multi-leg driver-days (13.5%) were actually run in a different order than scheduled**.
P50 and P75 are unaffected; only tail-calibrated gates are exposed. Use the tap-ordered figure.

Two smaller corrections: `STATIC_FLOOR_DWELL_MIN = 45` is a slight *under*-estimate at P75 and a large
one at P90 (measured 47/64 trustworthy, **54/77 all drivers**) — not an over-estimate; and
`SchedulerSettings` has **74** tunable fields, not ~90.

## C3. The blocking test — run this before the deep analysis

**Assumption A1: booked `pickup_time` is a usable proxy for when a car is occupied.**

Every load-bearing artefact rests on it: the `dow × hour` demand curve and its 09:00 spike (12.9% of
plateau volume in one hour), `day_setup.peak_concurrency` and therefore the incumbent required-driver
number, `concurrency_series`, the scheduler's hour buckets, and the farm-out-by-hour reading that calls
09:00–13:00 a capacity crunch.

**The repo has already measured that the proxy is wrong on half the board.**
`docs/operational-data-audit.md` §11: *"median `scheduled pickup − scheduled gate arrival` = **−1
minute**. There is effectively no deliberate buffer; pickup is scheduled at the gate time."* And §10.3,
gold cohort, driver-on-location vs booked pickup:

| Lane | n | median | p90 | % > 15 min late |
|---|---:|---:|---:|---:|
| MCO Terminal → Disney Resort | 2,319 | **+37** | +68 | **96%** |
| MCO Terminal → Universal Resort | 154 | **+38** | +79 | **96%** |
| MCO Terminal → Port Canaveral | 161 | **+35** | +76 | **84%** |
| Disney Resort → MCO Terminal | 2,564 | +0 | +16 | 11% |

Airport-origin legs are **48.5% of the window** [measured]. On that half the booked time precedes the
real event by ~35 minutes at the median; on the other half it is accurate to the minute. That is a
systematic, one-directional, lane-correlated shift applied to half the demand curve — and it is exactly
the half that produces the 09:00 spike (MCO Terminal is 46.4% of all pickups).

This is not necessarily fatal: a driver becomes *occupied* when they set off, which is before they are on
location, and `estimate_job_end_time` already anchors an arrival's *end* on the flight rather than on the
booked time. The point is that nobody has checked, and the answer changes the peak's height, its clock
time, and every shift boundary cut from it.

**The test** (cheap; the data exists; window 2026-02-08 .. 2026-07-10; the re-vetted 19-driver cohort).
Build the hour-by-hour occupancy series twice per date and compare:

1. **Booked anchor** — `[pickup_time, estimate_job_end_time]`, by *calling* `day_setup.peak_concurrency`,
   not by re-implementing it.
2. **Tap anchor** — `[picked-up, completed]` from `LegStatus`, `MIN(timestamp)` per (leg, status),
   UTC−5 before 2026-03-08 and UTC−4 after.

Report per date: peak height, clock time of peak, mean-absolute and max difference across the 30-minute
series, and `peak_concurrency(...)["overall"][0] + 1` against the DVA headcount actually rostered. DVA
covers 100% of dates in that window, so the comparison is complete.

## C4. Recommended decisions

1. **Pull a fresh production snapshot.** It costs little and buys six weeks — including the only period
   in which the redesigned Day Setup has actually been used. Every script here is parameterised on the
   window and re-runs unchanged. *Decision taken 2026-08-21: proceed on the 2026-07-11 cut; refresh
   before Phase 2 if convenient.*
2. **Run the §C3 test first.** It is a few hours and it gates the rest.
3. **Treat "reconcile with the incumbent" as a Phase 2 requirement, not a courtesy.** The specific
   question the spec must answer: does the new number *supersede* `peak + 1`, or *annotate* it? Both are
   defensible; having two on one screen is not.
4. **Extract one shared `gate_receiver()`** from `fold_advisor._simulate` and
   `rebalance_advisor._gate_receiver` before adding a third consumer of the gate stack.
5. **Fix the citation discipline, not just the citations.** Two of the reader passes disagreed on line
   numbers for the same constants; every `path:line` in this document has been re-verified mechanically
   with `grep -n`. Downstream deliverables must do the same.

---

## Appendix — analysis scripts

All are self-contained, take no arguments, open the snapshot read-only, print their assumptions in a
header block, and run from the repo root. CSV output lands in `analysis/out/`.

| Script | Answers |
|---|---|
| [`00_snapshot_provenance.py`](analysis/00_snapshot_provenance.py) | When the snapshot was cut; the booking-lead-time curve; the growth curve; the recommended window |
| [`01_window.py`](analysis/01_window.py) | Volume by week / month / day-of-week; the August truncation model; the forward book; `dow × hour` demand |
| [`02_status.py`](analysis/02_status.py) | Tap coverage; duplicates; the fabricating cohort; the gold-cohort re-vet; DST; core durations; turnaround |
| [`03_farmout.py`](analysis/03_farmout.py) | Farm-out identification, volume, premium by class (three methods), affiliate concentration, commit timing |
| [`04_supply.py`](analysis/04_supply.py) | DVA coverage and trustworthiness; roster over time; driver-day shape; fleet; availability declarations |
| [`05_flights.py`](analysis/05_flights.py) | Flight linkage and anchors; delay distribution; true dwell; location buckets; lane matrix; the base question |
| [`06_challenge.py`](analysis/06_challenge.py) | Adversarial re-computation of the six highest-stakes numbers above |

**Phase 1 continues with `DEMAND_AND_UTILIZATION.md` once this document is reviewed.**
