# 00 — Data Audit and Platform Inventory

**Phase 1, deliverable 1 of 5. This is the go/no-go gate for the rest of the engagement.**

| | |
|---|---|
| Produced | 2026-08-21 |
| Source | `content/db.sqlite3`, a **live** production copy pulled 2026-08-21 22:17 UTC, opened read-only (`file:...?mode=ro`) throughout |
| Supersedes | the 2026-08-21 version of this document, which was written against a snapshot frozen at 2026-07-11. **Every figure in it has been re-derived or replaced.** |
| Prior art reconciled | [`docs/operational-data-audit.md`](../operational-data-audit.md) (2026-07-31) |
| Scripts | [`analysis/`](analysis/) — eight self-contained scripts on a shared foundation ([`analysis/_common.py`](analysis/_common.py)). **No script contains a hardcoded analysis date.** Every window is derived from the database at run time. |
| Method | Eight independent analysis passes, one dedicated occupancy study, three adversarial verifiers re-computing the highest-stakes numbers by structurally different methods, two code-inventory verifiers, and one completeness critic. The lead independently reproduced the reconciliation, occupancy and farm-out headlines before accepting them. |

Every figure is labelled **[measured]** (computed from the database), **[inferred]** (derived under a
stated assumption), **[modeled]** (output of a model), or **[unavailable]** (the data does not exist).

---

## VERDICT

**GO.** The data supports every Phase 1 deliverable. Four findings change the shape of the work.

1. **The database is live and clean.** Nine independent production write streams all run to
   2026-08-21 22:17 UTC, agreeing within 0.3 h. There is no gap in any stream, and the
   microsecond fingerprint that exposed the previous snapshot as a partial export is absent
   [measured]. The previous version of this document was scoped to a 2026-07-11 cut; that
   constraint is gone, and with it every conclusion that rested on it.

2. **Demand stepped up ~20% in late July, and the old window missed it entirely.** A
   changepoint scan puts the current regime at **2026-07-24 → today, 108.4 legs/day**, against
   a prior plateau of **90.4 legs/day** [measured]. The superseded window ended 2026-07-11,
   wholly inside the old regime. The step is robust across every parameter setting, is
   corroborated by independent streams, and is not a booking artefact (§A2).

3. **`pickup_time` on airport arrivals is the flight's arrival time — mechanically, not
   approximately — and the previous document misread that as unreliability.** It matches the
   *actual* gate arrival within one minute on 74.5% of arrival legs [measured]. The ~35–40 min
   gap to the `picked-up` tap is **dwell**, not lateness. The real question — when a leg starts
   consuming capacity — is answered in §A5, and the answer is that `pickup_time` starts the
   occupancy interval **too late**, not too early.

4. **The incumbent required-driver number is wrong for a reason nobody had measured, and the
   error is not the interval — it is the denominator.** `peak_concurrency + 1` counts *farmed-out*
   legs against *in-house* headcount, comparing a total to a part. Over 155 days it exceeds the
   drivers who actually worked on **83% of them**, by a mean of **4.3 drivers** [measured].
   Restrict the same computation to legs in-house actually ran and the peak tracks the bodies
   fielded to within **0.2 on average** (13.6 vs 13.4), removing the bias entirely (§B2). This is
   the central design constraint for Phase 2, and it is now quantified rather than asserted.

**Two facts that bound what the rest of the engagement can promise.**

- **The fleet, not the roster, is the binding constraint.** Demand peaks above all 17 active cars
  on **64% of current-regime days**, against 30% in the prior plateau [measured]. Farming the top
  of the peak is arithmetic, not a scheduling failure — no shift structure covers a 22-car moment
  with 17 cars. Recapture increments must therefore be **driver *and* vehicle pairs** (§B2.2).
- **The 20% step-up was met by attendance, not hiring.** Distinct in-house people went 26 → 27;
  days worked per person per week went 3.80 → 4.01 [measured]. That lever has a short runway, and
  10.7% of consecutive-day pairs already fall under the live rest floor (§C1.1).

**And one number the whole engagement can aim at.** The most in-house drivers ever fielded in a
single day is **18**; the heaviest-farm-out days average **13.7** [measured]. Below the fleet
ceiling, the business is short **4.3 drivers on exactly the days it most needs them** — not for
want of cars or of people on the books, but because of who is on shift that day. **That is the
gap a shift architecture exists to close**, and it is measurable, bounded and specific.

---

# PART A — DATA

## A1. Provenance — the snapshot is live production

`content/db.sqlite3` was pulled by [`scripts/pull_prod_snapshot.py`](../../scripts/pull_prod_snapshot.py)
on 2026-08-21. **Do not use file mtime as evidence of freshness** — the previous snapshot had a
current mtime and 2026-07-11 contents. Use content.

Nine independent production write streams [measured]:

| Stream | Newest row | Behind newest |
|---|---|---:|
| `reservations_auditlog.timestamp` | 2026-08-21 22:17:09 | 0.0 h |
| `reservations_leg.driver_assigned_at` | 2026-08-21 22:17:09 | 0.0 h |
| `reservations_historicalleg.history_date` | 2026-08-21 22:17:09 | 0.0 h |
| `reservations_reservation.created_at` | 2026-08-21 22:09:21 | 0.1 h |
| `reservations_quote.created_at` | 2026-08-21 22:08:31 | 0.1 h |
| `reservations_lead.created_at` | 2026-08-21 22:07:54 | 0.2 h |
| `payment_payment.created_at` | 2026-08-21 22:00:19 | 0.3 h |
| `reservations_legstatus.timestamp` | 2026-08-21 21:59:20 | 0.3 h |

**`pull_utc = 2026-08-21 22:17:09 UTC = 2026-08-21 18:17 local`** (America/New_York, EDT).
This is the derived "present" for the engagement. No script hardcodes it.

**The three forensics that condemned the previous snapshot all come back clean** [measured]:

- **No holes.** Longest run of zero rows: `legstatus` 0 days over 195, `auditlog` 0 over 224,
  `historicalleg` 0 over 172, `ops_staffactivity` 0 over 160. The only zero runs anywhere are
  five days in April 2025 in `reservation`/`payment`, when the business was doing 2 legs/day.
  The previous file had a 37-day hole.
- **Microsecond fingerprint.** The previous export truncated timestamps to milliseconds, leaving
  99.90% of `legstatus` rows ending `.###000`. Here the millisecond-truncated share is
  **79 of 94,404 (0.08%)**, with z = −1.59 against chance — i.e. indistinguishable from a clean
  full-precision export. The same holds on all six streams tested.
- **No single-author tail.** The previous file's post-cut rows were all one local user id.

**Two tables that were nearly empty are now full**, because the previous export missed them:
`reservations_auditlog` (275 rows → **260,125**, from 2026-01-10) and
`reservations_historicalleg` (223 → **208,508**, from 2026-03-03). These are new evidence and are
assessed in §A4 and §A5.2.

**Completeness boundaries, derived** [measured]:

- **Demand is complete through today.** Bookings are made in advance (median lead 18–19 days), so
  a past date's leg count is final once the date passes; today's is final too, because today's
  legs were booked earlier. `last_demand_day = 2026-08-21`.
- **Actuals must stop the day before.** The pull lands mid-evening local, so today's late work has
  no taps yet. `last_actuals_day = 2026-08-20`.
- **Forward dates are structurally incomplete** and must never enter an aggregate. The forward
  book is 1,063 legs for the rest of August, 2,699 in September, 1,757 in October, tapering to a
  single leg in 2027-11.

### A1.1 Forward dates are under-booked — and this is a live product defect

A pickup date *K* days ahead only holds the legs booked at least *K* days out. Measured on fully
observed dates [measured, `00_horizon_and_window.py`]:

| Days ahead | H-0 | H-3 | H-7 | H-14 | H-21 | H-28 | H-45 | H-60 | H-90 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **Share of final demand already booked** (current regime) | 100% | 92% | 81% | **60%** | 45% | 35% | 20% | 13% | 6% |

Booking lead time, current regime: P10 3d · P25 8d · **P50 18d** · P75 36d · P90 66d [measured].

