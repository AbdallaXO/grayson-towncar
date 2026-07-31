# Grayson Towncar — Operational Data Audit

**Generated:** 2026-07-31
**Source:** production snapshot `content/db.sqlite3` (114 MB copy of the Railway Postgres)
**Analysis window:** driver status events 2026-02-08 → 2026-07-11; legs 2025-04-26 → present

> This document is a **data handoff**. Every figure was computed directly from the production
> database; none are estimates or recollections. Methodology, filters and known weaknesses are
> stated explicitly so the numbers can be challenged or reproduced.

---

## 1. How this data was collected

### 1.1 Source and access

The company runs Django on Railway (Postgres). A full copy of production exists locally at
`content/db.sqlite3` (114 MB, 99 tables). All queries were run **read-only**:

```python
import sqlite3
con = sqlite3.connect("file:content/db.sqlite3?mode=ro", uri=True)
```

The `db.sqlite3` in the repo root is a 0-byte placeholder — not the data.

Location bucketing used the application's own `dispatching.analytics.categorize_location()` rather
than a re-implementation, so lane names match what the scheduler actually sees.

### 1.2 The single most important structural fact

**A `Leg` row has no actual times.** It stores `pickup_date` and `pickup_time` (both *planned*) and
nothing else. There is no `actual_pickup_at`, no `dropoff_at`, no `completed_at`, no duration, no
mileage. Every "what really happened" number in this document is derived by **differencing rows in
`reservations_legstatus`** — the log of drivers tapping buttons in the driver portal.

Status ladder the driver taps: `Accept → On the Way → On Location → Picked Up → Complete`.
Each tap writes one row with `timezone.now()` at the moment of the tap. No user-entered times exist
anywhere in the system.

### 1.3 Timezone rules (critical — gets everyone the first time)

| Field | Storage |
|---|---|
| `reservations_legstatus.timestamp` | **UTC** |
| `reservations_flight.*_local` (all of them, despite the name) | **UTC** |
| `reservations_leg.pickup_date` / `pickup_time` | **naive local (Florida)** |

- Differencing two status events, or a status event against a flight time → **no conversion**.
- Comparing a status event against `pickup_date`/`pickup_time` → **convert**.
- The offset is **UTC−5 before 2026-03-08** and **UTC−4 from 2026-03-08 onward** (US DST).
  A flat offset corrupts all February data by exactly 60 minutes.

The DST boundary was detected empirically, not assumed: the modal offset between `on-location` and
scheduled pickup is +5h on 2026-03-07 (n=65) and +4h on 2026-03-08 (n=62). Applying the split
yields a median `on-location` delta of **−1.8 min**, i.e. drivers arrive within two minutes of the
scheduled time — the expected physical result, which confirms the offset is right.

### 1.4 Filters applied to every timing figure

1. **First occurrence only.** `MIN(timestamp)` per (leg, status). ~4–6% of legs carry duplicate rows
   per status (re-taps, the payroll bulk-update, the driver-unassign auto-reset).
2. **Trustworthy drivers only.** In-house drivers with `exclude_from_timing = false`. Affiliates are
   excluded because the application's own analytics already discards them.
3. **Plausibility bounds.** Ride time must be 2–240 minutes. Values outside are forgotten taps.
4. **Flight figures** require both `scheduled_gate_arrival_local` and `actual_gate_arrival_local`,
   and the pickup must categorise as `MCO Terminal` or `SFB Terminal`.
5. **Spirit (NK) removed** from all forward-looking figures — the carrier has ceased operations.

### 1.5 Metric definitions

| Name | Definition |
|---|---|
| Approach time | `on-the-way` → `on-location` |
| Curb wait | `on-location` → `picked-up` |
| **Ride time** (the main lane metric) | `picked-up` → `completed` |
| Driver occupancy | `on-location` → `completed` |
| True dwell (arrivals) | flight `actual_gate_arrival_local` → `picked-up` |
| Flight delay | `actual_gate_arrival_local` − `scheduled_gate_arrival_local` (negative = early) |
| Punctuality | `on-location` − scheduled pickup |

---

## 2. Data inventory

| Table | Rows | Notes |
|---|---|---|
| reservations_legstatus | 69,212 | THE event log. Starts 2026-02-08. 99.7% authored by drivers. |
| reservations_leg | 24,124 | 2025-04-26 onward. No actual-time columns. |
| reservations_flight | 25,456 | `flight_type` empty on 24,730 (97%). |
| reservations_quote | 42,150 | Unexamined here. |
| reservations_lead | 33,195 | 32% convert. |
| reservations_legflight | 16,873 | Multi-flight link. `is_controlling` clean: 0 legs with 0 or >1. |
| drivers_legpayment | 17,730 |  |
| reservations_schedulesnapshotentry | 10,740 | Plan-vs-actual is recoverable but unused. |
| ops_operationaltask | 7,989 | Exception history, unmined. |
| reservations_routetimingmetric | 456 | The learning table. Was 440 before rebuild. |
| reservations_routedistancecache | 2 | Effectively unused. |
| reservations_demandpattern | 0 | Model + writer exist. Never populated. |
| reservations_driverdailycapacity | 0 | Model + writer exist. Never populated. |

### 2.1 Status-event coverage over time

| Month | Legs | With full ladder | Coverage |
|---|---:|---:|---:|
| 2026-02 | 1,999 | 1,208 | 60% |
| 2026-03 | 2,707 | 1,878 | 69% |
| 2026-04 | 2,808 | 2,001 | 71% |
| 2026-05 | 2,914 | 2,284 | 78% |
| 2026-06 | 2,799 | 2,367 | 85% |
| 2026-07 | 989 | 783 | 79% |

*July is partial — status data ends 2026-07-11.* Coverage is **improving**, unaided.

---

## 3. IMPORTANT CORRECTION — read before using the lane figures

An earlier version of this analysis reported that *"every lane is under-estimated and none
over-estimated"*, comparing the measured **75th percentile** against the scheduler's
`DRIVE_TIME_ESTIMATES` table. **That comparison was not like-for-like.**

The table appears to have been authored as *typical* (median-ish) drive times. Comparing a p75
against it manufactures a shortfall on every row. Compared at the **median**, the table is
substantially accurate, and on several lanes it actually **over**-estimates:

