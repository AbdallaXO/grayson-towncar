# Scheduler Automation — Phase 2.5 Findings (Margin Re-measurement)

**Status:** **COMPLETE — Phases 2.5a + 2.5b + 2.5c done.** Same hard rules as Phase 2: strictly
read-only via the `readonly_local` role (`transaction_read_only=on`), `apply=false`, `find_swaps`
write-free, live data, **no writes**. Harness: `scratch/phase25a_*.py`, `scratch/phase25_margin.py`.
The `revenue_share` data-integrity problem is logged separately in
[`data_integrity_revenue_share_issue.md`](data_integrity_revenue_share_issue.md). **No Phase 3 / code changes.**

**Headline:** Recast on **margin (driver cost)** instead of revenue, the Phase-2 "engine covers more
but farms more revenue" concern **largely dissolves** — keeping a leg in-house costs ~$25–50 vs
~$70–230 to farm, so the auto-build's higher in-house coverage is a **net cost saving of ~$2,025
(handicapped) to ~$2,990 (level-field) across the 8 archetype days**. But it is **day-dependent**:
big savings on days with spare capacity (cruise/light), **break-even-to-worse on capacity-bound
busy days** — where farming was, in fact, the correct call. Strict negative-margin-to-farm legs are
**rare** (2 of 10 days).

---

## Part A — Phase 2.5a: The Affiliate / Farm Rate Model (investigation, nothing changed)

### A.1 Structure — confirmed

The affiliate rate card **is the `DriverPayRate` model** (`drivers/models.py:504`), keyed by
**(driver, route, vehicle, direction) → `base_pay`**, plus a **per-driver `Driver.night_bonus`**
(`drivers/models.py:64`, default $10) applied **10:01 PM–5:59 AM** (`drivers/pay_calc.py:140`).

- The "zone-pair" you described **= `rates.Route`** (origin `Location` → destination `Location`).
  Vehicle class = `rates.Vehicle`. Direction = `both` / `forward` / `reverse`.
- There is **no separate global "standard" rate-card model.** Each affiliate carries their own
  `DriverPayRate` rows. The "standard rates" are simply the **full-coverage affiliate cards**
  (anthony et al.); the "cheap" ones are affiliates with lower-priced cards (Oualid, shaq…).
- **In-house** legs do **not** use affiliate cards: they fall back to **`Route.inhouse_base_pay`**
  (`drivers/pay_calc.py:125-132`), with optional per-driver override.

### A.2 Rate resolution — confirmed (`drivers/pay_calc.py:calculate_driver_pay`)

For an **affiliate** leg, `_find_rate(driver, route, vehicle, direction)` tries, in order:
1. exact direction **+** vehicle → 2. `both` + vehicle → 3. exact direction + all-vehicles (NULL)
→ 4. `both` + all-vehicles. Returns `None` if no row matches (→ manual entry).
Direction is inferred by matching the leg's pickup text against the route origin/destination
names + aliases (`_determine_direction`). **A leg must have `leg.route` set to be card-priced.**

Night bonus: `calculate_night_bonus` adds `driver.night_bonus` iff pickup ≥ 22:01 or ≤ 05:59.

Per-leg cost/margin is already modeled in the system: `Leg.total_driver_pay`
(`driver_base_pay + gratuity + additional`, else legacy `driver_pay_amount`) and
`Leg.calculate_profit()` = `revenue_share − total_driver_pay` (`reservations/models.py:1220-1242`).
So **State A's *actual* farm cost is readable from stored fields** (no modeling needed); only
State B's counterfactual farming needs the waterfall.

### A.3 Cheap affiliates vs standard cards (LIVE data)

| Affiliate | id | #rate rows | base_pay range | Vehicles | night | Notes |
|---|---|---|---|---|---|---|
| **oualid** | 7 | 12 | **$70–$120** | ALL (NULL) | $10 | **Cheapest**; SUV-only (business rule, not in card); limited route coverage |
| shaq | 19 | 2 | $70–$80 | ALL | **$0** | Very cheap, tiny card (2 routes) |
| hany | 30 | 6 | $90–$140 | ALL | $10 | Small card |
| martin | 8 | 8 | $110–$190 | ALL | $10 | Mid |
| **anthony** | 29 | 65 | $90–$230 | per-vehicle | $10 | **Standard full card** (your reference) — a company; treat as unlimited capacity |
| wael | 5 | 81 | $0–$150 | all types | $10 | Largest card; **some $0 rows** (data oddity) |
| Cheapo Limo | 27 | 70 | $75–$195 | all types | $15 | Full card |
| babu | 44 | 65 | $90–$200 | all types | $10 | Full card |
| logictrans | 13 | 45 | $90–$220 | all types | $10 | Full card |
| 12 others | — | 0 | — | — | $10 | **No card rows** → manual pay only |

