# 01 — Revised Scope and Plan

**The 2026-08-23 scope correction, written down as the governing document.**

| | |
|---|---|
| Produced | 2026-08-23 |
| Status | Awaiting review **together with the amended [`00_DATA_AUDIT_AND_INVENTORY.md`](00_DATA_AUDIT_AND_INVENTORY.md)** — the two form one gate |
| Supersedes | The Phase-1 deliverable list and the "diagnostic panel" end-state in [`PROMPT.md`](PROMPT.md). PROMPT.md remains on file as the original brief; where the two disagree, this document governs. |
| Evidence | Four multi-agent verification sessions run 2026-08-22/23 against the live snapshot (adversarially verified; every headline re-derived by a structurally different method). Scripts land with deliverable 02. |

Every figure is labelled **[measured]** / **[inferred]** / **[modeled]** / **[founder-supplied]** /
**[unavailable]**, per 00's convention.

---

## 1. What the project now is

**The end product is a Day Setup optimizer, not a staffing diagnostic.** A dispatcher opens a
future date from a cold start — reservations exist, nothing assigned — and the system proposes
the day's operating plan: which drivers work and which stay off, which vehicle each starts with,
which trips go to whom in what order, approximate start/end times, where split shifts and vehicle
handoffs happen and whether each is feasible, what still gets farmed out, and where the plan is
tight. The dispatcher reviews, adjusts, approves. **Nothing auto-commits.**

The founder's success test: *"take a completely unassigned day and produce a substantially better
setup than we create manually today."* His definition of better: **more coverage, no conflicts,
realistic sustainable hours, more in-house.** That is a constrained maximisation — coverage is
the number that goes up; conflicts and hours are walls — not a weighted soup.

**And the governing style rule: available ≠ required.** "Use 9 drivers and 11 cars, leave these
3 people off and these 3 cars parked" is a *correct answer* on a light day. The objective must
prefer the smallest resource set that covers the day well — structurally (minimise driver-days
subject to coverage ≥ the hand-finished baseline), not as a tiebreak, because a
coverage-first ordering provably degenerates to "use everybody" on the 64% of days when demand
tops the fleet.

### 1.1 The founder's decision ledger (2026-08-23, binding on all later phases)

| # | Decision |
|---|---|
| D1 | "Required drivers" counts **in-house only**; total demand concurrency minus it = farm-out exposure. |
| D2 | Pre-build diagnostic **guides**; post-build advisors **govern**. |
| D3 | Phone-GPS capture stays **off**. |
| D4 | **Hours: 13.5 h soft cap enforced by default; 15 h tolerable on crunch days — but structured**: a visible per-driver exception with its price ("+1.5 h on X keeps 2 legs in-house, ≈$142"), never a silent habit. |
| D5 | **Alert precision bar: ≥7–8 of 10 warnings must be real.** Confirmed alerts become enforced tasks, which raises the stakes on precision. |
| D6 | **Minted second shifts ideally carry ≥2 jobs — soft, never a hard floor** (drivers are paid per job; there is **no call-out minimum pay**). |
| D7 | **The base handoff process is the model**: drop last guest → wash (MCO→wash 14–17 min, wash 15–20) → fuel → base at 6785 Narcoossee Rd (wash→base ~20; MCO→base 12; Disney/Universal→base ~40); incoming driver waits at base ≥1 h ahead. House handoffs exist but are exceptions — do not build around them. [founder-supplied] |
| D8 | **Fleet expansion ("how many cars, when") is the follow-on phase**, fed by the optimizer's residuals — optimize what we own first. |
| D9 | The build process to automate is the schedulers' own. Initial build: driver-by-driver with the per-driver Schedule Builder, in **descending vehicle-tier order** — Sprinter/14-pax drivers first (select vehicle, assign the 14-pax-only jobs), fill the gaps that creates with next-tier (van) work, descend the tiers, then **redistribute to balance**. Splits: vehicles→drivers first, then overflow drivers, then hunt/create a mid-day gap and pair the second driver on the same car. Named ground truth: **2026-08-20, Angel & Charlie and Jose & Omar** — reconstructed and validated in evidence. |
| D10 | **The per-trip what-if simulator is deferred.** ("Could this farmed/unassigned trip go in-house? What swap frees someone?") A natural later add-on over the proven engine parts — swap_optimizer/execute_swap, the Farm-Out Optimizer, the replay fill logic — not part of the first build. |
| D11 | **Additive deployment; promotion is the founder's judgment.** The current build flow stays exactly as it is; everything here ships alongside it as an additional tool. It becomes the main flow only when the founder decides it has proved itself — his call, made later; no formal metric gate is defined now. |
| D12 | **No rushed build.** "This is literally the heart of the operations." All Phase-1/Phase-2 documents are completed and reviewed before any code — the build starts from an approved build-readiness package (Gate 3), not from momentum. |