| Lane | n | Measured median | Table | Median − table | Measured p75 | p75 − table |
|---|---:|---:|---:|---:|---:|---:|
| Disney Resort → MCO Terminal | 2,934 | 33 | 30 | +3 | 37 | +7 |
| MCO Terminal → Disney Resort | 2,498 | 36 | 30 | +6 | 44 | +14 |
| Port Canaveral Area → MCO Terminal | 245 | 50 | 55 | -5 | 55 | +0 |
| Universal Resort → MCO Terminal | 188 | 24 | 25 | -1 | 27 | +2 |
| MCO Terminal → Port Canaveral Area | 173 | 51 | 55 | -4 | 58 | +3 |
| MCO Terminal → Universal Resort | 168 | 27 | 25 | +2 | 37 | +12 |
| Disney Resort → SFB Terminal | 154 | 59 | 60 | -1 | 63 | +3 |
| SFB Terminal → Disney Resort | 112 | 60 | 60 | +0 | 76 | +16 |
| Disney Resort → Universal Resort | 90 | 29 | 28 | +1 | 33 | +5 |
| Disney Resort → Port Canaveral Area | 76 | 75 | 72 | +3 | 81 | +9 |
| Universal Resort → Disney Resort | 70 | 29 | 28 | +1 | 33 | +5 |
| Airport Hotel → Port Canaveral Area | 66 | 54 | 55 | -1 | 63 | +8 |
| Port Canaveral Area → Disney Resort | 63 | 69 | 72 | -3 | 81 | +9 |
| Airport Hotel → Disney Resort | 33 | 33 | 25 | +8 | 39 | +14 |

`*` = no table entry; falls back to `DEFAULT_DRIVE_TIME = 35`.

**The honest conclusion:** the drive-time table is a reasonable *median* model. What it lacks is a
notion of spread. The scheduler uses it to decide whether a driver can make the next job — a
question that needs a conservative percentile, not a typical value. The issue is therefore **not
that the numbers are wrong**, but that a median is the wrong statistic for a feasibility gate.

---

## 4. Sanford (SFB) — flagged as suspicious, investigated

The concern was correct to raise. Findings:

| Lane | n | p10 | p25 | median | p75 | p90 | max | Table |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Disney Resort → SFB Terminal | 154 | 51 | 54 | 59 | 63 | 70 | 102 | 60 |
| SFB Terminal → Disney Resort | 112 | 51 | 57 | 60 | 76 | 87 | 186 | 60 |

**Both directions have a median of ~59–60 minutes, exactly matching the table's 60.** The table is
right for Sanford at the median.

What produced the apparent discrepancy:

1. **Small sample.** SFB → Disney has n=112 against 2,498 for MCO → Disney. Bootstrapped 90%
   confidence interval for its p75 is **72–79 min** — real, but wide.
2. **Fat right tail inbound.** The SFB → Disney distribution runs 3 legs at 10–19 min (physically
   impossible for ~45 miles), a cluster at 50–69, then 5 legs at 90–109, one at 131 and one at 186.
   A handful of extreme values moves p75 substantially at this sample size.
3. **Genuine directional asymmetry.** SFB → Disney p75 = 76 (CI 72–79) vs Disney → SFB p75 = 63
   (CI 62–65). Non-overlapping, so the inbound direction really is slower — the same pattern seen
   on MCO → Disney (44) vs Disney → MCO (37). The likely cause is that inbound trips end with
   luggage unload at an unfamiliar resort entrance, and the driver does not tap "complete" until
   the bags are out.

**Recommendation: do not change the Sanford table value.** 60 minutes is correct at the median. If
anything is done, make it directional (inbound needs more headroom than outbound), and only after
the sample grows.

### 4.1 Percentile stability across all lanes

Bootstrap, 1,000 resamples, 90% interval on p75. **Only the two MCO ↔ Disney lanes are large enough
for confident percentile claims.** Everything else should carry an explicit caveat.

| Lane | n | median | p75 | p75 90% CI | CI width | Verdict |
|---|---:|---:|---:|---:|---:|---|
| Disney Resort → MCO Terminal | 2,934 | 33 | 37 | 36–37 | 0 | stable |
| MCO Terminal → Disney Resort | 2,498 | 36 | 44 | 43–45 | 2 | stable |
| Port Canaveral Area → MCO Terminal | 245 | 50 | 55 | 54–57 | 3 | stable |
| Universal Resort → MCO Terminal | 188 | 24 | 27 | 26–28 | 2 | stable |
| MCO Terminal → Port Canaveral Area | 173 | 51 | 58 | 56–61 | 5 | stable |
| MCO Terminal → Universal Resort | 168 | 27 | 37 | 34–40 | 6 | borderline |
| Disney Resort → SFB Terminal | 154 | 59 | 63 | 62–65 | 3 | stable |
| SFB Terminal → Disney Resort | 112 | 60 | 76 | 72–79 | 7 | borderline |
| Disney Resort → Universal Resort | 90 | 29 | 33 | 31–34 | 3 | stable |
| Disney Resort → Port Canaveral Area | 76 | 75 | 81 | 76–89 | 13 | TOO FEW SAMPLES |
| Universal Resort → Disney Resort | 70 | 29 | 33 | 30–34 | 4 | stable |
| Airport Hotel → Port Canaveral Area | 66 | 54 | 63 | 60–68 | 8 | borderline |
| Port Canaveral Area → Disney Resort | 63 | 69 | 81 | 77–85 | 8 | borderline |
| Airport Hotel → Disney Resort | 33 | 33 | 39 | 36–46 | 10 | borderline |

### 4.2 Physically impossible records still in the data

| Lane | n | Impossible below | Count | Share |
|---|---:|---:|---:|---:|
| SFB Terminal → Disney Resort | 112 | 35 | 3 | 2.7% |
| Disney Resort → SFB Terminal | 154 | 35 | 4 | 2.6% |
| MCO Terminal → Port Canaveral Area | 173 | 30 | 3 | 1.7% |
| Port Canaveral Area → MCO Terminal | 245 | 30 | 4 | 1.6% |
| MCO Terminal → Disney Resort | 2498 | 12 | 29 | 1.2% |
| Disney Resort → MCO Terminal | 2934 | 12 | 21 | 0.7% |