**Oualid's card (cheapest, 12 routes):** MCO→{Disney, I-Drive, Kissimmee, Omni, Universal} = **$70**;
Sea World→Disney, Universal→Disney, Disney→Universal = **$70**; Disney→Port = **$120**;
Port→MCO = **$120**; Sanford→{Disney, Universal} = **$120**. Card is **ALL-vehicle** priced at
SUV level — so the SUV-only restriction must be enforced in the waterfall, not read from the card.

**Anthony's card (standard, 65 rows, per-vehicle):** local SUV/towncar/minivan **$90**, local van/14-pax
**$135**; Kissimmee/Omni **$100 / $145**; Disney→Port SUV **$165**, van **$230**; Port→MCO SUV **$145**,
van **$200**; Sanford **$145**.

### A.4 In-house is far cheaper than farming — the dominant economic fact

`Route.inhouse_base_pay` (all 19 routes populated): **local $25, Port $40, Sanford $40–50,
Omni $35, Flamingo $30.** Compare to farm cost **$70–$230**. So **keeping a leg in-house costs
~$25–50; farming the same leg costs ~$70–230 — a $45–180 premium per farmed leg.** This reframes
everything: farming is *expensive*, done only when in-house capacity runs out. Covering more legs
in-house (Phase 2's State B) is, on cost, a **margin gain** — provided the schedule is feasible
and capacity/quality limits hold.

### A.5 Zone classifier & mappability

13 `Location` zones, 19 `Route`s: Disney, Disney Springs, Flamingo Crossings, I-Drive,
Kissimmee 192, Omni Championsgate, MCO, **Port Canaveral**, Sanford, Sea World, Universal Area,
Universal Hotels, Winter Garden. Legs carry a `route` FK already — **no geocoding needed**.

Route coverage on the 5 Phase-2 days: **04-23, 05-09, 05-21, 05-02 = 100% routed; 03-29 (cruise
Sun) = 15% unrouted (20/131).** Unmappable patterns (flag, don't force):
- **Cruise-terminal-specific** pickups/dropoffs ("Royal Caribbean Port", "Norwegian Cruise Line",
  "Cruise Terminal 8") — not matched to the single `Port Canaveral` zone.
- **Named individual resorts → MCO** that didn't resolve to a route (Disney's Beach Club, Old Key
  West, Port Orleans, Yacht Club, Saratoga Springs…).
- **Routes with in-house pay but NO farm card**: →Winter Garden, →Flamingo Crossings exist in
  `inhouse_base_pay` but neither Oualid nor anthony has farm rows → those legs are **un-farm-priceable**
  by the waterfall (would need manual/another affiliate). Cocoa Beach / far hotels: not a zone at all.

These unmappable legs will be **excluded from the farm-cost model and reported separately**, not forced.

### A.6 ⚠ CRITICAL DATA FINDING — `revenue_share` is unreliable as per-leg guest price

**275 of 608 legs (45%) across the 5 days have `revenue_share = $0`**, and 273 of those belong to
reservations whose `total_price > 0`. Two causes:
1. **Round-trip allocation** — `recalculate_leg_revenue_shares` (`reservations/models.py:481`) splits
   `total_price` across legs; weighted splits put **all revenue on one leg and $0 on the sibling**.
2. **Single-leg reservations never populated** — e.g. res 9902 ($168, 1 leg, share $0), res 9349
   ($213, 1 leg, $0), res 8802 ($126, 1 leg, $0). And one reservation's leg shares **exceed** its
   total (res 6708: legs sum $1,035 vs total $740) — the denormalized split is simply broken for some.

**Implications:**
- Per-leg `revenue_share` **cannot** be used as `guest_price` for margin — it is $0 for ~45% of legs.
- **The Phase-2 "farmed revenue" figures (including the "+$1,437" busy-Saturday result) were computed
  on `revenue_share` and are therefore distorted** — treat them as withdrawn pending this re-measurement.
- Correct basis = **`reservation.total_price`** (the true guest fare). For multi-leg reservations,
  allocate per-leg as an even split (`total_price / num_legs`) for the flip analysis, and—because the
  guest revenue is identical across States A and B (every leg is served either way)—make
  **total driver cost** the primary margin metric (below).

### A.7 Proposed methodology for 2.5b/2.5c — please confirm before I run

1. **Primary metric = total driver cost per day** (lower = higher margin, since day revenue is fixed):
   - State A: Σ **actual** `total_driver_pay` over all legs (in-house + farmed), from stored fields.
   - State B: Σ `inhouse_base_pay` (+night) for in-house legs **+** waterfall farm cost for residuals.
2. **Farm-cost waterfall (2.5b):** residual **SUV-or-lower** legs → **Oualid first**, fit onto his
   single-vehicle chain via the real `check_feasibility`/chain engine (no time clashes), each at his
   card rate; legs that don't fit his chain (or aren't on his 12 routes) **spill to anthony** (standard,
   unlimited/simultaneous). **Van / 14-pax → anthony directly** (Oualid SUV-only). Night bonus applied
   per the model. Report Oualid's absorbed-leg count per day for your sanity-check.
