# Scheduler Automation — Phase 1 Findings

Investigation of the in-house scheduling automation in **grayson-towncar** (Django 5.1 booking/dispatch app). Domain recap: a `Reservation` has one or more `Leg`s (point-to-point trips with pickup/dropoff, time, vehicle type, passenger/luggage counts). Each leg is covered **in-house** (company `Driver` + `FleetVehicle`) or **farmed out** to an affiliate. Dispatchers build each in-house driver's day ("Pass 1") and hunt last-minute swaps before farming ("Pass 2").

This report is grounded in a read of `dispatching/scheduler.py`, `dispatching/swap_optimizer.py`, `dispatching/views.py`, `dispatching/models.py`, `reservations/models.py`, and the `daily_capacity_planner.html` template, plus adversarial verification of the seven highest-stakes claims. Where a verifier verdict conflicted with a reader's claim, the verdict is preferred and the correction is reflected below.

---

## 1. Executive Summary

**What the engine already does.** The system has two real, working schedule *builders* plus a *swap* search — none are mere re-scorers of human placements:

- **Per-driver from-scratch builder** — `build_smart_schedule` (`dispatching/scheduler.py:1572`) fills an entire empty (or partial) day for ONE driver: it inserts pinned legs first, then greedily adds the highest-tier feasible legs whose score > 0. The from-scratch/empty-day path is real and exercised by the caller (`dispatching/views.py:9128`, with `existing_schedule=None`).
- **Date-wide multi-driver assigner** — `auto_assign_drivers` (`dispatching/views.py:8501`) runs `suggest_assignments_clustered` (`scheduler.py:939`) over ALL unassigned legs for a date against ALL in-house drivers with a vehicle, in a single action. It writes only when called with `apply=true`; the default is preview/suggest.
- **Cascading swap search** — `find_swaps` (`dispatching/swap_optimizer.py:236`) does a genuine multi-leg, depth-up-to-5 displacement search to free room for ONE target leg.
- **Human-in-the-loop safety** — versioned `ScheduleSnapshot` undo (`reservations/models.py:2657`), atomic apply, and a 60s capacity-planner cache that every write path invalidates.

**What it does NOT do.** There is **no global optimization** anywhere — both builders and the swap search are pure forward greedy with no backtracking, no LP/Hungarian/assignment-matrix, and no loop-to-convergence (no `scipy`/`ortools`/`networkx` imports exist). There is **no passenger/luggage/car-seat capacity check** — vehicle matching is a pure tier hierarchy. There is **no arrival<->return pairing** — Pass 2 is realized only as generic same-tier leg reassignment. The swap search is **not wired into any automatic pre-farm pipeline**; it fires only from the human "Find Swaps" button. There is **no automatic farm-out write** — residual legs are flagged but a human must assign an affiliate. There is **no whole-day quality objective** and **no persistent lock** on any assignment.

**Why manual scheduling persists.** The engine *suggests*; humans *decide and commit*. Every algorithmic path defaults to a preview that requires a second, explicit click to write. The hardest judgment calls — which alternate makes a cleaner day, when to accept a tight turn, when to farm vs. swap, whether a vehicle truly seats the passengers/luggage — are either not modeled at all (capacity, arrival/return pairing, whole-day quality) or are encoded only as greedy per-leg scoring biases that a dispatcher must eyeball and correct.

**Single highest-value next automation step.** Wire the existing swap search into an **automatic pre-farm swap pass**: before presenting the farm list, iterate the residual (`suggested_driver_id=None`) legs and call `find_swaps` on each, surfacing the resulting in-house save opportunities. All the machinery exists (`find_swaps`, scoring, feasibility); only the orchestration loop and a batch UI are missing. This directly attacks the metric the business cares about (farm-outs) using already-trusted, read-only logic.

---

## 2. Current Engine Capability Map