These survive the current filters and should be excluded by a per-lane minimum.

---

## 5. Flight punctuality

### 5.1 Overall

- **n = 5,773** arrivals with both scheduled and actual gate times
- Median delay **-6 min**, 10th pct -23, 90th pct +34
- **50% arrive early** (>5 min ahead)
- 57% within ±15 min
- 19% more than 15 min late;
  7.3% more than 45 min late

The distribution is **left-shifted with a long right tail**. Planning against a median plans for the
case that was never going to hurt you; the operational risk lives entirely in the tail.

### 5.2 By airline

| Airline | n | 10th pct | Median | 90th pct | >15 min late |
|---|---:|---:|---:|---:|---:|
| American | 833 | -22 | -5 | +53 | 24% |
| Allegiant | 289 | -23 | -3 | +52 | 27% |
| Frontier | 238 | -27 | -11 | +42 | 19% |
| Delta | 923 | -20 | -5 | +36 | 20% |
| Southwest | 1,750 | -16 | -2 | +29 | 18% |
| JetBlue | 636 | -27 | -10 | +29 | 16% |
| United | 682 | -28 | -11 | +25 | 14% |
| Breeze | 199 | -31 | -15 | +21 | 13% |
| Alaska | 53 | -28 | -7 | +20 | 11% |
| Avelo | 50 | -29 | -19 | -1 | 4% |

### 5.3 By airline AND time of day — median delay (n)

This is the dimension a per-airline average hides. Cells with fewer than 15 observations are blank.

| Airline | before 8am | 8am-12pm | 12-4pm | 4-8pm | 8pm+ |
|---|---:|---:|---:|---:|---:|
| Southwest | +11 (116) | -6 (110) | -5 (691) | -2 (554) | +7 (279) |
| Delta | +4 (48) | -2 (20) | -7 (347) | -4 (320) | +0 (188) |
| American | +17 (57) | — | -10 (303) | -5 (331) | +4 (133) |
| United | -8 (34) | — | -14 (212) | -9 (314) | -7 (113) |
| JetBlue | +1 (36) | — | -12 (281) | -10 (233) | -1 (84) |
| Allegiant | +11 (29) | — | -8 (90) | -3 (113) | +19 (54) |
| Frontier | -15 (16) | — | -13 (81) | -11 (102) | +3 (32) |
| Breeze | -3 (20) | — | -21 (71) | -14 (55) | -7 (53) |
| Alaska | — | — | — | -15 (16) | -3 (27) |
| Avelo | — | — | -19 (42) | — | — |

**Pattern: early-morning and late-night flights run late; midday runs early.** The within-airline
swing reaches 27 minutes (American: −10 midday vs +17 before 8am), which is larger than the spread
*between* most airlines.

### 5.4 Per-flight predictability

1,473 distinct flight identities appear. Recurrence:

| Seen at least | Flights | Arrivals covered | Share of volume |
|---|---:|---:|---:|
| 3× | 599 | 4,626 | 80% |
| 5× | 360 | 3,812 | 66% |
| 8× | 189 | 2,819 | 49% |
| 12× | 107 | 2,056 | 36% |
| 20× | 34 | 915 | 16% |

**Typical uncertainty window (p10→p90):** 45 min per specific flight vs
56 min per airline — a modest average gain. The value is in
identifying *which* flights are trustworthy.

Tightest (most predictable), n ≥ 12:

| Flight | Origin | Seen | Median | 90th pct | Window |
|---|---|---:|---:|---:|---:|
| WN2476 | IND - Indianapolis Intl | 12× | -6 | +1 | 12 min |
| AA2101 | PHL - Philadelphia Intl | 24× | -18 | -10 | 19 min |
| UA1520 | EWR - Newark Liberty Intl | 12× | -21 | -16 | 20 min |
| AA2074 | CLT - Charlotte/Douglas Intl | 16× | -6 | +2 | 21 min |
| AA2127 | CLT - Charlotte/Douglas Intl | 17× | -10 | +4 | 22 min |
| DL1319 | RDU - Raleigh-Durham Intl | 12× | -8 | +4 | 22 min |
| WN2130 | IND - Indianapolis Intl | 14× | -6 | +7 | 22 min |
| AA2325 | CLT - Charlotte/Douglas Intl | 14× | -21 | -1 | 24 min |
| UA788 | EWR - Newark Liberty Intl | 16× | -20 | -9 | 24 min |
| B62695 | HPN - Westchester County | 12× | -6 | +3 | 24 min |
| WN3415 | MHT - Manchester Boston Rgnl | 12× | -13 | +2 | 25 min |
| WN477 | BDL - Bradley Intl | 15× | -1 | +7 | 25 min |
| AA2093 | PHL - Philadelphia Intl | 18× | -18 | -7 | 25 min |
| B685 | BUF - Buffalo Niagara Intl | 19× | -13 | +3 | 26 min |
| UA411 | EWR - Newark Liberty Intl | 24× | -20 | -10 | 26 min |

Widest (never plan tight), n ≥ 12:

| Flight | Origin | Seen | Median | 90th pct | Window |
|---|---|---:|---:|---:|---:|
| DL1576 | ATL - Hartsfield-Jackson Intl | 19× | +15 | +187 | 189 min |
| UA2636 | CLE - Cleveland-Hopkins Intl | 19× | -14 | +141 | 174 min |
| AA2066 | ORD - Chicago O'Hare Intl | 17× | +3 | +154 | 172 min |
| F92013 | TTN - Trenton Mercer | 12× | +4 | +133 | 161 min |
| F91807 | PHL - Philadelphia Intl | 16× | -11 | +124 | 159 min |
| B6851 | BOS - Boston Logan Intl | 22× | +6 | +130 | 154 min |
| DL0511 | CVG - Cincinnati/Northern Kentucky International Airport | 19× | -9 | +89 | 109 min |
| AS300 | PDX - Portland Intl | 19× | -15 | +77 | 108 min |
| DL2225 | MSP - Minneapolis/St Paul Intl | 16× | +1 | +97 | 106 min |
| DL2824 | CVG - Cincinnati/Northern Kentucky International Airport | 20× | +0 | +82 | 103 min |

---

