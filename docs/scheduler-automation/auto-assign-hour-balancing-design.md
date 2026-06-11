# Auto-Assign Hour Balancing — Design Record (2026-06-09)

> **STATUS UPDATE (2026-06-09, later same day): PHASES 1+2 BUILT (uncommitted) — see PART 3
> at the bottom for what shipped + validation results. Phases 3-5 remain designed-only.**

**Status: DESIGNED + ADVERSARIALLY REVIEWED, not built.** Produced by an 11-agent workflow
(5 code/data readers → 3 independent designs → 3-lens judge panel). All three judges
(founder-fidelity, engineering-risk, operational-realism) independently picked the same winner
(8/8/8) over the alternatives (7/7/7 and 4/4/5).

**Problem (founder):** Auto Assign All sometimes builds 15–18h driver days. Want realistic day
lengths WITHOUT losing in-house jobs, plus shift structuring (early crew finishes early, late
starters cover the night) and optional AM/PM splitting of one vehicle across two drivers.

---

## Root cause (the key discovery)

**A hard duty-span cap already exists end-to-end** — Guard C in `window_check`
([feasibility_guards.py:191-193](../../dispatching/feasibility_guards.py#L191)) hard-rejects
`span_hours_after > max_hours`, applies even to flexible drivers, and every insert path (greedy,
swap pass, gap compaction, manual swap validation) flows through the single
`get_effective_window` funnel ([feasibility_guards.py:128-146](../../dispatching/feasibility_guards.py#L128)).

The 15–18h days happen because of **values, not missing machinery**:

1. `USE_STUB_WINDOWS=True` → `max_hours` comes from `STUB_DRIVER_WINDOWS`
   (fg.py:54-83), which holds optimistic observed-history values: **David 24h, roberto/neuma 23h,
   Rayyan 22h, shelley 21h, ken/Aftab 20h**. The stub also DISCARDS the modal's start/end/max_hours
   (keeps only `flexible`); the modal hard window is only enforced by the greedy pickup-hour
   pre-filter (scheduler.py:1342-1346) — find_swaps and gap compaction never see it.
2. `build_smart_schedule` hardcodes `max_hours=None` (scheduler.py:2030) → **Build-1st seeding has
   no span cap at all**.
3. The existing soft span penalty (scheduler.py:1517-1530, 30 pts/hr over 13h) is too weak —
   routinely outweighed by chain (45) / coherence (50) / scarcity bonuses.

## Data audit — founder's own hand-built boards (calibration ground truth)

Measured over 39 in-house driver-days on 05-09, 05-16, 06-02 (06-01 is unbuilt in the local DB).
Harness: `scratch/span_audit_0609.py` (+`_share.py`, `_share2.py`), read-only, gitignored.

- **Raw duty span: median 12.3h, p90 15.2h, max 16.5h. 72% exceed 10h, 51% exceed 12h, 18% exceed 14h.**
  A 10h or even 12h cap would contradict the founder's own practice.
- **Every >15h day except one is a split-day with a 3–5h internal hole.** Max *continuous* duty
  (span minus biggest hole) ≈ 13.5h, except one 15.5h outlier (roberto 05-16). → the right soft
  metric is **effective span** (raw minus one ≥2h break), not raw span.
- **Late-night legs are rare** (two ≥21:00 legs across all three built dates). The real structure is
  an **early crew**: 05-16 has five drivers starting 03:00–04:30, clearing ~15:00–17:00. When a
  23:59 leg existed (05-09), the founder started roberto at 09:30 — the late-starter pattern.
- **Vehicle splitting is established founder practice**: 42 shared vehicle-days across 148 dates,
  **34 clean sequential AM/PM splits** (~1 per 3–4 operating days). e.g. 2026-05-28 unit #006:
  Michael Olmo 05:35–15:35 (7 legs) → Raymond 19:23–23:37 (2 legs). `DriverVehicleAssignment` is
  unique on (driver, date) only, so the schema already allows it — but **the engine has no vehicle
  occupancy model at all** and can silently double-book a shared car (the 06-07 Yovanny/Idrees
  overlap is a real instance).
- Job counts: busy-day norm 6–9 legs/driver-day (median 7–8, max 10).

## Winning design: "Span Governor" (+ grafts from the two losing designs)

Two tiers, two metrics, one funnel; an escalation ladder that **structurally cannot farm a leg**.

### Tier 1 — HARD ceiling on RAW span (feasibility)
- `get_effective_window` clamps `max_hours = min(stub, configured/modal, SPAN_HARD_HOURS_DEFAULT=17.0)`.
  Unknown driver ids get a synthetic `{start:None, end:None, max_hours:cap, flexible}` (start/end MUST
  stay None — an end value would newly enforce clear-by on drivers who today have no window).
- **17h raw** is calibrated to never block anything the founder built himself (his max: 16.5h) while
  trimming exactly the stub absurdities. It's a backstop, not the workhorse.
- Close the Build-1st hole: pass a real cap into `build_smart_schedule` (scheduler.py:2030).
- New `enforce_cap=False` kwarg for the manual swap-validation endpoint (manual stays sovereign)
  and pin `fleet_intel` / `farmout_optimizer` consumers to uncapped behavior (audit all
  `get_effective_window` call sites; regression-test `analyze_farmouts` output unchanged).

### Tier 2 — SOFT steering on EFFECTIVE span (scoring)
- `effective_span = raw span − largest PRE-EXISTING internal gap if ≥120min (credit capped at 300min)`.
  Gap credit computed from the schedule BEFORE the insert only (else the engine learns to mint holes).
- Replace the weak flat penalty (1517-1530) with **marginal progressive pricing** (grafted from
  Design 2): charge each candidate only the span-stretch THIS leg adds — free under ~12h effective,
  mild 12–13h, steep beyond. Empty schedule ⇒ ~0 cost, so a 22:00 leg cheaply seeds a fresh late
  starter while stretching a 04:00 driver costs hours×rate → **early/late shifts EMERGE without
  templates** (reproduces the founder's roberto-09:30-carries-the-23:59-leg move).
- Add the identical term to `_score_leg_for_smart_schedule` (~2493, currently has NO span term) —
  but note the seeder seats only on score>0 (scheduler.py:2293-2296): quantify the penalty against
  the seeder's score scale and regression-test the 06-03 Yovanny Build-1st case still seeds 7 legs.
- Soft target: **13.0h effective** (founder's continuous ceiling; coheres with existing
  span_threshold_hours=13).
- Optional "two-track" selection (always prefer an under-soft feasible driver; over-soft only when
  no under-soft exists, tagged amber) is a stronger variant — **ship penalty mode first**; if
  two-track is later enabled, its precedence vs the reserved-mismatch fallback (1548-1571) must be
  explicit: under-soft non-reserved > over-soft non-reserved > under-soft reserved > over-soft reserved.

### Escalation ladder (priority-#1 guarantee: the cap can NEVER silently farm)
1. Best under-soft driver → 2. best over-soft ≤ hard driver (amber badge) →
3. pre-farm swap cascade with **capped windows threaded in** (`recover_residuals_via_swaps` gains a
   `driver_windows` pass-through to `find_swaps`; the map MUST contain every working driver or the
   receiver pool silently shrinks — swap_optimizer.py:301-306 restricts to dict keys) →
4. **NEW `rescue_span_blocked_residuals` pass** (between swap pass and gap compaction): retry each
   residual with `max_hours` lifted; if a driver now passes, the leg was blocked ONLY by span —
   assign with a loud RED badge ("16.3h — OVER CAP, kept in-house to avoid farming ~$140") →
5. only if nobody passes even uncapped does it farm (same as today).
- Rescue must replicate the vehicle-tier filter and the modal pickup-hour filter (check_feasibility
  checks NEITHER — they're separate greedy pre-filters at 1358-1362 / 1342-1346).
- **Explicit dispatcher Max hrs is STRICT** (graft from Design 3): rescue may lift only the GLOBAL
  default/stub caps; a hand-typed per-driver cap farms with a named reason in Need Affiliates
  ("blocked by 10h cap; David was feasible at 11.2h"). Anything else recreates "it ignores my times".
- Frozen-driver fix: window_check gates on TOTAL span, not growth — a driver already over his cap
  (e.g. modal Max hrs below his saved day) would be frozen out of ALL inserts including hole-fills.
  Gate on the DELTA when span_before already exceeds the cap.

### Active shortener — `trim_spans_via_relocation` (grafted from Design 2; Phase 3)
Two-track/pricing only PREVENTS long days; days made long by Build-1st windows, pins, or
sole-feasible-driver chains need an active fix. Clone of `compact_gaps_via_relocation`
(scheduler.py:1743-1916), wired AFTER the swap pass, BEFORE gap compaction:
- Donors: effective span > 13h or raw > 15h. Candidate legs: donor's FIRST or LAST slot only
  (the only span-shrinking legs), unlocked.
- Receiver gates: tier compat, MODAL hard window honored (more correct than today's gap compaction,
  which builds from saved availability — thread modal windows there too as a companion fix),
  check_feasibility, receiver stays under both limits.
- Accept: `donor_relief ≥ 45min AND donor_relief − receiver_stretch ≥ 30min`. Lock moved legs
  (no ping-pong), ≤3 peels/donor, ≤2 receives/receiver, ≤12 moves. **Farms nothing, ever** —
  assert the assignment keyset is unchanged (one-line coverage invariant, add to every pass).
- This is literally the founder's "Roberto just starts later" move applied to day length.

### AM/PM vehicle splits — minimal-but-real MVP (Phase 4)
- Detect split pairs from same-unit `DriverVehicleAssignment` rows (zero schema change).
- Greedy maintains a merged per-vehicle timeline (initialized from PRE-EXISTING saved legs, not just
  new ones); every insert for a shared-vehicle driver must pass `vehicle_handoff_ok`: no overlap
  with the partner's slots + REAL reposition drive (`resolve_drive_minutes`) + 15min pad, checked
  cross-driver only. A fixed pad without drive time is not enough — hand-built splits had hours of
  separation, but an auto-builder packs to window edges.
- The gate must cover ALL insert paths: greedy, trim/gap receivers, swap-solution post-validation,
  rescue. Build-1st seeding bypasses the candidate loop → **exclude shared-vehicle drivers from
  Build-1st in v1** (warn in modal).
- Post-build `validate_vehicle_handoffs` red banner (graft from Design 3) as belt-and-suspenders —
  the only protection for manual pins.
- UI (graft from Design 3, client-side only): linked rows + ONE editable "Handoff hour" control
  that sets AM End / PM Start together; Flexible disabled for split drivers. No server-side window
  mutation.
- One-line refinement: `exact_type_driver_counts` counts a shared van once per driver — count
  distinct vehicle UNITS per tier so scarcity logic isn't fooled on split days.
- Deferred: engine-initiated splits, handoff-location modeling, interleaved sharing.

### Modal/preview UI
- Step-1: new "Max hrs" number column (blank = global 17; typed value = strict), included in the
  `driver_hours` payload.
- Preview: amber badge ("14.2h — over 13h target"), red rescue badge (names WHICH cap was lifted),
  warnings summary line ("2 drivers over target · 1 leg kept in-house over cap"), caps-too-tight
  notice if rescues pile up. Long-day explainability: "roberto 15.4h — could not shorten: no
  feasible receiver for his 23:59 leg; kept in-house per priority #1."

### Flags (proposed)
`ENFORCE_SPAN_CAPS=True` (master kill switch; False = byte-identical current behavior),
`SPAN_HARD_HOURS_DEFAULT=17.0`, `SPAN_SOFT_EFFECTIVE_HOURS=13.0`, `SPAN_GAP_CREDIT_MIN_MIN=120`,
`SPAN_GAP_CREDIT_MAX_MIN=300`, `SPAN_COVERAGE_RESCUE=True`, `SPAN_SOFT_MODE='penalty'` (marginal
pricing; 'two_track' as the stronger later variant), `AUTO_SPAN_TRIM_PASS` (+ trim knobs: target 13h
eff / 15h raw, relief≥45, net≥30, ≤12 moves), `SPLIT_VEHICLE_AWARE=True`, `VEHICLE_HANDOFF_PAD_MIN=15`.

## Ship order (judge-mandated)

1. **Phase 1 — hard tier + rescue + badges + Max hrs column.** Near-inert at 17h on real boards,
   structurally coverage-safe. Validate before anything else ships.
2. **Phase 2 — marginal effective-span pricing** (greedy + seeder). A/B on the three dates;
   two-track only if pricing under-delivers AND passes all coverage gates.
3. **Phase 3 — span-trim relocation pass.**
4. **Phase 4 — vehicle-split safety + handoff UI.**

## Validation gates (read-only harness, Django test Client preview path)

Dates 05-09 (busy), 06-01 (slow; engine pool — unbuilt locally), 06-02. Two identical runs must be
byte-equal (determinism). HARD gates:
- Coverage ≥ baseline on every date (05-09 holds 96; 06-01 holds 67/67) — any drop blocks shipping.
- 0 new impossible turns; deadhead/leg ≤ baseline + 0.5–1.0.
- Drivers >14h raw ≤ founder's own count (3 on 05-09) — proves the soft tier actually reorders.
- 06-02: gap compaction still performs the Yovanny-06:15→David move (explicit assert); David's
  16.5h-raw/4.5h-hole split-day is NOT flagged over-target by the effective metric.
- **Escape-hatch stress test: rerun 05-09 with hard cap=12.0 — coverage must be UNCHANGED while
  rescue count >0** (proves the cap structurally cannot farm).
- **Historical split replay: 2026-05-28 unit #006 (Olmo→Raymond)** — pair auto-detected, ZERO false
  handoff warnings on the founder's own pattern.
- ~14 new unit tests (cap clamp math, synthetic-window None/None, rescue tier+window filters,
  frozen-driver delta gate, trim accept rule, never-farm keyset invariant, sharer gates, seeder
  score>0 regression: Yovanny 06-03 still seeds 7 legs) + full suite green.

## Open questions for the founder

1. Confirm defaults: **17h hard backstop / 13h effective target** (from his own boards). Does he
   want the visible "target" to read as 13h continuous, or a different number?
2. Explicit per-driver Max hrs = strict (farm with named reason) vs stretch-with-red-badge — design
   says strict; confirm.
3. Split shifts: keep dispatcher-initiated (assign 2 drivers to one unit + set handoff hour), engine
   just enforces physics? (Engine-PROPOSED splits deferred.)

## Rejected alternative designs (for the record)

- **"Emergent Shifts" (pricing+trim only, no ceiling)** — elegant, structurally cannot farm, but no
  hard bound at all: stub 23–24h caps remain the only ceiling, so the founder's complaint is fixed
  probabilistically. Its two best mechanisms (marginal pricing, trim pass) were grafted in.
- **"Demand-Banded Shifts + 14h cap-with-relief"** — 14h RAW cap contradicts 18% of the founder's
  own driver-days, so relief/ambers fire routinely (warning fatigue); relief lifts back to stub
  values so worst-case length is unchanged; pre-build band planner re-introduces the
  vehicle-blind-allocator failure class; {start:0,end:23} stub-miss synthesis silently imposes a
  23:00 clear-by. Its strict-cap semantics, handoff-hour UI, post-build handoff validator, and
  historical-replay validation were grafted in.

Full agent outputs: workflow `wf_5c0ae479-11e` (session 5a76437a, 2026-06-09).

---

# PART 2 — Founder follow-up: cap calibration, split strategy, Second-Shift Advisor (2026-06-09)

Founder corrections/asks: (a) **the engine never farms — he farms manually** from the residual
"Need Affiliates" list, so "never silently farm" really means "never leave a leg in residuals when
in-house capacity exists"; (b) accepts 17h hard + gap credit; weighing **13.5 vs 15** for the soft
target; (c) wants to know WHEN to split a day between two drivers (possibly one vehicle AM/PM);
(d) wants the ENGINE to tell him when a day's volume needs a split — he can't see it coming on busy
days. Second workflow (`wf_23784a57-376`): cap-impact simulation + spare-capacity audit + advisor
design + adversarial verification against the measured data.

## Cap-impact simulation (founder's real boards; `scratch/cap_sim_0609.py`)

- **A 13.5h-effective cap bites on exactly 3 driver-days out of 39** — all on busy 05-16 (roberto
  15.5h eff, george 14.3h, sereen 13.8h). 05-09 and 06-02 trigger **zero** under either threshold
  (gap credit does the work: roberto's 15.4h raw on 05-09 → 10.3h effective via his 5.1h hole).
- **Every overflow is a one-leg evening tail** (16:13–21:16, van/mini_van, $100–115). Min-tail
  splitting (latest boundary keeping segment 1 under cap) is required — naive largest-gap splitting
  degenerates when the day's biggest gap is in the morning (george, sereen).
- **13.5 vs 15 differs by exactly 2 tiny driver-days across 3 dates.** 15.0 only avoids george +
  sereen's single-leg tails. Both tails are absorbable by short-day working drivers (shelley 5.6h,
  shipo 7.5h eff on 05-16) — i.e. the Phase-3 trim pass's job, no new shift needed.
- **DECISION: SPAN_SOFT_EFFECTIVE_HOURS = 13.5, strict-greater semantics** (Michael Olmo's
  founder-built 05-09 day sits at exactly 13.5h eff and must NOT trigger). This supersedes the
  Part-1 value of 13.0. Hard cap stays 17.0h raw.

## Spare-capacity audit (`scratch/spare_audit_0609.py`)

- **Drivers are plentiful**: 21 real active in-house drivers; exactly 13 work each sampled date →
  ~10-driver idle bench daily (names in script output). Caveat: `is_active` is current-state.
- **Fleet = 14 units** (SUV 5, Van14 4, MiniVan 2, Towncar 2, Van 1). Busy days park only ONE
  never-assigned unit (Towncar 015); slow days park 4. BUT 2–4 more units are **freed** by drivers
  clearing before 17:00 (incl. a Van14 on both busy days) → 3–4 evening-capable units even when busy.
- **Van scarcity is a MORNING phenomenon** (peak 8 simultaneous van-type demand vs 7 van units
  ~09:30–13:30 busy days); evenings always have ≥2 van units idle. Evening second shifts do NOT
  need to share a van.
- **Historical shared units are 64% SUVs** — sharing is a general "day ended early, next driver
  takes the car" practice, not tier-driven scarcity.
- KEY DISTINCTION the advisor must encode: never-assigned **spares** (S) are nearly empty on busy
  days; the real evening vehicle supply is **freed DVA-held units** — reachable only via a
  (soft) handoff from the early-clearing holder.

## Split-strategy recommendation (the founder's "when do I split?" answer)

Escalation when a day runs over target: (1) **relocate the tail leg to a short-day working driver**
(trim pass — covers all 3 measured cases, zero new shifts); (2) **idle driver + free car** — the
spare towncar or a FREED unit with a wide buffer (holder clears ≥1h before the shift's first
pickup; holder's End clamped); (3) **true tight AM/PM split with handoff gating** — only really
needed for midday van-tier pressure (09:30–13:30 busy days). Vehicle splits are the LAST resort,
not the default; founder's historical practice matches this.

## Second-Shift Advisor (Phase 5) — design + adversarial verdict

Read-only preview pass after build+swap+trim+gap: clusters (a) residual legs and (b) untrimmable
over-target tails into one-driver-feasible SHIFT PROPOSALS {tier, window, legs, revenue, source},
rendered as amber cards above Need Affiliates ("These 2 evening legs (4:13–9:16 PM · mini-van ·
$215) don't fit today's crew — Raymond is idle; Van14 #11 frees at 4:36 PM → handoff 5:30 PM.
[Add this shift]"). Accept = client edits the AUTHORITATIVE Step-1 modal (flip Off row on / append
synthetic row with `extra_units[driver]=unit`), re-runs preview; DB writes (DriverVehicleAssignment
get_or_create) only on Apply. Class-(iii) "no in-house option" cards degrade to today's residual
behavior. Flags: `ADVISOR_ENABLED`, `ADVISOR_MIN_LEGS`, `ADVISOR_CLUSTER_GAP_MIN=180`,
`ADVISOR_HANDOFF_PAD_MIN=15`, `ADVISOR_MAX_PROPOSALS=4`, `ADVISOR_SUGGEST_SCHEDULED_OFF=True`,
`ADVISOR_SUGGEST_SPLITS=False` until Phase-4 handoff gate.

**Adversarial review: NOT ship-shape as specced — 6 must-fixes before build (all verified):**
1. **Build-1st no-ops for extra_units drivers** — `build_smart_schedule` re-queries DVA itself
   (scheduler.py:2039-2055) and returns an EMPTY schedule when no DVA row exists (= every advisor-
   added driver); warnings discarded at views.py:9149. FIX: dvtypes override param + pass the
   proposal's legs as `pinned_leg_ids` (mechanism exists, scheduler.py:1980).
2. **ADVISOR_MIN_LEGS=2 deletes 100% of measured triggers** (all are 1-leg tails). FIX: default 1;
   never silently drop — sub-min clusters get an info line.
3. **Spare set S is one towncar on busy days** — the van-capable free units are FREED (DVA-held),
   reachable only via the disabled split source → v1 would say "farm" while capacity exists. FIX:
   add class (i-b) "freed unit, wide buffer (≥60min)" source in v1.
4. **Tier-blind greedy chaining mis-packs** the measured 05-16 slots (chains a mini_van leg onto a
   van leg, orphans the third). FIX: tier-aware/exhaustive packing (trigger sets are ≤6 legs).
5. **Threshold drift**: must be the founder-chosen 13.5 strict-greater, shared with Phase 2/3
   (update SPAN_SOFT_EFFECTIVE_HOURS to 13.5 everywhere; at 13.0 it fires on Olmo's clean day).
6. **Split accepts must not ride Build-1st** (contradicts Phase-4 must-fix: seeding bypasses
   `vehicle_handoff_ok`). Route via pinned legs in the general pass.
   Before Apply ships: exclude mid-shift/demo drivers from the idle roster (06-02: Abdalla + 2 demo
   accounts have legs but no DVA row; Priya/Yolanda are demo accounts); unit-still-spare
   re-validation at Apply + existing-row vehicle semantics; FleetVehicle has NO is_active flag
   (drivers/models.py:517) — add one or exclude long-unassigned units.
   Trigger (b) should detect on span unconditionally and emit an info-only card when nothing is
   movable ("roberto 15.5h — tail hand-pinned, nothing movable").

**What survived review**: modal-authoritative accept contract (views.py:9044-9057), never-farms
structure, the dvtypes hoist/overlay plan (all three passes accept driver_vtypes), min-tail trigger
math (no metric fork), determinism via id-tiebreaks, template anchors.

**Validation additions**: replay advisor on 05-16 → must produce 2 tier-correct proposals covering
all 3 measured tail legs; zero proposals on 06-02 AND 05-09 at 13.5; an extra_units Build-1st
preview must seed the pinned proposal legs (currently seeds zero). Harnesses:
`scratch/cap_sim_0609.py`, `scratch/spare_audit_0609.py`, `scratch/advisor_review_check_0609.py`.

## Revised ship order

1. **Phase 1** — hard 17h cap + rescue + badges + modal Max hrs (strict when typed). Unchanged.
2. **Phase 2** — marginal effective-span pricing, soft target **13.5** strict-greater.
3. **Phase 3** — span-trim relocation pass (the workhorse: absorbs the measured one-leg tails into
   short-day drivers with no new shifts).
4. **Phase 4** — vehicle-split safety (handoff gate, all insert paths) + Handoff-hour UI.
5. **Phase 5** — Second-Shift Advisor (residual + over-target triggers, sources i/i-b/iii at
   launch; source ii = tight splits flips on after Phase 4), with the 6 must-fixes applied.

Full agent outputs: workflow `wf_23784a57-376` (session 5a76437a, 2026-06-09).

---

# PART 3 — PHASES 1+2 BUILT (2026-06-09, uncommitted on branch farmout-optimizer-arc)

## What shipped (files)

- **`dispatching/feasibility_guards.py`** — Span Governor flag block (`ENFORCE_SPAN_CAPS=True`
  master kill-switch, `SPAN_HARD_HOURS_DEFAULT=17.0`, `SPAN_SOFT_PRICING=True`,
  `SPAN_SOFT_FREE_HOURS=12.0`, `SPAN_SOFT_EFFECTIVE_HOURS=13.5` strict-greater,
  `SPAN_SOFT_RATE=25`, `SPAN_STEEP_RATE=120`, `SPAN_SEEDER_RATE_SCALE=0.5`,
  `SPAN_GAP_CREDIT_MIN_MIN=120`, `SPAN_GAP_CREDIT_MAX_MIN=300`).
  `get_effective_window(driver_id, configured=None, enforce_cap=True)` clamps
  `max_hours=min(stub, configured/modal, 17)`; unknown drivers get a `{start:None, end:None,
  max_hours:17}` synthetic window. `window_check(..., span_hours_before=)` delta gate: a day
  already over its cap still accepts inserts that don't GROW the raw span (no frozen drivers).
- **`dispatching/scheduler.py`** — `SPAN_COVERAGE_RESCUE=True`;
  `marginal_span_penalty`/`_span_cost_points`/`_span_gap_credit_minutes` (gap credit from the
  PRE-insert schedule only — no credit for holes minted by the priced insert) +
  `effective_span_hours` (the badge metric). Greedy scoring block replaced (legacy block kept
  behind the flag); same term at half rate in `_score_leg_for_smart_schedule` (seeder seats
  only on score>0). `build_smart_schedule(max_hours=)` closes the uncapped Build-1st hole.
  `recover_residuals_via_swaps(driver_windows=, driver_hours=, flexible_drivers=)`: capped
  windows forwarded to find_swaps (map covers EVERY working driver — pool-restriction safe)
  + modal pickup-hour post-validation of every cascade move (fixes a pre-existing gap: swaps
  could land legs outside dispatcher-typed hours). NEW `rescue_span_blocked_residuals`:
  greedy-parity tier + modal-hour filters, capped-then-lifted two-probe, strict caps never
  lifted, deterministic.
- **`dispatching/views.py:auto_assign_drivers`** — parses modal `max_hours` (typed ⇒ STRICT,
  tracked in `strict_span_caps`; wins over DB availability value); builds the one
  `capped_windows` map; passes the cap into Build-1st seeding; wires the rescue pass between
  the swap pass and gap compaction (both preview AND apply paths); preview JSON gains
  per-driver `span_hours`/`effective_span_hours`/`span_warn`/`span_note` + top-level
  `span_warnings[]`. `_revalidate_swap_feasibility` pinned to `enforce_cap=False` (manual
  swaps sovereign); `fleet_intel.py` pinned to `enforce_cap=False` (shipped analytics
  unchanged). Gap compaction inherits the caps automatically via the funnel.
- **`daily_capacity_planner.html`** — Step-1 "Max hrs" column (blank = global 17; typed =
  strict; disabled with Off); payload carries `max_hours`; preview renders amber
  ("13.7h on duty — over the 13.5h target") / red ("over the cap; kept N leg(s) in-house
  instead of farming") badges, a span-warnings strip above the driver cards, and a
  "Long Days / Longest Day" summary chip.
- **Tests** — new `dispatching/tests_span_caps.py` (24 tests: clamp math, synthetic window,
  delta gate, gap-credit/pricing incl. founder-split-day + no-minted-hole cases, rescue
  incl. strict/tier/modal-window/turnaround/determinism); 2 legacy tests in
  `tests_feasibility_guards.py` updated for the new semantics. Full suite 442 pass
  (+1 pre-existing test_ghl_full cp1252 emoji failure, untouched).
  NOTE: `drivers/migrations/0035+0036` (Samsara) were materialized from main into the
  working tree — 0037's dependency needs them; they're exactly what the merge brings anyway.

## Validation (scratch/span_caps_0609.py — full preview path via Django test Client, boards
unbuilt inside a rolled-back transaction; saved-availability mode, so driver counts differ
from the founder's real boards — baseline-vs-capped is the apples-to-apples comparison)

| date | coverage base→cap | med raw | max raw | >17h raw | notes |
|---|---|---|---|---|---|
| 05-09 (busy) | 88 → **88** | 13.7→**12.8h** | 19.7→19.7h | 1→1 **(now RED-badged rescue)** | over-target days 5→4 |
| 05-16 (busy) | 98 → **100 (+2)** | 15.6→**15.1h** | 17.5→**16.8h** | **2→0** | saturated day, 7 ambers |
| 06-01 (slow) | 68 → 68 (full) | 10.9h | 14.6h | 0 | over-target 1→**0** (eff 13.8→12.6) |
| 06-02 | 52 → 52 | 11.8h | 15.6h | 0 | byte-identical board, no badges |

- **Stress test (hard cap forced to 12.0h): coverage UNCHANGED on all 4 dates while rescues
  fire (7/14/3/2)** — structural proof the cap cannot cost an in-house job.
- Determinism: double runs byte-identical on every date/config.
- Badge sanity on the founder's own built boards: ambers fire on exactly the 3 driver-days
  the cap simulation predicted (roberto/george/sereen 05-16) + Olmo 05-09 at 13.7h eff
  (the view's end-time estimates run ~0.2h over the sim's proxy — he sits ON the line).

## Still open (next arcs)

- ~~Phase 3 span-trim relocation pass~~ ✅ BUILT same day — see PART 3b below.
- Phase 4 vehicle-split handoff safety; Phase 5 Second-Shift Advisor (PART 2 must-fixes).
- Founder follow-ups: set real per-driver Max hrs in the modal when he wants tighter than 17;
  the strict-cap warning copy tells him exactly which leg his cap pushed out.

---

# PART 3b — PHASE 3 (SPAN-TRIM PASS) BUILT (2026-06-09, uncommitted)

`scheduler.trim_spans_via_relocation` (flag `AUTO_SPAN_TRIM_PASS=True`, + `SPAN_TRIM_RAW_MAX_HOURS=15.0`,
`SPAN_TRIM_MIN_RELIEF_MIN=45`, `SPAN_TRIM_MAX_MOVES=12`, `_MAX_PER_DONOR=3`, `_MAX_RECEIVE=2`),
wired in `auto_assign_drivers` AFTER the rescue pass, BEFORE gap compaction; moved legs are
locked against the gap pass. Donors = effective span > 13.5h OR raw > 15h; only FIRST/LAST
legs move (the only span-shrinking ones); receiver gates = tier compat + modal hard window +
check_feasibility under capped windows + **receiver stays under BOTH limits** (never mints a
new long day). **Design deviation from PART 1's accept rule, discovered in testing:** the
original `relief − receiver_stretch ≥ 30` netting REJECTS the founder's own move — handing a
tail leg to a short-day driver always stretches him a lot, harmlessly. Receiver stretch is now
only a TIEBREAK (least-stretched receiver wins); the under-both-limits gate is the real
protection. Coverage keyset asserted unchanged (farms nothing by construction). Preview JSON
gains `trim_moves`; summary shows a "Days Shortened" chip. 6 new tests (boundary-only moves,
locks, receiver-limit gate, modal window, determinism, flag-off) → suite 449 (448 pass + the
pre-existing ghl emoji error). Validation: 06-02 max raw 15.6→**14.1h**, median 11.8→**10.9h**,
coverage unchanged; 05-16 (saturated — every driver long, no legal receivers) correctly
no-ops: that day needs MORE drivers, which is Phase 5's job; gates all pass.

---

# PART 4 — "DAY SETUP" ROSTER + VEHICLE-PLAN ADVISOR (designed + adversarially reviewed
2026-06-09, workflow `wf_c6b6fcf8-a28`; **BUILT same day — see PART 4b below**)

**Founder ask:** "suggest who can work that day based on who is available, which vehicle to
assign them, and switch with who — I always have to do that manually." Root pain confirmed in
code: **no DriverVehicleAssignment row ⇒ the driver is invisible to the Auto-Assign modal**
(views.py:8536 page / 9024-9028 endpoint) **and dropped engine-side** (greedy dvtypes gate
scheduler.py:~1204; build_smart_schedule returns an EMPTY schedule, ~2305-2314) — so the
manual vehicle step gates everything.

## Measured reality (scratch/vehicle_affinity_0609.py — 1,496 rows / 148 dates)

- **Affinity is WEAK overall**: only roberto is a true one-car man (#004, 89.8%). Strong
  defaults exist for ~4 (roberto #004, David #008, sereen #003, Seline #11); the rest are
  fluid (runer has driven 13 of 14 units). **Copy-yesterday is a mediocre prior: only ~50%
  of (driver,unit) pairs repeat** day-to-day (roster repeats 75%; unit-given-working 66%).
- **Owner-vs-owner conflicts are rare** (12/148 dates, all unit #003, neuma vs sereen).
- **"Who works today" IS predictable**: saved availability catches 95.6% of the real crew
  (misses: rizwan 5, ken 4 worked-while-OFF — data hygiene); weekday work-rates are strong
  and person-specific (Olmo Mon-Wed 0% / Fri-Sat 95%; Steven Wed 0%). Availability ∩
  weekday-rate ≥0.5 trims the 67 available-but-idle false positives to the real ~10-12 roster.
- **Tier coverage**: founder under-covers ZERO days in 30; vans run hot (25/30 days are
  van-heavy; certification limits the Van14 pool to 7-8 drivers). Demand tier MUST be read
  from `reservation.vehicle` via `effective_vehicle_type` — **Leg.vehicle is never populated**.
- Today's flow: drag-drop on two pages → `update_inhouse_vehicle_assignment` (views.py:2351,
  per-driver POST, cert-gated, NO double-book check, swap = two non-atomic POSTs) +
  `copy_vehicle_assignments` (views.py:2431) — the existing preview→confirm precedent.
  `FleetVehicle` has no is_active (stale #015 must be inferred or a flag added).

## Design (v1, "HOLDS UP" verdict with must-fixes)

New "Suggest Day Setup" button next to "Use Previous Day" on the planner's vehicle panel →
preview→confirm modal: **ALREADY SET** (locked, founder's pre-built rows untouched) /
**SUGGESTED CREW** pre-checked (availability ∩ weekday-rate, with evidence hints: "works 9/10
recent Tuesdays") with a vehicle dropdown preset per driver (affinity + cert + tier
reservation; swap callouts: "sereen usually has #003 — neuma holds it; sereen → #009") /
**ALSO AVAILABLE** unchecked / **OFF** collapsed. Apply = ONE atomic validated endpoint
creating real DVA rows (cert hard-block server-side; intra-payload + cross-row double-book
checks; idempotent; no deletes). **Architecture: REAL DVA rows on explicit Apply, NOT a
virtual dvtypes overlay** — verified against every consumer (incl. two extra DVA-gated
consumers at views.py:12857/13158 that an overlay would have missed); after Apply the modal +
engine work unchanged. Pure module `dispatching/day_setup.py` (flags `DAY_SETUP_*`), two views
cloned from `copy_vehicle_assignments`, one modal cloned from the copy-prev pattern. AM/PM
shares deliberately deferred to Phase 5 (sharing is an evening freed-unit phenomenon, only
visible after the build). Ship gate: 30-date backtest must beat the 50.4% copy-forward pair
baseline with roster recall ≥90% and zero cert/tier violations.

## Review must-fixes (before build)

1. **Share-collision rollback bug**: the post-write "no vehicle twice" invariant fires on the
   founder's own hand-built AM/PM shares (~24% of dates) — scope it to payload-touched
   vehicles only; pre-existing locked duplicates warn, never fail.
2. **Tier coverage must RESERVE, not advise**: assign ceil(tier_legs/legs_per_unit) units of
   capacity ≥ tier to cert-eligible high-affinity drivers BEFORE the affinity-greedy phase
   (threshold-free), else routine van days produce plans with parked Van14s.
3. **Stale-unit rule fails on its own example** (#015 is 5 days stale, not 30): filter on
   usage DENSITY (<~5 rows in window = "rarely used", listed but never auto-suggested) — or
   just ship the one-field `FleetVehicle.is_active` migration in v1.
4. **Bound all history queries to date < target_date** (founder pre-builds ~4 days ahead;
   future rows would leak into weekday/affinity stats AND corrupt the backtest gate).
5. Should-fix: `apply_day_setup` must `cache.delete(capacity_planner_{date})`; 409-on-drift
   (echo preview snapshot, name the drifted row). Minor: demo-account name guard
   ("demo"/"placeholder" substring + id flag); numeric unit sort (`_cp_vehicle_sort_key`).

---

# PART 4b — DAY SETUP BUILT (2026-06-09, uncommitted)

All four must-fixes + should-fixes implemented. Files:

- **`dispatching/day_setup.py` (NEW)** — pure read-only `suggest_day_setup(target_date,
  ignore_existing=False)`. Roster: availability resolver = HARD gate (founder: "make sure
  they are physically available on the schedule" — OFF drivers land in a collapsed group,
  never suggested); pre-checked = weekday work-rate ≥0.5 **with the denominator starting at
  the driver's first appearance** (else recent hires like sereen get demoted every day) **OR
  worked the previous operating day** (active-streak rule; roster repeats 75%) OR <3 samples.
  Vehicles: P1 dedicated locks — **explicit admin `Driver.preferred_vehicles` unit first
  (founder's semi-permanent cars: george #005, David #008, roberto #004, sereen #003;
  threshold lowered to 0.50 so george's 53.6% history qualifies even unset)**, then
  history-share; P2 tier RESERVATION (ceil(legs/6) units of capacity ≥ tier, threshold-free);
  P3 fluid greedy (affinity + last-unit stickiness + preferred-type); P4 leftovers warned.
  Rarely-used units (<5 rows in window, catches maybe-retired #015) listed but never
  auto-suggested. History strictly `date < target` (pre-built future days can't leak).
  GOTCHA encoded: `rates.Vehicle.__str__` is `.title()`-cased — tier math must read the raw
  `vehicle_type` CharField (`_unit_tier`), same as `load_all_driver_vtypes`.
- **`views.py`** — `suggest_day_setup_view` (read-only preview) + `apply_day_setup`: ONE
  atomic validated write (cert hard-block; intra-payload duplicate-unit 400; unit held by an
  OUTSIDE-payload driver 400 with holder named — **scoped to payload vehicles only, so the
  founder's pre-existing AM/PM shares never block an unrelated Apply**; snapshot-drift 409;
  idempotent; never deletes; `cache.delete(capacity_planner_{date})`). `urls.py`: two routes.
- **`daily_capacity_planner.html`** — gold "Suggest Day Setup" button next to "Use Previous
  Day"; preview→confirm modal (groups: Already set locked / Suggested crew pre-checked with
  evidence hints + per-row unit dropdown + reason chips / Also available unchecked / Off
  collapsed; swap callouts amber; warnings + live duplicate-unit guard; Apply→reload).

**Tests**: 16 new in `dispatching/tests_day_setup.py` (off-gate, demo guard, dedicated lock,
cert gate, locked rows untouched, future-history bound, swap callout, determinism,
rarely-used; apply: idempotent, duplicate-400, cert-400, holder-named-400, share-scoped pass,
drift-409, 403). Full suite 465 (464 + pre-existing ghl emoji error).

**BACKTEST (ship gate) — PASSED** (`scratch/day_setup_backtest_0609.py`, last 30 operating
dates, that-day's rows masked): roster recall **93.9%** (gate ≥90; residual misses are mostly
the 16 worked-while-OFF data-hygiene cells the hard gate intentionally respects), checked-set
precision 90.5%, (driver,unit) pair accuracy **58.3%** vs the 50.4% copy-forward baseline,
**0 cert violations**. Earlier iterations failed recall at 62.5% — root causes fixed:
weekday-rate denominator predating recent hires, no active-streak rule, and now-inactive
drivers (alex/neuma) wrongly counted as misses.

**Founder follow-up (optional, improves suggestions immediately)**: set
`Driver.preferred_vehicles` in admin for the full-timers (george→#005, David→#008,
roberto→#004, sereen→#003) — the explicit lock outranks history and reads "his car (set in
admin)" in the modal.

---

# PART 5 — SECOND-SHIFT ADVISOR BUILT (2026-06-09, uncommitted)

The PART-2 design, simplified by what was built since: accepts ride the validated Day Setup
apply endpoint (real DVA rows) + Build-1st seeding + the trim pass, instead of the original
extra_units virtual-overlay machinery (whose biggest must-fix — build_smart_schedule's empty
return for no-DVA drivers — becomes moot because the DVA row is REAL before the re-preview).

- **`dispatching/shift_advisor.py` (NEW)** — `build_shift_proposals(...)`, preview-only.
  Triggers: (a) residual legs after all passes; (b) drivers still over the 13.5h-effective
  target whose MOVABLE min-tail suffix the trim pass couldn't drain (locked tails stay
  silent — the amber badge already explains them). Tier-aware clustering (per exact tier,
  chained by time, ADVISOR_CHAIN_PAD_MIN=20 / CLUSTER_GAP=180; MIN_LEGS=1 — all measured
  slots are 1-leg tails). Sources ranked: idle driver (available-today first; scheduled-off
  loudly labeled, ADVISOR_SUGGEST_SCHEDULED_OFF=True) × spare unit (rarely-used excluded) >
  FREED unit (holder's proposed clear + ADVISOR_FREED_BUFFER_MIN=90 before first pickup —
  the founder's AM/PM share; tight handoffs stay Phase 4) — cert + tier gated; mid-shift
  drivers (legs but no DVA row) excluded from the idle roster. ≤4 proposals by revenue +
  an honest "+N more" line; "no in-house option" cards degrade to Need Affiliates.
- **`views.py`** — `_overload_map` collected during span-badge serialization (badged drivers
  + this run's unlocked movable ids); advisor called preview-only inside try/except (advisory
  must never break the preview); response gains `advisor[]`. `apply_day_setup` gains a
  per-pair **`allow_share`** flag (advisor freed-unit accepts only — Day Setup never sets it)
  that skips the held-by-outsider check for that pair.
- **`daily_capacity_planner.html`** — gold "Second-Shift Advisor" panel above Need
  Affiliates: per card the finding (kind/legs/tier/window/$), the leg list, a source
  <select> (best + ≤3 alternates, freed/scheduled-off labeled), "Add this shift" +
  "Not today". Accept = POST apply_day_setup (real DVA row, allow_share for freed) →
  synthetic editable Step-1 row (prefilled window; Build-1st checked for residual proposals;
  overload proposals rely on the trim pass to drain the long day) → rePreview(). Dismissals
  are client-state per signature.
- **Tests**: 11 in `tests_shift_advisor.py` (spare/freed sources, cert gate, mid-shift +
  scheduled-off roster rules, tier-aware clustering, overload min-tail + locked-tail-silent,
  determinism, allow_share blocked-without/allowed-with). Suite 478 (477 + ghl, 1 skip).
- **Probe on real boards (rolled-back unbuild)**: 05-16 (46 residuals) → 4 ranked cards +
  honest no-option cards (only spare is a towncar; mini_van demand uncoverable) + "+48 more"
  line; 06-02 → clean spare-unit proposals; built/quiet days → no panel.

**Founder board-review fixes (2026-06-09, after the full-day simulation)**:
1. **Stale rows held by DEACTIVATED drivers** (neuma/shipo holding #003/#009 on the 05-16
   board with zero legs): Day Setup now treats them as stale — not rendered as crew, unit
   returned to the free pool, loud warning ("clear the row in the panel"); `apply_day_setup`'s
   held-by-outsider check ignores inactive holders. 2 new tests.
2. **Advisor fairness reservation**: high-revenue residual cards can no longer crowd every
   overload card out of the capped list — the best overload card always keeps a seat.
3. **Locked long days get an info card** ("X runs long, but his late legs are hand-assigned —
   unassign one (✗) and rebuild to let a second shift take it") instead of silence — the
   rizwan 3:45 AM–6:15 PM case when the tail is the dispatcher's own assignment.
4. Verified: Publix store stops ARE included in end-time estimates (PUBLIX_STOP_MINUTES in
   estimate_job_end_time) — the Aftab 10:42-Publix chaining complaint is greedy ORDER quality
   (known ceiling: single-pass greedy + swap/gap passes; chain-aware lookahead = next quality
   arc), not a timing bug. Suite 480 (479 + ghl, 1 skip).

**Prefill for unchecked-available drivers (founder live-testing, 2026-06-09)**: "Also
available" rows (e.g. Raymond/shelley, 0/12 recent Saturdays) used to show "— no unit —";
now a P3b pass presets each such dropdown with the best free unit (globally-best pair first,
one per unit, cert-gated, rarely-used excluded) WITHOUT reserving it — the unit stays parked
and in free_units until the row is checked. Ticking the box is now one click. 1 new test;
suite 481.

**PLANNED SHARED CARS (founder, 2026-06-09: "more drivers available than cars — solve the
long days by putting two drivers on one car") — BUILT:**
- **Migration `drivers/0038`**: `DriverVehicleAssignment.planned_start_hour/planned_end_hour`
  (nullable) — the planned working window for that day's assignment.
- **day_setup P3c share pass**: when checked drivers outnumber free cars, each car-less
  checked driver is paired onto the EARLIEST starter's unit as the PM shift
  (AM: avail-start→`DAY_SETUP_SHARE_HANDOFF_HOUR=15`, PM: 15→23). Partners come only from
  this run's proposals (founder-set rows untouched). Modal renders SHARED chips
  ("until 15:00 → hands to X" / "from 15:00 ← takes over from X") + a SHARED CAR callout.
- **apply_day_setup**: same unit twice in one payload is legal iff EVERY pair for it carries
  `allow_share` (accidental double-pick still 400s); planned hours persisted on the rows.
- **Auto-assign modal prefill**: planner context overlays planned hours over saved
  availability (forced non-flexible) — the split arrives as HARD windows, so the engine's
  pickup-hour filter + Guard C make double-booking the unit impossible by construction.
- **Gap-compaction parity** (the judges' companion fix, now required): the gap pass takes
  `driver_hours`/`flexible_drivers` and applies the modal pickup-hour filter on receivers —
  a relocation can no longer place a leg outside a share partition.
- **Rarely-used units softened** (founder: "no such thing as a car not working today"):
  last-resort in P2/P3/P3b ordering instead of banned; label retained.
- Tests: share suggest + share apply + accidental-dup + last-resort semantics (suite 484,
  483 + ghl, 1 skip). Live smoke: 06-02 Day Setup proposes 2 share rows (drivers > cars).
- Deferred still: mid-day handoff feasibility beyond the hard-window partition (Phase 4
  vehicle_handoff_ok — the partition + clear-by guard covers the planned-share case).

**Continuity rules (founder, same day)**: (1) non-regulars default to KEEPING the car they
drove most recently (`DAY_SETUP_YESTERDAY_BONUS=40`, strong enough to beat moderate affinity
for a different unit, reason chip "same car as last shift"); (2) a returning regular gets HIS
car back even though someone else drove it yesterday — structural (P1 dedicated locks claim
units before the yesterday bonus is ever scored) plus a handback callout ("#005 goes back to
george (his car) — X drove it last; X gets another unit"). A strong usual car (e.g. 80%
share) still beats a one-day fill-in for the same driver — correct by the same rule. Backtest
pair accuracy improved 58.3% → **63.2%** with continuity on; 2 new tests (returning-regular
handback, fluid-driver-keeps-yesterday-car); suite 467 (466 + ghl).

---

## Addendum 2026-06-10 — founder test drive, experiments, hour-balance guardrails (f2586291)

Founder drove the full blank-slate flow on 2026-05-16 (his hand-built board that day:
97 in-house / 49 farmed / 13 drivers); offline experiment harness
(`scratch/handoff_sweep_0516.py`, `fourteenth_car_*.py`) replays the same pipeline through
the Django test Client. GOTCHA: the UI modal sends `exclude_unpaid=true` by default, so UI
coverage reads ~3 lower than harness runs on this date.

**Experiment findings:**
- **Handoff hour is coverage-neutral** (12:00/13:00/14:00/15:00 all → 112 in-house): an
  earlier split only shifts jobs from the AM sharer to the PM sharer. Busy-day coverage is
  CAR-bound, not driver-hour-bound. The fixed 15:00 default stands; demand-aware handoff
  selection is downgraded to a workload-fairness knob.
- **A spare unit pays ONLY when checked drivers > cars**: +5 in-house (~$500-750/day) on
  05-16 (15 drivers / 13 cars), +0 on 04-03 / 03-28 / 05-02 (rosters of 10-13). Heuristic
  for the founder: every Day Setup SHARED badge marks a day a spare unit would have earned
  its keep.
- **Failure modes found on the live board**: the span-cap rescue lifted caps UNBOUNDED
  (17.8h / 18.3h raw days built around 00:0x airport arrivals), and night legs whack-a-mole
  onto whichever driver is still Flexible.

**Guardrails (commit f2586291; suite 527):**
- `SPAN_RESCUE_CEILING_HOURS=17` — the rescue lift stops at the absolute ceiling; a leg that
  fits nobody under 17h farms LOUDLY via the new `ceiling_blocked` warning kind (rendered in
  the planner warnings strip). Supersedes "the cap can NEVER cost an in-house job".
- `NIGHT_LEG_FLEX_BLOCK` / `NIGHT_LEG_BOUNDARY_HOUR=3` — Flexible never covers a
  00:00-02:59 pickup (founder boards legitimately start 03:00-03:45, so the boundary is 3).
  Escapes: an EXPLICIT window start covering the hour beats the flexible flag (builder typed
  From=00:00, accepted advisor night cards, night stubs), and `night_exempt` windows
  (get_effective_window(enforce_cap=False) — manual-sovereign callers) so execute_swap
  revalidation never hard-blocks a dispatcher's intentional move nor trips over his
  pre-existing night legs.
- `VEHICLE_SHARE_PAD_MIN=60` (was 30) — founder warehouse rule: AM sharer returns the car to
  base + wash/fuel (~30-40 min past his last clear), PM sharer drives out. Pinned by constant
  + boundary tests. The geography-aware split (car_ready = clear + drive_to_base + service;
  PM pickup >= car_ready + drive_out) needs a base-location concept the engine doesn't have.

05-16 validation with guardrails: worst day 17.8h → **14.4h raw**, zero shared-car overlaps,
coverage cost exactly the two 00:0x arrivals the founder always farmed by hand (110 vs 112).
An adversarial review caught 4 real pre-commit bugs (manual-swap night-block + pre-existing-
leg poisoning, silent ceiling farms, unreachable builder escape, unpinned pad) — repeat that
review pattern on engine-gate diffs.


# PART 6 — DEMAND-AWARE STAFFING: FOLD-OUT ADVISOR + SOLO-FIRST DAY SETUP (2026-06-10)

ROADMAP #1. One philosophy, two pieces: **the engine builds, then advisors adjust headcount
in BOTH directions from the real board** — the founder stops doing headcount math by hand.
Designs adversarially reviewed BEFORE build (4 lenses; 7 findings folded in) and the
implementation diff reviewed AFTER build (4 lenses; 2 blockers + 2 majors fixed pre-commit).

## Fold-Out Advisor (`dispatching/fold_advisor.py` — the founder's bigger pain)

Post-build advisory, the mirror image of the Second-Shift Advisor: "sereen has only 3 jobs
and they all fit on ken/george/rizwan — fold her out and free her car?" Propose-only.

- **Candidate gate** (ALL): in the modal working set; `0 < legs <= FOLD_OUT_MAX_LEGS=3`
  (0 = trivially foldable vehicle-holder, `FOLD_OUT_INCLUDE_EMPTY`); **whole day movable** —
  every slot is THIS run's unlocked proposal (`final_assignments` membership + not in
  `locked_leg_ids`; pre-existing DB assignments are absent from final_assignments, so one
  manual/seeded/trim-moved/dispatcher leg disqualifies — manual-sovereign); not Build-1st;
  **not a sharer** (v1: folding an AM sharer would orphan the partner's planned window);
  holds a vehicle (freeing the car is the point).
- **All-or-nothing simulation**: deepcopy the proposed board, remove the candidate, place his
  legs sequentially; every receiver passes the FULL gate stack (modal hard window parity →
  tier compat → `sharers_conflict` occupancy gate on the SIM board → `check_feasibility`
  under the cap-clamped window incl. the night rule → post-insert span ceilings
  **eff <= 13.5h AND raw <= 15.0h** — the trim pass's own trigger, so a fold can never mint
  a day the next preview wants to unwind). Receiver ranking `(tier_waste, eff_stretch,
  deadhead, rid)`. ANY leg unplaceable → no card. Receivers must already carry work (moving
  a thin day onto an idle body just swaps who gets released).
- **Suppressed when residuals exist** — a day needing MORE coverage never sees a release card.
- **Complement, not conflict, with gap compaction**: `GAP_COMPACT_PROTECT_DONOR_MAX_JOBS=3`
  refuses to strip thin donors piecemeal, so thin days arrive at the advisor INTACT — exactly
  what makes a whole-day fold possible.
- **Accept path (zero new endpoints, zero leg writes)**: pin every relocation via the
  existing `manual_assignments` map (sovereign + locked on every re-preview — the card's
  placements can't be reshuffled), mark the driver Off (modal-authoritative), DELETE his DVA
  row through the existing `update_inhouse_vehicle_assignment` endpoint (the exact mirror of
  the Second-Shift accept's row CREATE — the freed unit becomes a spare unit the Second-Shift
  Advisor can monetize on the next preview). Post-accept coverage check renders a red banner
  + **Undo** if the rebuild without him came up short; undo failure (cert re-check) alerts
  loudly instead of pretending.
- Flags: `FOLD_OUT_ENABLED=True`, `FOLD_OUT_MAX_LEGS=3`, `FOLD_OUT_MAX_PROPOSALS=2`,
  `FOLD_OUT_REQUIRE_VEHICLE=True`, `FOLD_OUT_SUPPRESS_ON_RESIDUALS=True`,
  `FOLD_OUT_INCLUDE_EMPTY=True`.
- Tests: `dispatching/tests_fold_advisor.py` — 23 tests: every candidate gate, every receiver
  gate (incl. night-leg-never-on-Flexible and the explicit-night-start escape), all-or-nothing,
  tier-match ranking, caps/determinism, and the DVA delete/recreate apply path (previously
  untested endpoint branch).

## Solo-first Day Setup (`DAY_SETUP_SOLO_FIRST=True`)

When checked drivers > cars, P3c no longer auto-proposes an AM/PM share: the extras stay
UNCHECKED ("available — add via Advisor if the day needs them") with one aggregated callout.
The Second-Shift Advisor — which reads the actual BUILT board — proposes adding them only
when the day truly needs a second shift (its freed-unit option IS the share path, protected
by the occupancy gate). `solo_first=False` (flag or per-request key on the suggest endpoint,
added for A/B) restores the legacy auto-share branch byte-identically.

## Regression found + fixed while building (shipped bug, `80556dc2`)

The decline-a-share fix stripped `allow_share` from EVERY single-pair share — which is
exactly the shape of a Second-Shift Advisor freed-unit accept (holder keeps his row, is not
in the payload) → the holder cross-check 400'd every advisor accept in prod since 06-10.
Fix: single-pair shares keep `allow_share` (the cross-check skip) and lose only the
partitioned planned windows (the decline semantics). `tests_shift_advisor.
test_share_allowed_with_flag` covers it; the orphan-strip + accidental-duplicate tests stay
green. The occupancy gate, not the flag, is what keeps a real share physically safe.

## Implementation-diff review fixes (workflow, 4 skeptic lenses)

1. Same-preview double-fold accept could chain pins onto a driver being folded (pins to a
   driver absent from `driver_hours` are silently dropped at the views merge) → accept now
   guards on `aaIsRebuilding` + disables sibling fold buttons; next-preview chains were
   already blocked by the manual-sovereign gate (pinned receivers hold locked legs → not
   foldable).
2. `_residual_objs` hoist re-wrapped so the advisory-only contract holds (an exception there
   silences both advisors instead of breaking the preview).
3. `update_inhouse_vehicle_assignment` now invalidates the capacity-planner cache (latent
   staleness, load-bearing once fold accepts delete DVA rows mid-session).
4. Backtest harness: per-date board snapshots persist to DISK before any mutation
   (`scratch/board_snapshots/`) and audit ScheduleSnapshot rows created by apply calls are
   cleaned per date — plus `scratch/restore_date_from_backup.py` + a full
   `content/db_backup_staffing_arc.sqlite3` online backup as the recovery path.

## Acceptance backtest (`scratch/staffing_backtest.py`) — ALL HARD GATES PASS

12 real dates replayed through the FULL UI pipeline (suggest → apply → auto-assign →
advisor rounds), each snapshot/restored byte-identical (verified; snapshots also persisted
to disk pre-mutation). RUN A = shipped behavior (auto-share, no accepts); RUN B =
solo-first + POLICY v2 auto-accepts (one card per preview round, scheduled-OFF options
skipped, **coverage-guarded**: an accept whose re-preview shows fewer assigned jobs is
undone — the founder's read-the-numbers judgment, now also a red banner in the UI for
second-shift accepts). Founder column = his real historical hand board (05-16 parsed from
the dashboard CSV, hard-asserted 97/49; 06-01 was never hand-built locally).

```
date        legs bucket   founder     runA     runB  B-A  drvA/B  carA/B   worstA/B  ovl n17 night acc
2026-04-03   157 busy       99/58    94/63    97/60   +3   11/13   11/12  16.7/16.9   0   0   0    3
2026-03-28   150 busy       95/55    81/69    82/68   +1    8/10    8/10  15.7/16.7   0   0   0    3
2026-05-02   149 busy       85/64    82/67    89/60   +7    9/11    9/11  16.8/16.8   0   0   0    3
2026-05-09   148 busy       97/51    98/50    98/50   +0   12/12   12/12  16.7/16.7   0   0   0    0
2026-05-16   146 busy       97/49   110/36   113/33   +3   15/13   13/13  14.3/16.1   0   0   0    0
2026-05-25    97 medium     82/15     89/8     90/7   +1   15/13   13/13  16.5/16.5   0   0   0    0
2026-04-24    97 medium     73/24    76/21    80/17   +4   11/12   11/12  15.2/14.7   0   0   0    1
2026-05-22    96 medium     82/14     88/8     92/4   +4   16/13   13/13  16.4/16.8   0   0   0    0
2026-06-01    68 medium      (unbuilt) 68/0    68/0   +0   15/13   13/13  14.8/14.8   0   0   0    0
2026-06-02    57 slow        57/0     56/1     57/0   +1   15/13   13/13  12.9/13.0   0   0   0    0
2026-05-19    39 slow        39/0     39/0     39/0   +0   13/11   13/11    9.3/9.8   0   0   0    2
2026-04-14    36 slow        35/1     35/1     36/0   +1     9/8     9/8  13.9/14.1   0   0   0    4
```

**Hard gates (all pass):** RUN B coverage >= RUN A on EVERY day (net **+25 in-house jobs**
across the 12 days, never negative); 0 shared-car overlaps; 0 days > 17h raw; 0 night-on-
flexible legs; every restore byte-identical.

**Readings:**
- **05-16 headline**: solo-first ALONE (13 solo drivers, zero shares, zero accepts) built
  **113/33 vs the shipped 110/36 that needed 15 drivers sharing 13 cars** — direct
  confirmation of the settled "coverage is CAR-bound" finding: planned shares fragment
  windows; 13 full-day drivers beat 15 fragmented ones. Same picture on 05-25/05-22/06-01/
  06-02 (13 solo drivers >= 15-16 shared, fewer bodies).
- **Fold-outs fire exactly where designed**: slow days. 05-19: two folds → 13→11 drivers,
  same 39/0. 04-14: two folds + one re-add → 9→8 drivers AND +1 coverage (36/0). Busy days:
  zero fold cards (receiver-gated silence) — correct.
- **The coverage guard earns its keep**: 5 cards rejected across 3 dates (e.g. 05-09's
  overload card would have COST 4 in-house jobs — a freed-unit share constrains the
  holder's car on a tight day). This measured failure mode is why the second-shift accept
  now gets the same red banner the fold accept has.
- **Engine-vs-founder gap on 03-28 (82 vs 95) and 04-03 (97 vs 99) predates this arc**
  (RUN A shows it identically) — that is the chain-aware-builder arc's target, not a
  staffing issue.
- Median spans rise on consolidation days (e.g. 06-02 6.3h→11.2h) — by design: fewer
  drivers each carry more, with the worst day still bounded by the 13.5h/15h/17h gates.


# PART 7 — ROUND 2: REBALANCE ADVISOR + PEAK ROSTER SIZING + FORCE-INCLUDE (2026-06-10/11)

Driven by the founder's 06-01 test drive. Three findings from that drive, in order:

## 7.0 Drive verification (scratch/verify_0601_fold.py) + a fold-suppression bug

- **Peak vindicated**: in-flight histogram (pickup → estimated clear) reads 12 concurrent
  at 09:30 on 06-01 (founder counted 13 — same peak within end-estimation error; the
  sweep counts starts before ends at ties, the conservative reading). Per-tier peaks:
  suv 5 @ 09:30, towncar 4 @ 09:00, mini_van 3 @ 09:06, van 2 @ 08:00. The 13-driver
  roster was PEAK-sized, not over-sized. Founder anti-requirement honored: no naive
  legs-per-driver sizing anywhere.
- **Fold suppression bug (fixed)**: `_residual_objs` counted legs the dispatcher
  deliberately skipped via the UI's exclude_unpaid default — ONE manually-unpaid test leg
  suppressed every fold card during the founder's drive (and could spawn Second-Shift
  cards for jobs he told the engine to ignore). Fix: skipped-unpaid legs are filtered out
  of the advisor residual set, mirroring the auto-pool filter ("treat unpaid as if they
  don't exist").
- **Fold's real verdict, via the new explain channel** (`build_fold_out_proposals(...,
  explain=True)` returns (proposals, rejections) with per-gate receiver elimination
  counts): Aftab (1 leg, 09:06) fails all-or-nothing placement with 11/13 receivers
  eliminated by FEASIBILITY (busy through the peak), 1 by tier — exactly the founder's
  "peak-anchored" hypothesis, now provable per card.

## 7.1 Rebalance Advisor (`dispatching/rebalance_advisor.py`) — "spread it evenly, keep it dense"

The founder's RELATIVE balance rule (his correction): no absolute jobs-per-driver target;
distribute whatever the day has roughly evenly (3-each on a slow day is fine); every day
DENSE — short-and-tight or full, never long-and-empty; never 1-vs-7 without a physical
reason (peak or vehicle tier); name the reason when it exists.

One kind ("rebalance"), two directions:
- **fill**: "Aftab has only 1 job vs the day's ~5.2 average — move these 3 from
  runer/shelley to him." Trigger: jobs <= max(1, floor(mean*0.5)) AND day spread
  (max-min) >= 3. Donors heaviest-first; donor floor `after >= ceil(mean)` AND
  `>= receiver_after` (a move can never invert the imbalance); per-card spread must not
  increase; partial fills OK (unlike fold's all-or-nothing — densifying at all serves
  the rule).
- **compress**: "Raymond's 16:45 + 22:24 stretch a hollow day to 14.8h — move them and he
  ends at 10:30." Trigger: the STATIC hollow predicate (raw >= 10h AND biggest internal
  hole >= 4h, `_is_hollow` — shared verbatim with the backtest metric so trigger and
  metric can never drift) AND eff <= 13.5 (else the trim pass owns him). Boundary legs
  peel inward, biggest span-collapse first; card iff total collapse >= 4h and >= 1 leg
  remains (0 legs = fold's territory).

Every move passes the full fold gate stack (modal window parity, tier, sharers_conflict
on the SIM board, check_feasibility incl. night rule, eff<=13.5/raw<=15.0 ceilings) PLUS
the **no-new-hollow invariant** (a move that would mint a hollow day on its donor or
receiver is rejected — the advisor never creates a day it would flag next preview).
Anti-oscillation is structural: accepted moves become locked pins, so a moved leg can
never move again. Manual-sovereign is per LEG here (a donor with one hand-placed leg
still donates his engine legs; the hand-placed leg never moves); Build-1st drivers are
excluded as subjects AND donors. Suppressed on (paid) residuals; runs after fold with
live-fold subjects excluded; one card per driver per response; compress keeps a fairness
seat against the 2-card cap. Physical-reason INFO card (<=1): dominant gate phrase +
"his 9:06 job IS the peak" qualifier when feasibility dominates at >=80% of the day's
peak concurrency.

Accept = **zero DB writes**: pin the moves (`manual_assignments`, sovereign + locked),
re-preview, the shared red coverage banner (now `undo_kind` fold/shift/rebal) with a real
Undo (delete the pins). Blue card box; mutual fold/rebal sibling-button disable per
preview. 19 tests (`tests_rebalance_advisor.py`).

## 7.2 Day Setup: peak-concurrency roster sizing (`DAY_SETUP_PEAK_SIZING=True`, buffer +1)

`peak_concurrency()` (day_setup.py): the in-flight sweep with exact-tier peaks (founder's
counting, for the callout) AND **cumulative peaks** (in-flight legs of tier >= t — the
correct coverage measure under nested vehicle compatibility: 2 van + 1 Van14 overlapping
need THREE van-capable units). Untiered legs count in overall only; demand-query parity
(unpaid counts — staffing for a maybe-paid leg errs safe); timing cache preloaded if cold
(one query; a suggest click never pays per-leg DB fallbacks).

Sizing: rates still rank WHO, the peak decides HOW MANY — `N = peak_overall + 1` (buffer
+1 reproduces the founder's 06-01 answer: measured 12 -> 13 checked; the peak is a LOWER
bound — turnaround/deadhead means a driver can't always chain adjacent legs). Drivers
beyond N step down to "available"/unchecked (P3b prefill keeps re-adding one click);
locked rows and forced picks never drop; a cert guard never drops below a certification
tier's cumulative peak; loud callout names the peak + who stepped down; loud warning when
the gate-passing roster can't even reach the peak. P2 tier reservation switches from
ceil(daily/6) to the cumulative peak (the descending-tier loop's tier>=t counting makes
higher-tier reservations count toward lower tiers — Hall condition on the nested
structure). Flag-off path byte-identical; per-request `peak_sizing` key for A/B.

## 7.3 Day Setup: force-include ("Yovanny in, someone out")

`suggest_day_setup(force_include=[ids], force_exclude=[ids])` + endpoint pass-through
(list-typed, string payloads rejected) + a modal "Re-suggest with my picks" button (sends
user-CHANGED checkboxes; forced rows carry a YOUR PICK chip and survive re-suggest
cycles). Semantics: availability stays the HARD gate (an OFF forced driver is refused
with a warning naming the Advisor path — it can suggest OFF drivers, labeled);
inactive/unknown ids warn; forced drivers bypass the rate/streak gate, rank top, are
never dropped by the peak cap, and are NEVER silently unchecked (an unseatable forced
driver stays ticked and falls to the loud P4 "No free unit" warning). New P3d pass: a
still-carless forced driver takes the unit of the lowest-priority THIS-RUN proposal —
never a locked row (real DVA rows are the founder's business; the warning says "clear one
in the panel first"), never a P1 dedicated lock (george keeps his car), preferring
victims not holding certification-tier units; the victim steps aside exactly like the
solo-first path (one click to re-add). Closes the ROADMAP "new hire can't be force-added"
gap. 12 new Day Setup tests.

## 7.4 Reviews

Round-2 design adversarially reviewed pre-build (real resolutions: pin-locking as the
cycle-proof, the static hollow predicate replacing a recursive check, T>=1-leg fill
floor, Build-1st donor exclusion, compress fairness seat, banner undo_kind shape, P3d
can never hit the apply cross-check because locked rows are never victims) and the diff
reviewed post-build (3 lenses clean; 1 claimed blocker REFUTED — the reviewer misread
tier indices, the cumulative math is pinned by a passing test; 2 real fixes applied:
list-type guard on force ids, cert-unit victim preference in P3d).

## 7.5 Acceptance backtest round 2 — ALL HARD GATES PASS

RUN A = shipped flags-off baseline; RUN B = solo-first + peak sizing + POLICY v3
coverage-guarded auto-accepts (second-shift, fold, rebalance). spr = job spread
(max-min among working drivers), hol = hollow days — both A/B.

```
date        legs bucket   founder     runA     runB  B-A  drvA/B  carA/B   worstA/B  ovl n17 night sprA/B holA/B acc
2026-04-03   157 busy       99/58    94/63    95/62   +1   11/14   11/12  16.7/16.3   0   0   0    3/9    0/0    3
2026-03-28   150 busy       95/55    81/69    82/68   +1     8/9     8/9  15.7/16.5   0   0   0    3/9    0/0    1
2026-05-02   149 busy       85/64    83/66    86/63   +3     9/9     9/9  17.0/16.8   0   0   0    4/4    0/0    0
2026-05-09   148 busy       97/51    98/50    98/50   +0   12/12   12/12  16.7/16.7   0   0   0    6/6    0/0    0
2026-05-16   146 busy       97/49   110/36   113/33   +3   15/13   13/13  14.4/16.1   0   0   0    8/4    0/0    0
2026-05-25    97 medium     82/15     90/7     90/7   +0   15/13   13/13  16.5/16.0   0   0   0    7/4    1/0    0
2026-04-24    97 medium     73/24    77/20    80/17   +3   11/12   11/12  15.2/15.5   0   0   0    3/7    0/0    2
2026-05-22    96 medium     82/14     88/8     92/4   +4   16/13   13/13  16.4/16.8   0   0   0    6/4    1/1    0
2026-06-01    68 medium      (unbuilt) 68/0    68/0   +0   15/13   13/13  14.6/14.3   0   0   0    6/5    1/1    1
2026-06-02    57 slow        57/0     56/1     56/1   +0   15/12   13/12  12.9/14.0   0   0   0    4/5    0/0    1
2026-05-19    39 slow        39/0     39/0     39/0   +0    13/9    13/9   9.3/12.4   0   0   0    4/2    0/0    1
2026-04-14    36 slow        35/1     35/1     36/0   +1    9/10    9/10  13.9/14.9   0   0   0    6/6    0/0    4
```

**Hard gates (all pass):** RUN B >= RUN A coverage every day (net **+16 vs shipped**);
0 shared-car overlaps; 0 days > 17h raw; 0 night-on-flexible; restores byte-identical.

**Readings:**
- **Peak sizing works in BOTH directions.** Slow/medium: 15->13/12 drivers, 13->9 on
  05-19, with coverage held. Busy under-checked days: the old rate-gate checked only
  8-11 bodies on 04-03/03-28/04-14 — peak sizing checks MORE (14/9/10) and coverage goes
  UP (+1 each). "Rates rank WHO, the peak sizes HOW MANY" is doing exactly that.
- **Rebalance fill cards fired and were accepted on the founder's target shapes**
  (rebal-fill on 05-19 and 04-14 — slow days, the 06-01/Aftab pattern). Compress had no
  qualifying accept this sweep (the two surviving hollow days sit on locked/infeasible
  shapes); hollow count never worsened and 05-25's hollow day disappeared (1->0).
- **The coverage guard kept earning**: e.g. 05-02 rejected an overload card that would
  have cost NINE jobs; net rejected cards across the sweep prevented every potential
  coverage regression — the same red banner the UI now shows on all three accept kinds.
- **Job spread rises on busy days (3->9 on 04-03/03-28)** — that is the SECOND-SHIFT
  adds carrying thin evening tails, and rebalance is suppressed there BY DESIGN
  (residuals exist -> under-coverage is the one question on screen). The founder's
  motivating cases (slow-day Aftab fill, Raymond compress) fire where residuals are
  zero. If busy-day balance becomes a pain, `REBALANCE_SUPPRESS_ON_RESIDUALS=False` is
  the one-flag experiment.
- **Run-to-run jitter (±1 coverage on a few dates vs the round-1 sweep)** comes from
  `USE_LIVE_DISTANCE=1` (the harness runs it for parity with published numbers; some
  drive times are live-API-dependent). Within one sweep, A and B share identical
  conditions, so the gates compare like with like.