| Capability | Exists today? | File / function | How it works | Human still required? | Gap |
|---|---|---|---|---|---|
| Build full schedule for one driver | **Yes** | `dispatching/scheduler.py:1572` `build_smart_schedule`; helper `:2089` `_add_leg_to_schedule` | Resolves driver tier, filters legs to window + compatible types, inserts pinned legs first (`:1684-1693`), then greedily adds highest-tier feasible legs scoring > 0 (`:1795-1820`). From-scratch path confirmed (`existing_schedule=None` -> empty start, `:1642`). | Yes — dispatcher sets window/preference/pins and clicks Preview then Apply (`views.py:9044`) | Single forward pass; never reconsiders or swaps out a placed leg; a bad early pick locks in |
| Build full schedule for all drivers globally | **Partial** | `views.py:8501` `auto_assign_drivers` -> `scheduler.py:939` `suggest_assignments_clustered` -> `:996` `suggest_assignments` | One endpoint processes ALL drivers (`views.py:8559-8569`) + ALL unassigned legs (`8623-8635`) in a single clustered call. "Global" in **scope**, NOT in optimality. | Yes — preview then "Apply Selected" | Cross-driver per-leg **greedy** (best driver per leg, simulated forward), not joint/LP optimization; processing order determines outcome; placed legs never revisited |
| Assign unassigned legs automatically | **Yes (suggests; writes on apply)** | `views.py:8660-8699` (apply) vs `8701+` (preview); engine `scheduler.py:996` | 3-pass scarcity order (vehicle-scarce, time-scarce, rest), picks max-score feasible driver per leg, simulates into working copies. `apply=true` snapshots then saves `leg.driver` per leg. | Yes — `apply=true` only sent by "Apply Selected" after preview | Each leg saved individually (no `bulk_update`/single txn); mid-loop failure leaves partial state (snapshot mitigates) |
| Optimize existing assignments | **No (not as a holistic re-optimizer)** | n/a — only `excluded_leg_ids` re-fill plumbing in `build_smart_schedule:1582` | A dispatcher can drop legs (exclude) and re-preview to let the greedy re-fill; there is no routine that takes a finished board and improves it in place. | Yes — manual exclude + re-preview loop | No board-wide improvement pass; no "make this day cleaner" optimizer |
| Suggest alternate jobs while building a driver day | **Yes** | `build_smart_schedule` returns an `alternatives` list (vehicle-compatible unassigned legs); rendered in builder modal (`views.py:9044`) | Other compatible unassigned legs are returned alongside the built chain; dispatcher pins/excludes and re-previews to swap one in. | Yes — manual pin/exclude + re-preview; not automatic | Alternates are listed, not auto-evaluated for "which is better"; no ranked what-if |
| Find arrival/return swaps | **No** | `swap_optimizer.py:236-521` (no trip-type pairing); arrival grace only at `scheduler.py:644-650` | No arrival/return identification or pairing exists. `trip_type` surfaces only as `ARRIVAL_GRACE_MINUTES` timing leeway inside `check_feasibility`, not as pairing logic. | Yes — dispatcher conceptually picks the target leg representing the intent | The documented Pass-2 "arrival<->return" optimization is **not implemented**; approximated only as generic same-tier reassignment |
| Find same-vehicle-type swaps | **Partial (tier, not strict)** | `swap_optimizer.py:100-105` `_vehicle_compatible` -> `scheduler.py:100-105` `get_compatible_vehicle_types` | Tier-compatible (own tier + all lower), NOT strict equality. Exact-type match is only a sort preference (`:387-392`) and scoring bonus `swap_tier_bonus` (`:220-229`). | No for the rule; Yes to invoke | A higher-tier driver can be pulled onto a lower-tier leg; not literally "same type" |
| Find cascade/chain swaps | **Yes** | `swap_optimizer.py:288-314` (iterative deepening), `:455-517` (recursive displacement) | Iterative-deepening DFS to `max_depth` (default `swap_max_depth=5`, `models.py:115`). Displacing a slot recurses to rehome the bumped leg on another driver — true multi-leg cascade, depth > 2 reachable. `visited` set prevents cycles. | Yes — per target leg, staff-triggered | Stops at first/shallowest successful depth (`:313-314`); caps at 20 raw / 10 returned; not exhaustive; **does not loop to convergence** |
| Reduce farm-outs | **Partial / manual** | `find_swaps` (read-only); `execute_takeback` `views.py:12108` | Swap search and single-leg affiliate takeback exist, but no automatic pre-farm pass scans residuals to pull legs in-house. | Yes — entirely human-driven (per-leg button) | No batch "save these from farming" automation; the highest-value gap |
| Detect impossible turns | **Yes** | `scheduler.py:614` `check_feasibility`, `:687-694`, `:705-714` | Computes earliest-available = prev_end + reposition drive + buffer; **negative buffer => feasible=False (hard reject)**. | No (automatic) | Checks only the adjacent preceding/following slot, not whole-chain ripple; relies on category-level drive-time estimates, not live routing |
| Detect tight turns | **Yes (warn only)** | `scheduler.py:687-694`, `705-714` | Buffer < 15 min => **warning only**, leg still allowed. Arrivals get a grace window. | No to flag; Yes to accept/reject the risk | Tight is non-blocking; mis-estimated drive times can make a "tight" turn actually impossible |
| Check driver availability | **Yes (upstream of engine)** | `drivers/models.py:190/199/213` `get_effective_availability`/`get_availability_for_date`/`get_full_availability` → `drivers/availability.py:113` `resolve_effective_availability`; used in `auto_assign_drivers` (`views.py:8577,8584`); cluster windows `assign_drivers_to_clusters:847` | View resolves each driver's `(is_available, start_hour, end_hour, flexible, max_hours)` and passes `driver_hours`/`driver_max_hours` windows into the clustered engine. Availability is NOT resolved inside `scheduler.py`. | Partially — engine respects windows; human sets/overrides | Engine itself reads only the resolved window tuple, not the raw availability model |
| Check driver time-off / exceptions | **Yes (upstream)** | `drivers/availability.py:113` `resolve_effective_availability` (+ `_pick_active_exception:55`); window check `is_pickup_within_window:290` | Layers the active **approved** `DriverDateOverride` exception over the weekly `DriverWeeklySchedule`; `exception_type='off'` forces unavailable and partial-day windows attach — all before the window tuple reaches the engine. | Partially | Folded into the availability window; no per-exception reasoning inside the scheduler/swap engines |
| Check vehicle type/capacity (type) | **Yes (type only)** | `scheduler.py:90` `get_vehicle_tier`, `:100` `get_compatible_vehicle_types`; enforced `:1221-1226`, `:1663-1666` | `VEHICLE_TIER_ORDER = [towncar, mini_van, suv, van, Van(14 Pax)]`; driver serves own tier + all below. Unknown type => allow all. | No for the gate; Yes to verify appropriateness | Strict downward hierarchy assumption; pinned legs bypass the filter (`:1670-1671`) |
| Check car seat / luggage / passenger fit | **No** | Slot fields `scheduler.py:279-282`, populated `:808-811`; Leg data `reservations/models.py:891,896,907-930` | Passenger/luggage/car-seat values are read into the slot for **display only** and never compared to any vehicle capacity in feasibility, compatibility, or scoring. | **Yes** — human must verify seating/luggage/car-seat fit | No seating/luggage capacity model; a 6-pax booking could be tier-compatible with an SUV that does not seat 6 |
| Balance workload between drivers | **Partial (scoring bias)** | `suggest_assignments` `scheduler.py:1348-1351` | Exponential load-balance penalty `load_balance_multiplier * (n_jobs ** load_balance_exponent)` as a driver accumulates jobs. | Yes — only biases greedy choices | A soft penalty, not a constraint; greedy order can still produce uneven days |
| Reduce deadhead | **Partial (proxy only)** | `scheduler.py:1283-1293` (proximity), `1325-1339` (backward chain) | Same-area/close repositioning bonus and backward-chain bonus approximate deadhead. The literal token "deadhead" appears **nowhere** in `scheduler.py`. | Yes — dispatcher eyeballs the timeline | No explicit deadhead-mileage minimization objective; approximated via category drive-time estimates |
| Score driver schedule quality | **Partial** | `suggest_assignments` inline scorer `scheduler.py:1242-1414`; `_score_leg_for_smart_schedule:1984-2086` | Per-leg additive scores: buffer band, tier, scarcity, proximity, flow, chain, load balance, idle gap, span, trip preference. `_score_leg_for_smart_schedule` adds **revenue** but has **no** idle-gap/load-balance/span terms. | Yes | It is a **per-leg** score summed greedily, not a holistic day score |
| Score whole-day schedule quality | **No** | n/a | No function scores a complete driver-day (or the whole board) as a single quality number. Quality is only the sum of greedy per-leg scores at placement time. | Yes — human judges the finished day | No standalone day/board quality metric to compare schedules or measure the engine |
| Produce final farm list automatically | **Partial** | Residuals `scheduler.py:1503-1511` (`suggested_driver_id=None`); `get_coverage_stats:2142`; UI `daily_capacity_planner.html:1506-1518` | The residual **list** is automatic: any leg the greedy can't fit emits a `None` suggestion ("No in-house driver available") and `get_coverage_stats` buckets `leg.driver==None` as `unassigned`. | Yes to actually **farm** | Engine never auto-creates affiliate assignments; converting a residual to FARMED is a manual "Assign to affiliate" action (`views.py:2017-2027`) |

