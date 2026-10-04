# 07 — Structured shifts, car splitting and driver facts (design)

**Status:** adopted by the founder on 2026-10-03 (rev 3, the "combined version"). Not built. The next
step is Stage 1 (§10), which the founder will start.
**Evidence:** every number comes from read-only queries of the dev DB (cut 2026-09-28) and was
re-derived by an independent check. "Estimate" marks modelled values. Delay reserves are
placeholders (§7).

---

## 1. Why

Flexible driver shifts are doing more harm than good. An early driver's last job runs late, the
day goes long, and tomorrow's early start suffers because each day is planned on its own.

**Goals:**
- structured shifts;
- cars used across the whole day;
- **as much work in-house as possible**;
- **every driver ≤ 12h**;
- driver facts held in the system instead of in dispatchers' heads.

**What the code does today (verified 2026-10-03):**
- **No shift record.** A "shift" is simply first pickup → last clear.
- **The engine ignores configured hours.** `feasibility_guards.USE_STUB_WINDOWS=True` applies a
  hardcoded Feb–May table instead.
- **Windows are crude.** They are whole hours capped at 23:00, and a window that crosses midnight
  rejects every pickup.
- **Hour caps:** 12h free / 13.5h soft / 15h hard / 17h absolute (founder D4). **This design
  replaces D4 with 12h.**
- **Rest:** a soft, look-back-only penalty on the planned end. Nothing re-checks span or tomorrow's
  start when a pickup moves.
- **Shared cars:** `DriverVehicleAssignment.planned_start_hour/end_hour` are hour-granular and NULL
  on all 3,340 rows. Nothing ties a second driver's start to when the car is actually back.

**How far today is from 12h (estimates):**
- In 10 sample days, **122 of 191 driver-days ran over 12h** base to base.
- On Saturday 9/26, 18 of 19 cars ran more than 12h.

## 2. Decisions

**Founder:**
| # | Decision |
|---|---|
| U1 | **12h is the line; in-house is the goal.** The plan never exceeds 12h (including delay reserve). On the day, a dispatcher may approve an overrun of ≤ 1h, and only if tomorrow's rest still holds; it is logged per driver. Replaces D4 (13.5/15h). |
| U2 | 12h is measured **base → base**: collect the car → car back at base. |
| U3 | Each driver has a regular shift; a weekly roster is built from it; the builder fills trips into the roster. |
| U4 | **No take-home cars.** All cars are pooled, sleep at the base, and go wherever they keep the most work in-house. |
| U5 | Driver facts the system uses: regular shift, hard earliest start / latest finish, weekly cap, regular car, open to extra shifts. Home area is not used. |
| U6 | The roster is staff-side first. How drivers see it is decided later; no driver-app change. |
| U7 | A dated weekly roster is the source of who works when. |
| U8 | The roster is sized on **projected** demand, not booked demand: core shifts + standby, re-forecast daily. |
| U10 | **Car splitting is the centre of the design.** For each car, each day: SPLIT (morning + evening driver), ONE DRIVER (Midday or Morning-only), or UNUSED. |
| U11 | Shift shapes are **targets, not limits**. The handover time is whatever is best that day. |
| U12 | **An evening driver gets ≥ 3 jobs.** No evening shift is rostered unless projected evening demand fills it. |
| U13 | Delay reserves are **placeholders until Stage 0 measures them.** |
| U14 | **Fuel at every handover, no wash at midday.** A drops the last guest → fuel stop (15 min) → base → swap → 15-min prep → B leaves. No gap-exception swap; no tank-range modelling. |
| U15 | No fixed handover window. A good handover means, in order: B gets real work (≥ 3 jobs), then **most trips kept in-house**, then **a clean last job for A** (ends at MCO, ideally a departure). An even split of hours is not a goal. |
| U16 | **15-min prep buffer** after the car is ready, one standard buffer for every job. **B reports at car-ready or 10–15 min before; his 12h counts from his report time.** |
| U17 | **The system decides each handover; a dispatcher OKs it** for now. Every rule is written down so it can later run on its own. |
| U18 | **Farm only what is physically impossible**, meaning every eligible car is busy at that time. Order: another car's shift with room → pool leftovers into an extra evening shift on a free car → farm. A free car with no driver is a staffing gap, flagged at D-3 so a standby gets called; it is never a reason to farm. |
| U19 | **Wash only at the end of the night.** The last driver of the day runs drop → wash → fuel → base (61 min after an MCO drop; setting), inside his 12h. |
| U20 | **Driver A running late** is handled by prevention in the plan, a re-check on facts, then a 5-step fix ladder (§9). The system names the exact car and driver for every fix. |
| U21 | **Each day's schedule is finished ~2 days ahead** (Monday for Wednesday), then new trips are added as they come. A target, not a lock. |
| U22 | **The combined build order (§5) is the design:** splits and the evening count are decided from the demand forecast; handover windows are reserved before packing; cars are packed for maximum in-house; one-driver windows float until the day is built; late trips may move a cut or a window. |
| U23 | **Weekly driver↔car pairing rotates week to week.** |
| U24 | **No fairness or evenness rules for now.** In-house coverage is the only objective. |
| U25 | **Staffing: the founder will hire to cover the extra driver-days** the 12h rule needs (§11.2). The ≤ 1h overrun stays a day-of exception only, never a planning tool. |

