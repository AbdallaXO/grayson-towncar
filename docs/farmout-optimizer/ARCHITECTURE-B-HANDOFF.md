# Farm-Out Opportunity-Cost Optimizer — Architecture B Handoff

**Status:** Waleed-only validation COMPLETE on real prod-copy data (uncommitted WIP).
**Next chapter:** Architecture B = the multi-affiliate roster.
**Read first:** auto-memory `project_farmout_optimizer`; this doc; then the loud header at the
top of `dispatching/farmout_optimizer.py` (lines ~85-150). Design doc:
`~/.claude/plans/you-are-continuing-the-composed-goose.md`.

This is a **read-only, retrospective, recommend-only** tool. It answers the one comparison the
founder makes by hand and nothing else in the codebase does: *"is it cheaper to farm this leg, or
keep it in-house and farm something cheaper instead?"* It judges a PAST day's farm decisions on
information known WHEN THE SCHEDULE WAS BUILT. It NEVER un-farms a committed leg and NEVER runs live.

---

## 1. CURRENT STATE — built + validated (Waleed-only)

Module `dispatching/farmout_optimizer.py` + offline command
`dispatching/management/commands/analyze_farmout_savings.py`. Reuses `fleet_intel.py` cost
primitives and the live scheduler/swap engine (`check_feasibility`, `find_swaps`) read-only.

Run:
```
DJANGO_DEBUG=1 .venv/Scripts/python.exe manage.py analyze_farmout_savings --date 2026-05-02 --html report.html
```

Built and validated against archetype days (busy/slow/cruise) 2026-05-01..07:

| Piece | Where | State |
|---|---|---|
| Capacity-aware farm-cost **waterfall** | `price_farm_waterfall`, `_price_one_leg`, `WaterfallLedger` | done |
| **Opportunity-cost** eval (depth-1 displacement: keep arrival / farm return) | `evaluate_target`, `summarize_savings_range` | done |
| Pricing from **real `DriverPayRate`** rows (vehicle+direction aware), never a constant | `_find_rate` -> `pay_calc._find_rate` | done |
| **minivan == SUV** pricing collapse (distinct from capability tiers) | `_pricing_vehicle` | done |
| **Port Canaveral & Sanford = own categories** (NOT departures) + directional drop-off rule | `is_departure`, `is_port_or_sanford`, `port_sanford_direction_tag` | done |
| **Scheduled-time** (decision-time) flight eval, not hindsight actual/estimated | `analytics.scheduled_flight_arrival_local` + `scheduler.USE_SCHEDULED_ARRIVAL_FOR_EVAL` + `_apply_decision_time_pickups` | done |
| **Real driver availability** (worked-leg span, not the stub) | `_worked_span_window` + `find_swaps(driver_windows=)` | done |
| **Calibrated drive times** (MCO<->Disney 30, <->Universal 25, <->SFB 60, Disney<->Port 72) | `scheduler.DRIVE_TIME_ESTIMATES` | done |
| **Approach A** far-destination ABSTAIN (uncomputable, not $0) | `_drive_uncomputable_far` / `LIVE_DISTANCE_UNKNOWN_CATS` | done |
| **Categorizer local-resort fix** (2026-06-07) | `analytics.categorize_location` curated lists | done |
| **Visual board report** (each rec shows both drivers' real days + feasibility math) | `_capture_boards`, `_placement_feasibility`, `_board_html` | done |

Representative validated result (2026-05-02, 149 legs / 64 farmed): 16 recs (14 free rescues, **0
opportunity swaps**, 2 policy departures), 11 true departures protected, free-rescue $ avoided
~$1,140, 1 leg abstained (Indian Shores). The **0 opportunity swaps is a finding, not a bug** — see §5.

---

## 2. CONFIRMED BUSINESS RULES (the decisions behind the code — don't lose these)

- **Objective = minimize farm-out spend.** In-house is ~free at the margin; guest revenue is
  identical either way (every leg is served), so the comparison collapses to pure driver cost:
  `net_opportunity(B over A) = recovered_margin(target) - SUM recovered_margin(displaced)` where
  `recovered_margin(leg) = farm_base - inhouse_base`. Keep the expensive-to-farm legs in-house;
  farm the cheap-to-farm ones. Evaluated as a realized board state, never a sum of per-leg claims.
- **Port Canaveral & Sanford are their OWN categories, NOT departures.** No automatic in-house
  protection — judged purely on the net-spend math.
- **Departures belong in-house, but SOFT.** A true departure (non-Port/Sanford leg whose dropoff is
  an airport) is never farmed in a bundle, but is only *rescued* back in-house if free or within
  `departure_rescue_max_premium` (**default $0**). A departure rescuable only by farming a much
  pricier leg was correctly left farmed.
- **VIP legs are never farmed and never displaced** (`Reservation.is_vip` OR Small World Big Fun
  agency; resolved up front to a protected id set).
- **$100 discretionary threshold** (`DEFAULT_MIN_SAVINGS`) — no nagging on small savings.
- **No displacement count cap.** Capacity is the physical feasibility chain (overlap + turnaround
  re-validated by `check_feasibility`), not an arbitrary number.
- **Retrospective only.** Never live/intraday; never suggests un-farming a committed leg (the
  founder can't take jobs back from affiliates same-day). Uses decision-time info only.
- **Recommend-only.** Strictly read-only — no model writes, no migrations.

---

## 3. WALEED — the validated single-affiliate TEMPLATE

Waleed (a.k.a. OUALID, **driver id 7**) is the one affiliate whose rules we encoded exactly, so
recommendations could be checked against days the founder remembers. His facts (HARDCODED in the
module today — see the loud header; Architecture B turns these into data):

- **Rates:** $70 local / $125 Port+Sanford, flat per route, vehicle-independent (one all-vehicle
  NULL `DriverPayRate` row per route). These were corrected in the LOCAL test DB by
  `scratch/seed_waleed_rates.py` (TEST DATA only — re-run if the local DB is rebuilt). Read live via
  `_find_rate`, never a constant.
- **Vehicle classes:** SUV-or-lower ONLY (towncar / mini_van / suv). **Never van / 14-pax** — those
  legs have no in-house-via-farm alternative this pass. Enforced by the `suv_or_lower` tier gate in
  `_price_one_leg` (note: `check_feasibility` itself has NO vehicle gate, so this gate is load-bearing).
- **Capacity = feasibility chain, NOT a count.** He is one physical vehicle; eligibility = his card
  prices the route AND the leg fits his growing `oualid_chain`. No daily-leg cap.
- **Directional drop-off rule:** drops at Port Canaveral / Sanford but **never picks up there** (no
  permit) — encoded by excluding any leg ORIGINATING at Port/Sanford (`is_port_or_sanford(pickup)`).

---

## 4. KNOWN-SOFT / DEFERRED

- **Approach B (live road distance) is deferred.** Feasibility uses the coarse Orlando category
  table. Two consequences:
  - The far-destination ABSTAIN is **target-only**. A neighbor/displaced leg with a far endpoint
    still uses the coarse table (a residual phantom-feasible risk). Live distance closes this.
  - **ChampionsGate / Reunion / Clermont** are priced as **Disney Resort (~30 min)** but are really
    ~35-40 min — optimistic. Acceptable for de-abstaining; tighten with real drive times.
  - Both tracked as **CLAUDE.md NEXT #7** (precomputed / offline-cached drive-time matrix, NO
    in-request network — the 2026-05-31 hotfix forbids synchronous Google calls in the render path).
- **The categorizer is keyword-based and extensible** (`analytics.categorize_location`). To add a
  place: drop its name — or an **Orlando-only area token** — into `_LOCAL_DISNEY_AREA_KEYWORDS` or
  `_LOCAL_IDRIVE_UNIVERSAL_KEYWORDS` (or the airport-hotel / `brightline orlando` rules). Rules:
  keep phrases SPECIFIC to one Orlando place or use a token that physically can't match a far
  location; never a broad word (`resort`/`hotel`) — it would wrongly price Tampa/Clearwater/Legoland
  as local. **Raw street addresses are deliberately NOT matched** (they stay abstained — we can't
  know where an anonymized home is). Promotions only ever make categorization MORE accurate (they
  run after airport/port/terminal and the existing Disney/Universal checks), so the change is safe
  for the shared route-timing + live-scheduler callers too.
- **Tier-2 displacement is DEPTH-1 only** (one displaced leg per swap). The waterfall/bundle
  structures already accept multi-leg sets, so deeper cascades are search, not re-architecture.
- **No unit tests, no dispatch-board UI panel yet.**
- **Validation gotcha for a fresh session:** you CANNOT `git stash` `analytics.py` to isolate a
  categorizer change — the prior WIP (e.g. `scheduled_flight_arrival_local`) lives in the same
  uncommitted file, so stashing reverts it too and breaks imports. To diff before/after, replicate
  the original `categorize_location` in-process and compare over `fi.legs_for_range(d, d)`.

---

## 5. WHAT ARCHITECTURE B MUST DO

**Goal:** widen the roster from Waleed-only to the full affiliate set.

1. **Re-enable the roster.** Today only Waleed prices legs. `resolve_curated_affiliates()` resolves
   Oualid (7) + Anthony (29), but `summarize_savings_range` **nulls Anthony after resolving** (so his
   "not found" alarm never fires) — every other affiliate is absent entirely. `_price_one_leg` has
   hardcoded OUALID + ANTHONY branches; these become a data-driven loop over the roster.

2. **Model affiliate capability / capacity / route-restrictions as DATA** (the open design question).
   Each affiliate needs:
   - **Capability** — which vehicle classes they serve (Waleed: SUV-or-lower).
   - **Route / permit restrictions** — directional rules (Waleed: Port/Sanford drop-off only, no
     pickup); possibly geographic limits.
   - **Capacity** — count cap (Anthony: 12/day) vs feasibility-chain single vehicle (Waleed) vs a
     multi-vehicle fleet (needs N parallel chains).
   - **Rates** are ALREADY data (`DriverPayRate`) and priced via `_find_rate` — the pricing layer is
     roster-agnostic today; capability/capacity is what's hardcoded.

   Decision to make: store this on `Driver`/affiliate fields, or a new `AffiliateCapability` /
   `AffiliateCapacity` model (a migration — the first write this otherwise read-only project would
   need). Keep the engine read-only; only the *config* of affiliate facts becomes persisted data.

3. **The key insight that makes Architecture B worth it:** opportunity-swaps only have value with
   **rate spread across multiple affiliates.** Waleed alone has a single flat rate ($70/$125), so
   there is no arbitrage — "farm the cheap one, keep the expensive one" can't fire, which is exactly
   why Waleed-only produced **~zero opportunity swaps** (0 on both 05-02 and 05-09). That's a
   correct result, not a bug. Multiple affiliates with differing per-class / per-route cards (e.g.
   Cheapo Limo's per-class rates) create the spread that makes the opportunity-swap half of the tool
   actually generate >=$100 recommendations. **Architecture B is what turns on the swap engine.**

---

## Key files & anchors
- `dispatching/farmout_optimizer.py` — `_price_one_leg` (roster + capability, ~330), `WaterfallLedger`
  (~272), `is_departure`/`is_port_or_sanford` (~240), `_drive_uncomputable_far` (~254),
  `resolve_curated_affiliates` (~166), `summarize_savings_range` (Anthony null + day loop, ~920).
- `dispatching/management/commands/analyze_farmout_savings.py` — report + `_board_html` renderer.
- `dispatching/analytics.py` — `categorize_location` + curated lists; `scheduled_flight_arrival_local`.
- `dispatching/scheduler.py` — `DRIVE_TIME_ESTIMATES`, `check_feasibility`, `USE_SCHEDULED_ARRIVAL_FOR_EVAL`.
- `dispatching/swap_optimizer.py` — `find_swaps(driver_windows=)`. `drivers/pay_calc.py` — `_find_rate`.
- `scratch/seed_waleed_rates.py` — local TEST rate seeding (re-run if local DB rebuilt).
