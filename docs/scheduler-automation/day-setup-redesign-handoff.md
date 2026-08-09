# Day Setup — audit + redesign (2026-08-09, uncommitted)

Founder brief: *"audit suggest day setup — it's hit or miss, and it's a lot of text, not
visual."* This is what the audit found, what changed, what was measured and rejected, and
what is still open.

**Status: NOT pushed.** Tests pass and the logic is unchanged, but it has never been
clicked through in the running app — see [Before you push](#before-you-push).

---

## 1. What the backtest found

23 built days (2026-06-23 → 08-06), that day's rows masked (`ignore_existing=True`),
compared against what the founder actually built.

| Measure | Result | Ship gate (2026-06) |
|---|---|---|
| Right driver (recall) | **79.8%** | 93.9% |
| Right car, all his crew | **52.2%** | 63.2% |
| Right car, *of people we both used* | **71.0%** | — |
| Cars on the road (count) | **MAE 0.52** | — |
| Warnings per open | 2.5 | — |
| Copy per open | ~487 words, no chart | — |

**The car count is excellent and was left alone.** The failure is the roster, and the noise.

Of the 55 drivers it missed:

| Cause | Count |
|---|---|
| More drivers than cars (`DAY_SETUP_SOLO_FIRST`) | **31** |
| Dropped by the peak cap | 14 |
| Below weekday work-rate | 7 |
| Schedule says OFF (hard gate, correct) | 3 |

**Roster errors cascade into car errors.** 07-11: the engine benched neuma; the founder used
neuma and gave him **#003 — sereen's own admin car**, so sereen moved to #007. One bad
benching produced two "wrong car" rows.

### The peak counted work we always farm out

`peak_concurrency` runs on every booked leg. On 07-12 it read **25 concurrent**; in-house has
never covered more than **13** at once. That inflated number fed:

- the `PEAK DEMAND:` banner (longest string in the modal);
- the per-size reservation targets — 07-11 asked for **19 towncar-capable and 16
  mini_van-capable units against a fleet of 13**, four of five sizes over-asking;
- four unactionable warnings a busy day (*"mini_van demand needs 20 units"* — we own two).
  Fired **24 times across 11 days**.

### Reason-chip reliability (car right, given the driver was right)

| Chip | Accuracy | Share of rows |
|---|---|---|
| `his car (set in admin)` | **82%** | 25% |
| `usual unit · N%` | **79%** | 27% |
| `covers <tier> demand (N legs)` | **56%** | **36%** |

The least reliable signal placed the most rows, wore the most confident wording, quoted the
day's *total* leg count so three rows read identically, and printed "covers towncar demand"
next to a Mini Van.

---

## 2. Two changes measured and REJECTED

Both were the obvious fix. Both made it worse. **Do not re-attempt without new evidence.**

| Change | Engine's own score | Accuracy vs founder |
|---|---|---|
| Global optimal matching (Hungarian) replacing greedy P2+P3 | **+3.4%** | **71.0% → 69.6%** |
| Delete the tier-reservation pass, let affinity assign everything | — | **71.3% → 68.6%** |

The matcher is not the bottleneck; the scoring is as good as its inputs allow, and optimising
it harder just fits the wrong objective more precisely. The tier pass earns ~3 points as a
tie-breaker, not as size logic — so **the machinery was kept and only its words were thrown
away**.

Consequence: **the assignment logic is byte-for-byte unchanged.** Recall 79.8%, precision
78.6%, pair 52.2% before and after. Every change below is about what it *says*, what it
*reports*, and how it *looks*.

---

## 3. Size demand was the wrong question

Founder's correction, and it is right: an SUV covers SUV work *and everything below it*, so
every car goes out — unless the day is quiet enough to leave one in. "How many of each size"
only ever mattered as a proxy for that.

`parkable_units(units, cumulative, overall)` asks it directly. Under nested compatibility the
fleet covers the day exactly when, for every size S, cars of size ≥ S outnumber peak
concurrent trips needing size ≥ S (Hall's condition — `cumulative` is already that left-hand
side). Park the least capable car first; stop when it breaks.

Validated on 21 days, run on **booked** legs only so it works on a day not yet built:

- **bias −0.19 cars, MAE 0.57** — errs toward sending cars out, the safe direction;
- says *"all cars out"* on 12 of 21 days;
- on the **9 days the founder used every car it agreed on 8** (07-06 it would have left the
  Towncar in);
- always parks the least capable first: **#13 Towncar, then #002 / #10 Mini Van**. Never an
  SUV while a Towncar is still out.

> Caveat: 2026-07-11's DVA rows were deleted from `content/db.sqlite3` mid-session (its 134
> legs remain), so it dropped out of the window. Earlier runs including it read 10 full-fleet
> days / 9 agreements. Cause not established — read-only scripts, tests use a separate DB, and
> the pollers are disabled under `shell`/`test`; most likely the app being driven locally
> (Reset All clears exactly this). Local dev data only — prod is Postgres.

---

## 4. What changed

### `dispatching/day_setup.py`

