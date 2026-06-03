# Fleet Capacity Intelligence System — Design & Status

**Last updated:** 2026-06-02. Read this header first when resuming.

> **What this is.** A decision-support layer that answers: *are we farming out too much, which
> vehicle types leak the most margin, and is the binding constraint a **vehicle** shortage (buy a
> car), a **driver** shortage (hire), or just **bad scheduling** (fix dispatch)* — so the founder
> can decide **buy a vehicle vs hire a driver vs keep farming**, with the math to back it.

---

## STATUS / RESUME HERE

- **Goal (priority order):** (1) maximize in-house jobs, (2) minimize empty deadhead, (3) minimize gaps;
  classify every farm-out by its true binding constraint; quantify recovered margin; produce a
  config-driven buy/hire/farm recommendation.
- **Locked decisions (2026-06-02):**
  1. **Build read-only analytics + classification FIRST** — no migrations, no dispatch changes.
  2. **Driver-pay-only recovered margin for v1** — no fuel/tolls/wear yet (add later as a cost layer).
  3. **Buy engine is config-driven with placeholder fixed costs** — outputs labeled *ESTIMATED* until
     real per-vehicle-type monthly costs are entered.
  4. **Future-demand forecasting deferred** to a later phase (future driver/vehicle availability still
     leans on stub windows — low confidence today).
  5. **Recovered margin compares base pay on both sides** (gratuity is a customer pass-through;
     `driver_additional`/night bonus excluded by default — tunable).
- **Current phase:** Deliverable 0 ✅ + **Phase A ✅** + **Phase B ✅** (dashboard live at
  `/dispatching/fleet-intel/`, superuser-gated, 5-min cached) → **Phase C next** (buy engine).
- **Next step:** Phase C — `VehicleTypeCostProfile` (config model + **migration**) +
  `simulate_plus_one_vehicle` + break-even / monthly-profit-change / BUY-HOLD-HIRE labels, all
  stamped *ESTIMATED* until real fixed costs are entered. This is the first schema change in the project.
- **Phase A validation (2026-06-02, local scrubbed DB):** `analyze_farmouts --date 2026-05-09`
  reconciles with the founder's real board — **97 in-house / 51 farmed (65.5% / 34.5%)**, recovered
  margin **+$3,385 net** (affiliate base ~$93/leg vs in-house counterfactual ~$27/leg), 100%
  counterfactual coverage. Binding constraint: **capacity 27 legs / positioning 19 / dispatch-leak 4
  / driver 1** — i.e. the busy day genuinely needs farming (matches CLAUDE.md). 17 formula tests pass.