---

## 3. Pass 1 Build Analysis

**Can it build Anthony's full day from scratch?** **Yes.** `build_smart_schedule` (`scheduler.py:1572`) is a genuine from-scratch single-driver day builder, not a re-scorer of human placements. The verifier confirmed: the signature takes one `driver_id`, the `existing_schedule` is optional, and when none is passed `existing_slots = [] ` (`:1642`) so the working `DriverDaySchedule` starts empty (`:1645-1650`). It inserts pinned legs first with feasibility checks (`:1684-1693`), then greedily adds the highest-tier feasible legs whose score > 0 (`:1795-1820`), re-checking feasibility against the growing chain. The caller supplies `available_legs` = unassigned legs and can pass `existing_schedule=None` (`views.py:9114-9139`), exercising the empty-day path. So a dispatcher can hand it Anthony with an empty day and get a complete proposed chain.

**Can it build all drivers globally?** **In scope, yes; in optimality, no.** `auto_assign_drivers` (`views.py:8501`) is a single endpoint that builds schedules for all in-house drivers with a vehicle assignment and runs one `suggest_assignments_clustered` call over the whole unassigned pool for the date (`views.py:8559-8569, 8623-8638`). But the verifier was emphatic: this is **not** global/joint optimization. It is a single forward **greedy** that is *cross-driver per leg* — for each leg it scans all drivers, scores each, picks the best, and simulates that pick forward (`scheduler.py:1204, 1420-1423, 1491-1492`) so later legs see updated chains. There is no LP, Hungarian, or backtracking (no `scipy`/`ortools`/`networkx`/`linprog` imports), and once a leg is placed it is never reconsidered. The 3-pass scarcity ordering and sort keys, not an optimizer, determine the outcome.

**Or only optimize human-placed legs?** No — it builds, it does not merely re-score. But it also does **not** re-optimize a finished board: there is no holistic "improve this existing schedule" pass (capability "Optimize existing assignments" = No). The only re-arrangement plumbing is `excluded_leg_ids` re-fill in the builder.