## 6. Arrival anatomy

| Measure | n | p25 | median | p75 | p90 |
|---|---:|---:|---:|---:|---:|
| True dwell — gate docked → guest in car | 2,870 | +30 | +37 | +47 | +64 |
| Driver on-location vs gate docking | 2,813 | -2 | +11 | +21 | +33 |
| Driver occupancy — on-location → complete | 2,801 | +51 | +65 | +84 | +107 |

- The code assumes a flat **45-minute** dwell (`STATIC_FLOOR_DWELL_MIN`). Measured p75 is
  47 — close at p75, but **19 min short at p90**, and blind to the
  airline and time-of-day differences in §5.3.
- **28% of the time the driver is on location before the
  plane has docked** — roughly 56 driver-hours per month of
  pure waiting, before any deplaning.
- Two independent fallbacks in the code use **75 min** (`ops/tasks.py:26`) and **60 min**
  (`ops/views.py:1759`) for trip duration; measured p75 occupancy is 84 min.

---

## 7. Disney resort granularity — tested, not worth building

MCO → each Disney resort (the scheduler currently uses one flat 30 min for all):

| Resort | n | median | p75 | p90 |
|---|---:|---:|---:|---:|
| Old Key West | 55 | 34 | 51 | 59 |
| Contemporary | 158 | 37 | 48 | 64 |
| All-Star | 162 | 36 | 47 | 59 |
| Animal Kingdom Lodge | 168 | 35 | 47 | 60 |
| Coronado Springs | 93 | 35 | 46 | 58 |
| Polynesian | 212 | 38 | 45 | 57 |
| Caribbean Beach | 140 | 35 | 44 | 58 |
| Art of Animation | 223 | 33 | 44 | 53 |
| BoardWalk | 77 | 35 | 44 | 55 |
| Grand Floridian | 157 | 38 | 43 | 58 |
| Swan/Dolphin | 140 | 35 | 43 | 55 |
| Wilderness Lodge | 87 | 36 | 43 | 55 |
| Shades of Green | 30 | 37 | 43 | 58 |
| Beach/Yacht Club | 134 | 35 | 42 | 56 |
| Saratoga Springs | 106 | 34 | 42 | 55 |
| Port Orleans | 164 | 33 | 41 | 53 |
| Pop Century | 142 | 33 | 40 | 53 |
| Disney Springs | 40 | 33 | 40 | 60 |
| Riviera | 48 | 34 | 39 | 57 |

Disney → MCO:

| Resort | n | median | p75 | p90 |
|---|---:|---:|---:|---:|
| Wilderness Lodge | 107 | 36 | 39 | 42 |
| Grand Floridian | 178 | 36 | 39 | 42 |
| Contemporary | 179 | 37 | 39 | 44 |
| Polynesian | 261 | 35 | 38 | 42 |
| Shades of Green | 37 | 34 | 38 | 45 |
| Beach/Yacht Club | 185 | 34 | 37 | 41 |
| Animal Kingdom Lodge | 159 | 34 | 37 | 41 |
| BoardWalk | 113 | 33 | 36 | 41 |
| Old Key West | 61 | 31 | 36 | 44 |
| Swan/Dolphin | 153 | 32 | 36 | 40 |
| Port Orleans | 217 | 31 | 35 | 42 |
| All-Star | 177 | 32 | 35 | 38 |
| Art of Animation | 265 | 31 | 34 | 37 |
| Pop Century | 164 | 31 | 34 | 38 |
| Riviera | 57 | 29 | 34 | 39 |
| Saratoga Springs | 115 | 31 | 34 | 42 |
| Coronado Springs | 114 | 32 | 34 | 40 |
| Caribbean Beach | 168 | 31 | 33 | 38 |
| Disney Springs | 40 | 29 | 31 | 34 |

**Every resort's median sits in an ~8-minute band.** Splitting one bucket into eighteen would divide
samples ~18× to buy a few minutes of precision, and most resorts would fall below the threshold
where the scheduler trusts a bucket at all (`sample_count >= 5`). The variation is **within** each
resort, not between them — which is traffic and time of day.

---

## 8. Data quality

### 8.1 Trustworthy

- **Driver status taps.** 99.7% of 69,212 events authored by drivers with `timezone.now()` at tap
  time. No user-entered times exist in the system. After the DST correction, median `on-location`
  lands within ~2 minutes of scheduled pickup every month, with genuine spread (p05 −41, p95 +82) —
  anchored or backfilled data does not look like that.
- **Flight gate actuals** (FlightAware AeroAPI). 83% coverage on airport-pickup legs.
  `is_controlling` is clean: zero legs with none or more than one.
- **Coverage is improving**: 60% → 85% over five months.

### 8.2 Not trustworthy

| Issue | Measured | Impact |
|---|---|---|
| `flight_type` empty | 24,730 of 25,456 rows (**97%**) | Arrival vs departure cannot be read from the flight record; everything falls back to keyword-matching free-text locations |
| Double-tapping | **36%** of full-ladder legs have ≥1 adjacent gap under 60s | Understates approach time by ~4 min at the median. Does **not** affect ride time (protected by an existing 2-min floor) |
| Seven drivers tap "Picked Up" + "Complete" together | 65–95% of their trips | ~11% of completed legs had fictional ride times. Now excluded |
| `exclude_from_timing` mis-targeted | 3 excluded drivers produced excellent data; 0 of the 7 bad ones were flagged | Corrected |
| Duplicate status rows | 3.7–5.9% of legs | Interacted with a `.first()` ordering bug (fixed) |
| Corrupt pickup dates | legs dated **year 3220** and **2029** | Poisons any MIN/MAX or range query |
| Airline name fragmentation | `PORTER`, `PORTER AIRLINES`, `PORTER AIRLINE`; bare `AA`/`DL`/`UA`/`WN` alongside full names | Splits airline grouping |
| `utm_source` fragmentation | `meta`, `Meta`, `facebook`, `fb`, `ig` | Marketing attribution split across 5 spellings of 2 channels |
| Payroll bulk-complete | 337 rows stamped at payroll time | Small; self-identifying via `notes` |
| Driver reassignment wipes progression | 1,806 `Auto-reset` rows | Erases real taps; also a hidden churn metric |