**Decided under delegation (the founder can overturn any):**
| # | Decision |
|---|---|
| C2 | Weekly cap = max days per week. |
| C3 | Regular car = tie-break only; it never costs a trip. |
| C4 | Projection = booked ÷ that weekday's booked share at the same lead time, using a cautious (25th-percentile) share. |
| C5 | Core roster published 7 days ahead; re-forecast D-3…D-1; standby confirmed or released by 6 PM on D-1. |
| C6 | Delay reserve = 80th percentile of (actual finish − night-before planned finish) per job kind, measured in Stage 0. |
| C7 | Rest 8.5h from realistic end to next start, checked both ways; a wall in planning, a warning for manual moves. |
| C8 | Regular shifts are pre-filled from each driver's last 8 weeks of real starts, for a manager to confirm. |
| C10 | **The handover time H is derived from A's real last job, never typed into a roster** (§6). |
| C11 | Planned times are stored in minutes in new fields. The legacy hour fields the driver portal displays (`drivers/views.py:156-158, 187-200`) stay unwritten until U6 is decided. |
| C12 | No 1-job shifts. A lone trip moves to another car's shift instead. |
| C13 | A call task goes to Driver B only when his report time moves by ≥ 15 min. |
| C14 | Weekly pairing (U23) is the default. The system may swap a car for a day when that keeps a trip. |
| C15 | When B's first job is a flight arrival, it needs ≥ 15 min of handover slack. A recaptured farm trip never becomes B's first job below that. |

## 3. Shift shapes, tuned to real demand

Source: 8,787 trips over 66 days (7/24–9/27). Demand rose sharply in September: Saturdays went
189 → 257 → 270 → 294 trips.

| Shape | What the data shows |
|---|---|
| **Morning** | Leaves base 3–6 AM. A 3 AM start is real for only 1–4 cars; most leave 4–6 AM. It is done somewhere between noon and 4 PM. |
| **Evening** | Starts at the handover. That is noon–2:30 PM on a crunch day (the 12h limit on early mornings sets it), 2–4 PM on a typical weekend, 1:30–3 PM on weekdays. The last car of the night is back about 2:15 AM, since the day's last pickup is a median 11:32 PM MCO arrival. |
| **Midday** | One driver, leaves base ~6–9 AM, back ≤ 12h later. |
| **Lull** | There is no noon lull: demand falls steadily from the 9–10 AM peak. A median weekend still needs ~19 cars at noon. |
| **Vans / 14-pax** | Almost no work after 5 PM, so evening shifts are SUV, minivan and towncar work. |
| **Evening ≥ 3 jobs** | Real today: 99 split car-days; the evening driver's first pickup is a median 4:55 PM; 3 jobs median; 76% get ≥ 3. |

## 4. How the pieces fit

```
Driver facts → Weekly roster (core + standby) → D-3 car plan from the forecast
→ D-2 build (windows reserved, pack, then set H) → late trips adjust → D-1 re-check → day-of cards
```
- **One availability door:** the roster is a layer in `drivers/availability.py:resolve_effective_availability`.
  Precedence: approved time off > published roster shift > weekly pattern > defaults.
- **One rules door:** all rules live in `dispatching/feasibility_guards.py` / `check_feasibility`, so
  the engine, swap search, Day-Builder and board warnings give one verdict.
- **Operating model:** propose-only; humans call drivers; nothing contacts drivers automatically.
  Every stage sits behind a `SchedulerSettings` switch, and promotion is the founder's call.