3. **Per-leg flip analysis:** guest price = `reservation.total_price / num_legs`; flag legs where
   **farm_cost ≥ guest price** (negative/thin farm margin — should never be farmed), and revisit the
   busy-Saturday case on margin (real margin lost vs thin-margin Port legs correctly farmed).
4. **Caveat to record:** State A's actual farming used a *richer* affiliate roster (shaq $70, Cheapo
   $75, oualid, wael…), so the Oualid→anthony waterfall may **overestimate** State B's farm cost vs
   what a human achieves with all affiliates — i.e., it is a conservative (pessimistic-for-B) model.

### A.8 Proposed day set for 2.5b/2.5c

**Archetype set (8, compared apples-to-apples):** 04-23, 05-09, 03-29, 05-21, 05-02 (original 5)
+ 05-16 (Sat), 05-23 (Sat), 05-17 (Sun).
**Cruise-weekday probes (2, reported separately — NOT aggregated into the archetypes):**
**2026-04-24 (Fri, 97 legs, 20 cruise)** and **2026-04-08 (Wed, 60 legs, 10 cruise = highest cruise
share of any Tue/Wed in the window)**. (Per your instruction: kept one Friday, swapped the second
Friday 04-03 for a midweek day so "weekday" isn't only Fridays.)

---

## Part B — Phase 2.5b: The Farm-Cost Waterfall

For each leg State B farms (its residuals after build + swap), farm cost is modeled as a
**feasibility-dependent waterfall**, not a flat lookup:

1. **SUV-or-lower residuals → Oualid first.** Oualid is modeled as a **single SUV vehicle**,
   feasibility-capped (not a number cap): each candidate is tested against his growing chain with
   the real `check_feasibility` (live `inter_job_buffer=0`, `arrival_grace=10`) in pickup-time order;
   if it fits **and** his 12-route card prices it, he absorbs it at his rate (+night bonus).
2. **Spill to anthony** (standard, treated as unlimited / simultaneous) for any SUV-or-lower leg that
   doesn't fit Oualid's chain or isn't on his 12 routes.
3. **Van / 14-pax → anthony directly** (Oualid is SUV-only).
4. **Uncarded** (route in neither card, e.g. →Winter Garden, cruise-terminal strings) → leg is
   **excluded** from the cost comparison and reported, never force-priced.

Costs come from the real rate model (`drivers/pay_calc.calculate_driver_pay` + `calculate_night_bonus`).

**Oualid absorption (sanity-check vs reality — does he really do ~this many SUV runs/day?):**

| Day | B-farmed | **Oualid absorbed** | anthony | uncarded/excluded |
|---|---|---|---|---|
| 04-23 | 5 | 1 | 4 | 0 |
| 05-09 | 49 | 5 | 44 | 4 excl |
| 03-29 | 41 | 5 | 31 | 5 uncarded + 22 excl |
| 05-21 | 6 | 1 | 5 | 2 excl |
| 05-02 | 47 | 5 | 42 | 5 excl |
| 05-16 | 41 | 5 | 36 | 3 excl |
| 05-23 | 7 | 1 | 6 | 0 |
| 05-17 | 23 | 3 | 20 | 2 excl |
| *04-24 (probe)* | 11 | 3 | 8 | 2 excl |
| *04-08 (probe)* | 7 | 1 | 6 | 2 excl |

Oualid absorbs only **1–5 SUV legs/day** — limited by both his single-vehicle feasibility chain **and**
his 12-route card (many residuals are off his routes or vans). **Please sanity-check this against how
many runs Oualid actually does.** If in reality he does more, the waterfall under-uses him and B's
farm cost is even more overstated (B looks even better than reported).

---

## Part C — Phase 2.5c: Margin Re-measurement (driver cost, A vs B)

**Cost basis.** Revenue is identical across states (every leg is served either way), so the margin
delta = **driver-cost delta**. Three cost figures per state:
- **A actual** = Σ stored `total_driver_pay` (what the day really cost; includes gratuities/overrides).
- **A modeled** = rate-model price of A's split, **A's farm legs at their real (often cheap) affiliates**.
- **A level-field** = rate-model price of A's split, **A's farm legs re-priced via the same Oualid→anthony
  waterfall as B** (isolates the schedule/coverage effect from affiliate-choice pricing).
- **B modeled** = rate-model price of B's split (in-house at `inhouse_base_pay`+night; residuals via waterfall).

> Δ (handicap) = A_modeled − B_modeled. Positive = B cheaper. B is **deliberately handicapped** here
> (A keeps its real cheap affiliates: shaq $70, Cheapo $75, Oualid…; B is restricted to Oualid+anthony).
> Δ (level-field) = A_level-field − B_modeled isolates the schedule by pricing both farms identically.

### C.1 Cost ladder — 8 archetype days

| Day | DOW | Legs | A in-house | B in-house | A actual $ | A modeled $ | A level-field $ | B modeled $ | **Δ handicap** | **Δ level-field** |
|---|---|---|---|---|---|---|---|---|---|---|
| 04-23 | Thu | 88 | 71 | 83 | 4,102 | 3,390 | 3,410 | 2,730 | **+660** | +680 |
| 05-09 | Sat | 148 | 97 | 99 | 9,019 | 6,985 | 7,235 | 7,530 | **−545** | −295 |
| 03-29 | Sun | 131 | 77 | 90 | 8,403 | 6,630 | 6,735 | 5,820 | **+810** | +915 |
| 05-21 | Thu | 92 | 82 | 86 | 3,809 | 2,975 | 2,995 | 3,015 | **−40** | −20 |
| 05-02 | Sat | 149 | 85 | 102 | 9,313 | 7,985 | 8,175 | 7,095 | **+890** | +1,080 |
| 05-16 | Sat | 146 | 97 | 105 | 8,421 | 6,885 | 7,135 | 6,725 | **+160** | +410 |
| 05-23 | Sat | 108 | 91 | 101 | 5,520 | 4,215 | 4,290 | 3,745 | **+470** | +545 |
| 05-17 | Sun | 116 | 93 | 93 | 6,011 | 4,705 | 4,760 | 5,085 | **−380** | −325 |
| **Total** | | **978** | **693 (70.9%)** | **759 (77.6%)** | **54,598** | **43,770** | **44,735** | **41,745** | **+2,025** | **+2,990** |

**Reading it:**
- **Net, B is cheaper** — by **$2,025** even handicapped, **$2,990** level-field — i.e. the auto-build's
  +66 in-house legs (8 days) save real driver cost. Per extra in-house leg the realized saving is
  ~$30–45 (less than the full $45–180 farm premium, because some legs go to cheap Oualid either way and
  busy-day farm mixes shift).
- **It's day-dependent.** B wins big where it has spare capacity to pull legs in
  (03-29 +$810/+$915; 05-02 +$890/+$1,080; 04-23 +$660). B is **break-even-to-worse on capacity-bound
  days** (05-09, 05-17, ~tie 05-21) — and on those days it loses **even level-field**, so it isn't just
  the affiliate handicap: when in-house capacity is exhausted, the engine's different farm mix is
  slightly costlier and farming is the right call.

### C.2 Cruise-weekday probes (reported separately — do NOT compare to the archetypes)

| Day | DOW | Legs | A in-house | B in-house | A modeled $ | A level-field $ | B modeled $ | Δ handicap | Δ level-field |
|---|---|---|---|---|---|---|---|---|---|
| 04-24 | Fri | 97 | 73 | 86 | 4,380 | 4,560 | 3,545 | +835 | +1,015 |
| 04-08 | Wed | 60 | 53 | 53 | 2,200 | 2,230 | 2,335 | −135 | −105 |

Same pattern: the cruise-heavy Friday (spare capacity, +13 in-house) saves ~$835–1,015; the low-volume
midweek Wednesday (engine adds **0** in-house — already near-saturated in-house) is ~break-even.

### C.3 The busy-Saturday revisit (your specific question)

Phase 2 flagged 05-09 as "engine farmed +$1,437 more revenue." **On margin, that was a false alarm.**
On 05-09 the engine adds only **+2 in-house** (97→99) — it is **capacity-bound** — and it costs **more**,
not less: **−$545 handicapped and −$295 even level-field.** So the legs the team farmed that day were
**correctly farmed** (no in-house capacity to keep them); the engine cannot do better, and the
"farmed revenue" figure was both distorted (`revenue_share`) and the wrong lens. **No real margin was lost.**

### C.4 Per-leg flip analysis (should-never-farm legs) — two tiers

Definition: a leg "flips" if **farm_cost ≥ guest price** (farming retains ≤$0 margin).
- **Tier 1 (high-confidence):** flips under **both** even-split (`total_price/num_legs`) **and** the
  leg's non-zero stored `revenue_share`.
- **Tier 2 (suspect):** flips under even-split **only** (its `revenue_share` is $0/contradicts — likely
  an artifact of the broken split; **do not act on these as if confirmed**).

**Result: flips are rare — 2 legs across all 10 days, both vans:**

| Tier | Day | Leg | Type | Farm cost | Guest (even) | revenue_share | Note |
|---|---|---|---|---|---|---|---|
| **1 (confirmed)** | 04-24 | 18426 | van | $135 | $135 | $135 | MCO→Disney van: fare $135 = anthony van rate $135 → **$0 margin to farm.** Keep in-house (~$25). |
| 2 (suspect) | 05-17 | 18043 | van | $135 | $135 | $0 | Universal→Disney van; `revenue_share=$0` so unconfirmed (likely same zero-margin pattern). |

**Interpretation:** your "$75–80 locals are negative-margin to farm" hypothesis **did not materialize** —
cheap local SUV/sedan legs route to **Oualid at $70**, which stays **below** the fare, so they're
margin-positive to farm. The only zero-margin cases are **low-priced vans** where anthony's flat $135
van rate equals the van fare. The real inefficiency is **not** a pile of negative-margin farm legs; it's
the **structural premium** (every farmed leg costs $45–180 more than in-house), which the auto-build
attacks by keeping more in-house — when capacity allows.

### C.5 Caveats & limitations

- **B is handicapped** (Oualid+anthony only; your real farming also used shaq/Cheapo/wael at lower
  rates). B still wins net → strong. Level-field confirms the win isn't only the handicap.
- **Exclusions.** Uncarded/unrouted legs are dropped from the cost comparison: heaviest on the cruise
  Sunday 03-29 (**27 of 131 ≈ 21%** — cruise-terminal strings + →Winter Garden/Flamingo with no farm
  card); ≤5/day elsewhere. So 03-29's deltas cover ~79% of legs. (These were flagged in §A.5.)
- **A actual > A modeled** ($54,598 vs $43,770 across 8 days) because actual includes
  gratuities/additional/overrides the model omits — another reason the modeled A-vs-B is the fair lens.
- **Quality costs carry over from Phase 2.** This analysis prices driver cost only; the auto-build's
  higher coverage still comes with the Phase-2 problems — **18–24h spans, more tight turns, no capacity
  check** (see `scheduler_automation_phase2_findings.md` §5, §7). A cheaper schedule on paper that a
  driver can't physically run is not actually cheaper. Margin and feasibility/quality must be read together.
- **Single-realization, static** greedy; `revenue_share` unreliable (used `total_price` instead).

### C.6 Bottom line (margin)

- On driver cost, the auto-build is **net cheaper (~$2,025–2,990 over 8 days, ~$250–375/day on average)** —
  it converts expensive farm-outs ($70–230) into cheap in-house runs ($25–50) **when in-house capacity
  exists**. The Phase-2 "farms more revenue" worry does not survive a margin lens.
- The savings are **concentrated on days with spare capacity** (light weekdays, cruise days) and
  **vanish on capacity-bound busy days**, where farming is correct. So the margin case for automation is
  really a case for **using spare in-house capacity better**, not for farming less on peak days.
- **Negative-margin-to-farm legs are essentially a non-issue** (2 vans in 10 days). Don't build policy
  around them; do consider a simple guard that **low-priced vans never farm to a $135 flat rate** when an
  in-house van is free.
- These margin gains are **only real if paired with the Phase-2 quality/capacity guards** — otherwise the
  "cheaper" schedule isn't operable.

*Phase 2.5 complete. No Phase 3 / code changes performed. Awaiting direction.*