**Where does the dispatcher still choose jobs manually, and why?**
- **Per-driver window, preferred trip type, and pinned legs** before each `build_smart_schedule` run (`views.py:9074-9085`, `scheduler.py:1577-1582`) — the engine has no autonomous policy for these and they heavily bias the greedy fill.
- **Swapping in an alternate for a cleaner day** — the builder returns an `alternatives` list, but choosing which alternate is better is a manual pin/exclude + re-preview loop; nothing auto-evaluates the alternatives.
- **Capacity sanity** — because no passenger/luggage/car-seat check exists, the dispatcher must verify the chosen tier actually seats the party.
- **Accepting tight turns** — `check_feasibility` only warns on < 15 min buffers; the human decides whether to accept the risk.

**Does it know what makes a "better driver day," or only coverage?** It knows *more than* raw coverage but *less than* true day quality. The greedy explicitly prefers cleaner chains (proximity `:1283-1293`, backward-chain `:1325-1339`, flow bonuses `:1295-1314`), penalizes idle gaps (`:1353-1378`), over-long spans (`:1380-1393`), and unbalanced load (exponential, `:1348-1351`), and `_score_leg_for_smart_schedule` adds a revenue/desirability term (`:2082-2084`). **But** these are per-leg scores summed at placement time, not a holistic day-quality objective, and deadhead is only a proximity *proxy* (the word never appears). Critically, **"a better day for the driver" is not a single numeric the engine optimizes or reports** — it emerges implicitly from greedy choices.

**Summary for Pass 1**
- **What exists:** real from-scratch single-driver builder; real date-wide greedy multi-driver assigner; per-leg quality biases (chains, proximity, load, idle, span, revenue); preview/apply with snapshots.
- **What is missing:** global/joint optimization; a holistic driver-day (and whole-board) quality score; auto-evaluation of alternates; capacity-fit checking; explicit deadhead minimization.
- **What is manual:** windows/preferences/pins; choosing the better alternate; capacity sanity; accepting tight turns; committing (Apply).
- **What to automate first (Pass 1):** a **driver-day quality score** (single numeric over a finished day: deadhead proxy + idle + span + balance + desirability) so alternates and whole-board outcomes can be ranked/compared — a prerequisite for any "is the auto-build actually better?" measurement.

---

## 4. Pass 2 Pre-Farm Swap Analysis

**Is arrival<->return implemented?** **No.** The verifier *refuted* this. `swap_optimizer.py:236-521` is a generic cascading-displacement search with no arrival/return identification or pairing anywhere. The only place trip type matters is `scheduler.check_feasibility` (`:644-650`), where an "airport arrival" gets `ARRIVAL_GRACE_MINUTES` timing leeway — it does NOT pair an arrival with its matching return. The documented Pass-2 "arrival<->return" intent is realized only as a side effect of generic reassignment plus arrival grace.

**Same-vehicle-type?** **Tier, not strict.** `_vehicle_compatible` (`swap_optimizer.py:100-105`) uses `get_compatible_vehicle_types` (own tier + all lower). Exact-type equality is only a sort preference (`:387-392`) and a `swap_tier_bonus` in `_score_solution` (`:220-229`) — never required. A leg with no vtype matches everyone; unknown driver vtype allows all.

**Only in the swap tester? Part of normal flow? Runs automatically before the farm list?** **Human-triggered only; not part of the automatic flow; never runs before the farm list.** The verifier confirmed via repo-wide search that `find_swaps` is called from exactly one place — `find_swap_suggestions` (`views.py:11913`, calls at `:11933`/`:11986`), a staff-only POST AJAX endpoint wired to URL `find-swaps/` (`urls.py:42`) and invoked by the "Find Swaps" buttons in `swap_tester.html:497` and `daily_capacity_planner.html:4607`. Nothing in `scheduler.py`, `suggest_assignments_clustered`, or any auto-assign/farm-list path calls it. The swap tester page itself only renders the debugger UI; it does not auto-fire the search server-side.

**It IS a real cascade, though.** Multi-leg chains deeper than pairwise are confirmed: iterative-deepening over `range(1, max_depth+1)` with `max_depth` default `swap_max_depth=5` (`swap_optimizer.py:288, models.py:115`), recursing on each displaced leg (`:455-517`). But it **does not loop until no further improvement** — it stops at the first/shallowest depth that yields any solution (`:313-314`, "prefer shallower"), and it runs under a hard fixed budget (`_budget_exceeded`: `max_iterations=5000` AND `time_limit_ms=5000`, `:191-195`) plus a 20-solution cap.

**Why is Pass 2 still done by hand?** All of the following apply:
- **Not auto-triggered** — no pipeline calls `find_swaps`; a human must pick each target leg and click the button. (Primary reason.)
- **Per-leg only, no batch** — one search per leg; no "find swaps for all residual legs."
- **No loop-until-improve** — stops at the shallowest solution; does not iterate toward a better board.
- **Missing arrival/return logic** — the conceptual "swap the arrival for the return" decision lives entirely in the dispatcher's head.
- **Missing strict vtype logic** — tier-compatible, so the human still vets whether the substitute vehicle is appropriate.
- **Execute is loosely guarded** — `execute_swap` (`views.py:12045`) applies the client-supplied move chain atomically but re-validates only presence of `leg_id`/`to_driver_id` (`:12070-12082`); it does **not** re-run `check_feasibility`/vehicle compatibility at execute time, and there is **no undo/snapshot** beyond the per-request atomic transaction — so a human must vet each chain before applying.