## 5. The daily build — the combined version (U22)

**Step 1 — D-3, from the demand forecast: which cars split, and how many evening drivers.**
- **The forecast curve:** the projected demand curve (P50 occupancy by 15 min and class, Day Setup's
  `concurrency_series`, scaled per C4) is stacked into car "layers".
- **Each layer is labelled:**
  - SPLIT: > 12h, with a cut that gives B ≥ 3 jobs and keeps both shifts ≤ 12h.
  - ONE DRIVER: ≤ 12h (Midday or Morning-only).
  - UNUSED.
- **Evening drivers are named to a count:** one per SPLIT layer of an evening-capable class (SUV,
  minivan, towncar). The roster and standby offers follow from that count.
- **On supply-limited days**, cars with the most evening work split first, and the shortfall is
  flagged so a standby gets called.
- **Why the forecast and not a packed preview:** the head-to-head test (§11.3) showed that deciding
  splits from a tightly packed day finds almost no legal handover, because packing removes the gaps.
  Only 8 splits happened in 10 days, and on 9/19 no trip after 6 PM was kept.
- **The forecast is stable enough for this:** on 9/26, the views from 3 days out and from the night
  before both gave the same label on all 19 layers as the final day.

**Step 2 — D-2: cars confirmed, handover windows reserved before packing.**
- Evening drivers are attached to specific cars.
- Each split car gets a provisional handover time from the curve. The latest possible is set by the
  morning shift's 12h; the earliest by B's 12h and ≥ 3 jobs.
- A window of about 27 min before to 37 min after that time is reserved on the car (drive back + fuel;
  prep + drive out).

**Step 3 — Pack for maximum in-house (no fairness, U24).**
- Trips are placed to keep the most work in-house. Each trip goes to its booked class first, then a
  higher tier.
- Reserved windows stay clear.
- **One-driver cars have no fixed 12h window during packing.** Their window floats and is placed
  afterwards where it covers the most trips. Fixing it early lost very early departures in the test.

**Step 4 — Set the real handover and the 12h limits.**
- **Derive H** on each split car from A's actual last job (§6).
- **Fine-tune all cuts jointly,** not car by car, using the Day-Builder's candidate-plan machinery
  (`dispatching/day_planner.py`). The search includes "don't split: one driver ≤ 12h, and move the
  first/last trip elsewhere".
- **Place each one-driver car's 12h window** over its densest run of trips.
- **Re-home displaced trips under U18,** and run recapture of farmed trips before pooling.

**Step 5 — D-2 to the day: late trips adjust the plan.**
- About 6% of trips arrive after D-2.
- A late trip may move a cut or a one-driver window, not only drop into a gap. In the test, adding
  late trips without moving anything farmed 65 of 74.
- The system proposes the change and the dispatcher OKs it.
- **D-1 evening:** every handover is re-checked against current pickup times. Call tasks go out per
  C13.

**Coverage view (Planner).** One row per car, which the dispatcher OKs:
- Example row: `Car 001 · SUV · SPLIT · Morning: Angel 4:10 AM → car ready ~1:00 PM after fuel (6
  jobs, last = 12:00 PM MCO departure) · Evening: Charlie reports ~12:45 PM, leaves ≥ 1:15 PM, first
  job 1:40 PM MCO → back ~1:10 AM (4 jobs)`.
- Flags: evening < 3 jobs, any shift > 12h, handover < 15 min slack, car down, staffing gap.
- Footer: drivers needed vs available, and projected in-house vs farm.

## 6. Planning rules (all in `feasibility_guards` / `check_feasibility`)

1. **Shift = base → base.**
   - Start = first pickup − drive base→zone − pickup buffer (10 airport / 15 other). An evening
     driver starts at his report time.
   - End: a morning shift ends at H; the last driver of the night ends at last clear + drive to MCO +
     wash/fuel/base (61 min).
   - Test: ≤ 12h. This replaces the 13.5/15h caps and retires the priced 13.5–15h choice.
2. **Handover (U14, U16).**
   - `H = A's last clear (pickup + P50 tail) + reserve(kind) + drive(drop zone → base) + 15 fuel`.
   - B reports at H − 10–15 min and leaves base ≥ H + 15.
   - B's first pickup ≥ H + 15 + drive base→zone + pickup buffer.
   - H is stored in minutes on both drivers' rows (C11).
   - Settings: `handover_fuel_stop_min` 15, `handover_prep_min` 15, `night_return_min` 61.
   - Founder's example: A's 12:00 PM MCO departure clears at 12:33 → fuel → car ready ~1:00 PM → B
     reports ~12:45, leaves ≥ 1:15 → earliest first pickup ~1:37 PM at MCO, ~2:05 PM at Disney.