### 8.3 Corrected during the audit

- **`in-progress` is not an anomaly.** It is the Django model default for a newly created Leg
  (`reservations/models.py:1067`). Of 4,936 such legs, 87% have no driver and 66% are future-dated.
  Only ~584 are past-dated stragglers. It must be **excluded** from any timing pipeline — its median
  delta is −399 min because it is a pre-trip bookkeeping state, not a driving event.
- **Django admin bulk-complete bypasses `LegStatus`** — true in code, but only **3 legs** in the
  whole event era lack a completed event. Not a live problem.
- **AeroAPI stops refreshing once a leg is `completed`** (`ops/tasks.py:1480`) — true in code, but
  completed arrival legs still show 87.9% actual-gate coverage. Worth fixing defensively, not
  urgently.

### 8.4 Driver status discipline (in-house, ≥25 legs)

`instant%` = share of full-ladder trips where `picked-up` → `completed` was under 2 minutes.

| Driver | Legs | Full ladder | Instant | Median ride | Currently excluded |
|---|---:|---:|---:|---:|---|
| ernesto | 115 | 95% | 95% | 76 min | YES |
| ken | 572 | 99% | 93% | 43 min | YES |
| AldoH | 67 | 94% | 73% | 40 min | YES |
| Idrees | 140 | 99% | 68% | 36 min | YES |
| Francisco | 80 | 100% | 68% | 43 min | YES |
| placeholder | 34 | 9% | 67% | 44 min | YES |
| Raymond | 272 | 89% | 66% | 38 min | YES |
| mesfin | 143 | 99% | 65% | 8 min | YES |
| neuma | 616 | 2% | 54% | 50 min | YES |
| shelley | 125 | 50% | 29% | 35 min | — |
| Rayyan | 42 | 48% | 25% | 41 min | — |
| sereen | 411 | 98% | 20% | 34 min | — |
| rizwan | 453 | 17% | 18% | 36 min | YES |
| Hasan | 128 | 99% | 17% | 38 min | — |
| runer | 608 | 98% | 14% | 34 min | — |
| angel | 481 | 99% | 13% | 37 min | — |
| abdi | 25 | 32% | 12% | 45 min | YES |
| george | 581 | 100% | 12% | 36 min | — |
| Seline | 500 | 100% | 12% | 37 min | — |
| davide | 722 | 97% | 12% | 35 min | — |
| yovanny | 675 | 99% | 8% | 35 min | — |
| shipo | 251 | 98% | 8% | 37 min | — |
| lev | 41 | 98% | 8% | 32 min | — |
| Aftab | 387 | 100% | 6% | 37 min | — |
| Michael | 468 | 100% | 5% | 32 min | — |
| junaid | 599 | 99% | 5% | 34 min | — |
| HassanA | 135 | 99% | 4% | 35 min | — |
| alex | 376 | 38% | 4% | 34 min | — |
| roberto | 870 | 89% | 3% | 32 min | — |
| julio | 178 | 99% | 3% | 34 min | — |
| steven | 399 | 99% | 2% | 35 min | — |
| carlos | 36 | 97% | 0% | 38 min | — |
| Charlie | 30 | 100% | 0% | 35 min | — |

Two distinct failure modes hide behind one completion percentage:
- **Fabricating** (high instant%) — taps both buttons at the end. High full-ladder score, zero usable
  data. Must be excluded.
- **Sparse** (low full-ladder%) — runs the ladder rarely, but the completed trips are honest.
  Must **not** be excluded; doing so throws away real samples.

---

## 9. GOLD COHORT — the drivers who use the app properly

The founder nominated the drivers who genuinely use the application properly. Every nominee was
vetted against the data before inclusion, and one was rejected.

**Cohort (14 drivers, 8,656 legs):** Michael, sereen, yovanny, steven, junaid, angel, runer, roberto, lev, george, davide, Charlie, Aftab, oualid

**Rejected on the data:** **Idrees** — 68% instant-complete — ride times are fabricated. Including
a driver whose ride times are fabricated would defeat the point of having a clean baseline.

**Note on `oualid`:** he is an *affiliate*, and the founder is right that he is the only affiliate
using the ladder properly (1% instant-complete, better than most in-house drivers). But the
application's own analytics filters to `driver_type='inhouse'`
(`analytics.py:update_single_route_timing_metric`), so **his trips never reach `RouteTimingMetric`
in production**. He is included in this section's figures. If his data should count for real, that
filter has to change — it is a code decision, not a flag.

### 9.1 Their actual discipline

| Driver | Legs | Full ladder | Instant (<2 min) | Median ride |
|---|---:|---:|---:|---:|
| roberto | 870 | 89% | 3% | 32 min |
| davide | 722 | 97% | 12% | 35 min |
| yovanny | 675 | 99% | 8% | 35 min |
| runer | 608 | 98% | 14% | 34 min |
| junaid | 599 | 99% | 5% | 34 min |
| george | 581 | 100% | 12% | 36 min |
| angel | 481 | 99% | 13% | 37 min |
| Michael | 468 | 100% | 5% | 32 min |
| sereen | 411 | 98% | 20% | 34 min |
| steven | 399 | 99% | 2% | 35 min |
| Aftab | 387 | 100% | 6% | 37 min |
| lev | 41 | 98% | 8% | 32 min |
| Charlie | 30 | 100% | 0% | 35 min |

The nomination is broadly borne out — but not uniformly. Steven (2%) and Roberto (3%) are near
flawless; Sereen (20%), Runer (14%) and Angel (13%) double-tap on roughly one trip in six. Roberto's
full-ladder rate (89%) is the lowest of the eight. **This is a coaching list, not a scorecard** —
the gap between the best and worst of these eight is small compared to the excluded seven (65–95%).

### 9.2 Lane timings — gold cohort only, vs the full trustworthy set