---

## 2. What the verification sessions established

The compressed evidence base the plan rests on. Full derivations and scripts land in deliverable 02.

**The wall.** Reshuffling trips among the drivers already on cars is worth ≈ nothing: a full
greedy re-seat under the 13.5 h cap reaches 77.5% in-house coverage; the humans' own board forced
under the same cap reaches 77.1% — two independent constructions 0.4 pp apart [measured]. The
roster is consumed (15.11 rostered drivers/day on 13.89 cars, 0.07 idle). **The farm-out is a
capacity fact, not a scheduling fact.**

**The whole-board build is extinct; the board is built driver-by-driver.** Board-level
auto-assign last ran 2026-08-10; 1 of 28 current-regime dates [measured]. What runs daily is the
per-driver Schedule Builder in descending vehicle-tier order — Sprinter/14-pax drivers first,
gaps then filled with next-tier work, then manual redistribution to balance [founder-supplied;
corroborated by median 9 builder bursts/day on all 28 dates]. The incumbent to beat is that
builder-assisted, hand-finished board (state B: 81.3% in-house,
108.0 legs/day) — and today's 81.3% is partly **bought with illegal hours** (4.00 driver-days
>13.5 h and 2.18 >15 h *per day*, max 23.6 h) and carries **11.61 hard-infeasible turn pairs/day**,
which measurably matter (2.06× late-arrival lift) [measured].

**Where coverage is actually lost.** Reassignment churn is net-negative in *every*
time-to-pickup band; coverage is created at first placement, peaks ~T-12, then leaks: **~7.5
legs/day walk out to affiliates in the 24–72 h build/finalize window (~$196k/yr at the $70.99
premium)**, releases outrun recaptures 3:1, and day-of work is drift absorption (flight retiming
adds +4.18 hard turns/day; dispatchers absorb 3.75) [measured]. **Consequence: the optimizer is a
day-before build tool, never a day-of rescuer.**

**The lever that works — the founder's own.** Enforcing 13.5 h *alone* loses 1.25 legs/day.
Enforcing it **while minting standby second shifts on shared cars** nets, vs today's 81.3%
[modeled, adversarially verified — an earlier +4.0 headline was cut to +2.4 after the verifier
caught a car-in-two-places bug]:

| Setting | Coverage | Net legs/day | Gross ≈ net $/yr (no call-out minimum exists) |
|---|---|---|---|
| Conservative (gap 180 / buf 45) | 82.5% | +1.29 | ~$33k |
| **Central (gap 120 / buf 30)** | **83.5%** | **+2.36** | **~$61k** |
| Generous (gap 90 / buf 5) | 84.9% | +3.89 | ~$101k |

Zero driver-days over 13.5 h, zero new rest breaches — and capping *heals* existing breach-pairs
68→32. Standby usage 2.7 mints/day, inside the founder's 1–4 envelope on 24/28 days. The
behavioural standby pool is 6–9/day (never below 4); the reachable core matches the founder's
1–4 [measured/inferred]. Pool is thinnest Friday — inside the Fri–Sun band carrying 76.3% of
farm-out — but bodies bound only 23 of 476 fill failures; **cars bound the rest**.

**The ≥2-job preference (D6) costs nothing soft, and a hard floor would gut the program**
[modeled]: soft packing = +2.39 legs/day (+$925/yr vs unrestricted) with 1-leg mints trimmed
54→50; a hard 2-leg floor collapses it to +0.61 (~$16k), re-farming 50 legs. ~Two-thirds of mints
are structurally single-job (no second pool leg can feasibly join). **Implement soft packing +
dispatcher discretion on the residual ~1.8 single-job call-outs/day — offer, let the driver
decline.**