| Added | Purpose |
|---|---|
| `concurrency_series(date, legs, step_minutes=30)` | Hour-by-hour concurrency by type — the histogram the engine already built to size the roster and then threw away. Same legs, same estimator as `peak_concurrency`, so chart and headline can never disagree. |
| `parkable_units(units, cumulative, overall=0)` | Section 3. `overall` is a floor: an untyped leg needs a body but appears in no tier, so without it a day of untyped legs parks the whole fleet. Pinned by test. |
| `capacity` in the payload | `{fleet, staffed, idle[], can_park[], must_run, series[]}` — the UI draws from this instead of parsing prose. |
| `unit_options` per row | Ranked car list for the dropdown: free units first with fit %, then units held by someone with *who loses it*, cert-blocked excluded. |

| Changed | Why |
|---|---|
| `PEAK DEMAND:` banner deleted | Quoted a driver count taken *before* solo-first unchecked the car-less — on 07-12 it said 19 while the list showed 13. Replaced by structured `capacity` + one settled `Left available: …` line. |
| Per-size `need` capped at fleet capability | Stops impossible targets being reported as findings. **Changes no assignment** — the loop already stopped when the pool emptied. |
| Four per-size warnings → one cert-specific warning | Only a free unit nobody left is certified for is a real, fixable, tier-specific finding. Units/bodies running out is neither, and the settled headcount line says it once. |
| `covers <tier> demand (N legs)` chips relabelled | Honest per-pair reason: `same car as last shift` / `usual unit · N%` / `#009 Suv suits him better` / `his usual cars are taken` / `best fit`. Relabelled *after* all passes, so "a better car is free" is true when shown. |
| A parked car is offered to **one** row only | First cut told ernesto *and* Francisco "#009 suits him better" — two advertised fixes, one car. Highest scorer wins; the rest get the honest fallback. |
| Rows sort by **unit number**, not driver name | Founder's rule — the yard, the board and the printed sheet are all in unit order. `_unit_sort_key` is numeric-aware, so #10 lands after #009, never next to #002. Carless rows stay alphabetical at the end of their group. |

### `dispatching/templates/dispatching/daily_capacity_planner.html`

New `.ds-*` stylesheet block and a rewritten `dsRenderModal`:

- **coverage chart** — stacked by vehicle type in the board's own `--vtype-dot` hues, dashed
  line for staffed cars, verdict line above it;
- **ledger** — Trips / Cars out / Idle / Can stay in, replacing five paragraphs;
- **rows** — checkbox, name, one evidence chip, work-rate as tick marks (`works 7/8 recent
  Mondays` → eight ticks, seven lit), unit dropdown grouped *Free now* / *Would take it from
  someone*;
- **handback callouts moved onto the two rows they concern** (`← #001 returns`,
  `→ hands #10 back`) instead of amber sentence blocks at the top;
- **bench** section carries the park verdict; **off** collapses to one line.

The wire contract is unchanged — `.ds-row[data-driver-id]`, `.ds-check`, `.ds-veh`,
`.ds-count`, `.ds-cancel`, `.ds-resuggest`, `.ds-apply` all keep their names, so
`dsCollect`/`dsRefresh`/re-plan/Apply were untouched. **`apply_day_setup` is not modified.**

### Result

| | Before | After |
|---|---|---|
| Warnings per open | 2.5 | **0.1** |
| Copy per open | ~487 words | **~395 words + a chart** |
| Latency | 31 ms | 39 ms |
| Accuracy | 79.8 / 78.6 / 52.2 | **identical** |

### Tests

`dispatching/tests_day_setup.py`: **49 pass** (was 40). New: fleet cap suppresses impossible
warnings, park-least-capable-first, busy day parks nothing, untyped legs still need a body,
quiet day names the cars, series matches the peak, no `covers ` chip survives, unit-number
sort, one-offer-per-parked-car.

Rest of `dispatching`: 4 failures + 2 errors in `tests_fleet` / `tests_samsara` /
`tests_overnight_arrival` — **pre-existing**, identical counts with these changes stashed.

---

## Before you push

1. **Click it through locally.** Never rendered in the running app — there is no browser
   automation in this environment. It *was* rendered by extracting the real `dsRenderModal`
   and the real stylesheet and running them against a real payload, which is strong evidence
   but not the same thing. Check: chart draws, dropdown groups, Apply still writes, re-plan
   round-trips.
2. **Prod has 17 cars; this snapshot has 13.** The park line and the chart's capacity line are
   untested at 17 units. `_unit_sort_key` already handles #10–#17 numerically.
3. **Don't sweep in `schedule_board.html`.** That 10-line right-click/popup fix in the working
   tree is unrelated in-flight work — commit it separately.

## Still open

- **The 31-driver gap (`DAY_SETUP_SOLO_FIRST`).** The single biggest accuracy item, and a
  genuine trade-off, so it was left alone deliberately: solo-first won a coverage backtest
  (13 solo drivers built 113 in-house vs 110 with 15 sharing on 05-16) but loses against
  founder behaviour — he runs 14–16 bodies on 13 cars on half his busy days. Changing it
  changes what lands on real boards. **Founder's call.**
- **Recall drift 93.9% → 79.8%** since June. Not diagnosed beyond the four causes above.
- **ernesto-class mis-seating** (tier pass picks the unit, then hunts a driver, seating
  someone in an unfamiliar car while a car he actually drives sits parked). Now *flagged* in
  the chip and one click from fixed, but not fixed automatically — the two rejected
  experiments in §2 are evidence that chasing score does not improve accuracy.