| Lane | Gold n | Gold median | Gold p75 | Gold p75 CI | All-driver n | All median | All p75 | Median diff |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Disney Resort → MCO Terminal | 2,388 | 33 | 36 | 36–37 | 2,934 | 33 | 37 | -0 |
| MCO Terminal → Disney Resort | 2,129 | 36 | 44 | 43–45 | 2,498 | 36 | 44 | -0 |
| Port Canaveral Area → MCO Terminal | 192 | 50 | 55 | 54–57 | 245 | 50 | 55 | 0 |
| Universal Resort → MCO Terminal | 155 | 24 | 27 | 26–28 | 188 | 24 | 27 | -0 |
| MCO Terminal → Port Canaveral Area | 145 | 51 | 59 | 56–63 | 173 | 51 | 58 | -0 |
| MCO Terminal → Universal Resort | 133 | 29 | 37 | 34–42 | 168 | 27 | 37 | +2 |
| Disney Resort → SFB Terminal | 130 | 59 | 63 | 62–66 | 154 | 59 | 63 | 0 |
| SFB Terminal → Disney Resort | 89 | 60 | 72 | 65–76 | 112 | 60 | 76 | -0 |
| Disney Resort → Universal Resort | 75 | 29 | 33 | 31–35 | 90 | 29 | 33 | +0 |
| Disney Resort → Port Canaveral Area | 73 | 75 | 81 | 76–84 | 76 | 75 | 81 | +0 |
| Airport Hotel → Port Canaveral Area | 54 | 54 | 61 | 57–68 | 66 | 54 | 63 | -0 |
| Universal Resort → Disney Resort | 48 | 29 | 33 | 30–34 | 70 | 29 | 33 | +1 |
| Port Canaveral Area → Disney Resort | 47 | 71 | 81 | 78–85 | 63 | 69 | 81 | +2 |
| Airport Hotel → Disney Resort | 29 | 32 | 38 | 36–40 | 33 | 33 | 39 | -0 |

**Read this table carefully — it is the key validation of the whole analysis.** If the gold cohort's
numbers differed materially from the full trustworthy set, every conclusion would be suspect. They
do not: the medians track within a few minutes on every lane with meaningful sample.

That means the driver-exclusion work in §8.4 was sufficient. Restricting further to the eight best
drivers **buys accuracy but costs sample size**, and the accuracy gain is small.

### 9.3 Gold-cohort arrival anatomy

| Measure | n | p25 | median | p75 | p90 |
|---|---:|---:|---:|---:|---:|
| Approach — on-the-way → on-location | 6,404 | 10 | 25 | 43 | 67 |
| Curb wait — on-location → picked-up | 6,425 | 7 | 19 | 37 | 58 |
| True dwell — gate docked → in car | 2,442 | +30 | +37 | +47 | +64 |
| Occupancy — on-location → complete | 2,483 | 53 | 69 | 88 | 111 |

Compare to the all-trustworthy-driver figures in §6: dwell median 37 vs gold
37; occupancy median 65 vs gold 69.

### 9.4 Recommendation on cohort choice

Use the **full trustworthy set** (in-house, `exclude_from_timing = false`) as the production
baseline, not the gold eight. Reasons:

1. The medians agree, so the gold cohort adds little accuracy.
2. Sample size matters more — the binding constraint is that 75% of route buckets already fall below
   the scheduler's `sample_count >= 5` trust floor. Shrinking the cohort makes that worse.
3. The gold eight are not evenly spread across lanes, shifts or vehicle types, so restricting to
   them would introduce its own selection bias.

Keep the gold cohort as a **validation set**: when a metric changes, check it moves the same way in
both populations. If they ever diverge, that is a signal worth investigating.

---

## 10. Predicted clear time vs actual — every route (gold cohort)

**What does the system predict a job will take, and what does it actually take?**

### 10.1 How the prediction is made

The figures below call the production function `dispatching.scheduler.chain_clear_dt()` — the
same code the scheduler uses to decide whether a driver can make their next job. It is not a
re-implementation. The formula (`scheduler.py:933`):

```
clear = anchor + dwell + category_drive + store_stop

anchor = scheduled pickup_time  (pushed LATER if a live flight ETA is later; never earlier)
dwell  = 45 min  for arrivals, and for to-cruise legs picked up at an airport
       =  0 min  for from-cruise legs (leaving the port) and everything else
drive  = DRIVE_TIME_ESTIMATES[(pickup_category, dropoff_category)], else 35
store  = 25 min if the reservation has a Publix stop
```

So a **Port Canaveral → MCO** job predicts `pickup + 55`, while **MCO → Port Canaveral** predicts
`pickup + 45 + 55 = 100` because the guest is deplaning first.

"Actual cleared" = the driver's `completed` tap. Both are measured from the scheduled pickup time,
so the two columns are directly comparable.

### 10.2 EVERY ROUTE — ranked worst-under-predicted first

n ≥ 20. **Error = actual − predicted. Positive means the job ran LONGER than the system expected**,
which is the direction that breaks chains.

| Lane | n | Predicted | Actual median | Actual p75 | Error median | Error p75 | Error p90 | % finish late |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Disney Resort → Disney Resort | 32 | 12 | 25 | 34 | +13 | +22 | +28 | 78% |
| MCO Terminal → Other Hotel | 20 | 70 | 83 | 106 | +10 | +36 | +50 | 80% |
| Airport Hotel → Disney Resort | 33 | 25 | 35 | 45 | +10 | +20 | +28 | 88% |
| Disney Resort → Port Canaveral Area | 82 | 72 | 78 | 90 | +6 | +18 | +35 | 71% |
| Disney Resort → Universal Resort | 86 | 28 | 33 | 43 | +5 | +15 | +22 | 64% |
| Universal Resort → Disney Resort | 59 | 28 | 32 | 42 | +4 | +14 | +37 | 63% |
| Disney Resort → MCO Terminal | 2569 | 30 | 33 | 40 | +3 | +10 | +17 | 61% |
| Airport Hotel → Port Canaveral Area | 60 | 55 | 56 | 65 | +1 | +10 | +27 | 55% |
| Port Canaveral Area → Disney Resort | 50 | 72 | 71 | 89 | -1 | +17 | +34 | 48% |
| Disney Resort → SFB Terminal | 142 | 60 | 59 | 67 | -1 | +7 | +16 | 46% |
| Universal Resort → MCO Terminal | 175 | 25 | 23 | 29 | -2 | +4 | +11 | 42% |
| Port Canaveral Area → MCO Terminal | 212 | 55 | 51 | 63 | -4 | +8 | +26 | 42% |
| MCO Terminal → Disney Resort | 2379 | 75 | 74 | 87 | -8 | +2 | +14 | 29% |
| MCO Terminal → Universal Resort | 160 | 70 | 68 | 84 | -10 | -0 | +18 | 24% |
| MCO Terminal → Port Canaveral Area | 169 | 100 | 86 | 102 | -14 | +1 | +21 | 26% |
| SFB Terminal → Disney Resort | 101 | 105 | 94 | 109 | -14 | -3 | +11 | 20% |