**This is not just an analysis constraint — it is a defect in the shipped tool.**
`peak_concurrency` ([day_setup.py:100](../../dispatching/day_setup.py#L100)) runs on *booked* legs
with no lead-time correction, so a dispatcher building a day in advance is sized against partial
demand. Measured across 156 days, using a 75-minute occupancy proxy [measured]:

| Built K days ahead | Peak visible then | Final peak | Mean deficit | Days under-sized |
|---|---:|---:|---:|---:|
| 7 | 16.1 | 19.2 | 3.1 | **91.7%** |
| 14 | 12.9 | 19.2 | 6.3 | **100%** |
| 21 | 10.5 | 19.2 | 8.7 | **100%** |
| 28 | 8.8 | 19.2 | 10.4 | **100%** |

The further ahead a day is built, the more structurally under-sized the roster is. Nothing in the
codebase models this. It belongs in the Phase 2 spec.

## A2. The analysis window — derived, not chosen

The brief asks for "the most recent 6–8 months". The business was not one business for that
period. Binary-segmentation changepoint detection on the raw daily leg series
[measured, `_common.changepoints()`]:

| Segment | Days | Legs/day |
|---|---:|---:|
| 2025-10-02 .. 2025-11-05 | 35 | 24.4 |
| 2025-11-06 .. 2025-12-25 | 50 | 34.6 |
| 2025-12-26 .. 2026-02-05 | 42 | 48.0 |
| 2026-02-06 .. 2026-03-18 | 41 | 76.3 |
| **2026-03-19 .. 2026-07-23** | **127** | **90.4** |
| **2026-07-24 .. 2026-08-21** | **29** | **108.4** |

**The late-July step is robust.** Across 12 combinations of `min_seg` (21/28/35) and `min_effect`
(0.06/0.08/0.10/0.15) the boundary lands between 2026-07-17 and 2026-07-31 and the ratio between
1.16 and 1.29 [measured]. Every day of the week grew: Mon ×1.42, Tue ×1.46, Wed ×1.23, Thu ×1.23,
Fri ×1.17, Sat ×1.26, Sun ×1.19 — weekday growth outpacing weekend, i.e. the business is filling
in its weak days.

**Falsification tests — the step is genuine demand, not an artefact** [measured]:

- **Not more legs per booking.** Legs per reservation is flat: 1.54 (Jun) → 1.50 (Jul) → 1.48 (Aug).
- **Not one customer or agency.** Growth is spread across booking sources — direct 42.8% of the
  increase, google_ads 19.6%, bing_ads 17.9%, meta_ads 17.8%. Travel-agent volume *fell*; the
  growth is retail. No single customer contributes more than 1.8%.
- **Corroborated by independent streams.** Tap volume, auditlog volume and distinct working
  drivers all move with it.

**The two limits that more analysis cannot fix** [unavailable]:

1. **Season and growth are perfectly confounded.** Leg data starts 2025-04-26 at ~2 legs/day, so
   there is no prior-year August to compare against. Whether the step is summer seasonality or
   continued growth **cannot be determined from this data**. Templates cut on this window are
   *summer* templates. This must be stated wherever a level claim is made.
2. **The current regime is 29 days — about four of each weekday.** That is enough for a level
   claim and far too thin for a P90 claim on a `dow × hour` cell.

### A2.1 Windows by purpose

| Purpose | Window | Days | Basis |
|---|---|---:|---|
| **LEVEL** — staffing, capacity, required drivers | **2026-07-24 .. 2026-08-21** | 29 | The only regime that describes today. A level cut from the prior plateau understates by ~20%. |
| **SHAPE** — `dow × hour`, class mix | **2026-07-24 .. 2026-08-21**, *not pooled* | 29 | See the stationarity result below — pooling is **not licensed**. |
| **ACTUALS** — durations, dwell, turnaround | **2026-02-08 .. 2026-08-20** | 194 | Floored by the first `legstatus` row, ceilinged by `last_actuals_day`. |
| **REPLAY** | **2026-03-01 .. 2026-08-20** | 173 | Where DVA coverage, tap coverage and assignment are all high enough (§A4). |
| **REJECTED** | anything before 2026-02 | | A materially smaller company; cancellation flag not in use before 2026-01 (§A6, A9). |

**Shape is NOT stationary across the regime boundary — this corrects the superseded document.**
It claimed low total-variation distances licensed pooling a long window for shape. Against a
proper permutation null [measured, `00_horizon_and_window.py`]:

| Distribution | Cells | TVD | Null P95 | Permutation *p* | Verdict |
|---|---:|---:|---:|---:|---|
| day-of-week | 7 | 0.0343 | 0.0331 | 0.037 | borderline |
| **hour** | 24 | 0.0610 | 0.0489 | **0.001** | **NOT stationary** |
| **dow × hour** | 166 | 0.1189 | 0.1124 | **0.005** | **NOT stationary** |
| trip kind | 3 | 0.0207 | 0.0238 | 0.095 | stationary |
| lane bucket | 32 | 0.0472 | 0.0425 | 0.015 | borderline |

A raw TVD looks small until you compare it to what resampling produces by chance. **Hourly shape
and `dow × hour` shape changed with the regime.** Only trip-kind mix may be pooled. This is a real
constraint: the shift architecture cannot borrow hourly shape from the prior plateau to thicken a
thin sample, and must say so.

## A3. The occupancy anchor — the correction this redo exists to make

*This section supersedes the superseded document's §C3 "blocking test" and reinterprets
`docs/operational-data-audit.md` §10.3 and §11.*

### A3.1 What was got wrong

The previous document observed that on MCO/SFB-origin legs the booked `pickup_time` precedes the
`picked-up` tap by ~35 minutes, and concluded that booked `pickup_time` is unreliable for demand
and concurrency modelling. **That conclusion was wrong**, and it misread the prior audit, which
had already got this right — `docs/operational-data-audit.md` §10.5 says plainly that the
airport-origin rows are *"not a lateness problem — it is a labelling problem … the 'pickup time'
is effectively the flight's arrival slot, not the moment the guest reaches the car."*

### A3.2 The convention, proved mechanically

For airport arrivals `pickup_time` **is** the flight's arrival time. On 7,749 arrival legs carrying
a flight row, treating the flight table's `*_local` columns as UTC (they are UTC despite the name —
`operational-data-audit.md` §1.3, re-confirmed here) [measured]:

| Flight column | n | Median difference from booked `pickup_time` | Within ±1 min |
|---|---:|---:|---:|
| **`actual_gate_arrival_local`** | 6,991 | **0.0 min** | **74.5%** |
| `estimated_gate_arrival_local` | 7,474 | 0.0 min | 31.1% |
| `scheduled_gate_arrival_local` | 7,476 | −3.0 min | 14.7% |

Roughly two-thirds of arrival legs match the **actual** gate arrival exactly as first match. This
is not a correlation; it is a mechanism.

**Verified a second way, without touching the flight table at all:** of the **13,772** `pickup_time`
edits recorded in `reservations_auditlog`, **100% carry a flight-match reason string** [measured,
`06_challenge.py`]. The convention is visible in the edit log alone. Nothing edits an arrival's
pickup time except the flight tracker.

### A3.3 The full ladder

Minutes from booked `pickup_time` to each tap, live, all drivers [measured]:

| Kind | Tap | n | P25 | P50 | P75 | P90 |
|---|---|---:|---:|---:|---:|---:|
| ARRIVAL | `on-the-way` | 6,275 | −46.3 | **−17.5** | +4.7 | +32.7 |
| ARRIVAL | `on-location` | 6,447 | −2.6 | +11.8 | +28.5 | +58.0 |
| ARRIVAL | `picked-up` | 6,660 | +30.1 | **+39.8** | +60.1 | +85.7 |
| ARRIVAL | `completed` | 7,264 | +65.2 | +77.0 | +93.4 | +118.3 |
| DEPARTURE | `on-the-way` | 5,817 | −55.2 | **−36.2** | −20.9 | −2.4 |
| DEPARTURE | `on-location` | 5,749 | −20.3 | −9.8 | −1.1 | +13.1 |
| DEPARTURE | `picked-up` | 5,982 | −4.1 | **+2.6** | +14.9 | +37.5 |
| DEPARTURE | `completed` | 6,677 | +27.3 | +35.0 | +44.3 | +58.7 |

Three things follow, and the third is the one that matters:

1. **On departures the booked time IS the passenger event** — `picked-up` at P50 +2.6 min. On that
   half of the board the booked time is accurate to the minute.
2. **On arrivals the booked time is the flight time**, and the guest boards ~40 min later. That is
   dwell. The prior audit's independent measurement of true dwell (gate docked → guest in car,
   P50 +37) agrees.
3. **On BOTH, `on-the-way` precedes the booked time.** Using `pickup_time` as the start of
   occupancy therefore starts the interval **too late** — by ~18 min on arrivals and ~36 min on
   departures. This is the opposite of the superseded document's framing.

### A3.4 Is `on-the-way` a real event, or bookkeeping?

A driver closing job *N* and opening job *N+1* in the same moment would make `on-the-way`
meaningless. Tested by splitting on a driver's **first leg of the day**, where the tap is
unambiguous [measured]:

| Kind | Position | n | P50 lead |
|---|---|---:|---:|
| ARRIVAL | first of day, clean cohort | 331 | −19.2 |
| ARRIVAL | later legs, clean cohort | 3,876 | −16.4 |
| DEPARTURE | first of day, clean cohort | 1,212 | −35.9 |
| DEPARTURE | later legs, clean cohort | 3,587 | −37.3 |

The two differ by **1.4–2.7 minutes**. `on-the-way` behaves the same when it cannot be contaminated
as when it can, so **as a percentile over many legs it is a valid occupancy anchor.**

**But it must not be used as a per-leg departure instant.** `completed(N) → on-the-way(N+1)` has
P50 8.5 min, and the distribution is **bimodal, not centred**: **30.9% of gaps land inside one
minute** (the driver closes A and opens B in the same tap), 41.4% inside eight minutes, and
**8.3% are negative** — the next job opened before the last one closed [measured].

The bookkeeping mass sits at zero, which *shortens* modelled approach time. For an aggregate
staffing buffer that is the conservative direction and the anchor holds. **For any per-leg risk
score it is the wrong direction, and `on-the-way` should not be trusted at leg granularity.**
Phase 2 must respect that distinction.

### A3.5 The recommended occupancy interval

Fitted on legs carrying **both** taps, so lead and tail come from the same legs [measured]:

| Kind | n | lead P50 | lead P75 | tail P50 | tail P75 | duration P50 | consistency check |
|---|---:|---:|---:|---:|---:|---:|---:|
| ARRIVAL | 5,740 | 20.6 | 48.1 | 75.5 | 90.2 | 99.7 | −3.5 |
| DEPARTURE | 5,656 | 36.3 | 55.0 | 34.8 | 43.8 | 71.2 | −0.1 |
| OTHER | 958 | 39.8 | 62.0 | 53.6 | 76.5 | 94.3 | −0.9 |

The consistency check is `(lead P50 + tail P50) − duration P50`. It is ≈ 0, so the construction is
sound. **Fitting lead and tail on different leg subsets inflates the interval — a trap we hit and
corrected.**

**Recommendation** [modeled]: occupancy interval =
`[pickup_time − lead(kind), pickup_time + tail(kind)]`, with the **P50** parameters for staffing
and the P75 pair reserved for feasibility gates. Rationale for P50 rather than the brief's usual
P75: this interval is summed across many concurrent legs, and a P75 interval applied to *every*
leg simultaneously compounds into a peak nobody has ever had to cover (§B2). Conservative
percentiles belong on a single-leg feasibility decision, not on an aggregate.

### A3.6 What this does and does not change

Three-way comparison over **155 days**, 2026-03-19 .. 2026-08-20 [measured / modeled]:

| Measure | Mean peak |
|---|---:|
| **INCUMBENT** — `day_setup.peak_concurrency`, `[pickup_time, estimate_job_end_time]` | 16.7 |
| **CORRECTED P50**, all legs | 19.4 |
| **REALISED** from taps, legs with taps only | 15.3 |
| corrected, restricted to legs in-house actually ran | **13.6** |
| *in-house drivers who actually worked* | *13.4* |

Compared **like-for-like on the same leg set**, the corrected model gives 17.1 against a realised
16.0 — a difference of about one leg. The larger all-legs gap is the tap-coverage difference, not
a modelling gain.

> A hypothesis that the residual was caused by a deterministic model synchronising overlaps was
> **tested and refuted**: re-injecting the measured per-leg jitter *raised* the peak to 17.75
> rather than lowering it toward 16.0.

**Conclusion: the interval definition is a second-order issue, worth ±1–3 legs. The first-order
error in the incumbent staffing number is the denominator, not the interval** (§B2). The corrected
interval is still the right definition — it is defensible, it is measured, and it removes the need
for the `+1` fudge — but it is not where the incumbent's failure comes from.

### A3.7 Reinterpreting the prior audit's punctuality tables

`operational-data-audit.md` §10.3 reports airport-origin lanes at "96% more than 15 minutes late".
Under the corrected reading **those rows are not lateness at all** — they measure dwell against a
flight-time anchor. The audit itself says so at §10.5 for the port lane; the same reading applies
to every MCO/SFB-origin row in §10.3.

- **Survives:** every non-airport-origin row (Disney → MCO median +0, Universal → MCO −0, port
  departures). Those are genuine punctuality.
- **Must be re-labelled:** all airport-origin rows. `MCO → Disney` at "+37 median, 96% late" is
  dwell of +37 min, which is *normal and expected*.
- **§11's "median `scheduled pickup − scheduled gate arrival` = −1 minute; there is effectively no
  deliberate buffer"** is confirmation of an intentional convention, not a criticism.

**Do not compute an on-time metric for airport arrivals against `pickup_time`.** It will mark
~96% of them late, and it will be wrong every time.

### A3.8 The cost: arrival `pickup_time` is a moving field

Because it tracks the flight, arrival `pickup_time` is rewritten as the flight updates. From
`reservations_historicalleg` [measured]:

| Kind | n | Never changed | P10 | P50 | P90 | Moved > 15 min |
|---|---:|---:|---:|---:|---:|---:|
| **ARRIVAL** | 7,382 | **11.3%** | −36.0 | −6.0 | +51.0 | **49.7%** |
| DEPARTURE | 6,452 | 78.7% | −30.0 | 0.0 | 0.0 | 18.2% |

44.8% of arrival legs take four or more distinct `pickup_time` values.

**Measured a second way, against the higher-fidelity `auditlog` trail inside the current regime,
it is worse still** [measured]: **97.5% of ARRIVAL legs are retimed**, about **7.6 times each**, at
roughly **392 retime events per day**. Retime size |Δ| P50 6 min / P75 13 / P90 25, with 7.3%
moving 30 minutes or more — and **83.2% of retimes happen inside the service day itself.**

**Three consequences.**

- **For sizing, this is fine and arguably correct** — the historical value is the realised arrival,
  which is when capacity was really consumed.
- **For validating a planning tool, it is look-ahead leakage.** A replay that reads today's stored
  `pickup_time` credits the planner with knowledge it did not have. `historicalleg` makes the
  plan-time value recoverable for legs after 2026-03-03, and **any Phase 2 replay must use it**.
- **And the peak's clock time is soft on half the board.** On a future date, essentially every
  arrival leg will be retimed before it runs, most of it *on the day*. Any UI that states a peak
  time to the minute is overstating its precision, and a Day Setup panel built the night before is
  reading a demand curve whose arrival half has not settled yet.

**`reservations_historicalleg` is the only way to recover what `pickup_time` was at a past
instant**, and it starts 2026-03-03. That is the hard floor on any faithful replay.

## A4. Source-by-source reliability

| Source | Grade | Trustworthy from | Notes |
|---|---|---|---|
| Leg demand (`pickup_date` / `pickup_time`) | **A** | 2025-04-26 | 99.9% populated. But see §A3 — on arrivals this is the flight time, and it moves. |
| Driver status taps (`legstatus`) | **A−** | 2026-02-08 | Full-ladder coverage improved to **88.0% overall / 91.0% in-house** (Aug) from 73.2%/80.3% (Feb) [measured]. Affiliates lag badly (§A4.1). |
| `reservations_auditlog` | **A** | 2026-01-10 | 260,125 rows. Full assignment and status churn history. **New.** |
| `reservations_historicalleg` | **A** | 2026-03-03 | 208,508 rows, field-level Leg history. Recovers plan-time values. **New.** |
| Farm-out identification | **A** | 2026-02-08 | `driver.driver_type = 'affiliate'`; agrees with the payment ledger. |
| Driver pay | **A** | 2025 | The cleanest data in the file. |
| Leg revenue | **C** | — | `leg_base_price` is a dead column; use `reservation.total_price ÷ leg count` and never as a load-bearing term. |
| Roster — who *worked* | **A** | 2025-10-01 | ≥97% of legs carry a `driver_id`. |
| Roster — who was *rostered* (DVA) | **C** | 2026-01-18 | 2,591 rows. High agreement with reality, but see A9 — it is a near-mirror of the assignment table, so the agreement is largely circular. |
| ***When*** anyone was on duty | **F** [unavailable] | never | `planned_start_hour` / `planned_end_hour` populated on **0 of 2,591** DVA rows [measured]. **Capacity can only be modelled as bodies-per-day, never as shift windows.** |
| Declared weekly availability | **F** — decoration | never | 252 rows; **77.0% carry the seeded default window 6–23**; `preference` blank on **100%** of rows (the field is inert); `preferred_shift` blank on 95%; undated, so it cannot describe a past date [measured]. |
| **Time-off overrides** | **B** | 2026-04-24 | **The only forward availability signal in the database, and it is respected.** 131 rows, 124 approved; of 197 past approved day-offs a driver drove anyway on **8 (4.1%)**. 104 forward-dated day-offs currently stand [measured]. |
| Fleet size over time | **F** [unavailable] | never | `in_service_since` NULL on **all 18** rows. Current fleet: 18 rows, **17 active** (suv 8, Van(14 Pax) 5, mini_van 2, towncar 2 incl. 1 inactive, van 1) — up from 13 in the previous snapshot, corroborating the handoff note. Lower bound from DVA first-appearance: 8 units by 2026-01, 13 by 03, 18 by 2026-08 [inferred]. |
| Flight anchors | **A−** | 2026-04 | Arrival legs resolve a flight at high rates; `actual_gate_arrival_local` present on ~90%. |
| `reservations_driverlocation` | **C, then F** | 2026-03 .. 2026-06 | 145,756 rows, but **real coordinates stop in July**: 0 of 2,537 rows in Jul and 0 of 2,284 in Aug carry non-zero lat/lng [measured]. See §A4.2 — this is a live regression worth reporting to the team. |
| `reservations_schedulesnapshot` | **B** | 2026-02-10 | 240 snapshots / 14,431 entries — a real record of board state over time. **New.** |
| `reservations_legkeoi` | **C** | 2026-07 | 63 hand-written dispatcher risk flags. Small but the highest-signal qualitative source in the system (§A4.3). |

### A4.1 Tap discipline — the record is honest, the humans are not

**The event log itself is trustworthy.** 99.67% of 88,469 ladder rows carry a driver account as
author. `auditlog` and `legstatus` agree on **99.95% of 77,498 shared events to within 5 seconds**
(|Δ| P50 0.02 s, P90 0.07 s); `legstatus` holds zero events `auditlog` lacks. A third writer,
`historicalleg`, reproduces ride-time percentiles to 0.2 min. **Every reliability problem below is
a human one, not a recording one** [measured].