**Scope to implement an automatic pre-farm swap pass:**
1. **Orchestration loop** — after `suggest_assignments_clustered`, collect residual (`suggested_driver_id=None`) legs and call `find_swaps` for each (read-only); rank the resulting in-house-save chains.
2. **Batch UI** — present "N legs can be saved from farming via these swaps" with one-click apply per chain (reusing `execute_swap`).
3. **Execute-time re-validation** — re-run `check_feasibility` and vehicle compatibility inside `execute_swap` before writing (close the stale-move-set gap).
4. **Snapshot before swap** — auto-create a `ScheduleSnapshot` before applying a swap chain (currently only reset/auto-assign/restore snapshot), enabling undo.
5. (Optional) **Same-vtype / arrival-return preference weighting** — boost `swap_tier_bonus` for exact matches and add a trip-type-aware preference if the business genuinely wants arrival/return pairing.

---

## 5. Current Objective / Scoring Analysis

Two scorers exist in the scheduler, plus one in the swap optimizer. All weights come from the DB-backed `SchedulerSettings` singleton (`dispatching/models.py:9`); numeric defaults below are model field defaults and could be overridden by the live row.

| Dimension | Modeled numerically? | Where / note |
|---|---|---|
| In-house coverage (count) | **Implicit only** | Greedy places as many feasible legs as score > 0 allows; coverage is a *byproduct*, not an explicit maximized term. `get_coverage_stats:2142` tallies it but does not feed scoring. |
| Farmed legs | **No** | Residuals are emitted (`scheduler.py:1503-1511`) but "number farmed" is not a scored/minimized objective. |
| Driver-quality / job desirability | **Yes** | Revenue bonus `min(revenue/divisor, cap)` in `_score_leg_for_smart_schedule` (`:2082-2084`) and swap `_score_solution` (`:214-217`). Trip-type preference `:2031-2036` / `:1395-1409`. **Note:** the inline `suggest_assignments` scorer has **no revenue term.** |
| Deadhead | **Proxy only** | Same-area/close proximity bonus (`:1283-1293`) and backward-chain bonus (`:1325-1339`). No mileage/deadhead minimization; the token "deadhead" appears nowhere in `scheduler.py`. |
| Idle gaps | **Yes** | `idle_gap_penalty_per_min` over `idle_gap_threshold` on both sides of the insertion, `suggest_assignments` `:1353-1378`. **Absent** from `_score_leg_for_smart_schedule`. |
| Tight turns | **Yes (buffer band)** | Buffer-quality bands perfect/sweet/tight/good/loose/risky (`:1242-1257`; builder `sb_buffer_*` `:2038-2049`). |
| Impossible turns | **Yes (hard gate, not score)** | `check_feasibility` negative-buffer => `feasible=False` (`:687-694, 705-714`); a constraint, not a scored term. |
| Workload balance | **Yes** | Exponential load penalty `load_balance_multiplier*(n_jobs**load_balance_exponent)` (`:1348-1351`). **Absent** from `_score_leg_for_smart_schedule` and from swap scoring. |
| Route cleanliness | **Partial / proxy** | Via proximity + flow + chain bonuses; no explicit route-quality metric. |
| Arrival/return pairing | **No** | No pairing in either scheduler scorer or the swap optimizer. Flow bonuses (`:1295-1314`, `sb_flow_*` `:2058-2072`) only discourage consecutive arrivals / reward a return breaking an arrival run — not true pairing. |
| Same-area chaining | **Yes** | Proximity bonus `loc_same_area`/`loc_close` (`:1283-1293`) and backward-chain bonus (`:1325-1339`). |
| Driver preferences | **Yes** | Per-driver trip-type preference modes only/heavy/prefer (`:1395-1409`); preferred-trip-type in the builder (`:2031-2036`). |
| Vehicle type fit (tier) | **Yes (gate + score)** | Hard tier gate (`:1221-1226, 1663-1666`) plus tier preference bonus `tier_exact..tier_4_down` (`:1259-1271, 2005-2014`). |
| Vehicle capacity fit (seats/luggage/carseats) | **No** | Never read in any feasibility/compatibility/scoring branch — display-only on the slot. |
| High-value trips | **Yes (builder + swap)** | Revenue bonus in `_score_leg_for_smart_schedule` (`:2082-2084`) and `_score_solution` (`:214-217`); **not** in the `suggest_assignments` inline scorer. |

**Explicit flag:** **"A better day for the driver" is NOT a numeric objective.** No function computes a holistic driver-day (or whole-board) quality score. The day-quality dimensions that *are* modeled — workload balance, idle gaps, span — live **only** inside `suggest_assignments` as per-leg-assignment penalties used to pick the best driver greedily; `_score_leg_for_smart_schedule` does **not** include idle-gap, load-balance, or span at all, and the swap `_score_solution` includes none of the four day-quality dimensions (it scores depth, min-buffer, revenue, tier). Deadhead is everywhere only a proximity proxy.