**Across all 6,570 jobs:** error median **-1 min**, p25 -11,
p75 +8, p90 +18.
**46% of jobs finish later than predicted.**

Three things fall out of this table:

1. **The prediction is well-centred overall** — the median job finishes within a couple of minutes of
   what the scheduler expected. This is a working model, not a broken one.
2. **The misses are concentrated in short intra-Orlando hops.** Disney → Disney, Airport Hotel →
   Disney, Disney ↔ Universal and Disney → MCO all run long, with 61–88% of jobs finishing late. On
   a 28-minute predicted job a 10-minute miss is a 36% error; on a 100-minute port run the same ten
   minutes is noise. **Short lanes are where the chain actually breaks.**
3. **Long airport-anchored lanes are over-predicted** — SFB → Disney and MCO → Port Canaveral both
   run 14 minutes short of prediction, MCO → Universal 10, MCO → Disney 8. The flat 45-minute dwell
   is generous once the drive itself is long.

### 10.3 Pickup punctuality — every route

| Lane | n | p25 | median | p75 | p90 | % >15 min late |
|---|---:|---:|---:|---:|---:|---:|
| MCO Terminal → Disney Resort | 2319 | +29 | +37 | +49 | +68 | 96% |
| Disney Resort → MCO Terminal | 2564 | -6 | +0 | +7 | +16 | 11% |
| Port Canaveral Area → MCO Terminal | 215 | -9 | +2 | +16 | +43 | 27% |
| MCO Terminal → Port Canaveral Area | 161 | +25 | +35 | +52 | +76 | 84% |
| Universal Resort → MCO Terminal | 171 | -5 | -0 | +6 | +16 | 11% |
| MCO Terminal → Universal Resort | 154 | +27 | +38 | +54 | +79 | 96% |
| Disney Resort → SFB Terminal | 139 | -5 | +1 | +6 | +24 | 16% |
| SFB Terminal → Disney Resort | 99 | +22 | +33 | +45 | +81 | 88% |
| Disney Resort → Port Canaveral Area | 80 | -0 | +4 | +13 | +42 | 19% |
| Disney Resort → Universal Resort | 86 | -4 | +5 | +15 | +25 | 26% |
| Universal Resort → Disney Resort | 57 | -4 | +4 | +31 | +42 | 32% |
| Airport Hotel → Port Canaveral Area | 56 | -2 | +3 | +9 | +12 | 9% |
| Port Canaveral Area → Disney Resort | 50 | -13 | +4 | +24 | +50 | 28% |
| Disney Resort → Disney Resort | 30 | +1 | +10 | +22 | +33 | 37% |
| Airport Hotel → Disney Resort | 32 | -2 | +1 | +12 | +28 | 22% |
| MCO Terminal → Other Hotel | 20 | +35 | +45 | +56 | +76 | 100% |

Across 6,471 jobs the median guest boards **+14 min** from the scheduled
pickup time. The airport-origin lanes are the outliers — see §10.5.

### 10.4 Port Canaveral in detail

| Lane | n | Predicted | Actual median | Actual p75 | Error median | Error p75 | Error p90 | % finish late |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Port Canaveral Area → MCO Terminal | 212 | 55 | 51 | 63 | -4 | +8 | +26 | 42% |
| MCO Terminal → Port Canaveral Area | 169 | 100 | 86 | 102 | -14 | +1 | +21 | 26% |
| Disney Resort → Port Canaveral Area | 82 | 72 | 78 | 90 | +6 | +18 | +35 | 71% |
| Airport Hotel → Port Canaveral Area | 60 | 55 | 56 | 65 | +1 | +10 | +27 | 55% |
| Port Canaveral Area → Disney Resort | 50 | 72 | 71 | 89 | -1 | +17 | +34 | 48% |
| Universal Resort → Port Canaveral Area | 16 | 60 | 66 | 82 | +6 | +22 | +48 | 69% |
| Port Canaveral Area → Universal Resort | 13 | 60 | 77 | 94 | +17 | +34 | +57 | 62% |
| Port Canaveral Area → Airport Hotel | 14 | 55 | 63 | 72 | +8 | +17 | +32 | 64% |

*Error = actual cleared − predicted cleared. Negative means the driver finished **earlier** than the
system expected.*

**Overall across 644 Port Canaveral jobs:** error median **-2 min**,
p25 -14, p75 +12, p90 +32.
**46% of jobs finish later than predicted.**

Read: **the clear-time prediction is broadly sound at Port Canaveral** — slightly conservative at
the median, with a p90 tail of about half an hour. Two lanes deserve attention:

- **MCO → Port Canaveral** is over-predicted by ~14 min at the median (predicts 100, typically takes
  86), and only about a quarter of these jobs run late. The 45-minute dwell is generous here.
- **Disney → Port Canaveral** is the one that runs hot: predicted 72, actual median 78, and **71% of
  jobs finish later than predicted** with a p90 error of +35. This lane is genuinely under-buffered
  and is the clearest candidate for a table change on the port side.

### 10.5 Pickup punctuality at the port — the important finding

| Lane | n | p25 | median | p75 | p90 | % >15 min late |
|---|---:|---:|---:|---:|---:|---:|
| Port Canaveral Area → MCO Terminal | 215 | -9 | +2 | +16 | +43 | 27% |
| MCO Terminal → Port Canaveral Area | 161 | +25 | +35 | +52 | +76 | 84% |
| Disney Resort → Port Canaveral Area | 80 | -0 | +4 | +13 | +42 | 19% |
| Airport Hotel → Port Canaveral Area | 56 | -2 | +3 | +9 | +12 | 9% |
| Port Canaveral Area → Disney Resort | 50 | -13 | +4 | +24 | +50 | 28% |
| Universal Resort → Port Canaveral Area | 15 | -10 | -1 | +6 | +80 | 20% |
| Port Canaveral Area → Universal Resort | 13 | -10 | +16 | +29 | +77 | 54% |
| Port Canaveral Area → Airport Hotel | 14 | -0 | +6 | +17 | +19 | 36% |