3. **Delay-safe end of a morning shift.** With the reserve added, H must still meet B's first
   pickup. A late flight arrival is a poor last job for A: an MCO→Disney arrival makes the car ready
   about 1 hour later than a Disney→MCO departure at the same time.
4. **Rest both ways as a wall** (C7), measured from the realistic end.
5. **Manual moves warn, never block** (`dispatching/assign_warnings.py`).

**Worked example — Jose (corrects rev 1, which wrongly had the evening driver start at 2 PM):**
- **Last job a 2:30 PM Disney→MCO departure.**
  - Guest dropped ~3:05 PM → fuel → H ≈ 3:32 PM. Jose's day: 5:00 AM → 3:32 PM = 10.5h.
  - The evening driver reports ~3:17–3:22 PM, not 2 PM. His earliest first pickup is ~4:09 PM at MCO.
  - Real analogues: 9/7 car 13 and 9/17 car 14, where the evening driver's first pickup was about
    5 PM.
- **Last job a 2:30 PM MCO→Disney arrival instead.** H ≈ 4:35 PM, and Jose's day is 11.6h, leaving
  ~25 min for the largest delay reserve of any job kind. A departure is the better last job (U15).
- **The rev-1 rule would not have caught the 2 PM start:** it compared the handover only with B's
  first pickup. The derived-H rule closes that gap.

## 7. Delay reserves — placeholders (U13)

No reserve is known yet; every example uses R = 0.

**What is measured:**
- **Pickup drift after the night before:** departures move ≥ 15 min later 1.6% of the time
  (n 3,727); arrivals 24% (n 4,312), and 14.8% move ≥ 30 min later.

**What is not measured:**
- **Finish later than the night-before plan:** no committed script measures it. A first unreviewed
  look suggests departures at P75 +8 min and arrivals at P75 +26 min; do not use these as the number.
- **The handover chain itself** (wash/fuel/base) and any real base-to-base shift.

**Stage 0 source for plan-time values:** the later of `reservations_auditlog` and
`reservations_historicalleg` at or before D-1 8 PM.

## 8. Driver facts and roster

**Driver facts:**
- `ShiftTemplate` (Morning / Midday / Evening): target bands from §3, manager-editable, 12h ceiling.
- `DriverWeeklySchedule` rows point at a template.
- New optional `Driver` fields: `hard_earliest_start`, `hard_latest_finish` (may cross midnight),
  `max_days_per_week`, `extra_shift_days`.
- `preferred_vehicles` moves onto the staff profile form.
- A rostered shift is a fixed window; "flexible" is retired for rostered drivers.

**Foundation repairs:**
- minute-precision, midnight-crossing windows (`window_check`);
- a "drivers without a regular shift" list;
- `USE_STUB_WINDOWS=False` once that list is empty.

**Roster (`RosterShift`):**
- Fields: driver, date, template, start, end, core|standby, draft|published|released.
- Generation: from regular shifts, minus time off, within hard limits, the weekly cap, and rest
  between days (no evening→morning flip).
- Sizing: on projected demand (C4/C5). Standby goes to "open to extra shifts" drivers.
- Pairing: weekly driver↔car pairing rotates (U23/C14).

## 9. Day of

**"Handover at risk" (Driver A runs late, U20):**
- **Prevent:** A's last job should be one that doesn't move (a departure ending at MCO).
- **Detect, on facts only:** A's last flight is retimed, A taps in late, or there is no "completed"
  tap by the expected time.
- **Fix ladder** (each step pre-checked; the dispatcher picks and calls):
  1. The delay fits in the 15-min buffer → nothing moves; a call task gives B his new report time.
  2. **B takes a free car.** The card names the car, class, location (GPS where tracked), ready time,
     and that it is free for B's whole shift.
  3. Another in-house driver takes B's first job.
  4. A drives B's first job straight after his own, within 12h; otherwise only as the approved
     ≤ 1h overrun, and only if tomorrow's rest holds.
  5. Farm — only if 1–4 are impossible.
