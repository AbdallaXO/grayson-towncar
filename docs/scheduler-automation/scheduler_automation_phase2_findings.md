# Scheduler Automation — Phase 2 Findings (Measurement)

**Date run:** 2026-05-30 · **Mode:** strictly read-only against the **live** production
database via the `readonly_local` role (`transaction_read_only=on`, SELECT-only grants).
No `apply`, no writes, no assignment changes — every "engine run" below is an in-memory
simulation. `find_swaps` is write-free. Live `SchedulerSettings` (pk=1) row was used for
all scoring magnitudes (per the Phase-2 instruction); key live values:
`inter_job_buffer=0`, `arrival_grace_minutes=10`, `idle_gap_threshold=120`,
`swap_max_depth=5`, `swap_time_limit_ms=5000`.

> Harness scripts (throwaway, read-only) live in `scratch/phase2_measure.py` and
> `scratch/phase2_stateA_swaps.py`; raw outputs in `scratch/phase2_results.json` and
> `scratch/phase2_stateA_swaps.json`.

---

## 1. Executive Summary

We compared, on five real historical days, the day **as your team actually ran it (State A)**
against the **best available automatic result (State B)** — `auto_assign_drivers` (the
clustered, all-at-once engine), run from scratch with every leg treated as an in-house
candidate, followed by an automatic `find_swaps` pass over the residual/farm legs.

**The headline reframes the Phase-1 hypothesis.** The biggest lever is the **build (Pass 1)**,
not the pre-farm swap pass (Pass 2):

1. **The automatic build alone covers more legs in-house than the manual board** — +38 legs
   across the 5 days (412 → 450; 67.8% → 74.0%). On the high-farm Saturday it was +16
   (85 → 101); on the cruise Sunday +11 (77 → 88).