- **Presence improved; usability did not.** In-house full-ladder presence runs 80.3% (Feb) →
  92.8% (Jun) → 91.0% (Aug); all arms 73.2% → 88.0%. But the application's *own* validity gate,
  `has_valid_status_chain` ([analytics.py:493](../../dispatching/analytics.py#L493)), passes only
  **57.2–67.0% and is FLAT across the whole window with no trend** [measured]. About a third of
  legs that *look* complete are rejected by production's own code. **That gate, not tap presence,
  is the real ceiling on how many legs can enter a timing pipeline.**
- **19.5% of the operational record is fabricated, and it is concentration, not spread**
  [measured]. Fleet-wide instant-completion rose 5.9% (Feb) → 29.0% (Jun) → 24.7% (Aug). Split
  apart: the fabricating cohort went 40.7% → 72.2% while *everyone else held a flat 5.2–11.8%*.
  What changed is the cohort's **share of the work** — their legs went from 27/month to 558/month.
  Ten drivers now carry 3,416 of 17,552 era legs. Impossible ride times are therefore **not
  improving; they are getting worse**, and this is a staffing and coaching fact, not a clock fact.
- **The `exclude_from_timing` remediation did not hold — this refutes the superseded document's
  "catches all 9, zero false negatives".** Production flags 8 drivers; a behavioural rule finds 10;
  **only one name overlaps** [measured]. Nine fabricators are **unflagged**, including the two
  largest bad sources in the system — `ken` (726 legs, 93.6% instant-complete) and `anthony`
  (700 legs, 92.2% instant). Five flagged drivers do **not** fabricate, costing ~1,470 honest
  full-ladder legs; the worst is `davide` (894 legs, 97.9% full ladder, 12.5% instant) — a driver
  the prior audit itself nominated as *gold*.
- **The duplicate-tap cause was misattributed.** Duplicates did rise (legs carrying one: 18.9% Feb
  → 46.6% Jul), but **not because unassignment rose** — `auditlog` `driver_unassigned` events are
  flat-to-*falling* (1,240/month Feb → 762/month Aug) [measured]. What happened is that the system
  **started writing** an auto-reset row on **2026-04-28**
  ([reservations/models.py:1891-1900](../../reservations/models.py#L1891)): `Leg.save()` writes a
  `LegStatus` row with `status='in-progress'`, wiping the leg's progression so the next driver
  re-runs the whole ladder. **The duplicate rate tracked a code deploy, not dispatcher behaviour.**
  A leg with an auto-reset carries a duplicated status 96.4% of the time versus 20.0% without.
- **The `.first()` ordering bug is FIXED**, correcting the superseded document. Every production
  reader that wants an earliest tap now gets one (`analytics.py:441/493`,
  `conflict_advisor.py:441/515`, `driver_data_quality.py:113`). Still use `MIN(timestamp)` per
  `(leg, status)` in new analysis — but this is no longer a live defect.

### A4.1b The gold cohort, re-vetted — and the trap it sets

Gates declared then applied (≥25 legs, full-ladder ≥85%, instant ≤10%, collapsed ≤5%,
non-monotonic ≤2%). Of the prior audit's 14 nominees **8 survive**; 6 drop (`sereen` 16.0% instant,
`angel` 12.8%, `runer` 12.2%, `davide` 12.5%, `george` 11.8%, `oualid` on full-ladder 68.1%). The
data **adds 4 never nominated** (`HassanA`, `carlos`, `julio`, `shipo`). Final in-house gold =
**12 drivers, 5,107 legs = 36.6% of in-house era legs** [measured].

**Three affiliates pass every gate** — `hany` (232 legs, 99.6% full ladder, 3.9% instant),
`martin`, `wael` — yet production analytics filters to `driver_type='inhouse'`, so their clean data
can never reach `RouteTimingMetric` however good it is. **That is a code decision, not a flag.**

**The generalisation gap is wider than previously published, and it sets a trap.** True dwell
(gate actual → picked-up): gold P50 37.2 / P75 **45.2** / P90 58.0 versus all-driver P50 39.2 /
P75 **53.8** / P90 **77.7** [measured] — the fleet is **+8.6 at P75 and +19.7 at P90**. The
superseded document said +8/+13; the live P90 gap is half again as large.

> `STATIC_FLOOR_DWELL_MIN = 45` measures **45.2 at P75 on the gold cohort** — i.e. it looks
> perfectly calibrated to exactly the cohort a naive analysis would choose, and is 9 minutes tight
> on the fleet. **That is the trap, in one line.** Use the all-driver row for anything that must
> hold fleet-wide.

### A4.2 The GPS archive is closed — deliberately, not by decay

`reservations_driverlocation` holds 145,756 rows, of which **122,807 (84.3%) carry real
coordinates — and every one falls in 2026-03-06 .. 2026-06-11**. From 2026-06-12 onward, 100% of
rows are `latitude = 0, longitude = 0` [measured].

**This was not a silent regression.** Commit `94c88ba7` ("Driver portal v2") removed the phone-GPS
capture block on 2026-06-11, and the comment at `drivers/views.py:222` states it outright. The
table still writes rows to within an hour of the pull, but they carry only status and ETA.

Two consequences: the coordinate archive **predates the current regime entirely** (it closes 43
days before the regime opens), and **deadhead and repositioning are permanently [unavailable]**
unless capture is restored. A trap for anyone reopening it: rows are **not** one-per-status —
13,574 `(leg, status)` groups hold 122,807 fixes, mean 9.0 and max 508, so any per-leg statistic
computed by counting rows is wrong by an order of magnitude.

### A4.3 There is no base — in the code *or* in the geography

The scheduler says so itself ([scheduler.py:133-139](../../dispatching/scheduler.py#L133)): a
geography-aware split *"needs a base-location concept the engine doesn't have yet."* Confirmed in
code — no base address, no depot `Location` row, no per-vehicle home yard.

**The GPS archive was tested for a *behavioural* base and does not support one** [measured]:

| Test | Result |
|---|---|
| Share of driver-day starts in the busiest ~0.55 km cell | **13.0%** of 683 qualifying starts |
| Days a driver starts at his **own** modal point | median **46.5%** |
| Median distance between two drivers' modal start points | **7.5 km** |
| Drivers starting in the top cell on half their days or more | **1 of 20** |

> An early read of this data by the lead — "15 distinct drivers share the top cell, therefore
> there is a yard" — **was wrong**, and the fuller analysis overturned it. Fifteen drivers touching
> a cell at some point is not the same as fifteen drivers *based* there. The signature here is a
> **home-kept fleet with per-driver origins**, not a depot.

**This confirms rather than rescues the previous document's conclusion.** The brief's handoff chain
(clear → wash/fuel → base → possession → reposition → pickup) **cannot be validated**, because
there is no base to return to and wash/fuel has no event, field or table anywhere [unavailable].

**Recommendation:** model the whole outgoing-to-incoming gap as **one tunable constant**, tuned
against the measured same-driver reposition and observed vehicle-handoff distributions, and state
plainly that its components are unobservable. Do not decompose it — that buys false precision.
A home-kept fleet also means "return to base" is the wrong mental model entirely for Phase 2: a
handoff is a **driver-to-driver meet**, not a yard visit.

### A4.4 What dispatchers actually worry about

The 63 KEOI flags are free-text and hand-written, and they are the closest thing to ground truth
about what a scheduling failure feels like [measured]:

| Theme | Flags | Share |
|---|---:|---:|
| Flight timing is the risk | 42 | 70.0% |
| Chained conflict — previous job may run late | 26 | 43.3% |
| An explicit swap plan is named | 19 | 31.7% |
| Guest/passenger readiness | 15 | 25.0% |
| **"This is NOT a conflict"** — annotating a false positive | 6 | **10.0%** |
| Wants to avoid a farm-out | 3 | 5.0% |

Two things to carry into Phase 2. Dispatchers reason in **chains**, not in day totals — 43% of
flags are about a *previous* job threatening the next one, which no staffing-level number
addresses. And **one flag in ten exists to tell the system it is wrong**. A new signal that fires
often and cannot be dismissed will be resented.

KEOI is usable as a **labelled test set of worked examples** and as a source of failure *modes* —
never as training data, and never as a base rate: it records what a dispatcher noticed, and the
denominator (near-misses nobody flagged) does not exist [unavailable].

### A4.5 A prediction log exists — but not the planner's

The prior audit's §12.6 called it the single biggest structural gap: *nothing records what the
scheduler predicted, so estimate accuracy can never be tracked.* **That is now partly wrong, and
the part that survives is the part that matters most.**

`reservations_historicalleg` carries **15,560 individually timestamped `dispatch_eta` predictions
across 4,774 legs**, from 2026-06-08 to within hours of the pull, and they can be scored against
realised taps for the first time [measured]:

| Prediction | n | \|error\| P75 | \|error\| P90 |
|---|---:|---:|---:|
| en route → pickup (final call) | 2,948 | 5.7 min | 8.7 min |
| on trip → dropoff (final call) | 3,283 | 5.5 min | 7.7 min |

Short calls are excellent (ETA 0–5 min: 97.5% within 5 min); **long calls degrade badly** (ETA
16–30 min: |error| P90 **76.9** min).

**Three qualifications, and they are load-bearing:**

1. **This is `samsara_risk`'s live, same-day, GPS-driven ETA — not the planner's.** Nothing
   persists auto-assign's estimate of when a driver would finish job A and reach job B. **§12.6
   stands for the planner** [unavailable].
2. **The log is incidental, not designed.** `sweep_eta` persists via
   `Leg.objects.bulk_update()` ([samsara_scheduler.py:282](../../dispatching/samsara_scheduler.py#L282)),
   which fires no `post_save`, so simple-history never records the sweep. Every ETA value in
   `historicalleg` was copied onto a history row by an *unrelated* `.save()`. Proven two ways —
   the `history_date` offset distribution matches `INTERVAL_SECONDS = 3*60` exactly, and 90.2% of
   ETA-bearing history rows sit within 2 seconds of a tap on the same leg. **A feature that
   depends on this log is depending on an accident**, and Phase 2 must not assume it will persist.
3. It begins 2026-06-08 — inside the prior plateau, covering only part of the window.

**The shipped risk band is correctly ordered but over-warns badly** [measured]: `at_risk` is a
false alarm **66%** of the time (n=338); `watch` is a false alarm **97%** of the time (n=987);
`late` is "right" 100% of the time only because it is not a prediction —
`samsara_risk.py:297-300` sets it once the deadline has already passed.

> This is independently corroborated by the dispatchers: 6 of 60 KEOI flags exist purely to
> annotate that a detected conflict is **not** one. **A new Day Setup signal that over-warns at
> these rates will be ignored within a week.** Calibration is a first-class requirement for
> Phase 2, not a polish item.

The first empirical **turnaround** number the engagement has also falls out of this: where the
chained next-pickup estimate binds (0–15 min slack, n=360) the driver misses the deadline **28.6%**
of the time; where the chain says he cannot make it at all (negative slack, n=443) he misses
**71.8%**. But the relationship is **not monotone** — the 60+ min slack bucket misses about as
often (36.0%) as the binding one, so slack alone does not predict misses and something else is
driving the tail.

## A5. Assumptions register

| # | Assumption | Basis | If wrong |
|---|---|---|---|
| **A1** | The occupancy interval is `[pickup_time − lead(kind), pickup_time + tail(kind)]`. | §A3.5, fitted on paired taps, consistency check ≈ 0, validated against realised concurrency to ~1 leg. | Moderate. The peak moves by ±1–3. It does **not** change the §B2 conclusion, which is about scope, not interval. |
| **A2** | The database is live and complete to 2026-08-21 22:17 UTC. | Nine agreeing streams, no holes, clean microsecond fingerprint. | Nothing — this is as well established as anything in the file. |
| **A3** | `driver_type = 'affiliate'` identifies a farm-out. | **The retro-relabel risk the previous document called unresolvable is now RESOLVED.** `django_admin_log` — a real audit trail nobody knew existed — dates **25 "Driver type" edits across 19 drivers**; only two land inside the window, bounding the contamination at **0.21% of assigned legs (30 of 14,617)** [measured]. Independently, a price-only classifier (`base_pay ≥ 2.25 × route inhouse_base_pay`) agrees with the flag on **99.37%** of 14,384 scoreable legs, balanced accuracy 0.987. | Negligible. This assumption is now the best-supported one in the register. |
| **A4** | `is_active` / `driver_type` / `exclude_from_timing` are current-state flags applied retroactively. | No history table for any of them. | Material and one-directional. **Never filter a replay on these flags** — doing so erases drivers who really worked. |
| **A5** | The gold cohort does not generalise to the fleet, and the gap is measured. | True dwell P75 45.2 (gold) vs **53.8** (all); P90 58.0 vs **77.7** [measured]. | A buffer fitted to gold and shipped fleet-wide is **~9 min tight at P75 and ~20 at P90**, in exactly the tail where it fails. `STATIC_FLOOR_DWELL_MIN = 45` reads as perfectly calibrated on gold and is 9 min tight on the fleet. **Use the all-driver row for anything fleet-wide.** |
| **A12** | Cohort choice is immaterial at the median and decisive in the tail. | The gold-vs-fleet dwell gap is +1.2 min at P50 but +6.2 at P75 and +9.8 at P90 [measured]. | The prior audit's §9.4 recommendation ("medians agree, so cohort adds little") is right for the wrong reason. Any figure that becomes a **buffer** must state its cohort; any figure quoted at the median need not. |
| **A13** | Turnaround percentiles are meaningless without their pairing cap. | The out-of-order P90 shift is +0.0 min at a 120-min cap, +2.2 at 180, +4.6 at 240, +7.9 at 480 [measured]. | **Any turnaround figure quoted without its cap is not reproducible.** Neither prior document stated one. This document uses an 8-hour cap throughout. |
| **A6** | Demand *shape* is **not** poolable across the regime boundary. | Permutation test: hour *p*=0.001, `dow × hour` *p*=0.005 (§A2.1). | This is the conservative direction. If shape were stationary we would merely have a larger sample. |
| **A7** | Season and growth cannot be separated. | No prior-year August exists. | **[unavailable].** Every level claim is a summer claim. If the step is seasonal, a template sized on it over-staffs the autumn. |
| **A8** | Idle-vehicle carrying cost must be founder-supplied. | No acquisition cost, lease, insurance premium or maintenance spend anywhere in the schema. | Must be carried as an explicit **[assumed]** parameter with a sensitivity range, and **never blended into a figure that also contains [measured] farm-out dollars.** |
| **A9** | DVA is a weak roster record. | Over the derived 215-day contiguous DVA era: precision 99.5%, recall 95.5% against in-house drivers. But **only 13 driver-days (0.5%) are in DVA and did not drive** — that is the entire incremental information the table adds beyond `leg.driver_id` [measured]. The agreement is circularity, not corroboration. | It is an **A for "which car"**, a **C for "who was rostered"**, an **F for "who was available"**. DVA holds exactly **one affiliate driver ever**, so it describes only the in-house arm. |
| **A10** | Affiliate driver rows are **companies, not people**. | Two independent tests. From taps: one affiliate row reached **10** simultaneous in-flight legs against a maximum of 3 in-house. Model-free, from **booked times only**: **32.1% of affiliate driver-days carry two booked pickups ≤15 min apart, against 3.1% in-house** [measured]. | Load-bearing. Counting affiliate rows as bodies understates farm-out capacity several-fold and breaks any headcount comparison. The booked-time test cannot be explained away by tap latency. |
| **A11** | Historical arrival `pickup_time` is the *realised* flight arrival, not the planned one. | §A3.8; 88.7% of arrival legs were edited. | A replay reading it uncorrected credits the planner with foresight. Use `historicalleg` to recover plan-time values. |

## A6. Non-negotiable filters

```sql
WHERE (l.status IS NULL OR l.status <> 'cancelled')
  AND r.status NOT IN ('cancelled', 'canceled')     -- BOTH spellings exist
  AND l.pickup_date BETWEEN '2025-01-01' AND '2027-12-31'   -- never MIN()/MAX()
```

- **`'canceled'` (one L) is real.** [day_setup.py:122-123](../../dispatching/day_setup.py#L122)
  excludes only the two-L spelling. A small bug today that will grow silently.
- **Do NOT exclude `status = 'in-progress'`** — it is the Django model default for a new Leg and
  means "not started". Excluding it deletes a quarter of the table.
- **Do NOT filter `exclude_from_analytics`** for demand — it is a timing-quality flag, not a
  demand flag.
- **Never bound a window with `MIN()`/`MAX()` on `pickup_date`.** Junk dates exist.
- Anything reading taps additionally needs `pickup_date <= last_actuals_day`.

---

# PART B — INVENTORY

*The code has not changed since this inventory was first written: `git diff b59ac8f5..HEAD` touches
only `.gitignore` and `scripts/pull_prod_snapshot.py`. Part B is therefore carried forward and
**spot-verified mechanically** rather than re-derived. Every `path:line` below was re-checked with
`grep -n`.*

## B1. This is the fourth demand-vs-staffing signal, not the first

| Question | Answered today at | The number |
|---|---|---|
| How many bodies does the day need? | [day_setup.py:443](../../dispatching/day_setup.py#L443) | `peak_concurrency(date)["overall"][0] + DAY_SETUP_PEAK_BUFFER` — peak concurrent in-flight legs, **+1** |
| How many cars must go out? | [day_setup.py:199](../../dispatching/day_setup.py#L199) `parkable_units` | `len(must_run)` — Hall's condition over per-tier cumulative peak |
| Are we short? | [day_setup.py:945](../../dispatching/day_setup.py#L945) | `final_checked < len(must_run)` → *"The busiest moment needs N cars out but only M drivers are ticked…"* |
| Hour by hour? | [day_setup.py:158](../../dispatching/day_setup.py#L158) `concurrency_series` | 30-minute grid, split by vehicle type |
| Does leftover work need another body? | `shift_advisor.py` | residual legs → a named driver and car |
| Can I release a body? | `fold_advisor.py` | whole-day-or-nothing, six-gate simulation |
| Is work spread fairly? | `rebalance_advisor.py` | the founder's relative-balance rule |
| Is the driver day understaffed? | [schedule_risk.py:46](../../dispatching/schedule_risk.py#L46) | a **flat `COVERAGE_TARGET_DEFAULT = 14`**, seven days a week, all year |

`DAY_SETUP_PEAK_BUFFER = 1` at [day_setup.py:59](../../dispatching/day_setup.py#L59) [verified].
The comment at [day_setup.py:941](../../dispatching/day_setup.py#L941) records why the raw peak was
demoted: it *"counts bookings we farm out and **fired on 9 of 23 days**"*.

## B2. The reconciliation — measured

**This is the most important section for Phase 2.** The brief requires that any new staffing number
be reconciled against `peak + 1`, `must_run`, and the three advisors. Here is the measurement.

Over **155 live days** (2026-03-19 .. 2026-08-20), against an active fleet of **17 cars**, a signal
"fires a shortage" when it exceeds the in-house drivers who actually worked
[measured, `05_flights_and_occupancy.py`]:

| Signal | Days it fires |
|---|---:|
| raw peak | 108 of 155 (**70%**) |
| **`peak + 1` — the shipped target** | **128 of 155 (83%)** |
| `must_run` — the shipped **warning** | 101 of 155 (**65%**) |

Mean `(peak + 1) − in-house drivers who worked` = **+4.3 drivers**. The codebase measured the raw
peak firing on 9 of 23 days (39%); on live data it is far worse, because demand grew ~20% while the
comparison stayed structurally wrong.

### B2.1 Why — the denominator, measured

`peak_concurrency` counts **every** leg, including the ~19% that are farmed out. It therefore
measures **total demand concurrency** and is being compared against **in-house headcount** — a
total against a part.

Restricting the *same* computation to legs an in-house driver actually ran, over the same 155 days
[measured]:

| Peak measure | Mean | Exceeds in-house drivers who worked |
|---|---:|---:|
| corrected interval, **all legs** | 19.4 | 139 of 155 (**90%**) |
| corrected interval, **in-house legs only** | **13.6** | 70 of 155 (45%) |
| *in-house drivers who actually worked* | *13.4* | — |

Mean gap between the two peaks = **+5.8 legs**. **The in-house-restricted peak tracks the bodies
actually fielded to within 0.2 on average**, while the all-legs peak runs nearly six above it.
**The gap `peak + 1` reports is not a shortage — it is the farm-out volume, restated.**

> **Precision matters here, so state it exactly.** Under the *incumbent* interval, the
> in-house-restricted peak exceeded in-house headcount on **0 of 28** days. Under the *corrected*
> (longer) interval it exceeds on **45%** of 155 days — but by small amounts, and the means differ
> by 0.2. The correct claim is that restricting the denominator **removes the systematic bias**
> (+4.3 drivers), not that it produces a signal that never fires. A residual 45% fire rate at
> ±1 driver is a calibration question for Phase 2, not a structural error.

Compounding this, **affiliate driver rows are companies, not people** (assumption A10): one
affiliate row ran **10** legs simultaneously. So the farmed side of the peak is covered by an
elastic pool that no headcount can represent.

### B2.2 `must_run` has saturated — because the FLEET is now the binding constraint

`must_run` equals the entire 17-car fleet on **78 of 155 days (50%)**, because the raw peak reaches
or exceeds fleet size on the same 50% of days [measured]. On those days the shipped warning
degenerates into *"fewer than 17 drivers are ticked"* and carries no information about the day at
all. **A signal that is pinned to a constant on half of all days is not a signal.**

The reason is the deeper finding: **the fleet, not the roster, is what binds.** Peak simultaneous
in-flight legs in the current regime run P50 18.5 / P75 20.2 / P90 22.6 against **17 active cars**.
**18 of 28 current-regime days (64%) peak above the entire fleet**, up from 38 of 127 days (30%) in
the prior plateau [measured].

**Farming the peak is arithmetic, not preference.** No shift structure can cover a moment that
needs 22 cars with a fleet of 17. This bounds what the whole engagement can claim: a restructure
can move work *within* the fleet's capacity, but the top of the peak is only reachable by adding
vehicles — which is precisely the buy-vs-hire-vs-farm question, and the engine for it does not
exist (§C2). It also means `FARMOUT_RECAPTURE.md`'s +1/+2/+3 increments must be **driver *and*
vehicle pairs**, never drivers alone.

### B2.3 What the new number should be

**Recommendation: the new number ANNOTATES `peak + 1`; it does not silently replace it** —
but `peak + 1` must stop being presented as a required-driver count.

| Quantity | Recommended treatment |
|---|---|
| **In-house required drivers** | The corrected in-house-only occupancy peak. Tracks reality: mean **13.6 modelled vs 13.4 actually worked** across 155 days [measured]. Needs a calibration pass (§B2.1) before it ships. |
| **Total demand concurrency** (today's `peak`) | Keep, but **re-label**. It is a demand measure, not a staffing target. |
| The difference between them | **This is the farm-out exposure** — exactly what the brief wants surfaced, and it falls out of the same computation. |
| `must_run` | Keep for the car question, but note the saturation in B2.2. |
| `DAY_SETUP_PEAK_BUFFER = 1` | A fudge for unmodelled turnaround. The corrected interval models it explicitly; the `+1` should be retired *with* the interval change, not before. |

**The three advisors do not conflict with this**, because none of them produces a required-driver
number — they read the *output* of a build. But the timing hazard identified previously is real
and unresolved: Day Setup runs **pre-build**, all three advisors run **post-build**, so a
pre-build "you need N" can be contradicted seconds later by a zero-residual preview and a Fold-Out
card proposing to *remove* a driver. **Phase 2 must state the precedence rule explicitly.**

## B3. Module inventory

*Carried forward; constants re-verified by `grep -n` against the current tree.*

### Constants — all verified at the cited lines

| Constant | Value | Location |
|---|---:|---|
| `DAY_SETUP_PEAK_BUFFER` | 1 | [day_setup.py:59](../../dispatching/day_setup.py#L59) |
| `ARRIVAL_MEET_GRACE_MIN` | 10 | [pickup_policy.py:46](../../dispatching/pickup_policy.py#L46) |
| `PAX_READY_MIN` | 15 | [pickup_policy.py:52](../../dispatching/pickup_policy.py#L52) |
| `ARRIVAL_DWELL_MIN` | 45 | [pickup_policy.py:63](../../dispatching/pickup_policy.py#L63) |
| `WATCH_SLACK_MIN` | 10 | [pickup_policy.py:66](../../dispatching/pickup_policy.py#L66) |
| `TURN_TIGHT_SLACK_MIN` | 15 | [pickup_policy.py:87](../../dispatching/pickup_policy.py#L87) |
| `DEPLANING_GRACE_MIN` | 10 | [feasibility_guards.py:39](../../dispatching/feasibility_guards.py#L39) |
| `SAFETY_PAD_MIN` | **0** | [feasibility_guards.py:44](../../dispatching/feasibility_guards.py#L44) |
| `MIN_TURN_BUFFER_DEFAULT` | 5 | [feasibility_guards.py:64](../../dispatching/feasibility_guards.py#L64) |
| `SPAN_HARD_HOURS_DEFAULT` | 15.0 | [feasibility_guards.py:97](../../dispatching/feasibility_guards.py#L97) |
| `SPAN_SOFT_EFFECTIVE_HOURS` | 13.5 | [feasibility_guards.py:109](../../dispatching/feasibility_guards.py#L109) |
| `DEFAULT_DRIVE_TIME` | 35 | [scheduler.py:83](../../dispatching/scheduler.py#L83) |
| `VEHICLE_SHARE_PAD_MIN` | 60 | [scheduler.py:138](../../dispatching/scheduler.py#L138) |
| `STATIC_FLOOR_DWELL_MIN` | 45 | [scheduler.py:195](../../dispatching/scheduler.py#L195) |
| `VEHICLE_TIER_ORDER` | `towncar < mini_van < suv < van < Van(14 Pax)` | [scheduler.py:257](../../dispatching/scheduler.py#L257) |
| `FALLBACK_TRIP_DURATION_MINUTES` | 75 | [ops/tasks.py:31](../../ops/tasks.py#L31) |

**Live `SchedulerSettings` singleton: 75 columns / 74 tunable fields** [measured].
`rest_min_gap_minutes = 510`, `rest_penalty_per_hour = 40`, `min_turn_buffer = 5`,
`arrival_grace_minutes = 10`, `reserve_max_scarcity = 2`, `reserve_penalty = −60`,
`idle_gap_threshold = 120`, `cluster_gap_minutes = 120`, `time_scarcity_bonus = 30`,
`span_threshold_hours = 13`, `span_penalty_per_hour = 30`.

### Shipped constants vs measured reality

The prior audit's §11 table, recomputed on live data [measured]. **These are P75/P90 comparisons
against constants that were authored as typical values — the prior audit's own §3 warns that this
manufactures a shortfall, so read the *direction*, not the magnitude, as the finding.**

| Constant | Shipped | Measured P75 | Measured P90 | Verdict |
|---|---:|---:|---:|---|
| `STATIC_FLOOR_DWELL_MIN` / `ARRIVAL_DWELL_MIN` | 45 | 53.8 | 77.7 | Tight: +9 at P75, +33 at P90 |
| same, gold cohort only | 45 | 45.2 | 58.0 | OK at P75, tight at P90 |
| `ARRIVAL_MEET_GRACE_MIN` / `DEPLANING_GRACE_MIN` | 10 | 25.1 | 41.8 | Tight: +15 at P75 |
| `PAX_READY_MIN` (guest-only dwell) | 15 | 47.9 | 69.5 | Tight: +33 at P75 |
| `FALLBACK_TRIP_DURATION_MINUTES` | 75 | 74.6 | 97.6 | OK at P75, tight at P90 |
| second fallback, `ops/views.py:1762` | 60 | 74.6 | 97.6 | Tight: +15 at P75 |
| `SAFETY_PAD_MIN`, `MIN_TURN_BUFFER_DEFAULT` | 0, 5 | — | — | **[unavailable]** — nothing records required turnaround |

The tail has grown since the prior audit: true dwell P75 moved 47 → ~54–58 and P90 64 → ~78–82.
The audit called 45 "close at p75"; **that is no longer true fleet-wide.**

### The three staffing advisors

| | Second-Shift (`shift_advisor.py`) | Fold-Out (`fold_advisor.py`) | Rebalance (`rebalance_advisor.py`) |
|---|---|---|---|
| Demand notion | residual leg count + revenue + span hours | jobs per driver + receiver span | jobs per driver + span + max gap |
| Produces a required-driver number? | **No** | No | No |
| Gate stack | none — defers to Apply | six gates | the same six, **hand-copied** |

All three are **ON in production**, unconditionally, for every `is_staff` user, governed by
module-level Python constants. They run only inside `views.auto_assign_drivers` when a human clicks
Build Schedule. **None reads the day's booking volume, revenue, hourly concurrency or tier mix** —
they read only the output of a build that already happened.

**There is no shared gate helper.** `fold_advisor._simulate` and `rebalance_advisor._gate_receiver`
are two hand-maintained copies. **Extracting one shared `gate_receiver()` should be a prerequisite
of this feature, not a follow-up.**

### `day_setup.py` — what Phase 3 extends

- The **"pure function of (date, DB), no writes" contract** is directly true but transitively
  questionable: `peak_concurrency → estimate_job_end_time → resolve_drive_minutes` can reach a
  `RouteDistanceCache` write path. *Scope correction:* `scheduler.py:91` records the live
  Distance-Matrix lookup as **default OFF since a 2026-05-31 hotfix**, and `route_distance.py:12`
  says the paid call runs only from a management command — so the previous document's "spawns a
  billed Google call" is **overstated**. Phase 2 should still either fix the write path or restate
  the contract honestly.
- **A locked DVA row bypasses the availability hard gate.**
- **Availability is used as one bit.** `end_hour`, `max_hours`, `flexible`, `available_until`,
  `available_after` and `preferred_shift` are all discarded — so a driver available 4–8 p.m. is
  counted against a 09:30 peak they cannot serve.
- **Apply never deletes**; unticking removes from the payload but does not clear the DVA row.
- **Load-bearing UI contracts:** `swaps` strings are regex-parsed, `hint` must keep an `N/M`
  substring, `vehicle_label` must stay `"#NNN <type>"`, and injected DOM must not match
  `.ds-row` / `.ds-check` / `.ds-veh`. **Adding new keys to the payload is safe.**

### Other modules that matter

- **`pickup_policy.turn_band(slack)` returns three values** — `''` / `'tight'` / `'critical'` — not
  the brief's five. **INVALID and CRITICAL both collapse onto `critical`; EXCESSIVE has no
  equivalent.** Phase 2 must extend in place or justify a parallel vocabulary.
- **`required_turnaround`** ([feasibility_guards.py:184](../../dispatching/feasibility_guards.py#L184))
  models a driver-continues turn, **not a vehicle handoff**. No wash/fuel, no base return.
- **`scheduler.py` has no capacity model.** "Cannot be covered in-house" is an emergent residue of
  six hard gates. The engine never says "we are N drivers short".
- **`rest_advisor.py` is ON** — see §C2.
- **`board_validation.turn_slack_minutes()` is *the* slack formula.** Any new diagnostic rendering
  slack must call it.
- **`conflict_advisor.py`'s two-clock policy** is the operational-discipline pattern to copy: a
  detection clock that re-anchors on recorded pickups, a planning clock that is never optimistic,
  and `estimate_job_end_time` **banned** from feasibility.
- **`load_insights.py` carries the threshold doctrine**: each outlier rule needs **both** a
  relative condition and an absolute floor; a purely absolute cutoff *"is the `COVERAGE_TARGET = 14`
  mistake."* The codebase's own verdict on the number this engagement replaces.
- **`samsara_scheduler.py` is the only background loop.** No cron, no Celery — the only place a
  periodic job could live.
- **Fleet Capacity Intelligence's buy/hire/farm decision engine does not exist in code** (§C2).
  `fleet_intel.affiliate_base_cost` / `inhouse_counterfactual_cost` / `recovered_margin` do, and
  are worth reusing.
- **`farmout_optimizer.py`'s `WaterfallLedger`** prevents the marginal-vs-total double count —
  **the pattern any "+1 driver recaptures N legs" number must use**, because per-leg
  recoverability is not additive.

## B4. Where the new feature's config belongs

| Concept | Home |
|---|---|
| Turnaround / handoff feasibility | `fg.required_turnaround()` — **a function, not a number.** Do not mint a parallel knob. |
| Planning turn buffer | `min_turn_buffer` + `Driver.default_min_turn_buffer` + `fg.BUFFER_MODES` |
| Overnight rest | `rest_min_gap_minutes` / `rest_penalty_per_hour` — live; honour 0 = disabled |
| Idle-hole definition | `idle_gap_threshold` = 120 min — three places already agree |
| Day-length limits | `SPAN_SOFT_EFFECTIVE_HOURS` 13.5 / `SPAN_HARD_HOURS_DEFAULT` 15 |
| **Occupancy lead/tail by trip kind** | **nothing fits** — new, and the most important new config |
| **Staffing target / headcount** | **nothing fits** — needs a per-date or per-weekday home, not a constant |
| **Percentiles** | **nothing fits** — the engine has exactly one, `p75`, hard-coded |
| Available for repurposing | `time_scarcity_bonus`, `span_threshold_hours`, `span_penalty_per_hour` — live, UI-exposed, **never read by any code** |

---

# PART C — GO/NO-GO

## C1. What this database supports

| Phase 1 deliverable | Feasible? | Constraint |
|---|---|---|
| `DEMAND_AND_UTILIZATION.md` | **Yes** | Levels from the 29-day current regime; shape **not** poolable across the boundary (§A2.1). |
| `SHIFT_ARCHITECTURE.md` | **Yes, with two named gaps** | Templates are *summer* templates [unavailable]. There is **no record of when anyone was on duty**, so supply is bodies-per-day only. |
| `FARMOUT_RECAPTURE.md` | **Yes** | Premium is solid (§C1.1). The +1/+2/+3 estimate must use a sequential capacity-consuming replay (`WaterfallLedger`), never a count of legs that individually pass feasibility. |
| `REPLAY_AND_EVIDENCE.md` | **Yes, 2026-03-01 .. 2026-08-20** | Must recover plan-time `pickup_time` from `historicalleg` (A11) and must **never** filter on `is_active` / `driver_type` / `exclude_from_timing` (A4). |
| Idle-vehicle carrying cost in dollars | **No** [unavailable] | Founder-supplied parameter with a sensitivity range. |
| Seasonality | **No** [unavailable] | No prior-year August. |
| Deadhead / repositioning distance | **No** [unavailable] | GPS coordinates stopped in July (§A4.2). |

### C1.1 Farm-out — the headline numbers

**Share of assigned legs, by month** [measured]: 40.5% (Oct 25) → 26.6% (Feb) → 21.1% (May) →
14.1% (Jun) → **13.2% (Jul)** → **19.2% (Aug)**. Weekly, the trough was 11.4% (w/c 07-06) and the
peak 23.3% (w/c 08-03).

**The late-July step-up was absorbed by ATTENDANCE, not by hiring** [measured]:

| Per day | Prior plateau | Current regime | Change |
|---|---:|---:|---:|
| legs | 90.34 | 108.41 | **+20.0%** |
| in-house drivers **deployed** | 12.94 | 15.55 | **+20.1%** |
| in-house legs | 73.34 | 88.41 | +20.6% |
| **in-house legs per driver** | **5.65** | **5.68** | **+0.4%** |
| farmed legs | 17.00 | 20.00 | +17.6% |

Daily deployment rose with demand and driver density is an operating constant — but **the extra
bodies are not new hires.** Length-matched, distinct in-house people went **26 → 27 (+1)**: three
names appear, two stop. What rose is how often the same people work — **days worked per body per
week 3.80 → 4.01** [measured]. Under every baseline tried, attendance is the only factor that rises
consistently; headcount and density are sign-unstable and are therefore **not established**.

**This matters for the shift architecture.** The business met a 20% demand step by asking the
existing crew to work more days, not by growing the crew. That is a lever with a short runway, and
it is already showing strain: **258 of 2,415 in-house consecutive-day pairs (10.7%) fall under the
live 8.5-hour rest floor**, with 44 breaches in the current regime alone affecting 15 of 26
drivers [measured] — despite the Rest Advisor being ON.

**The decline stopped — it did not continue.** Against the equal-length 29 days immediately before
the step-up, farm-out share rose **+5.7 pp (12.8% → 18.4%)** and farm-out **volume rose +77.9%**
(326 → 580 legs) [measured]. 2026-07 is the **floor of the entire series, not a trend** — it was
simply the last complete month the superseded snapshot could see, and extrapolating from it was
the error.

**Per-leg premium, four structurally different estimators spanning 10.2%** [measured/modeled]:
**$70.99 per farmed leg**, range $68.13–$75.45. Over the 156-day window that is **$193,087**,
or roughly **$452k/year at the window rate**. Per class: towncar ~$58–61, mini_van ~$69–71,
suv ~$69–73, van ~$111–127, Van(14 Pax) ~$126–134.

**Affiliates take 18.6% of legs but 39.7% of driver dollars** — $297,921 of $750,141, at
**$109.53/leg against $38.08 in-house (2.88×)** [measured].

**Concentration is extreme and it is a duopoly, not a single vendor:** Cheapo Limo 34.3% and
`anthony` 33.8% together carry **68.1% of farmed legs and 69.3% of farmed dollars**; top three
80.1% [measured]. **78% of affiliate records (14 of 18) have no `AffiliateProfile` row at all**,
and only two carry a `daily_cap` — so **31.9% of farm-out volume is governed by no declared
capacity constraint whatsoever.** Where a cap exists it is declarative, not enforced: Cheapo Limo
exceeds its cap of 12 on 16.1% of days worked (max 21).

**Farm-out IS mostly a genuine supply limit — but the binding constraint is headcount *on shift*,
not idle drivers** [measured]. Correlation between farmed legs/day and in-house legs per working
driver = **+0.68**; 63.6% of farmed volume leaves on a day at or past a P75 constraint; and
rostered-but-legless in-house drivers are **0.04/day** — there is essentially no idle in-house
capacity to reclaim.

> **The single most actionable number in this document.** The most in-house drivers ever fielded in
> one day is **18**. The heaviest-farm-out days average only **13.7** [measured]. On the days that
> most need bodies, the business fields **4.3 fewer drivers than it has itself demonstrated it can
> field.** That gap is not a fleet limit and not a hiring problem — it is *who is on shift that
> day*. It is also exactly the lever a shift architecture can pull.

**Dispatchers do not farm first — they release.** **66.3% of farmed legs were held in-house first
and released later**, at a median of **14.4 h before pickup** (82.0% inside 24 h) [measured].
68.2% of all legs change hands at least once. And **affiliates never release**: 0 of 2,739 farmed
legs were released by an affiliate login, though they log in heavily and re-save legs they already
hold.

**The commit habit changed with the step-up**, and this is new: affiliate commit lead jumped from a
~15 h median in every month January–July to **25.8 h**, with the within-24 h rate falling to
**49.5%** [measured]. The in-house arm did not move. The superseded document's "86.1% committed
within 24 h" reproduces at 77.0% over the window and **does not hold at all in the current
regime**.

**How much is recoverable is threshold-sensitive and must always be quoted with its cut.** The
"slack both ways" pool — farmed legs on days where both load and roster had room — ranges from
**3.1% to 64.6% of farmed volume** depending on where the lines are drawn; the P75/P75 headline is
**36.4%** [modeled]. The *direction* is robust; the magnitude is not. `FARMOUT_RECAPTURE.md` must
publish the threshold alongside every recapture figure.

### C1.2 Demand shape, current regime

In aggregate a **single broad morning-centred peak**, not a two-peak commuter shape [measured]:

- booked pickups peak at **09:00** (15.0% of all legs in that one hour)
- realised occupancy peaks at **10:00** at **17.8 mean concurrent**
- long afternoon decay to ~19:00, then a **never-zero overnight floor of 2–4 concurrent**

**But that single peak is two different businesses overlapping, and shift design has to see them
separately** [measured]:

| Hour | Arrivals | Departures | Character |
|---|---:|---:|---|
| 03:00–07:00 | 33 | 300 | almost purely **departures** — early flights out |
| 08:00 | 98 | 136 | departures still lead |
| **09:00** | **206** | **204** | **both waves crest together — the true crunch hour** |
| 10:00–13:00 | 618 | 367 | **arrivals** dominate |
| 14:00–18:00 | 420 | 312 | arrivals lead, tapering |

An early **departure wave** (03:00–09:00) and a later **arrival wave** (09:00–18:00) overlap at
09:00. This matters because the two have different occupancy shapes: a departure ties up a driver
~36 min *before* the booked time and clears ~35 min after (71 min total), while an arrival ties one
up ~21 min before and ~76 min after (96 min). **A shift boundary placed at 09:00 cuts the busiest
moment of the day in half** — the one thing the architecture must not do.

Day-of-week, current regime: Sat P50 **161** legs, Sun 133, Fri 117, Mon 108, Thu 91, Wed 80,
Tue **83** — a Sat:Tue ratio of **1.94**. Fri+Sat+Sun carry the majority of volume on 43% of days.

Class mix: suv 32.6%, towncar 26.1%, mini_van 22.2%, van 14.6%, Van(14 Pax) 4.5%. Fleet (17
active): suv 8, Van(14 Pax) 5, mini_van 2, towncar 2 (1 inactive), van 1 — reasonable under nested
compatibility. **Farm-out is worst on towncar (24.7% farmed, lift 1.32×)** and lowest on the large
classes.

Two concentration facts that any recapture case must respect [measured]: **Fri–Sun carry 76.3% of
farm-out on 55.0% of legs** (lift 1.39×), and the **08:00–12:00 arrival bank carries 68.5% of
farm-out on 48.2% of legs**. **ARRIVALS are farmed at 26.9% against DEPARTURES at 8.8%** — a 3×
difference that points squarely at the arrival bank as the structural shortage.

**Class is over-booked, not just short.** Where the booked class can be compared against the
minimum class the party actually needed, **15.7% of testable legs are booked as SUV when a
mini_van would seat them** [measured]. That is demand the fleet is meeting with a larger, scarcer
vehicle than required — a lever that belongs in `DEMAND_AND_UTILIZATION.md`.

### C1.3 One leg of the brief's objective function is nearly empty

The brief names three costs of poor shift structure: the **farm-out premium**, **idle vehicle
carrying cost**, and **driver fairness/density**. The middle one barely exists in this business
[measured]:

- Of 2,394 rostered vehicle-days, only **5 (0.2%)** carry zero legs and only **53 (2.2%)** carry
  two or fewer.
- Vehicle utilisation (productive minutes ÷ raw span) is P50 51.0% / P75 58.5% / P90 66.0%; legs
  per vehicle-day P50 6.

**Caveat, and it is a real one:** this is partly circular. A DVA row is generally created *when a
driver is assigned work*, so a rostered-and-unused car is nearly unrecordable by construction. The
true idle-vehicle count is **[unavailable]**. But the direction is clear enough to act on: cars
sitting idle *while rostered* is not where the money is.

**Consequence for the engagement:** the farm-out premium is the money metric, essentially alone.
Combined with §B2.2 (the fleet binds on 64% of days) and §C1.1 (farm-out correlates +0.610 with
driver loading), the honest framing is that **this operation is capacity-limited, not
arrangement-limited.** `DEMAND_AND_UTILIZATION.md` should not go looking for large idle-vehicle
savings; it will not find them. Driver fairness/density remains fully in scope.

## C2. Premises in the brief that are wrong

| Brief says | Actually |
|---|---|
| Rest Advisor is "built but currently off, default `rest_min_gap_minutes=510`" | **It is ON.** The live singleton carries `rest_min_gap_minutes = 510` **and** `rest_penalty_per_hour = 40`, so both the scorer penalty and the cards are armed [measured]. Any new inter-shift minimum must read this field, not fork it. |
| Fleet Capacity Intelligence "already has a config-driven margin engine" for buy vs hire vs farm | **The margin functions exist; the decision engine does not.** `VehicleTypeCostProfile`, `simulate_plus_one_vehicle` and `fleet_simulation.py` are documented and unbuilt. There is no cost input anywhere in the schema. |
| The handoff chain: "return to base (MCO ↔ base ≈ 12 min)" | **There is no base in the code** — no address, no depot row, no per-vehicle yard. But the **GPS shows a behavioural base** (§A4.3): 15 drivers start their day in one ~500 m cell. Model the round trip as one tunable constant anchored on that geography, and state that its components are unobservable. |

## C3. What is still needed before Phase 2 can start

Nothing here blocks the *research*; these are decisions and inputs the build plan will need.

1. **Confirmation that "required drivers" means in-house only.** The whole §B2 correction rests on
   it. If the founder wants the number to include the affiliate pool, it is a different number with
   a different denominator, and assumption A10 makes it much harder.
2. **A precedence decision**: when the pre-build diagnostic and a post-build advisor disagree,
   which one is the dispatcher supposed to believe? (§B2.3.) This is a product decision, not an
   analytical one.
3. **A false-alarm budget.** The shipped risk band is a false alarm 66% of the time on `at_risk`
   and 97% on `watch` (§A4.5), and dispatchers already annotate 10% of KEOI flags to say a
   detected conflict is not real. **Phase 2 must state the maximum acceptable false-fire rate for
   the new signal before it is designed**, or it will land in the same place.
4. **A founder-supplied idle-vehicle carrying cost**, with a range (assumption A8) — *if* the
   buy-vs-hire question is in scope at all. §C1.3 argues idle-vehicle cost is nearly empty here, so
   this may be safely deferred; the fleet-ceiling finding (§B2.2) is the reason it might not be.
5. **`gate_receiver()` extracted** from `fold_advisor._simulate` and
   `rebalance_advisor._gate_receiver` before a third consumer is added.
6. **A decision on whether phone-GPS capture is restored** (§A4.2). It was deliberately removed in
   `94c88ba7`. Without it, deadhead and repositioning stay [unavailable] permanently — which is
   survivable for this engagement but forecloses a whole class of later work.

## C4. Recommended decisions

1. **Adopt the corrected occupancy interval (§A3.5) as the definition of demand concurrency**, and
   retire `DAY_SETUP_PEAK_BUFFER = 1` *with* it, not before.
2. **Split the incumbent number in two** (§B2.3): an in-house required-driver count, and a total
   demand-concurrency figure re-labelled as demand. Their difference is the farm-out exposure the
   brief asks to surface.
3. **Never compute an on-time metric for airport arrivals against `pickup_time`** (§A3.7).
4. **Use the current regime for every level claim**, and say in the deliverables that these are
   summer numbers (A7).
5. **Report the driver-GPS regression to the team** as a defect found in passing (§A4.2).

## C5. Verification ledger

Every load-bearing claim was re-derived by a structurally different method
[`06_challenge.py`](analysis/06_challenge.py). **Two of these refuted a hypothesis this document's
own author had already written down**; both are recorded rather than quietly dropped, because a
verification pass that only ever confirms is not doing its job.

| Claim | Verdict | Independent method and result |
|---|---|---|
| Demand stepped up ~20% at 2026-07-24 | **CONFIRMED** | **4 of 4** independent streams (taps, audit log, payments, quotes) also rose >5%. A booking-side artefact could not lift payment or tap volume. |
| Arrival `pickup_time` is re-synced to the flight | **CONFIRMED** | **100% of 13,772** `pickup_time` edits in `auditlog` carry a flight-match reason — established without consulting the flight table. |
| Affiliate rows are vendors, not chauffeurs | **CONFIRMED** | Booked times only, no taps: **32.1%** of affiliate driver-days carry two pickups ≤15 min apart against **3.1%** in-house. |
| `on-the-way` is a physical departure event | **CORRECTED** | It is a **mixture**: P50 8.4 min after the previous completion, but **31.6% land inside one minute** and **8.0% are negative**. Valid as a percentile over many legs; **invalid per leg**. |
| Deterministic modelling inflates the peak by synchronising overlaps | **REFUTED** | Re-injecting measured per-leg jitter **raised** the peak (17.05 → 17.49 against a realised 15.88). The hypothesis was wrong; the real cause is that the all-legs model covers legs with no taps, so the comparison was never like-for-like. |
| There is a de facto base the fleet returns to | **REFUTED** | The busiest cell holds only **12.0%** of 1,006 day-starts, and a driver returns to his **own** modal cell on a median **23.1%** of days. Drivers *touching* a cell is not drivers *based* there. This is a home-kept fleet. |

Two methodological rules fell out of this pass and apply to every downstream deliverable:

1. **Fit lead and tail on the same legs.** Different subsets inflate the interval.
2. **Quote every turnaround percentile with its pairing cap.** The out-of-order effect is +0.0 min
   at a 120-minute cap and +7.9 at 480 [measured]. Neither prior document stated a cap; this one
   uses 8 hours throughout.

---

## Appendix — analysis scripts

All scripts are self-contained, take no arguments, open the database read-only, print their derived
horizon and assumptions in a header, and run from the repo root. **No script contains a hardcoded
analysis date** — every window comes from [`_common.py`](analysis/_common.py) at run time. CSV
output lands in `analysis/out/`.

| Script | Answers |
|---|---|
| [`_common.py`](analysis/_common.py) | Shared foundation: read-only connection, derived `Horizon`, the demand filter, DST-aware conversion, changepoint detection, tap loading, percentiles |
| [`00_horizon_and_window.py`](analysis/00_horizon_and_window.py) | Freshness and provenance; growth and regimes; is the step-up real; booking lead time and forward-date under-sizing; the recommended windows |
| [`01_demand.py`](analysis/01_demand.py) | Volume by month/week/day-of-week; `dow × hour`; class and lane mix; stationarity |
| [`02_status_and_actuals.py`](analysis/02_status_and_actuals.py) | Tap coverage; duplicates; the fabricating cohort; the gold cohort and its generalisation gap; DST; core durations; turnaround; constant recalibration |
| [`03_farmout.py`](analysis/03_farmout.py) | Farm-out identification, trend, concentration, commit timing, premium by class, affiliate capacity, and whether farm-out is a capacity signal |
| [`04_supply.py`](analysis/04_supply.py) | DVA coverage and trustworthiness; roster over time; driver-day shape; fleet; availability declarations; schedule snapshots |
| [`05_flights_and_occupancy.py`](analysis/05_flights_and_occupancy.py) | Flight anchors; the occupancy ladder; the corrected interval; three-way concurrency; **the required-driver reconciliation** |
| [`06_challenge.py`](analysis/06_challenge.py) | Adversarial re-computation of the highest-stakes claims by structurally different methods |
| [`07_new_evidence.py`](analysis/07_new_evidence.py) | The five tables the previous snapshot lacked: `historicalleg`, `auditlog`, `driverlocation`, `schedulesnapshot`, `legkeoi` |

**Phase 1 continues with `DEMAND_AND_UTILIZATION.md` once this document is reviewed.**