**The ceiling and the ladder.** Unlimited standby saturates at **84.1%** — cars run out, not
bodies; 88% of residual farm-out picks up 08:00–12:59, where no rolling car has a free side
[modeled]. The priced ladder: standby mints → ~83.5% (~$61k/yr) · each +1 rostered morning
driver-shift ≈ +2.75 legs/day (~$71k/yr) · past ~93%, vehicles (Fri–Sun 10:00 crest runs ~6 cars
short). Rungs 2–3 are the D8 follow-on.

**Handoffs.** 8.7% of vehicle-days, 21/28 dates, never 3+ drivers/car, gaps n=32 min 72 / P50 220
/ P90 394 [measured]. The founder's chain closes against practice with ~51 min median headroom;
the 72-min floor is the skip-wash fast path; west-side handoffs bypass the base. The shipped
`VEHICLE_SHARE_PAD_MIN = 60` sits at ~P9 of reality. **Every handoff ever was arranged by hand**;
no scheduler path writes a DVA row; the split-window columns built for exactly this are NULL on
all 2,591 rows. The 2026-08-20 example: splits pre-planned two days out, jobs shuffled between
pair members to carve the gap, farm-out crushed to 4 legs vs the 20.18 mean — **splits are
farm-out suppression on soft days, not only crunch capacity** — and one oversized gap silently
absorbed a 4.4 h flight swing, so gap-shaving must price flight volatility.

**Dispatcher load.** ~148 hand moves/day across ~5 dispatchers (≥92.6 distinct clock-minutes/day,
a floor) [measured]. Removing that work is a first-class benefit; never convert it to dollars
without stating the assumption.

---

## 3. The deliverable plan

### Track A — the optimizer (the main line)

| Step | Artifact | Gate |
|---|---|---|
| **A0 (done)** | Amended 00 + this document | **Gate 1 — founder approves both. Nothing later starts first.** |
| **A1** | `02_BENCHMARK_AND_EVIDENCE.md` + committed `analysis/` scripts reproducing every §2 headline from the DB at run time (state-B benchmark, de-phantomed transition stream, standby pool, cap+mint replay, hour-binder, zone chain validation). The session findings become re-runnable — when the data moves, the numbers move. | Gate 2 — scripts reproduce §2 within stated tolerances. |
| **A2** | `03_STANDBY_AND_HANDOFF_MODEL.md` — the config the optimizer will carry: the zone-labeled handoff chain table (every component tagged [founder-supplied]/[shipped-estimate]/[assumed]; fuel time still **[assumed 5–10 min]** — founder to confirm), the standby eligibility definition, mint rules (soft ≥2 packing, thin-mint flag), the 13.5/15 exception structure, green/amber/red handoff feasibility. | Reviewed with A1. |
| **A3** | `PLANNER_AND_BUILD_PLAN.md` (Phase 2 of the brief — a spec, no code). Must cover the engineering the sessions proved necessary: **extracting the build pipeline from the 700-line view** into a callable that accepts hypothetical rosters (the six unparameterised call sites + eight view-level ones are enumerated in session evidence); the **co-driver car-share gate** (the replay verifier proved its absence mints physically impossible plans — ~2 legs/day of them — so it is a hard prerequisite); the **apply-path write contract per output field** — including "leave this driver off" (the current Apply cannot delete a DVA row) and held-date behavior (roster writes must not leak live); the surrogate-noise test before any roster ladder ships; server-side enforcement of ≤2 drivers/vehicle-day; alert calibration to D5's 7–8/10 bar; where every config value lives. Explicit non-goals: no auto-apply, no new write surface beyond the specced doors. | **Gate 3 — founder approves the spec. Deviations go back for review, not judgment calls.** |
| **A4** | Phase 3 — the build, into Day Setup, per spec exactly. Dispatcher-visible ⇒ ships with a release note per CLAUDE.md. Browser-tested on a real date. | **Gate 4 — acceptance:** on replayed dates, proposed plans ≥ the hand-finished board on in-house coverage with 0 hard conflicts, 0 days >15 h, exceptions priced and visible; flagged conflicts ≥70–80% real. |