- **Never suggested:** moving a customer's pickup time.
- **Edge cases:** B calls out; a car problem at fuel; several handovers late at once (departures are
  protected first); a 14-pax-only first job; B pushed past 12h at night.

**"Over the line":**
- **Triggers, on facts only:** a pickup move (`ops/move_impact.py`) or an overrun
  (`conflict_advisor._detect_overruns`).
- **Ranked fixes, each pre-checked:** another driver → standby on a free car → overrun ≤ 1h if rest
  holds → farm.
- Approved overruns go into a `ShiftOverrun` log.
- The system never contacts a driver.

## 10. Build order (each stage gets its own spec → plan → review)
| Stage | Content | Release note |
|---|---|---|
| 0 Measure | (a) Delay reserve per job kind. (b) Booking-projection back-test at D-7 and D-3. (c) **Replay of the combined version (§5) on the same 10 days, at today's driver counts and with enough drivers. It has NOT been tested yet. Gate: in-house trips within the real-chain baseline (≈ 1,287 over the 10 days with enough drivers, §11.2); 0 planned driver-days > 12h; 0 evening shifts < 3 jobs; late-trip adjustment keeps most late bookings in-house.** (d) Measure the handover chain where taps and ETA samples allow. | none |
| 1 Foundation | Minute / cross-midnight windows; driver facts and profile card; templates; pre-filled regular shifts; stub-retirement switch. | yes |
| 2 Roster + car plan | `RosterShift`; §5 steps 1–2; coverage view; re-forecast; availability layer. | yes (behind switch) |
| 3 Build + rules | §5 steps 3–5 and §6 rules; retire 13.5–15h. Switched on only after the Stage 0c gate. | yes |
| 4 Day of | §9 cards and `ShiftOverrun`. | yes |

**Staffing — decided (U25):** holding 12h without losing in-house work takes about a third more
driver-days (§11.2). The founder will hire to cover them. The ≤ 1h overrun stays a day-of exception
only.

## 11. Evidence

### 11.1 Real busy day — 2026-09-26 (294 trips)
- **What really ran:** 24 drivers on 19 cars, 181 trips in-house (+1 parked placeholder), 112
  farmed. 18 of 19 cars ran > 12h.
- **Where the farming sits:** 74 of the 112 farmed trips fell between 7 AM and noon. That is the
  morning peak, which is limited by cars, not drivers (the fleet question, D8).
- **Re-cutting the real chains into shifts** puts handovers between noon and 2:30 PM on a day like
  this (Appendix B).

### 11.2 Staffing — real chains re-cut under the final rules, drivers capped (10 days)
| Day | Today: in-house / drivers / > 12h | Same drivers: kept | Enough drivers: drivers → kept | Same drivers + ≤ 13h overrun: kept |
|---|---|---|---|---|
| Sat 9/26 | 181 / 24 / 15 | 161 | 31 → 171 | 172 |
| Sat 9/19 | 174 / 21 / 16 | 145 | 29 → 164 | 155 |
| Sun 9/27 | 147 / 19 / 17 | 125 | 28 → 142 | 130 |
| Sat 8/22 | 144 / 19 / 11 | 125 | 26 → 138 | 130 |
| Sat 8/29 | 127 / 18 / 11 | 110 | 25 → 128 | 115 |
| Sun 8/16 | 114 / 17 / 11 | 105 | 22 → 113 | 112 |
| Mon 9/21 | 141 / 20 / 14 | 125 | 26 → 139 | 133 |
| Thu 9/17 | 130 / 22 / 13 | 114 | 30 → 129 | 117 |
| Wed 9/9 | 94 / 16 / 8 | 84 | 19 → 89 | 87 |
| Wed 8/26 | 72 / 15 / 6 | 69 | 18 → 74 | 70 |
| **Total** | **1,324 / 191 / 122** | **1,163 (−12%)** | **254 → 1,287 (−3%)** | **1,221 (−8%)**, ~90 driver-days 12–13h |

- **Every plan has 0 driver-days over 12h.**
- **The 36-driver cap never binds:** the most any day usefully uses is 31.
- **Trips lost at today's headcount: 174.** Extra drivers each would need: 1 for 32 trips, 2 for 31,
  3–4 for 35, 5+ for 37, and no number of drivers saves 39. These per-trip counts are path-dependent
  by ±1–2.

### 11.3 Head-to-head: fill methods (10 days, same packing model)
| | Shifts first, then fill | Pack, then split (window kept clear) |
|---|---|---|
| Kept, today's drivers | 1,218 | 1,224 |
| Kept, up to 36 drivers | 1,236 | 1,234 |