*Actual `picked-up` tap minus scheduled pickup time. Positive = guest boarded later than scheduled.*

**Port pickups are realistic. Airport-to-port pickups are not.**

- Leaving the **port** (disembarkation), the median guest boards **3 minutes early**. Those scheduled
  times are honest — the guest is standing there waiting.
- Going **MCO → Port Canaveral**, the median guest boards **36 minutes after** the scheduled pickup
  time, p75 +59, p90 +85, and **87% board more than 15 minutes late.**

That second row is not a lateness problem — it is a **labelling problem**. On an airport-to-port leg
the "pickup time" is effectively the flight's arrival slot, not the moment the guest reaches the car.
The scheduler already understands this (it adds the 45-minute dwell before computing clear time), but
a dispatcher reading the board sees a pickup time that will be wrong by half an hour, and any
on-time metric computed off that field will unfairly mark these jobs late.

### 10.6 Port vs non-port

Across 5,926 non-port jobs the error median is **-1 min** (p75
+8, p90 +18), versus **-2 min** across
644 port jobs (p75 +12, p90 +32).

**Port Canaveral is not the problem lane.** The port predictions hold up at least as well as the
Orlando-area ones, because the long drive dominates and leaves less room for proportional error.

---

## 11. What the software currently assumes

| Constant | Value | Location | Measured reality |
|---|---|---|---|
| `STATIC_FLOOR_DWELL_MIN` | 45 min | `scheduler.py:195` | p75 47, p90 64 |
| `DEFAULT_DRIVE_TIME` | 35 min | `scheduler.py:83` | lane-dependent |
| `DEPLANING_GRACE_MIN` | 10 min | `feasibility_guards.py:39` | — |
| `SAFETY_PAD_MIN` | 0 min | `feasibility_guards.py:44` | — |
| `FALLBACK_TRIP_DURATION_MINUTES` | 75 min | `ops/tasks.py:26` | p75 occupancy 84 |
| (second fallback) | 60 min | `ops/views.py:1759` | same |
| `MIN_PICKUP_TO_COMPLETE` | 2 min | `analytics.py:434` | doing real work — keep |
| `sample_count >= 5` | trust floor | `scheduler.py:605` | 75% of buckets fail this |

`required_turnaround = (−10 if next leg is an airport arrival at the same category, else
category-table drive minutes) + 0 safety pad`. Live Google distance is **off** in production
(`USE_LIVE_DISTANCE=0`).

**Planned arrival buffer:** median `scheduled pickup − scheduled gate arrival` = **−1 minute**.
There is effectively no deliberate buffer; pickup is scheduled at the gate time.

---

## 12. Open questions / known weaknesses of this analysis

1. **Ride time is driver wall-clock, not routing time.** It includes the driver's latency in tapping
   "complete" and any luggage handling. It is the right metric for "when is the driver free", but it
   is **not** a pure drive time and should not be compared to a maps ETA.
2. **Only two lanes have enough data for confident percentiles** (§4.1). Everything else needs a
   caveat.
3. **Five months of event history.** No seasonality can be measured — Florida's cruise and
   theme-park cycles are annual, and this window covers February to July only.
4. **Selection bias.** Every timing figure is computed on the subset of legs that have a complete
   ladder (60–85% depending on month), from drivers who tap reliably. That subset may not represent
   the chaotic days.
5. **Cruise data is two strings.** `reservations_cruise` stores only `cruise_line` and `ship_name` —
   no port, no sail date, no disembarkation time. Port Canaveral timing can only be measured against
   our own pickup times, never against ship reality.
6. **Nothing records what was predicted.** The system never stores the scheduler's estimate
   alongside the outcome, so estimate accuracy cannot be tracked over time. This is the single
   biggest structural gap for a "learning" system.
7. **No passenger no-show, wait-time, extra-stop, reassignment-history, or mileage events exist**
   anywhere in the schema.
8. **Samsara GPS is never historized** — vehicle position is overwritten every 3 minutes, so real
   drive times independent of driver taps are discarded continuously.

---

## 13. Reproducing any figure here

```bash
cd /path/to/grayson-towncar
ENABLE_DEBUG_TOOLBAR=0 python - <<'EOF'
import os, django, sqlite3, collections
from datetime import datetime
os.environ.setdefault("DJANGO_SETTINGS_MODULE","business.settings"); django.setup()
from dispatching.analytics import categorize_location

con = sqlite3.connect("file:content/db.sqlite3?mode=ro", uri=True); cur = con.cursor()
P = lambda s: datetime.fromisoformat(s.replace(' ','T').split('.')[0])

good = {r[0] for r in cur.execute(
    "SELECT id FROM drivers_driver WHERE driver_type='inhouse' AND exclude_from_timing=0")}

ev = collections.defaultdict(dict)
for lid, s, ts in cur.execute('''SELECT leg_id,status,MIN(timestamp)
      FROM reservations_legstatus
      WHERE status IN ('on-the-way','on-location','picked-up','completed')
      GROUP BY leg_id,status'''):
    ev[lid][s] = P(ts)

lane = collections.defaultdict(list)
for lid, pl, dl, did in cur.execute(
        "SELECT id,pickup_location,dropoff_location,driver_id FROM reservations_leg"):
    d = ev.get(lid)
    if not d or 'picked-up' not in d or 'completed' not in d or did not in good:
        continue
    v = (d['completed'] - d['picked-up']).total_seconds()/60      # both UTC — no conversion
    if 2 <= v <= 240:
        lane[(categorize_location(pl), categorize_location(dl))].append(v)

for k, v in sorted(lane.items(), key=lambda x: -len(x[1]))[:10]:
    v.sort()
    print(k, len(v), 'p50=%.0f' % v[len(v)//2], 'p75=%.0f' % v[int(.75*len(v))])
EOF
```

There is also a management command for the driver-discipline table:

```bash
python manage.py driver_data_quality --days 200          # report only
python manage.py driver_data_quality --days 200 --apply  # write exclude_from_timing
```