### Track B — quick fixes (independent of the big build; small, high-certainty)

1. The plain assign endpoint runs **no feasibility and no share validation** — established from
   the code itself [verified 2026-08-23]: `update_leg_assignment`
   ([views.py:2719](../../dispatching/views.py#L2719)) checks only staff permission and
   cancelled-status, warns (without blocking) on pending refunds, then calls `set_leg_driver`
   ([assignment.py:126](../../dispatching/assignment.py#L126)), which routes sandbox-vs-live and
   writes `leg.driver` — neither layer calls `check_feasibility`, any turn/slack test, any
   vehicle-share/co-driver check, or any span/rest gate. Adding the existing validation to that
   path is still the recommended fix, on code grounds alone. *(Correction, 2026-08-23: an
   apparent quadruple booking of unit #14 on 2026-08-21 was previously cited as this gap's
   consequence; founder review established it was a **vehicle swap**, not simultaneous use. No
   incident is attributed to this gap — and rapid same-day driver changes on one unit are a swap
   signature, not evidence of conflict. That caution applies to any future reading of the
   assignment history.)*
2. The `signals.py:751` skip-guard writing phantom assignment rows (30.8% of the trail).
3. `VEHICLE_SHARE_PAD_MIN` 60 → the zone chain (or at minimum 120, the empirical anchor).
4. The `'canceled'` one-L spelling gap in `day_setup.py:122`.

Each is dispatcher-invisible or bug-fix-grade; released per CLAUDE.md rules as they land.

### Track C — follow-ons (in order, after Gate 4)

1. **Live-ops assistant** — day-of drift repair: watches the board, proposes *validated* fixes
   (the shipped-but-unused `execute_swap` endpoint becomes the default path instead of raw
   drag-drop), calibrated to D5. Advises dispatchers only; never contacts drivers. Same organs
   as the optimizer, different clock — sequenced, not blended.
2. **Question 2 — capacity planning** (D8): how many drivers/vehicles, which types, when, at what
   ROI — fed by months of optimizer residuals, which are by construction the legs current
   resources genuinely couldn't reach, reconciled against Fleet Capacity Intelligence and the
   Farm-Out Optimizer per the original brief.

### What moved out or died

- **`FARMOUT_RECAPTURE.md` leaves this project** — it is Question 2 (D8). Keeping it would invert
  the founder's ordering and invite blaming plan residuals on headcount.
- **`DEMAND_AND_UTILIZATION.md` / `SHIFT_ARCHITECTURE.md` / `REPLAY_AND_EVIDENCE.md`** as
  originally scoped are absorbed: demand/waste and the replay become A1's benchmark evidence;
  shift templates become A2's standby/mint/handoff model (the sessions showed the real
  architecture is *base shifts + minted splits*, not a template menu).
- The **"you are short N drivers" diagnostic** as the end product. The corrected in-house
  concurrency number survives *inside* the optimizer (D1/D2), not as the deliverable.

---

## 4. Honest limits (carried from 00, plus new ones)

1. **Summer numbers.** Season and growth remain confounded; every level claim is a summer claim.
2. **28-day regime.** Every current-regime percentile carries n=28 days.
3. **Standby willingness is unrecorded.** The 6–9 pool is behavioural; who answers the phone is
   [unavailable]. Replay results inherit that assumption; the same-day pull-in record (0.75/day,
   tripled since the step-up) is the floor of true reachability.
4. **All replay gains are counterfactuals** against fixed demand with greedy fills — treat +2.36
   as a defensible central estimate, not a promise. ~46% of specific farm refills drift >30 min
   by service time and need day-of repair; the aggregate survives plan-time re-derivation
   (+2.71/day).
5. **Arrival clocks move** (97.5% retimed, mostly in-day). Plans must be built on forecast times
   and expect repair; any handoff the optimizer proposes must survive the measured flight
   volatility (the 08-20 example absorbed a 4.4 h swing only because its gap was oversized).
6. **Deadhead stays unobservable** (D3). The zone chain prices the base process; it cannot see
   where a car actually is.

---

*Next artifact: `02_BENCHMARK_AND_EVIDENCE.md` with committed scripts — after Gate 1.*