- **A tie.** Each wins on different days by 1–4 trips.
- **Both decided splits from a packed preview, and that found almost no legal handover:** 8 splits in
  10 days, and only 179–186 driver-days used.
- **Late trips added with nothing allowed to move:** 65 of 74 farmed.
- **The model packs ~12% tighter than the dispatchers** (1,482 vs 1,324 kept with no hour limits),
  so absolute numbers here are optimistic; the comparison is fair.
- **Hence U22:** decide splits from the forecast, reserve windows first, let one-driver windows
  float, and let late trips adjust.

### 11.4 Earlier uncapped replay (historical — it used a since-dropped gap-exception swap)
- 1,297 kept as written (≈ 1,316 with checker fixes), against 1,324 today, using 303 driver-days.
- It split nearly every long car. The capped run (§11.2) splits only where a second driver keeps
  trips, which is why it needs 254 driver-days instead.
- Lost trips: Appendix A.

## 12. Verification (per stage)
- **Unit tests:**
  - cross-midnight windows;
  - base→base span including the buffer;
  - H derivation, and B's start ≥ H;
  - car-plan labels on synthetic curves;
  - the ≥ 3-job gate;
  - rest checked both ways;
  - roster refusals;
  - late-trip adjustment.
- **How to run them:** `ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching drivers`.
- **Switch-off parity:** with the new switches off, `run_assignment_pipeline` output is
  byte-identical on 10 dates (analysis/14 pattern).
- **The Stage 0c replay gate** is re-run before each switch-on.
- **A browser check** of the coverage view and roster pages, with a screenshot.

---

## Appendix A — trips lost in the earlier uncapped replay (historical rules)
Cheapest fix per the replay agent; the checkers later found more fixes on 9/19, 8/22, 8/16 and 9/21.
"None (every car busy)" means farming was allowed under U18.