- **⚠ METHODOLOGY FINDING (important):** per-leg `find_swaps` recovery was over-counting — it says
  almost every farmed leg is *individually* swap-recoverable, but they all compete for the SAME
  finite slack (can't absorb 51 onto a 97-leg/14-car board). So swap-recovery is **OFF by default**
  (`USE_SWAP_RECOVERY=False`, opt-in via `--swaps`), reported as an UPPER BOUND only. The honest
  binding constraint comes from direct feasibility + failure reasons; the TRUE absorbable count is
  the Phase C +1-vehicle simulation. **Do not sum per-leg "preventable" margin as recoverable.**
- **Safe test env:** local SQLite is a scrubbed copy of prod. Run offline `manage.py`. Server:
  `DJANGO_DEBUG=1 .venv/Scripts/python.exe manage.py runserver 127.0.0.1:8000 --noreload`, login
  `localtest` / `Local2026!`. **Caveat:** local DB has **no `LegStatus` history** → actual-completion /
  dwell realism is prod-only; classification validation is best on prod-representative data.

### Phase checklist
- [x] **Deliverable 0** — this design/status doc + CLAUDE.md pointer
- [x] **Phase A** — `fleet_intel.py` (economics + classification), `analyze_farmouts` command,
      `tests_fleet_intel.py` (17 pass). Validated on 2026-05-09.
- [x] **Phase B** — `fleet_intel_dashboard` view + template + URL at `/dispatching/fleet-intel/`
      (superuser-gated, 5-min cache, no math in template). Renders 200; reuses the report styling.
- [x] **Phase B.5 (founder feedback 2026-06-03)** — plain-English relabel + "how to read this" legend
      (founder found "recovered margin / recoverable / validated / margin coverage" opaque); every table now
      shows **Paid + Could-keep + %** (per-group `spend`/`inhouse` added to `_acc`); and a **leak finder** at
      `/dispatching/fleet-intel/legs/` (`collect_leaks` + `classify_farmout(detail=True)`) grouping every
      farmed job into ACTION buckets — **Preventable / Hire / Delay / Buy / Positioning** — with the proof:
      which in-house driver was free (vehicle + buffer) and **who farmed it** (`driver_assigned_by`). Both
      pages render 200; 17 tests pass. See [[feedback_fleet_intel_auditable]].
- [ ] **Phase C** — `VehicleTypeCostProfile` + `simulate_plus_one_vehicle` buy engine (estimated)
- [ ] **Deferred** — `FarmOutDecision` capture, fuel/tolls/wear layer, forecast, trends, scheduling assist

---

## THE BIG PICTURE (why this is cheap to build here)

The read-only audit found that **most of the system is already computable from existing data plus the
existing scheduling engine** — the core insight needs **no schema changes**:

- **Farm-out is derived**, not a stored flag: `leg.driver.driver_type == 'affiliate'`.
- **Affiliate cost** = that leg's `driver_base_pay (+ gratuity + additional)`, populated from
  `DriverPayRate` at `Leg.save()`. **In-house counterfactual cost** = `route.inhouse_base_pay` (or an
  inhouse `DriverPayRate` override) via `drivers/pay_calc.py:calculate_driver_pay`. So
  **recovered margin = affiliate_base − inhouse_base** is computable today.
- The **scheduling engine is pure / side-effect-free** (it writes to the DB only on an explicit
  `apply=True` save in the view). It can be **replayed read-only** to (a) classify *why* a leg was farmed
  and (b) simulate **+1 vehicle** to estimate marginal absorbable legs.

⚠️ **The central trap:** the affiliate's price and an in-house driver's pay live in the **same Leg
fields**, discriminated only by `driver_type`. Naively summing driver pay blends affiliate spend with
in-house labor. Always branch on `driver_type`.

---

## 1. CURRENT BACKEND MAP

Apps (`INSTALLED_APPS`, `business/settings.py`): `rates`, `reservations`, `users`, `services`, `blog`,
`payment`, `drivers`, `dispatching`, `ghl_integration`, `ops`.

| Area | Where | Role |
|---|---|---|
| Booking models | `reservations/models.py` | Reservation, Leg, LegStop, LegFlight, Flight, RefundRequest, RouteTimingMetric, DemandPattern, DriverDailyCapacity, LegStatus |
| Pricing / vehicle types | `rates/models.py` | Vehicle (type/tier), Route, Rate, Location, LocationGroup |
| Drivers / fleet / pay | `drivers/models.py`, `drivers/pay_calc.py`, `drivers/availability.py` | Driver, FleetVehicle, DriverVehicleAssignment, DriverPayRate, DriverPayment, schedules |
| **Scheduling engine** | `dispatching/scheduler.py`, `dispatching/feasibility_guards.py`, `dispatching/swap_optimizer.py` | Feasibility / build / suggest / swap / gap — **reusable read-only** |
| Dispatch views/dashboards | `dispatching/views.py` | `capacity_planner`, `auto_assign_drivers`, `vehicle_profit_report`, `analytics_dashboard` |
| Analytics | `dispatching/analytics.py` | Route timing, dwell/drive, demand patterns (inhouse/affiliate split), driver daily capacity |
| Revenue KPIs | `ops/kpis.py` | Transaction-level **cash-basis** revenue (canonical money figure) |
| Ops task queue | `ops/models.py`, `ops/services.py`, `ops/tasks.py` | OperationalTask pattern (dedup/cooldown, `classify_turn`) |
| Async / batch | `reservations/utils.py:_run_in_background`, `ghl_integration/scheduler.py` (30-min daemon, PG advisory lock) | No Celery/Redis; single gunicorn worker |
| Tests | sparse: `ops/tests/`, `drivers/tests_gusto_export.py`; core `reservations/tests.py`/`dispatching/tests.py` are stubs; guard/scheduler tests referenced in root `CLAUDE.md` | Formula tests will be added |

## 2. CURRENT MODEL RELATIONSHIPS

```
Reservation (trip_type one_way|round_trip, total_price, status, travel_agent)
  └─ legs → Leg                                   ← THE operational/financial unit
       ├─ driver  → drivers.Driver (driver_type: inhouse | affiliate)   ← farm-out discriminator
       ├─ vehicle → rates.Vehicle  (leg override; else reservation.vehicle)  ← vehicle TYPE/tier
       ├─ route   → rates.Route    (origin/destination Location; inhouse_base_pay)
       ├─ flight_information → Flight (legacy 1:1);  leg_flights → LegFlight → Flight (controlling)
       ├─ stops   → LegStop (extra_fee, stop_type)
       └─ status_history → LegStatus (timestamped → actual completion time)

drivers.Driver ─< DriverVehicleAssignment (driver, date, FleetVehicle)   ← who drives which car each day
               ─< DriverPayRate (driver, route, vehicle, direction, base_pay)  ← affiliate rate / inhouse override
               ─< DriverWeeklySchedule / DriverDateOverride               ← shift windows
drivers.FleetVehicle (vehicle_number, vehicle_type→rates.Vehicle, year/make/model)  ← physical fleet
rates.Vehicle ─< rates.Rate (vehicle, route, oneway_price, round_trip_price)
payment.Payment (reservation, amount, refunded_amount, status)            ← cash-basis revenue source
```

No `Affiliate` model and no vehicle fixed-cost model exist. "Affiliate" = a `Driver` with
`driver_type='affiliate'`; its price is the leg's driver-pay fields, from `DriverPayRate`.

## 3. EXISTING FIELDS WE CAN REUSE

| Concept | Field / source | Notes |
|---|---|---|
| Service date | `Leg.pickup_date` / `pickup_time` | KPI date axis (service date over payment date ✓) |
| Status | `Leg.status` ∈ in-progress, confirmed, on-the-way, on-location, picked-up, completed, cancelled | "completed"=done; "in-progress"+`driver is None`=unassigned |
| Actual completion time | `LegStatus` where `status='completed'` | Not a Leg column; from `status_history` |
| **Farm-out flag** | **derived** `leg.driver.driver_type == 'affiliate'` | No explicit field |
| Driver assignment | `Leg.driver`, `driver_assigned_by/at` | |
| Vehicle type | `Leg.effective_vehicle` → `rates.Vehicle.vehicle_type` ∈ towncar, suv, mini_van, van, Van(14 Pax) | |
| Physical fleet | `drivers.FleetVehicle` + `DriverVehicleAssignment(date)` | Fleet size by type = group FleetVehicle by `vehicle_type` |
| Revenue per leg | `Leg.revenue_share`; `recalculate_leg_revenue_shares()` | **Round-trip split already solved** |
| Affiliate cost / driver pay | `Leg.total_driver_pay` = base+gratuity+additional (`drivers/pay_calc.py`) | For affiliate legs **this is the affiliate cost** |
| **In-house counterfactual** | `route.inhouse_base_pay` / inhouse `DriverPayRate` | Computes what a farmed leg *would* have cost in-house |
| Profit | `Leg.calculate_profit()` = revenue_share − total_driver_pay; `Leg.profit_estimate` | Excludes fuel/tolls/wear |
| Zones | `categorize_location()` + `LocationGroup` (Disney/Universal/Port/airport) | pickup/dropoff zone KPIs |
| Trip type | `Leg.get_trip_type()` → arrival/return/cruise/other | |
| Flight + delay | `Flight.scheduled/estimated/actual_arrival_local`, `best_arrival_local()`; `Leg.has_flight_time_mismatch()` | Delay computed on the fly |
| Revenue (cash) | `ops/kpis.py:payment_revenue_qs` (net = amount − refunded) | Canonical money |
| Per-vehicle profit | `dispatching/views.py:_build_vehicle_profit_report` | Reuse aggregation shape |
| Data-quality flags | `Leg.exclude_from_analytics`, `Driver.exclude_from_timing` | Honor in every query |

## 4. MISSING FIELDS / DATA GAPS

- **Phase 0 (capture):** no explicit `fulfillment_method` (derived today); **no farm-out reason / no
  `FarmOutDecision` snapshot**; no farm-out task type. *(Deferred — forward-looking.)*
- **Economics:** **no fuel / tolls / wear / mileage / distance** anywhere; no marginal-cost config;
  `route.inhouse_base_pay` nullable + `DriverPayRate` may miss → counterfactual coverage holes.
- **Buy decision:** **no vehicle fixed monthly cost** fields; **no `is_active`/`status` on FleetVehicle**;
  no persisted simulation table.
- **Forecast:** no `ForecastRun`; future availability relies on stub windows.

## 5. WHAT CAN BE COMPUTED NOW (no schema change)

Operational (counts, farm-out/in-house rate; splits by type/trip/zone/day/hour/affiliate); financial
(revenue, driver pay/affiliate cost, in-house gross profit, **recovered margin** pos/neg/net via the
counterfactual); fleet (size by type, legs/vehicle, utilization, highest-recoverable-margin type);
**classification** (replay the day's board + `check_feasibility` → VEHICLE_TYPE_SHORTAGE /
DRIVER_SHORTAGE / POSITIONING / DISPATCH_LEAK); descriptive future demand (booked legs by day/type/window).

## 6. WHAT NEEDS NEW CAPTURE/SNAPSHOT

True farm-out **intent** (smart vs forced); marginal cost beyond labor (fuel/tolls/wear); vehicle buy
economics (fixed costs); confident DRIVER_IDLE vs UNIT_CAPACITY split (needs clean real shifts);
**point-in-time** availability at the decision moment (replay reconstructs the *final* board, not the
dispatcher's live state); authoritative flight-delay causation.

## 7. FARM-OUT REPRESENTATION TODAY

- **Appears as:** a Leg assigned to a `Driver` with `driver_type='affiliate'`. No farm-out status, no
  boolean, no fulfillment field — it's a join through the driver.
- **Affiliate cost:** yes but **implicit** — in the leg's `driver_base_pay/gratuity/additional`, from
  `DriverPayRate(driver, route, vehicle, direction)` at save; can be `None` (manual entry) if no rate matches.
- **Affiliate identity:** the `Driver` record. No separate Affiliate entity; no rate card beyond `DriverPayRate`.
- **Farm-outs are legs** (assignments), not reservations/statuses.

## 8. EXISTING SCHEDULING / POSITIONING ENGINE

- **Files:** `dispatching/scheduler.py` (`build_driver_schedules`, `suggest_assignments_clustered`,
  `check_feasibility`, `estimate_job_end_time`, `resolve_drive_minutes`, `recover_residuals_via_swaps`,
  `compact_gaps_via_relocation`, `get_coverage_stats`, `load_all_driver_vtypes`, `preload_timing_cache`),
  `dispatching/feasibility_guards.py` (`required_turnaround`, `get_effective_window`, `window_check`),
  `dispatching/swap_optimizer.py` (`find_swaps`).
- **Inputs:** a date's legs, in-house drivers, per-driver windows `{start,end,max_hours,flexible}`,
  per-driver vehicle types (tier `towncar<mini_van<suv<van<Van(14 Pax)`), flight arrivals (flight-aware
  clearing, 15-min deplaning grace).
- **Reusable? Yes — it's the centerpiece.** Pure/in-memory; DB writes only on explicit `apply=True`.
  - **(a) Classification:** rebuild the actual board, loop `check_feasibility` over in-house drivers per
    farmed leg → "could have gone in-house" + which guard bound it.
  - **(b) +1 vehicle sim:** inject a hypothetical driver+`vehicle_type` into `load_all_driver_vtypes`,
    re-run suggest → swap → gap, diff coverage → **marginal absorbable legs**.
- **Caveats / flags:** `USE_LIVE_DISTANCE=False` (category drive-times; `=1` only in offline harness),
  `USE_STUB_WINDOWS=True` (shift confidence limited), `AUTO_PREFARM_SWAP_PASS`/`AUTO_GAP_COMPACT_PASS=True`.
  Local DB lacks `LegStatus` history (dwell realism is prod-only).

## 9. INITIAL RECOMMENDED ARCHITECTURE

- **Add to Leg (deferred):** thin `fulfillment_method` mirror (nullable, backfilled). Nothing else.
- **`FarmOutDecision` snapshot (deferred, forward-looking):** immutable per-farm-out capture (decided_at/by,
  picked_reason, vehicles/drivers-free counts, positioning_feasible, flight_delay_related,
  computed_binding_constraint, preventable, smart_farm_out) — mirror `ops/services.create_task` dedup/cooldown.
- **`LegEconomics` (batch, recomputable):** per-leg derived economics. *In v1 these are computed in-memory by
  the service; persist later if live calc is too slow.*
- **Config models:** `VehicleTypeCostProfile` (fixed monthly cost per type) for the buy engine;
  `AffiliateRate` only if `DriverPayRate` proves insufficient; `ForecastRun` deferred.
- **Placement:** new read-only service module `dispatching/fleet_intel.py`; **no KPI math in templates**;
  batch jobs as management commands run by the existing daemon; dashboard reuses the `vehicle_profit_report` shape.

## 10. KEY ASSUMPTIONS (correct if wrong)

- Recovered margin uses **base pay** on both sides (gratuity = pass-through; `driver_additional`/night
  bonus excluded by default, tunable).
- "Rides performed" excludes `status='cancelled'`; service date = `pickup_date`.
- Legs with no matched route / null `inhouse_base_pay` are **coverage gaps**, not imputed.
- Driver pay is **per-trip** (`DriverPayRate` / `route.inhouse_base_pay`); **no guaranteed/daily/hourly
  minimums found** — flag if any appear.
- Any driver can drive any vehicle (tier permitting); **no driver→vehicle qualification restrictions found.**
- **Permits out of scope** (all current vehicles permitted) — future/new-vehicle onboarding note only.

## 11. PHASED PLAN

- **Phase A (build first, read-only, no migrations):** `dispatching/fleet_intel.py` — leg loading +
  `fulfillment_of`; per-leg economics (driver-pay-only recovered margin pos/neg/net with coverage handling);
  `classify_farmout` via the engine; KPI aggregators by type/reason/zone/day/hour/affiliate;
  `analyze_farmouts` management command (offline harness); `tests_fleet_intel.py`.
- **Phase B:** `fleet_intel_dashboard` view (staff-gated, 60s cache) + template + URL. Reuse
  `vehicle_profit_report` layout; exec cards, in-house vs farm-out, reason table, vehicle-type margin, leaks.
- **Phase C:** `VehicleTypeCostProfile` (config, placeholder fixed costs, *is_estimated*) +
  `simulate_plus_one_vehicle` (engine replay) → marginal absorbable legs, break-even, monthly profit change,
  **BUY / HOLD / HIRE-DRIVER / KEEP-FARMING** labels (conservative gating), all stamped *ESTIMATED*.
- **Deferred:** `FarmOutDecision` capture + reason picker + `Leg.fulfillment_method`; fuel/tolls/wear layer;
  future-demand forecast + `ForecastRun`; scheduling assist; 30/90-day trends.

### Classification decision tree (Phase A)
1. No tier-compatible in-house vehicle deployed that day → **VEHICLE_TYPE_SHORTAGE**.
2. Compatible vehicle existed, no compatible driver passes `check_feasibility` (window/off-shift) →
   **DRIVER_SHORTAGE** → split **DRIVER_IDLE_OR_OFF_SHIFT** vs **UNIT_CAPACITY**.
3. Driver+vehicle existed, fails only on turnaround/reposition → **POSITIONING_ISSUE**.
4. `check_feasibility` passes for ≥1 driver: direct insert → **DISPATCH_LEAK**; only via `find_swaps`
   cascade → **SCHEDULING_PROCESS_LEAK**.
5. Conflict driven by flight arrival/delay (`classify_turn` / `has_flight_time_mismatch`) → tag
   **FLIGHT_DELAY_LEAK**.
6. In-house feasible *and* it protected a higher-value job → **SMART_FARM_OUT** (low-confidence/inferred).

## 12. RISKS & ASSUMPTIONS

- **No historical decision snapshots** → pre-launch classification is *reconstructed* (final board) and
  *inferred*; confidence improves only after `FarmOutDecision` capture runs forward.
- **Counterfactual coverage holes** where route/`inhouse_base_pay` null → report coverage %, exclude, don't impute.
- **Fuel/tolls/wear are assumptions** — report margin with and without any cost layer; avoid fake precision.
- **Stub driver windows** blur DRIVER_IDLE vs UNIT_CAPACITY and weaken future forecasts.
- **Local DB lacks `LegStatus` history** → validate on prod-representative data before trusting recommendations.
- **Performance:** service-layer aggregates + caching only (the 2026-05-31 hotfix was a sync-call-in-render
  timeout); never live N+1.
- **Migration risk:** additions nullable/backward-compatible; never alter dispatch/payment/scheduling behavior;
  batch jobs run under the existing daemon advisory lock (no Celery).
- **Trust:** Phase A–B are decision-support; a BUY recommendation (Phase C) is trustworthy only after
  ≥90 days of data + clean shifts + real costs.

## 13. WHAT TO BUILD FIRST

The **read-only economics + farm-out classification service** (Phase A) — zero migrations, zero dispatch
changes, immediate insight, validated offline. Delay the schema-touching capture (`FarmOutDecision`,
`Leg.fulfillment_method`) and the buy simulation until that layer validates the numbers and cost inputs are confirmed.

---

## CRITICAL FILES

- **New:** `dispatching/fleet_intel.py`, `dispatching/management/commands/analyze_farmouts.py`,
  `dispatching/tests_fleet_intel.py` (A); `content/templates/dispatching/fleet_intel_dashboard.html` (B);
  `dispatching/fleet_simulation.py` + `VehicleTypeCostProfile` model/migration/admin (C).
- **Modify:** `dispatching/views.py` (+dashboard view), `dispatching/urls.py` (+route), `dispatching/admin.py` (C).
- **Reuse (do not duplicate):** `dispatching/scheduler.py`, `dispatching/feasibility_guards.py`,
  `dispatching/swap_optimizer.py`, `dispatching/analytics.py`, `drivers/pay_calc.py`, `ops/kpis.py`,
  `ops/tasks.py:classify_turn`.

## VERIFICATION

1. **Offline harness:** `manage.py analyze_farmouts --start 2026-05-09 --end 2026-05-09` (busy) and
   `--start 2026-06-01` (slow); reconcile in-house/affiliate/unassigned counts vs `get_coverage_stats`.
2. **Formula tests:** `manage.py test dispatching.tests_fleet_intel`; keep the existing suite green.
3. **Hand spot-check:** one affiliate leg → `recovered_margin = driver_base_pay − route.inhouse_base_pay`
   and pos/neg/net bucketing.
4. **Dashboard (B):** run local server (`localtest`/`Local2026!`), load the view, confirm 60s cache + no
   `SlowRequestMiddleware` (`perf`) warnings.
5. **Simulation (C):** run on 2026-05-09; assert marginal absorbable legs ≤ that day's farm-out count and
   coverage diff reconciles with the engine; confirm outputs render the *ESTIMATED* label until real costs entered.