2. **The Pass-2 swap pass adds very little on top of the automatic build** — only **10 legs
   across all 5 days** (the "(c−b)" recovery). Once the greedy build has packed the day, there
   is almost no slack left to swap. **94% of the legs the automatic build farms are *not*
   swap-recoverable** — they fail for lack of a compatible in-house driver with room, which no
   swap algorithm can fix (it's a fleet-capacity limit).
3. **But on YOUR current (manual) board, Pass-2 swaps have real, modest value** — `find_swaps`
   could pull **62 of 196 farmed legs (31%) back in-house (~12/day)**; filtering to genuinely
   worthwhile ones (≥$100 revenue, ≤2h resulting idle) leaves **~25 across 5 days (~5/day)**.
   So automating Pass-2 *as you work today* is worth a few clean saves per day — real, but
   second-order compared to adopting the automatic build.

**Three honest caveats that shrink the apparent build win** (details in §5, §8):

- **State B coverage is an upper bound.** It treats *every* leg as an in-house candidate,
  including legs your team farms **by choice** (affiliate relationships, service quality,
  special handling). The engine doesn't know those reasons, so some of its "extra coverage"
  is coverage you deliberately declined.
- **Higher coverage ≠ more revenue retained.** The auto-assign scorer has **no revenue term**
  (confirmed in Phase 1). On 2 of 5 days the engine covered *more legs* yet **farmed *more*
  revenue** than your team (e.g., busy Sat: engine kept 2 more legs in-house but farmed
  **$1,437 more** revenue). It packs in cheap legs and can farm the expensive ones.
- **Higher coverage comes with quality costs.** With `inter_job_buffer=0` and no whole-day
  quality cap, the engine builds **longer driver days** (avg span +1.5–2.5h, max spans of
  **18–24h**) and **more tight turns** (e.g., 11→22 on the high-farm day). These are exactly
  the Phase-1 gaps ("no whole-day quality objective") now quantified.

**Separate service-risk finding (not a coverage number).** Tier-only vehicle matching let
**9 assignments** across the 5 days exceed the physically assigned vehicle's capacity —
luggage, car-seat, or passenger — on **both** the human board (6) and the engine board (3).
The cleanest cases: a mini-van booking needing **3 child seats** assigned a mini-van that
fits **2**; a booking needing **4 forward-facing car seats** on a vehicle maxing **3**. One
severe/likely-data case: an 11-passenger / 14-pax-van booking sitting on a driver whose
recorded vehicle seats **5**. The scheduler cannot catch any of these.

**Bottom line for automation priority:** the build (Pass 1) is where the coverage is, but it
must be paired with a **revenue-aware objective**, a **hard day-length / tight-turn guard**,
and a **capacity check** before it could be trusted to run unattended. The pre-farm swap pass
(Pass 2) is a modest, second-order win (~5 clean saves/day on today's board) and shrinks to
near-zero if the automatic build is adopted.

---

## 2. Method, States, and Safety

| State | Definition |
|---|---|
| **A** | The day **as actually run** — real `leg.driver` assignments reconstructed from history. This is the target/ground truth. |
| **B (build)** | **Best available automatic build.** Mirrors `auto_assign_drivers` (`views.py:8501`, `apply=false`) exactly: clustered greedy (`suggest_assignments_clustered`) over the whole day's legs treated as unassigned, using each driver's **live** availability windows / preferences / max-hours. **This is the clustered all-at-once path, NOT your manual one-by-one `build_smart_schedule` build path** — so a build-path difference should not be read as the engine "underperforming." |
| **B (after swap)** | State B build + an automatic `find_swaps` pass over the residual/farm legs (highest-revenue first), applying each feasible swap chain in memory before the next search. |

**Coverage ladder (per the Phase-2 spec):** for each day we report
`a` = State A coverage, `b` = State B build coverage, `c` = State B after-swap coverage, and
the three labeled deltas: `a−b` (total gap), `c−b` (legs the swap pass recovers — the free
win), `a−c` (residual gap = the Pass-1 "better driver day" scoring problem).

**Safety:** connected as `readonly_local`; the harness `assert`s the DB user before running
and never calls `.save()/.create()/.update()`. Writes are physically impossible at the DB
(role grants + `default_transaction_read_only=on`). Nothing about production data, code, or
assignments was changed.

**What the metrics mean (all from the engine's own helpers, identical logic for A and B):**
- *Deadhead* = sum of inter-job reposition drive-time minutes (`_recalculate_timing_details`
  `reposition_drive_time`). It is a **proxy** (category drive-time estimates; there is no true
  mileage in the system) — stated explicitly per the instructions.
- *Idle* = sum of positive between-job buffer minutes; *big gaps* = buffers ≥ 120 min.
- *Tight turns* = 0 ≤ buffer < 15 min; *Impossible turns* = buffer < 0 (model-relative — see §8).
- *Span* = first pickup → last estimated end, per driver.
- *Constraint violations* = vehicle-tier mismatch + availability-window violations. **Capacity
  is tracked separately** (§7), per instruction.

---

## 3. Test Days (as approved)

| Archetype | Date | DOW | Legs | In-house drivers |
|---|---|---|---|---|
| Normal weekday | 2026-04-23 | Thu | 88 | 13 |
| Busy weekend | 2026-05-09 | Sat | 148 | 13 |
| Port-Canaveral-heavy | 2026-03-29 | Sun | 131 | 12 |
| Airport-heavy | 2026-05-21 | Thu | 92 | 16 |
| High-farm / swap-rich | 2026-05-02 | Sat | 149 | 13 |

State A in-house counts reproduced the survey exactly (integrity check passed).

---

## 4. Coverage Ladder — A vs B (build) vs B (after swap)

| Day | Legs | **a** = A | **b** = B build | **c** = B after-swap | **a−b** total gap | **c−b** swap recovers | **a−c** residual gap |
|---|---|---|---|---|---|---|---|
| Normal weekday (04-23) | 88 | 71 (80.7%) | 83 (94.3%) | 83 (94.3%) | **−12** | **0** | −12 |
| Busy weekend (05-09) | 148 | 97 (65.5%) | 95 (64.2%) | 99 (66.9%) | **+2** | **+4** | −2 |
| Port-Canaveral (03-29) | 131 | 77 (58.8%) | 88 (67.2%) | 90 (68.7%) | **−11** | **+2** | −13 |
| Airport-heavy (05-21) | 92 | 82 (89.1%) | 83 (90.2%) | 86 (93.5%) | **−1** | **+3** | −4 |
| High-farm (05-02) | 149 | 85 (57.0%) | 101 (67.8%) | 102 (68.5%) | **−16** | **+1** | −17 |
| **Total** | **608** | **412 (67.8%)** | **450 (74.0%)** | **460 (75.7%)** | **−38** | **+10** | **−48** |

**How to read this:**
- `a−b` is **negative on 4 of 5 days** → the automatic *build* covers **more** in-house than
  your manual board (only the busy Saturday had the engine slightly behind, +2). The Phase-2
  framing assumed manual ≥ automatic; the data shows the opposite on coverage count.
- `c−b` (the swap-pass "free win") is **tiny** — 0–4 legs/day, **10 total**. The greedy build
  leaves almost nothing for swaps to recover.
- `a−c` being negative everywhere means there is **no positive "manual beats automatic"
  residual gap** in coverage terms. The Pass-1 "better driver day" problem does **not** show up
  as the engine under-covering — it shows up as **worse schedule quality** at higher coverage
  (next section). Read `a−c` here as "how far automatic exceeds manual on count," not as a
  quality deficit.

---

## 5. Operational Metrics — A vs B (after-swap)

Per-day totals across all in-house drivers. (B = State B after-swap, the full automatic result.)

| Day | State | In-house legs | Deadhead/leg (proxy) | Idle min | Tight turns | Impossible turns | Span avg / max (h) | Farm count | **Farm revenue** |
|---|---|---|---|---|---|---|---|---|---|
| 04-23 | A | 71 | 22.5 | 2,528 | 15 | 8 | 10.6 / 13.1 | 17 | $1,230 |
| 04-23 | B | 83 | 18.2 | 3,404 | 20 | 5 | 13.1 / **18.0** | 5 | $1,065 |
| 05-09 | A | 97 | 21.0 | 2,175 | 20 | 19 | 12.4 / 15.6 | 51 | $5,826 |
| 05-09 | B | 99 | 20.8 | 2,545 | **31** | 8 | 13.2 / 16.9 | 49 | **$7,263** |
| 03-29 | A | 77 | 22.9 | 2,515 | 18 | 10 | 12.7 / 17.4 | 54 | $4,795 |
| 03-29 | B | 90 | 23.2 | 2,540 | 18 | 15 | 14.7 / **24.2** | 41 | $3,190 |
| 05-21 | A | 82 | 19.4 | 3,901 | 14 | 10 | 11.1 / 23.1 | 10 | $944 |
| 05-21 | B | 86 | 18.5 | 3,274 | **22** | 4 | 11.1 / 24.1 | 6 | $999 |
| 05-02 | A | 85 | 20.2 | 2,539 | 11 | 13 | 10.8 / 14.2 | 64 | $3,653 |
| 05-02 | B | 101→102 | 18.6 | 2,317 | **22** | 16 | 12.8 / 18.0 | 47 | **$4,053** |

**Reading the quality story:**
- **Deadhead/leg is roughly flat or slightly lower** for the engine — the proximity/chain
  scoring does keep repositioning down even at higher coverage. (Proxy only; no true mileage.)
- **Tight turns rise** under the engine on 3 of 5 days (e.g., 11→22 on 05-02; 14→22 on 05-21)
  — a direct consequence of `inter_job_buffer=0` and no whole-day quality cap. It packs jobs
  closer than your team does.
- **Span blows out**: engine avg spans run +1.5–2.5h, with **max spans of 18–24h**. The 24.2h
  span on 03-29 (B) is physically impossible for one driver — the engine only applies a *soft*
  span penalty, never a hard cap, and does not hard-enforce `max_hours`. (Note 05-21 State A
  also shows a 23.1h max span — that one is a **data artifact**, an overnight leg pair on one
  `pickup_date`; see §8.)
- **Revenue retained is worse on the busy/high-farm days.** Because auto-assign has **no
  revenue term**, on 05-09 the engine kept 2 more legs in-house yet farmed **$1,437 more**
  revenue, and on 05-02 it kept 17 more legs yet still farmed **$400 more** revenue than your
  team. "More legs in-house" is not the same as "more money in-house."
- **Impossible turns** are model-relative and noisy (State A — the real board — shows 8–19 of
  them, which obviously *ran*); see §8. A-vs-B is comparable, literal counts are not.

---

## 6. Headline — Pass-2 Swap Recovery (the size of the prize)

### 6a. As specified: legs the **automatic build (State B)** farmed that `find_swaps` recovered

| Day | B-build residuals | **Recovered by swap** | No feasible swap | Cascade depths used |
|---|---|---|---|---|
| 04-23 | 5 | **0** | 5 | — |
| 05-09 | 53 | **4** | 49 | 2, 3, 4, 6 |
| 03-29 | 43 | **2** | 41 | 1, 3 |
| 05-21 | 9 | **3** | 6 | 1, 2, 3 |
| 05-02 | 48 | **1** | 47 | 3 |
| **Total** | **158** | **10** | **148** | up to 6 |

**Only 10 of 158 farmed legs (6%) were swap-recoverable on the engine's board**, and several
are low-quality:
- A **6-move cascade** to save one $275 SUV leg (05-09) — would you ever shuffle 6 drivers for one leg?
- Two "recoveries" are **00:38 / 00:40 overnight legs** slotted with **351–374 min idle
  buffers** (technically feasible, operationally absurd).
- Several recovered legs are **$0 revenue**.

The reason the number is so low: the greedy build already consumed the slack, and the
remaining 148 residuals are **capacity-bound** (no compatible in-house driver has room) —
unfixable by swapping.

### 6b. Supplement — Pass-2 swaps over **your actual hand-built board (State A)**

This directly answers "on the board as you build it today, how many farmed legs could a swap
pull in?" — the practical Pass-2 prize. The human board leaves more slack than the greedy
build, so the number is higher:

| Day | Farmed (State A) | **Swap-recoverable** | % | Clean (≥$100 & ≤2h idle) | Depths |
|---|---|---|---|---|---|
| 04-23 | 17 | 12 | 70% | 1 | 1,2 |
| 05-09 | 51 | 9 | 17% | 4 | 1,2,4 |
| 03-29 | 54 | 12 | 22% | 5 | 1,2,6 |
| 05-21 | 10 | 7 | 70% | 2 | 1,2,4 |
| 05-02 | 64 | 22 | 34% | **13** | 1,2,3,4,6 |
| **Total** | **196** | **62 (31%)** | | **~25** | up to 6 |

So the realistic Pass-2 prize on today's board is **~12 mechanically-recoverable legs/day,
~5 of them clean and worth executing**. It is highest on the high-farm Saturday (13 clean
saves) and lowest, proportionally, on the genuinely capacity-bound busy Saturday (17%). Note
many of these saves are also captured automatically *if the build is automated* — see the gap
between 6a (10) and 6b (62): most of that 62 is absorbed by the automatic build, not the swap.

---

## 7. Separate Finding — Capacity Overruns (service risk, not a coverage metric)

Vehicle matching is **tier-only**; the scheduler never compares booked party size / luggage /
car-seats against the assigned vehicle's physical capacity. Live per-type capacities:

| Type | Seats | Luggage | Car-seat cap |
|---|---|---|---|
| towncar | 4 | 3 | 1 |
| mini_van | 5 | 5 | 2 |
| suv | 6 | 6 | 4 |
| van | 10 | 11 | 4 |
| Van(14 Pax) | 14 | 14 | 4 |

Across the 5 days, **9 in-house assignments exceeded the assigned vehicle's physical capacity**
— 6 on the human board (State A), 3 on the engine board (State B). Specific bookings:

| Day | State | Leg / Res | Time | Booked type | Assigned seats | Party | Overrun |
|---|---|---|---|---|---|---|---|
| 04-23 | A | 16643 / 9679 | 11:00 | van | 6 (SUV) | 6 pax, 9 luggage | **luggage 9 > 6** |
| 04-23 | A | 14534 / 8430 | 11:30 | mini_van | 5 | 5 pax, 1ff+2b seats | **3 child seats > 2** |
| 04-23 | B | 8279 / 4821 | 16:13 | van | 10 | 7 pax, 3 boosters | **boosters 3 > 2** |
| 05-09 | A | 9123 / 5317 | 09:00 | Van(14) | 14 | 12 pax, 4 ff seats | **ff car-seats 4 > 3** |
| 03-29 | A | 14370 / 8331 | 00:38 | Van(14) | **5** | **11 pax**, 10 luggage | **pax 11 > 5; luggage 10 > 5** |
| 03-29 | B | 12261 / 7118 | 08:15 | mini_van | 5 | 5 pax, 1rf+1ff+1b | **3 child seats > 2** |
| 05-02 | A | 17398 / 10125 | 12:10 | mini_van | 5 | 3 pax, 6 luggage | **luggage 6 > 5** |
| 05-02 | A | 9122 / 5317 | 13:09 | Van(14) | 14 | 12 pax, 4 ff seats | **ff car-seats 4 > 3** |
| 05-02 | B | 14831 / 8604 | 12:05 | mini_van | 5 | 5 pax, 1rf+2b | **3 child seats > 2** |

**Notes:**
- The **car-seat overruns** (res 8430, 7118, 8604) are the purest illustration of the tier-only
  blind spot: vehicle *type* matches perfectly (mini-van booked → mini-van assigned), but the
  child-seat demand exceeds what the vehicle physically holds. Tier matching has no way to see this.
- The **luggage overruns** and the **van→SUV** case (16643) occur where the assigned physical
  vehicle is smaller than the booked type — partly a tier/data question (see §8): the driver's
  recorded `DriverVehicleAssignment → FleetVehicle` capacity is below the booked type.
- **Res 8331 is severe and likely also a data issue**: a 14-pax-van booking (11 passengers)
  sitting on a driver whose recorded vehicle seats 5. Worth a manual check of that day's vehicle
  assignment regardless of scheduling.
- **Res 5317 recurs** (05-09 and 05-02) — a systematically mis-sized recurring reservation
  (4 forward-facing car-seats vs a 3-max vehicle). Worth fixing at the booking level.

Passenger overruns are rare because the tier order *is* passenger-capacity-monotonic
(4<5<6<10<14); the real exposure is **luggage and child seats**, which tiers do not track.

---

## 8. Data-Quality Flags & Limitations

These bound how literally the numbers should be read. None invalidate the headline; several
should be cleaned up before any Phase-3 build.

1. **Window violations are unreliable (stale historical availability).** Availability-window
   violations are high on **both** boards — e.g., 03-29 State A (the *real* board) = 44, State
   B = 56; 05-09 A=25, B=24. Since the **human** board violates windows heavily, this almost
   certainly reflects incomplete/stale `DriverWeeklySchedule`/`DriverDateOverride` data for past
   dates (or drivers routinely working off-nominal hours), not a real engine fault. A and B are
   similar, so it is **not** an engine-vs-human differentiator. Treat the absolute counts as
   noise; do not use them to judge either schedule.
2. **Impossible/tight-turn counts are model-relative.** State A (which actually ran) shows
   8–19 "impossible" turns/day — these reflect estimate conservatism (category drive times;
   flights that landed earlier than estimated; `inter_job_buffer=0`), not real failures.
   A-vs-B deltas are meaningful; absolute counts are not literal.
3. **Span outliers / no hard day cap.** Max spans of 18–24h (both states on some days) come
   from overnight or very-early legs landing on one `pickup_date`, plus the engine's lack of a
   **hard** day-length / `max_hours` cap (only a soft span penalty). The 24.2h engine span
   (03-29) is not a schedule a human would run.
4. **State B coverage is an upper bound.** It offers *every* leg to the in-house pool, ignoring
   the business reasons your team farms some legs (affiliate commitments, service quality,
   capacity needs the engine can't see). Real adoptable coverage is somewhere between A and B.
5. **No revenue/profit objective in auto-assign** (Phase-1 confirmed) — so engine coverage
   gains can come with *worse* revenue retention (§5). Any "coverage %" comparison must be read
   alongside farm **revenue**, not just count.
6. **Deadhead is a proxy** (category drive-time minutes); there is no true mileage in the system.
7. **Swap budget is the live one** (`swap_max_depth=5`, 5,000 ms / 5,000 iterations). A larger
   budget might find a few more deep-cascade recoveries, but those (depth 4–6) are operationally
   impractical anyway.
8. **Static, single-realization.** Greedy output is order-dependent; this is one realization per
   day, not a distribution. No code was executed against production logic in write mode.

---

## 9. What the Data Implies for Automation Priority (no code proposed — measurement only)

Stated as implications of the numbers, **not** as a Phase-3 plan (awaiting your go-ahead):

- **The build (Pass 1) is the real lever, not the swap pass (Pass 2).** The automatic build
  closes ~38 legs of coverage vs manual; the swap pass on top adds only ~10. If you want fewer
  farm-outs, trust/assist the *build*, not a pre-farm swap loop.
- **An automatic build is not yet trustworthy unattended** without three guards the data shows
  it lacks: (a) a **revenue/profit-aware objective** (it currently farms money), (b) a **hard
  day-length / tight-turn cap** (it builds 18–24h days and packs tight turns at
  `inter_job_buffer=0`), and (c) a **capacity check** (luggage/child-seat overruns).
- **Pass-2 swap automation is a modest, second-order win** — ~5 clean saves/day on your current
  manual board, shrinking toward zero if the build is automated. Worth a lightweight assist
  (surface the handful of clean swaps before farming), not a major build.
- **A large share of farm-outs is fleet-capacity-bound, not algorithmic** — 148 of 158 engine
  residuals and 134 of 196 manual-board farmed legs had *no* feasible in-house placement at all.
  Beyond a point, the lever is fleet size / vehicle-type mix, not scheduling logic.
- **Capacity overruns are a live service risk today** (on the human board too) — independent of
  any automation decision, the recurring car-seat/luggage mismatches (esp. res 5317, res 8331)
  are worth a booking-time or assignment-time check.

---

## 10. Reference — Raw Numbers & Provenance

- **Days:** 2026-04-23 (normal wkdy), 2026-05-09 (busy Sat), 2026-03-29 (cruise Sun),
  2026-05-21 (airport Thu), 2026-05-02 (high-farm Sat).
- **Totals:** 608 legs; A in-house 412 (67.8%); B-build 450 (74.0%); B-after-swap 460 (75.7%).
- **Pass-2 prize:** State-B residuals recovered 10/158 (6%); State-A farmed recovered 62/196
  (31%, ~25 clean).
- **Capacity overruns:** 9 (6 State A, 3 State B).
- **Live `SchedulerSettings`:** `inter_job_buffer=0`, `arrival_grace_minutes=10`,
  `idle_gap_threshold=120`, `load_balance_multiplier=5`, `retention_bonus=35`,
  `swap_max_depth=5`, `swap_time_limit_ms=5000`, `swap_max_iterations=5000`.
- **Engine entry points exercised (read-only):** `suggest_assignments_clustered`
  (`dispatching/scheduler.py:939`), `build_driver_schedules` (`:724`),
  `_recalculate_timing_details` (`:1853`), `check_feasibility` (`:614`),
  `find_swaps` (`dispatching/swap_optimizer.py:236`); availability via
  `drivers/availability.py:resolve_effective_availability`; capacity via
  `rates.Vehicle` / `DriverVehicleAssignment`.
- **Harness (read-only, in `scratch/`):** `phase2_measure.py`, `phase2_stateA_swaps.py`;
  outputs `phase2_results.json`, `phase2_stateA_swaps.json`. DB user asserted `readonly_local`.

*Phase 2 is measurement only. No Phase-3 work or code changes performed. Awaiting direction.*