| Day | Car | Pickup | Kind | Trip | Cheapest fix |
|---|---|---|---|---|---|
| 09-26 | 14 | 10:30 AM | Dep | Wilderness Lodge → MCO | None: keeping it loses the 11:36 AM arrival instead |
| 09-26 | 002 | 10:30 AM | Dep | Saratoga Springs → MCO | None: keeping it loses the 11:37 AM arrival |
| 09-26 | 13 | 11:11 AM | Arr | MCO → Radisson at the Port | None: keeping it loses the 2:00 PM departure |
| 09-26 | 003 | 11:13 AM | Arr | MCO → All-Star Music | None: one driver keeps it but drops two later arrivals |
| 09-26 | 009 | 11:55 AM | Arr | MCO → Polynesian | None: one driver keeps it but loses the 4:29 PM arrival |
| 09-26 | 18 | 12:00 PM | Dep | Art of Animation → MCO | None: keeping it loses the 12:23 PM arrival |
| 09-26 | 004 | 12:25 PM | Dep | Riviera → MCO | None: keeping it loses the 1:24 PM arrival |
| 09-26 | 007 | 12:53 PM | Arr | SFB → Port Orleans Riverside | None: keeping it loses two other trips |
| 09-26 | 006 | 1:49 PM | Arr | MCO → Riviera | Extra driver on free car 11 |
| 09-26 | 17 | 3:15 PM | Oth | Four Seasons → Inter&Co Stadium | Checker: the car-11 extra driver can take it |
| 09-26 | 10 | 11:12 PM | Arr | MCO → Beach Club | Extra driver on free car 13 (B's 12h clock) |
| 09-19 | 006 | 9:34 AM | Arr | MCO → Contemporary | Move the cut: one driver; first/last trips to cars 005 and 001 |
| 09-19 | 001 | 9:42 AM | Arr | MCO → Saratoga Springs | Move the cut: one driver; early departures to car 005 |
| 09-19 | 19 | 10:00 AM | Dep | Port Orleans Riverside → MCO | Move the cut: one driver; 5:03 PM arrival to car 001 |
| 09-19 | 15 | 11:00 AM | Arr | MCO → Royal Caribbean, Port | None (every car busy) |
| 09-19 | 009 | 11:09 AM | Arr | MCO → Kidani Village | Move the cut: one driver; 5:02 PM arrival to car 001 |
| 09-19 | 007 | 11:45 AM | Arr | MCO → Grand Floridian | Move the cut: 6:15 AM departure to car 12 |
| 09-19 | 002 | 12:20 PM | Arr | MCO → Hyatt Regency | None (every car busy) |
| 09-19 | 18 | 12:26 PM | Arr | SFB → Port Orleans | None (checker: a one-driver fix exists before recapture) |
| 09-19 | 13 | 12:30 PM | Dep | Animal Kingdom Lodge → MCO | Move the cut: one driver |
| 09-19 | 11 | 1:23 PM | Arr | MCO → Beach Club | Another car: car 17's morning |
| 09-19 | 12 | 1:27 PM | Arr | MCO → Portofino Bay | Move the cut: one driver |
| 09-19 | 17 | 1:27 PM | Arr | MCO → Polynesian | None (checker: a one-driver fix exists before recapture) |
| 09-19 | 16 | 1:32 PM | Arr | MCO → Grand Floridian | Move the cut: one driver |
| 09-19 | 005 | 3:00 PM | Oth | Animal Kingdom Lodge → Fairfield Inn Airport | Move the cut: one driver |
| 09-19 | 003 | 3:52 PM | Arr | MCO → Old Key West | Earlier evening start |
| 09-27 | 005 | 7:45 AM | Oth | Port Canaveral → private home | Extra driver on a free car |
| 09-27 | 003 | 8:15 AM | Dep | NCL Port Canaveral → MCO | None (every car busy) |
| 09-27 | 14 | 9:45 AM | Dep | Pop Century → MCO | Move the cut to after this trip |
| 09-27 | 13 | 10:21 AM | Arr | SFB → Pop Century | None (every car busy) |
| 09-27 | 008 | 10:23 AM | Arr | MCO → Wilderness Lodge | None (every car busy) |
| 09-27 | 009 | 10:38 AM | Arr | SFB → Art of Animation | None (every car busy) |
| 09-27 | 18 | 10:38 AM | Arr | SFB → Art of Animation | None (every car busy) |
| 09-27 | 12 | 11:04 AM | Arr | MCO → Yacht Club (14-pax) | Extra driver on a free car |
| 09-27 | 002 | 12:30 PM | Dep | Port Orleans French Quarter → MCO | None (every car busy) |
| 09-27 | 007 | 11:00 PM | Arr | MCO → Pop Century | Extra driver on a free car (B's 12h clock) |
| 08-22 | 007 | 8:00 AM | Oth | Disney Cruise Line Port → Polynesian | None (checker: moving the cut keeps it) |
| 08-22 | 009 | 9:30 AM | Dep | Beach Club → MCO | None (checker: a re-cut keeps it) |
| 08-22 | 004 | 10:24 AM | Arr | MCO → Endless Summer | None (checker: moving the cut keeps it) |
| 08-22 | 003 | 10:25 AM | Dep | Yacht Club → MCO | Move the cut |
| 08-22 | 10 | 10:30 AM | Dep | Caribbean Beach → MCO | Another car: end of car 004's morning |
| 08-22 | 006 | 11:00 AM | Dep | Old Key West → MCO | Move the cut |
| 08-22 | 12 | 12:02 PM | Arr | MCO → Polynesian (14-pax) | None (every 14-pax busy) |
| 08-22 | 14 | 1:30 PM | Dep | Cabana Bay → MCO | Another car: car 11's evening |
| 08-22 | 001 | 1:40 PM | Arr | MCO → Port Orleans Riverside | None (every car busy) |
| 08-22 | 002 | 1:51 PM | Arr | MCO → Wilderness Lodge | None (every car busy) |
| 08-22 | 001 | 11:02 PM | Arr | MCO → Grand Floridian | Extra driver (checker: fits car 009's evening after a re-cut) |
| 08-29 | 008 | 8:30 AM | Dep | Animal Kingdom Villas → MCO | Move the cut |
| 08-29 | 006 | 9:00 AM | Oth | Helios Grand → Fort Wilderness | Move the cut |
| 08-29 | 007 | 11:22 AM | Arr | MCO → Bay Lake Tower | Another car: car 15's morning |
| 08-16 | 003 | 9:23 AM | Arr | MCO → Grand Floridian | Move the cut: one driver |
| 08-16 | 11 | 10:45 AM | Dep | Bay Lake Tower → MCO | None (checker: one driver on car 11 keeps it) |
| 08-16 | 006 | 11:01 AM | Arr | MCO → Port Orleans | Move the cut: one driver |
| 08-16 | 004 | 11:14 AM | Arr | MCO → Bay Lake Tower | None (checker: one driver on car 004 keeps it) |
| 08-16 | 005 | 2:00 PM | Oth | Marriott Airport Lakeside → Riviera | Another car: car 12 (borderline) |
| 09-21 | 006 | 8:15 AM | Dep | Dolphin → MCO | Another car: car 16's morning |
| 09-21 | 17 | 9:57 AM | Arr | MCO → Beach Club | Checker: one driver on car 17 keeps it |
| 09-21 | 19 | 10:30 AM | Dep | Animal Kingdom Lodge → MCO | None (checker: poolable once 9:57 AM is fixed) |
| 09-21 | 003 | 12:45 PM | Dep | Caribbean Beach → MCO | Move the cut one job later |
| 09-21 | 15 | 4:39 PM | Arr | MCO → Beach Club | Extra driver on a free car |
| 09-17 | 16 | 2:53 PM | Arr | MCO → Dolphin | Checker: move the cut |
| 09-17 | 12 | 4:00 PM | Dep | Beach Club Villas → MCO | Move the cut |
| 09-09 | 12 | 11:35 AM | Arr | MCO → Art of Animation | Another car |
| 09-09 | 004 | 11:45 AM | Arr | MCO → Art of Animation | Extra driver on free car 005 |
| 09-09 | 003 | 11:48 AM | Arr | MCO → Port Orleans Riverside | Another car: car 16's morning |
| 09-09 | 15 | 12:48 PM | Arr | MCO → Port Orleans French Quarter | None (every car busy) |
| 09-09 | 16 | 2:27 PM | Arr | MCO → Grand Floridian | Extra driver on a free car |
| 08-26 | 004 | 7:00 PM | Oth | Signia Bonnet Creek → Contemporary | Extra driver on a free car |
| 08-26 | 16 | 8:12 PM | Arr | MCO → Animal Kingdom Lodge | Another car: car 17 (checker) |
| 08-26 | 004 | 10:30 PM | Oth | Contemporary → Signia Bonnet Creek | Extra driver on a free car |

## Appendix B — 2026-09-26 car by car (historical: wash-at-handover rule)
| Car | Shape | Morning | Handover | Evening ends | Jobs M / E |
|---|---|---|---|---|---|
| 001 SUV | Split | 2:55 AM → | 1:36 PM | 12:44 AM | 6 / 3 |
| 002 Minivan | Split | 4:40 AM → | 12:06 PM | 9:16 PM | 5 / 5 |
| 003 SUV | Split | 5:40 AM → | 11:26 AM | 9:36 PM | 4 / 4 |
| 004 14-pax | One driver (its lone 3:40 AM moves to 005) | | — | | 6 |
| 005 Van | Split | 4:10 AM → | 1:06 PM | 8:06 PM | 6 / 3 |
| 006 SUV | Split | 5:40 AM → | 2:22 PM | 11:55 PM | 5 / 4 |
| 007 SUV | Split | 5:55 AM → | 12:36 PM | 9:10 PM | 4 / 5 |
| 008 14-pax | One driver, trimmed | 7:30 AM → 7:08 PM | — | | 5 |
| 009 SUV | Split | 5:40 AM → | 12:21 PM | 10:16 PM | 4 / 4 |
| 10 Minivan | Split | 3:40 AM → | 2:00 PM | 1:58 AM | 5 / 3 |
| 11 14-pax | One driver | 4:10 AM → 3:14 PM | — | | 6 |
| 12 14-pax | Split | 4:40 AM → | 2:06 PM | 8:06 PM | 5 / 4 |
| 13 Towncar | Split | 3:10 AM → | 12:21 PM | 8:38 PM | 6 / 4 |
| 14 SUV | Split | 5:10 AM → | 12:06 PM | 9:30 PM | 4 / 4 |
| 15 14-pax | Split | 3:40 AM → | 10:06 AM | 8:06 PM | 4 / 5 |
| 16 SUV | Split | 5:40 AM → | 1:36 PM | 11:12 PM | 5 / 5 |
| 17 SUV | Split | 2:40 AM → | 2:21 PM | 1:46 AM | 9 / 6 |
| 18 SUV | Split | 5:10 AM → | 9:21 AM | 8:44 PM | 3 / 7 |
| 19 SUV | Split | 3:40 AM → | 2:36 PM | 1:06 AM | 8 / 4 |