---

## 6. Where Humans Still Do the Thinking

| Manual decision | Why the engine can't (today) | Difficulty to automate |
|---|---|---|
| **Which leg to give a driver** | Builder/assigner suggest, but final per-driver job selection (windows, pins, preferences) is human-seeded (`views.py:9074-9085`, `scheduler.py:1577-1582`). | medium |
| **Which alternate is better** | `alternatives` are listed but not auto-evaluated; choosing the cleaner-day swap is a manual pin/exclude + re-preview loop. Needs a day-quality score to rank. | medium |
| **When to swap arrival/return** | No arrival/return pairing exists anywhere; the conceptual target-leg choice is entirely human. | hard |
| **When to farm** | Residual list is automatic, but converting a residual to FARMED requires a manual "Assign to affiliate" action (`views.py:2017-2027`); no affiliate selection/pricing logic. | hard |
| **When to override** | No persistent lock; the human is the only thing that keeps a good assignment from being re-suggested/displaced on the next run. | should-stay-manual |
| **When to accept a tight turn** | `check_feasibility` only warns on < 15 min buffers (`:687-714`); accepting the risk is judgment on real-world drive-time confidence. | should-stay-manual |
| **When to balance workload** | Load balance is a soft exponential penalty, not a constraint; the human decides if a lopsided day is acceptable. | medium |
| **When to protect schedule quality** | No whole-day quality metric and no lock; the dispatcher eyeballs the timeline to protect a clean day from greedy re-runs. | medium |

---

## 7. Automation Opportunity Ranking

| Rank | Opportunity | Manual work removed | Coverage impact | Risk | Difficulty | Recommended phase |
|---|---|---|---|---|---|---|
| 1 | **Automatic pre-farm swap pass** (loop `find_swaps` over residuals, surface in-house saves before farming) | Eliminates per-leg manual "Find Swaps" hunting before farming | **High** — directly converts farm-outs to in-house | Medium — search is read-only; risk only at apply (mitigate with re-validation) | Medium (orchestration + batch UI; engine exists) | Phase 2 |
| 2 | **Farm saver** (rank residuals by savable revenue/feasibility, recommend top swaps/takebacks) | Removes manual triage of which residuals are worth saving | **High** | Medium | Medium (builds on #1 + scoring) | Phase 2 |
| 3 | **Auto-build all drivers for a date** (promote `auto_assign_drivers` preview to a trusted default fill) | Removes most per-driver manual building | High | Medium — greedy can produce uneven days; needs review | Low–Medium (exists; needs confidence) | Phase 2 |
| 4 | **Schedule score** (single numeric whole-board quality metric) | Enables ranking/comparison instead of eyeballing | Indirect (enabler) | Low — read-only metric | Medium | Phase 2 (foundational) |
| 5 | **Driver-day quality score** (per-driver day metric: deadhead proxy + idle + span + balance + desirability) | Lets the engine/UI auto-pick the cleaner alternate | Indirect (enabler) | Low | Medium | Phase 2 (foundational) |
| 6 | **One-click apply selected recommendations** (extend existing "Apply Selected") | Reduces click-through friction on reviewed suggestions | Medium | Medium — must keep human review gate | Low (extends `views.py:8660`) | Phase 2 |
| 7 | **Improve existing schedule** (in-place board re-optimizer / swap-to-improve loop) | Removes manual re-arranging of a finished board | Medium–High | High — must not regress good assignments | High (new optimizer; needs day score) | Phase 3 |
| 8 | **Cascade/chain swap expansion** (deeper/exhaustive search, multi-target, loop-to-improve) | Surfaces swaps the shallow-first search misses | Medium | Medium — budget/runtime | Medium–High | Phase 3 |
| 9 | **Locked assignments** (persistent do-not-move flag on Leg) | Removes need to re-pin / avoid re-running auto-assign | Indirect (protects coverage) | Low | Low–Medium (new field + engine respect) | Phase 2 |
| 10 | **Schedule versions / undo** (extend snapshots: vehicles, swaps, settings; snapshot before swap) | Removes fear-of-loss friction, enables aggressive automation | Indirect (enabler) | Low | Low–Medium (`ScheduleSnapshot` exists) | Phase 2 |

---

## 8. Recommended Phase 2 Measurement Plan

**Strictly read-only.** All measurement must run the engine in **preview/suggest mode only** (`apply=false`, `find_swaps` is already write-free) against historical dates. **No `leg.driver` writes, no snapshots applied, no DB mutation.** If a read-only prod connection is needed, use the existing `USE_PROD_RO=1` path (dedicated `readonly_local` role) rather than live writes.

**Candidate historical test days (pick 3–5, by archetype):**
1. **Normal weekday** — baseline load, mixed trip types.
2. **Busy weekend** — high volume, peak load-balance / span stress.
3. **Port Canaveral-heavy day** — cruise legs dominate (tests `return/cruise` flow bonuses, long-span penalties, and arrival/return intent gaps).
4. **Airport-heavy day** — many arrivals (tests arrival grace, consecutive-arrival penalties, tight-turn handling, flight-arrival-based end times).
5. **High-farm / high-manual-swap day** — a day the dispatchers historically farmed a lot and hand-swapped (the clearest test of the farm-saver / pre-farm swap value).

**Three schedules compared per day:**
- **Current real schedule** — the assignments as they actually ran (ground truth from history).
- **Fully automated engine** — `suggest_assignments_clustered` + an automatic `find_swaps` pre-farm pass, preview-only.
- **Human final** — the dispatcher's finished board (same as "current real" unless a separate "as-first-proposed vs. as-finalized" record exists).

**Metrics to compute for each schedule (read-only):**
- Farmed legs (count and revenue)
- In-house coverage % (legs covered in-house / total legs)
- Swaps used (count; cascade depth distribution)
- Deadhead (proxy: reposition drive-time minutes between consecutive legs, since no true mileage metric exists — state this limitation)
- Idle gaps (count and total minutes over `idle_gap_threshold`)
- Tight turns (count of buffers < 15 min)
- Impossible turns (count of would-be negative-buffer placements — must be 0 for any valid schedule)
- Constraint violations (vehicle tier mismatches; availability/time-off window violations; and — flagged separately because the engine ignores it — passenger/luggage/car-seat capacity overruns)
- **Legs farmed that a human could have saved** — residuals for which `find_swaps` (or a takeback) finds a feasible in-house chain; the headline metric for the pre-farm swap opportunity.

**Reporting:** per-day and aggregate deltas (engine vs. human), with explicit notes where the engine's "deadhead" is only a proxy and where capacity overruns are invisible to the engine.

---

## 9. Risks / Unknowns

- **`SchedulerSettings` live values not read.** All numeric weights/thresholds cited are model field defaults (`dispatching/models.py:9, 111-121`). The live singleton row could override them (signs, thresholds), changing behavior; the report's *structure* of scoring is verified, the *magnitudes* are not.
- **Greedy order sensitivity.** Both builders' outputs depend heavily on processing order (3-pass scarcity + sort keys, cluster pre-assignment). Small input changes can produce materially different boards; measurement must treat the engine output as one greedy realization, not "the optimum."
- **Feasibility only checks adjacent slots.** `check_feasibility` (`:614`) validates the immediately preceding/following slot, not whole-chain ripple. Multi-leg ripple effects could make a "feasible" insertion infeasible downstream; this is a known blind spot.
- **Drive-time estimates are category-level, not live routing.** Feasibility relies on `DRIVE_TIME_ESTIMATES` + learned `RouteTimingMetric`, not real-time ETAs/traffic. Tight/edge turns may be mis-estimated either way — affects deadhead proxy and tight/impossible classification.
- **Capacity blind spot.** No passenger/luggage/car-seat check anywhere; a tier-compatible assignment can still be physically infeasible. Whether *booking-time* validation enforces capacity before legs reach the scheduler was **not** audited — needs confirmation in Phase 2.
- **`execute_swap` does not re-validate at apply time** (`views.py:12070-12082`) and has **no undo** beyond the atomic transaction. Any automation that applies swaps must add re-validation + snapshot first.
- **Template JS not exhaustively read.** The ~4700-line `daily_capacity_planner.html` and `swap_tester.html` were spot-checked (button wiring confirmed); a client-side auto-fire of `find-swaps` cannot be 100% ruled out, though server-side it is human-triggered only.
- **Call-site coverage.** The two primary entry points were traced (`views.py:9128`, `8638`); not every one of the ~12 `build_smart_schedule`/`suggest_assignments` call sites was read, so alternate pre/post-processing may exist.
- **No code was executed.** This is a static read-only investigation; runtime behavior (cache staleness, partial-apply failure modes) is inferred from code paths.

---

## 10. Exact Files / Functions Discovered (Reference Index)

**`dispatching/scheduler.py`** (core engine)
- `:20-69` `DRIVE_TIME_ESTIMATES` — hardcoded category drive-time table
- `:87` `VEHICLE_TIER_ORDER = [towncar, mini_van, suv, van, Van(14 Pax)]`
- `:90` `get_vehicle_tier` — tier index for a vehicle type
- `:100` `get_compatible_vehicle_types` — own tier + all below (the ONLY vehicle gate)
- `:134` `compute_leg_scarcity` — eligible-driver count per leg
- `:279-282` `ScheduleSlot` capacity fields (passengers/luggage/luggage_type/carseats — display only)
- `:359` `get_drive_time`; `:411` `get_airport_dwell_time`; `:463` `_get_best_flight_arrival`
- `:488` `estimate_job_end_time` — arrivals use flight arrival + dwell + drive; others pickup + drive
- `:614` `check_feasibility` — adjacent-slot gap check; negative buffer => hard reject; < 15 min => warn; arrival grace at `:644-650`
- `:808-811` slot capacity fields populated (display only)
- `:847` `assign_drivers_to_clusters` — greedy driver-to-cluster pre-assignment (availability overlap + tier)
- `:939` `suggest_assignments_clustered` — gap-based clustering wrapper + shift coherence
- `:996` `suggest_assignments` — 3-pass scarcity greedy; best-score driver per leg; simulate-forward
- `:1221-1226` tier hard-gate in `suggest_assignments`
- `:1242-1414` inline assignment scorer (buffer/tier/scarcity/proximity/flow/chain/backward-chain/shift/load/idle/span/pref; **no revenue**)
- `:1348-1351` exponential load-balance penalty; `:1353-1378` idle-gap penalty; `:1380-1393` span penalty
- `:1503-1511` residual leg emitted with `suggested_driver_id=None` ("No in-house driver available")
- `:1572` **`build_smart_schedule`** — per-driver from-scratch greedy day builder
- `:1642` empty-start when `existing_schedule=None`; `:1663-1666` tier gate; `:1670-1671` pinned bypass filter; `:1684-1693` pin insert; `:1795-1820` greedy add if score > 0; `:1853` `_recalculate_timing_details`
- `:1984` **`_score_leg_for_smart_schedule`** — builder scorer (tier/scarcity/pref/buffer/proximity/flow/chain + **revenue** `:2082-2084`; **no** idle/load/span)
- `:2089` `_add_leg_to_schedule` — append + re-sort (no removal -> pure greedy)
- `:2142` `get_coverage_stats` — inhouse/affiliate/unassigned tallies

**`dispatching/swap_optimizer.py`** (Pass-2 swap search)
- `:100` `_vehicle_compatible` — tier-based (not strict equality)
- `:191` `_budget_exceeded` — `max_iterations` AND `time_limit_ms`
- `:198` `_score_solution` — `1000 - depth*penalty + min_buffer*w + revenue + tier_bonus` (no deadhead/idle/load/coverage term)
- `:236` **`find_swaps`** — iterative-deepening DFS; `:288-314` depth loop, `:313-314` stop at shallowest; `:329/:350` sort + top 10
- `:360` `_search` — recursive; `:455-517` displacement + recurse to rehome (cascade); `:494-497` cycle prevention
- `:387` `_driver_sort_key` — exact-tier match first, then most room
- `:523` `_build_diagnostic` — per-driver no-solution report

**`dispatching/views.py`** (orchestration)
- `:1940` `update_leg_assignment` — single-leg manual driver/status write (AJAX)
- `:2017-2027` manual affiliate assignment path
- `:2140` `check_driver_feasibility` — AJAX availability + tier + buffer check
- `:8010` `capacity_planner` — read-only dispatcher hub; `:8187-8202` 60s LocMemCache `capacity_planner_{date}`
- `:8468` `_create_schedule_snapshot`; `:8501` **`auto_assign_drivers`** (`:8559-8569` drivers, `:8577` `get_availability_for_date`, `:8623-8638` legs + clustered call, `:8660-8699` apply/write, `:8701+` preview)
- `:8835` `reset_schedule`; `:8964` `restore_schedule_snapshot`
- `:9044` **`smart_schedule_builder`** (`:9114-9139` builds `available_legs` + calls `build_smart_schedule` at `:9128`; `:9268` apply skips already-assigned)
- `:11913` **`find_swap_suggestions`** (calls `find_swaps` at `:11933`/`:11986`) — staff-only, the only caller
- `:12045` **`execute_swap`** — atomic apply of one move chain; `:12070-12082` presence-only validation (no feasibility re-check); cache invalidation `:12103`
- `:12108` `execute_takeback` — single affiliate leg -> in-house

**`dispatching/models.py`**
- `:9` `SchedulerSettings` singleton (~50 knobs); `:111-121` swap budget defaults (`swap_max_depth=5`, `swap_time_limit_ms=5000`, `swap_max_iterations=5000`, `swap_depth_penalty`, `swap_buffer_weight`, `swap_revenue_weight`, `swap_tier_bonus`); `:126` `get_settings`

**`reservations/models.py`**
- `:891` `passenger_count`, `:896` `luggage_count`, `:907-930` car-seat fields; `:1087-1090` `effective_vehicle_type`; `:1100-1140` `effective_passenger_count` / `effective_luggage_count`
- `:2657` `ScheduleSnapshot`; `:2684` `ScheduleSnapshotEntry`

**`drivers/availability.py` & `drivers/models.py`** (availability / time-off / fleet)
- `drivers/availability.py:113` `resolve_effective_availability` — weekly schedule + active **approved** date override + `Driver.default_*` → rich availability dict (window tuple)
- `drivers/availability.py:55` `_pick_active_exception` (single-day beats range; ties by `updated_at`); `:290` `is_pickup_within_window`
- `drivers/models.py:190/199/213` `get_effective_availability` / `get_availability_for_date` / `get_full_availability` (Driver accessors the views call)
- `drivers/models.py:226` `DriverWeeklySchedule`; `:304` `DriverDateOverride` (`status='approved'` gates effect); `:469` `FleetVehicle`; `:487` `DriverVehicleAssignment` (unique per driver+date → derives vehicle type)

**`dispatching/urls.py`**
- `:41` auto-assign; `:42` `find-swaps/`; `:45` `swap-tester/`

**`dispatching/templates/dispatching/daily_capacity_planner.html`**
- `:1393` Auto-Assign All; `:1506-1518` residual "Assign to affiliate" + Find Swaps; `:2392` Apply Selected; `:4607` find-swaps button; `:4721` `applySwapFromPlanner`
