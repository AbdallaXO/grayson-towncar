# 06 — The Day Manager (D13): design plan

**Status: plan only, no code.** Produced 2026-09-03, checked against the repo and the local
snapshot on 2026-09-05. Lands on `scheduling-redesign/phase-1` — every module this plan names
lives on that branch, not on `main`.

Every figure is labelled **[measured]** / **[session-measured]** / **[modeled]** /
**[founder-supplied]** per 00's convention.

**Snapshot provenance (settled 2026-09-05).** Baselines are cut from the **desktop copy**, now
in place at `content/db.sqlite3` (660 MB). Every independent production write stream in it stops
at **2026-08-21 22:17 UTC**, so that — not 2026-09-01 — is this document's horizon, and every
day-level figure stops at **2026-08-21**. The earlier MacBook copy was newer (writes to
2026-09-01) but partial in exactly the tables §1.1 depends on; it has been replaced. **One
consequence matters: the five tuning commits of 2026-08-25 → 08-27 land after this snapshot
ends, so their effect cannot be measured here at all** (§0.2).

---

## 0. Verification log (2026-09-05)

### 0.1 Code claims — all confirmed on `scheduling-redesign/phase-1`

| Claim | Evidence |
|---|---|
| The advisor exists and is 2,9xx lines, read-only, propose-only | `dispatching/conflict_advisor.py` — 2,943 lines |
| Gated to superusers only | `advisor_views.advisor_visible_to` (`advisor_views.py:68`) — `is_authenticated and is_superuser`, one line to change |
| Fixed 4 s budget, ≤6 planned cards, hygiene TTL 90 min | `conflict_advisor.py:134` `ADVISOR_BUDGET_MS = 4000`; `:129` `ADVISOR_MAX_DISRUPTIONS = 6`; `:111` `ADVISOR_HYGIENE_TTL_MIN = 90`; per-card budget at `:2889` |
| Board fingerprint cache | `compute_board_fingerprint` (`:267`), `_advisor_state` (`:2855`) |
| **The live clock split is real** | `board_validation.turn_slack_minutes` (`board_validation.py:52`) contains no `take_later` rule; its docstring still claims it is "the SAME arithmetic `scheduler.check_feasibility` uses" — a parity `CHAIN_CLEAR_TAKES_LATER` broke |
| Engine side of the same rule | `scheduler.py:236` `CHAIN_CLEAR_TAKES_LATER = True`; `_slot_chain_end` (`:1106`); `check_feasibility` (`:1127`), fixed-time branches at `:1246` and `:1317` |
| **The scanner is a third clock** | `ops/tasks.py:154` calls `scheduler.estimate_job_end_time`; `classify_turn` (`:260`) bands on `TIGHT_TURN_RED_AFTER_MIN = 10`. The module's own comment (`:222`) already admits "a chain could be feasible to the engine and red to the scanner at the same moment" |
| Call-in tier has its parts | `standby_mints.standby_pool_ids` (`:104`), `FARMOUT_PREMIUM_PER_LEG = 70.99` (`:38`) |
| No new daemon needed / allowed | `samsara_scheduler.py` — 180 s, lock `737_202`, `sweep_eta` at `:234`; `ghl_integration/scheduler.py` — 1800 s, lock `737_201`, runs `generate_ops_tasks` at `:183`. 04 §6 (`:183`) bans a new periodic daemon outright |
| `AdvisorEvent` / `DispatchEtaSample` do not exist | absent from `dispatching/models.py` — both are new work in Phase 1 |
| **The advisor has never been applied in production** | `reservations_schedulesnapshot.trigger` holds only `before_reset` (119), `before_auto_assign` (45), `manual` (36). No `conflict_advisor` trigger, ever |
| Analysis script numbering 23–25 is free | `docs/scheduling-redesign/analysis/` runs 00–20, 22 (21 is unused); 23–28 are now taken |

### 0.2 Snapshot reconciliation and the scanner baseline [measured, 2026-09-05]

Run: `analysis/25_scanner_outcomes.py` → `analysis/out/25_scanner_by_month.csv`,
`25_scanner_closures.csv`, `25_scanner_by_day.csv`.

**The §1.1 figures reproduce on the desktop copy, and the current regime is worse than this
document originally said.** `driver_conflict` + `tight_turn` filing, by month, per active day:

| Month | Active days | Tasks | Tasks/day | Legs flagged | % of legs | Open P50 | Open P75 | % moved | % no-move | % hand-closed blank | **% wasted look** |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2026-05 | 31 | 405 | 13.1 | 301 | 10.5% | 61 m | 194 m | 26.4 | 57.8 | 12.3 | 70.1 |
| 2026-06 | 30 | 1,110 | 37.0 | 646 | 23.7% | 84 m | 197 m | 23.0 | 54.8 | 18.2 | 73.1 |
| 2026-07 | 31 | 1,157 | 37.3 | 698 | 25.7% | 62 m | 208 m | 23.8 | 46.5 | 23.0 | 69.4 |
| **2026-08 (1–21)** | **21** | **1,489** | **70.9** | **814** | **33.9%** | **68 m** | **276 m** | **24.5** | **40.0** | **26.0** | **65.9** |

Against the original claims: "65 tasks/day" → **70.9**; "31% of the day's legs" → **33.9%**;
"median 79 min open, P75 4.7 h" → **68 min / 4.6 h**; "45% no-move + 21% blank" → **40% + 26%,
i.e. 65.9% of closes bought nothing**; "30% closed by a reassignment/unassign" → **24.5% moved
plus 4.8% retimed = 29.3%**. Escalations (a tight turn hardening into a conflict — one problem
counted twice) are a further 6.4% and are excluded from those shares.

**The trend is the part that was missing.** Filing has gone 13/day → 37 → 37 → **71**, and the
share of the day's legs carrying an alarm has gone 10.5% → 33.9%, in five months. A third of
every day's trips now raise an alarm, two thirds of which resolve without anyone moving
anything. That is a stronger case for this project than the flat 65/day figure made, and it is
also the noise floor the advisor has to beat: **≤5 cards a glance against ~71 tasks/day today.**

**What still cannot be answered here.** Five commits — `2c36aada` (2026-08-09, builder stops
building undriveable chains), `c04489f8` and `083a7d0a` (2026-08-25, conflict tasks turn critical
only when the driver truly won't make it; 10-minute boundary), `2419c414` and `076dfe8e`
(2026-08-27, flags come down when the conflict does; reassigning clears the flag on the spot) —
were aimed squarely at this noise. **Four of the five land after this snapshot's last write, so
their effect is unmeasurable on any copy currently on disk.** The August figures above are
therefore a *pre-tuning* baseline. Re-running `25` on a pull taken after 2026-08-27 is the one
outstanding Phase 0 item, and it decides how much of Phase 1.4 and Phase 2.2 is still needed. It
does not block `23` or `24`.

Everything downstream of §1.1 — the ranking of failure modes, the choice of engine, the
precision problem, the ladder, the phasing — stands on the code and the replay, not on the task
table, and is unaffected either way.

---

## Context

The day-before builder (Build 3b) ships. This plan covers D13: managing the day as it unfolds.
The brief asks for a system that spots trouble early, works out the smallest safe set of
changes, explains it, and lets a dispatcher approve — never rebuilding the day to fix one trip.
Constraints: one gunicorn worker, 60 s requests, no new daemon (04 §6), `set_leg_driver` is the
only write door, nothing driver-facing, propose-only but auto-apply-ready (D14), held dates
never leak, and a warning class ships only at ≥70–80% precision (D5).

**The headline finding, in one paragraph.** The day manager already exists.
`dispatching/conflict_advisor.py` (2,943 lines, shipped 2026-08-05) is a read-only,
deterministic, two-clock, propose-only day-of repair engine with five detectors, a
monitor→match-flight→reassign→swap→takeback→farm ladder, whole-remaining-day validation, a 4 s
budget, a 60 s fingerprint poll, snooze, Undo, and an apply path through `set_leg_driver` with
staleness 409s. It is gated to **superusers only** and has **never been applied in production**.
Its precision has **never been measured**. Meanwhile the floor runs on three *other* detectors
on three different clocks. The work is not a new engine. It is: measure the advisor, fix the one
clock split it shares with the board, collapse the competing detectors onto it, and promote it
through the D5 gate — with a persisted outcome log so the precision number keeps being true
after launch.

---

## 0.3 Your figures, verified

| Your figure | What the data says now | Source | Verdict |
|---|---|---|---|
| ~148 hand moves/day across ~5 dispatchers | In-house→in-house reassigns 127/day + pullbacks 15 + releases 6.4 + recaptures 2.1 + vendor swaps 3.5 ≈ **154/day over the whole booking window**. The 24–72 h band alone carries **147.6 transitions/day**. **Same calendar day as pickup: 53 transitions on 26 legs** (28 reassigns, 13 assigns, 10 unassigns). | 08 re-run 2026-09-03 [measured] | Verified as a total; **not a day-of figure**. The day-of number is ~53. |
| Churn net-negative in every time-to-pickup band | −1.53 (24–72 h), −2.53 (6–24 h), −1.66 (1–6 h), −0.72 (<1 h), −0.38 (after) legs/day | 08 §6 [measured] | Verified. Day-of revision leaks ~2.8 legs/day in the last 6 h. |
| Flight retiming adds ~4.18 hard turns/day; dispatchers absorb 3.75 | Not reproducible from any committed script; appears only in 01 §2 as a session figure. | grep of `analysis/*.py` | **Unverified [session-measured].** The Phase-0 replay harness replaces it. |
| 8.6% of trips / 14.6% of arrivals move ≥30 min after the night-before cutoff | 10.6% of legs / 21.6% of airport pickups on Aug 4–28 | auditlog vs day-before 20:00 [measured] | Verified in direction; a little higher on the newer window. |
| 97.5% of arrivals retimed, mostly in-day | ~385 pickup_time writes/day, 85% same-day, 69% under 15 min — **but 9,642 rows collapse to 913 bursts (36.5/day) and only 7.1/day are single-leg hand matches.** | auditlog burst analysis (≤2 s gap) [measured] | Verified **and reclassified**: bulk "Flight match" button work, already automated, not hand moves. |
| Shipped risk band false-alarms 66–97% | `at_risk`: 34% end up past the deadline at all, **4% by >15 min**; `watch`: 3% / 2%; `late`: 100% / 43% (post-hoc, not a prediction) | 07 [measured] | Verified; worse than stated once "late" means >15 min. |
| ~10 fixed-time pickups/day the driver could not reach, fixed by `CHAIN_CLEAR_TAKES_LATER` | 107 → 1 over 11 dates (9.7/day); cost 40 trips / 11 days (~3.6/day farmed) | 22_later_clock.csv [measured] | Verified. |
| 24% of accepted turns ran >15 min late; 46% of refused ones did | Committed: 24.2% / 46.3% (914 pairs). Re-run on the 10 newest trustworthy days: **23% / 32%** (762 pairs). Precision flat across deficit size (<−30: 50%, −30..−15: 42%, −15..0: 47%). | 19 re-run [measured] | Verified; separation is weaker on newer dates. |
| Does a similar clock split exist on the live/day-of path? | **Yes, twice.** (1) `board_validation.turn_slack_minutes` — used by board chips, `assign_warnings`, the advisor's overlap/cascade/reach math, `validate_post_move_board` and swap revalidation — never applies `take_later`. On 21 real hand-built days: 38.8 fixed-time turns/day, **10.8 band flips/day**, **6.1/day where the live path says clean/tight and the engine's corrected clock says negative.** (2) The 30-min ops scanner runs a third clock: `estimate_job_end_time` (p75) + reposition, red at >10 min. | scratch `live_clock_split.py` [measured]; code §0.1 | Confirmed and quantified. |

---

## 1. What actually goes wrong during the day, ranked

**Plain English first.** Three things eat dispatcher time on the day. In order of work:
(1) triaging alarms that are mostly wrong or self-resolving; (2) moving trips because a
driver's previous job ran long or a plane moved; (3) clicking the bulk flight-match button.
In order of service damage: a driver arriving late to a fixed-time pickup because the job
before it ran long — which no plan can see and only taps and GPS can.

### 1.1 Dispatcher work per day (Aug 4–28, 25 days)

| Work | Volume/day | What the record shows |
|---|---|---|
| **Alarm triage** — `driver_conflict` + `tight_turn` tasks from the 30-min scanner | **70.9 tasks on 39 legs/day — 33.9% of the day's legs** (Aug 1–21, pre-tuning); median 68 min open, P75 4.6 h [measured, §0.2] | How they close: **40%** because the arithmetic changed or the leg simply completed; **24.5%** by a reassignment/unassign and **4.8%** by a retime; **26%** closed by hand with a blank note; **4.4%** cancelled; 6.4% escalate rather than close. **65.9% of closes bought nothing.** Plus system-raised KEOI flags, `flight_verify` tasks, board pills, and the Samsara chip |
| **Same-day driver changes** | **53 transitions on 26 legs** (28 reassign, 13 assign, 10 unassign) [measured] | The true "hand moves on the day" number. Two-thirds of the ~150 total happen in the 24–72 h build/finalize window — the builder's territory |
| **Flight bookkeeping** | 36 bulk "Flight match" bursts (385 row writes); 7 single-leg hand matches [measured] | Already a button. Not a target |
| **Farm leak from day-of revision** | ~2.8 legs/day in the last 6 h ≈ $200/day at $70.99 [measured] | Small next to the 24–72 h leak (7.5/day) |

### 1.2 Service damage [measured, 19 re-run]

| Turn as the engine sees it | Share arriving >15 min late at on-location |
|---|---|
| Accepted, next job a **return/departure** (fixed time) | **8%** (n=267) |
| Accepted, next job a **cruise** | 19% (n=54) |
| Accepted, next job an **airport arrival** | 42% (n=285) — noisy: "booked" is the landing slot, so this measures deplaning more than lateness |
| Refused (negative slack), any | 32–46% |

Slack alone barely separates late from on-time. What separates them is the *kind* of next job
and whether the previous job is demonstrably running long — a fact only taps and GPS carry.

### 1.3 Failure modes, ranked by (work × damage)

1. **The previous job runs long** (dwell, traffic, a late drop). The dominant real cause of
   lateness. Invisible to any plan; visible only from the recorded picked-up tap, the mid-trip
   GPS ETA to the next pickup, or the job overrunning its estimate.
2. **Alarm noise itself** — three detectors on three clocks, **70.9 tasks/day on a third of the
   day's legs, two thirds of them buying nothing**, and rising sharply month over month. This is
   what generates the most work and is why D5 exists. The August figure is **pre-tuning**;
   re-measure on a post-2026-08-27 pull before sizing Phase 1.4 (§0.2).
3. **A later plane breaking the turn OUT** of an arrival. 10.6% of legs move ≥30 min after the
   cutoff; the bulk match handles the bookkeeping, the scanner raises the conflict, dispatchers
   absorb most of it by hand.
4. **Same-day new or uncovered work** — 13 same-day first assignments/day.
5. **Driver or vehicle outages, no-shows** — unrecorded anywhere in the data [unavailable];
   founder-supplied only. Handled by the same ladder, not by a detector.
6. **Coverage** — explicitly not the day manager's job (verified: net-negative churn in every
   band).

---

## 2. Share the builder's engine, or be its own thing?

**Answer: share the organs, never the pipeline. The day manager is the Recovery Advisor,
promoted — not a new engine and not the builder.**

| | Builder pipeline (`assignment_pipeline.run_assignment_pipeline`) | Recovery Advisor (`conflict_advisor.compute_advisor_state`) |
|---|---|---|
| Runtime | 3–27 s measured on real dates, superlinear in trips; `find_swaps` per residual leg with a 5 s wall budget; **non-deterministic when the budget binds** | Fixed **4 s** wall cap, ≤6 cards planned, swap search 1.2 s/card, **deterministic**, exactly 15 queries pinned by test |
| Scope | Whole day; no time-window parameter; `legs` must include every assigned leg or occupancy is wrong | Remaining day only; frozen legs (picked-up / on-location / past due) never move; ≤2 target legs per card |
| Clock | Planning clock only (static), no notion of *now* | Two clocks: detection re-anchors on recorded pickups, planning is never optimistic (`max(static, actual)`) |
| Objective | Coverage-first at a fixed roster (the wrong objective day-of: churn is net-negative) | "Fix what BREAKS, never a signal on its own" (prime directive) |
| Writes | Propose-only; its apply path in `views.auto_assign_drivers` re-implements `set_leg_driver` inline and has drifted (D14 gap) | Front door only (`set_leg_driver`, `apply_pickup_time_move`), row locks, staleness 409, snapshot Undo — **D14-ready today** |
| Where it runs | Background daemon thread + `DayPlan` ledger row | Inside the 60 s poll's GET, cached per (date, board fingerprint) for 120 s |

Both already share the same organs: `scheduler.check_feasibility`,
`board_validation.validate_post_move_board`, `swap_optimizer.find_swaps`,
`car_share.sharers_conflict`, `feasibility_guards.required_turnaround`,
`farmout_optimizer.WaterfallLedger`, `pickup_policy`, `assignment.set_leg_driver`. That is the
right sharing: one feasibility law, two tools with different clocks.

What the advisor is missing, and what this plan adds: (a) a measured precision number and a live
outcome log; (b) the `take_later` clock rule on the live path; (c) collapsing the ops scanner and
system-raised KEOI onto the advisor's detection so the floor sees one verdict per turn; (d) a
call-in tier; (e) a runtime check under today's volume.

**Runtime budget — settled, no work needed [measured, 23, 3,864 computes].** P50 **60 ms**, P90
**325 ms**, max **905 ms**, and **zero ticks over the 4 s budget** across 28 days at 15-minute
ticks. An earlier pass recorded one 70 s tick; that same minute re-runs in 52 ms in isolation and
the uncontended full pass tops out at 905 ms, so it was host contention (two replays sharing a
660 MB copy), not the advisor. **The Samsara-tick precompute contingency
is dropped** — there is nothing to precompute around. The card cap is also less pressing than
the prototype suggested: `ADVISOR_MAX_DISRUPTIONS = 6` truncates plan generation on **25% of
ticks**, not 55%. Never the assignment pipeline.

---

## 3. Detection design

### 3.1 What it watches, on which clock

The five existing detectors stay; each carries a `basis` so a dispatcher can verify the flag:

| Detector | Trigger | Clock / basis | Ships as a card only if… |
|---|---|---|---|
| `overlap` | adjacent-pair slack < 0 (critical) or thinned from ≥15 to <15 by a **recorded pickup** (warning) | detection clock re-anchored on the picked-up tap; planning clock for "before" | the class passes the gate (§3.3) |
| `late_cascade` | fresh GPS at_risk/late on a pickup target, or clock-overdue with no status motion, propagated down the chain | `gps_fresh` / `clock_only` / `gps_stale_parked` | GPS negative signal only; clock-only never strips work off a driver (guard 5) |
| `overrun` | still on a job past estimate + 20 min, or mid-trip GPS says the chained next pickup is blowing | `recorded_pickup` / `gps_fresh` | breaks downstream exist |
| `flight_change` | controlling flight moved ≥15 min off its **own schedule** (not off the booking) and the turn OUT breaks or thins | `flight` | a following job exists on an in-house driver |
| `unassigned` | driverless inside 120 min | `clock_only` | always (trivially real) |

Two rules kept verbatim: **never a signal on its own** (a moved plane is a fact for the board,
not a card) and **never a moment that has passed** (every card carries `expires_at`).

Two clock fixes this plan adds:

1. **`take_later` on the live path.** `turn_slack_minutes` gains the same rule
   `check_feasibility` got on 2026-09-02: when the next pickup is fixed-time (return, departure,
   cruise — not an airport arrival), clear = `max(chain_clear_dt, estimated_end_time)`. This
   closes the 6/day live-says-clean-engine-says-negative gap and makes chips, warnings, advisor
   and apply-revalidation agree with the engine again — the docstring at `board_validation.py:58`
   currently claims a parity it no longer has.
2. **One clock for the scanner.** `ops/tasks.classify_turn` and `detect_driver_conflicts` are
   re-pointed at `turn_slack_minutes` + `_turn_severity` (or retired in favour of tasks filed
   from advisor cards, guard 9). Today they price the same turn from `estimate_job_end_time`
   (p75) with a 10-min red line — the module's own comment already flags the contradiction.

### 3.2 Why precision is the whole problem, with numbers

Measured against "the impact leg's driver arrived >15 min after booked / gate+10":

| Signal, alone | Precision | Source |
|---|---|---|
| Planning-clock negative slack (any deficit) | 32–47% | 19 |
| GPS `at_risk` band | 4% (34% for "late at all") | 07 |
| GPS `late` band | 43% (post-hoc; not a prediction) | 07 |
| GPS mid-trip, chained next-pickup slack < 0 | 39% (>15), 48% (>10), **72% (late at all)** | 07 |
| Manual-assign turn warnings vs the **09 definition** (not reality) | 92.5% / 80.5% | 12 re-run |

No single signal clears 70% on the >15-min bar. The 92.5% number that let Build 1 ship measures
agreement with a *definition*, not with what happened. The advisor's compound classes had never
been scored.

**The design bet was: a card requiring two independent facts — a live fact (recorded pickup,
fresh negative GPS, flight actual) AND a negative engine slack on a fixed-time impact leg —
clears 70%. Expected to pass: `flight_change` into a fixed-time job, `overlap` re-anchored on a
recorded pickup. Expected to fail and be demoted: clock-only overdue, hygiene.**

**Measured 2026-09-05 (§3.3): the bet is refuted, and the ranking is inverted.** The two classes
predicted to pass are the two worst forecasters on the rail — `overlap` on a recorded pickup
**32.4%**, `flight_change` **24.9%** — while the clock-only cascade the plan wanted demoted is
the best of them at **43.2%**. A recorded pickup turns out to say very little about whether the
*next* leg runs late; it mostly says the driver is running early or late by a few minutes, which
the deplaning grace and the turnaround pad absorb. Two-independent-facts is not the discriminator
this design hoped for, and no rewriting of the detectors is justified by this evidence alone —
what is justified is §3.3's conclusion about what the advisor is actually *for*.

### 3.3 How precision gets measured before shipping

**`analysis/23_advisor_replay.py` — the gate script, written and baselined before any product
code.** Prototype run on the throwaway copy (`scratchpad/advisor_replay.py`), so the method is
proven:

- For each date in the 28-day regime and each tick (every 15 min, 06:00–23:00): **rewind** the
  copy's leg rows to their state at the tick — `driver_id` and `pickup_time` from
  `historicalleg`, `status` from the earliest `LegStatus` taps ≤ tick, later taps deleted, flight
  `actual_*` fields **masked** until they happened, `dispatch_*` blanked; call
  `compute_advisor_state(day, now=tick)`.
- Score every card's **impact leg** (last id in `leg_ids`) against reality: first on-location tap
  vs `max(booked, actual gate + 10)`; batch taps discarded (19's rule); unscorable cards counted,
  never dropped.
- Report per `(kind, severity, basis)`: cards/day, precision at >15 and >10, share with move
  plans, and compute time per tick. Also the **dispatcher-noise proxy**: cards per day against
  whatever §0.2 establishes as the scanner's true current rate.
- Known blind spot, stated on the output: GPS `dispatch_*` fields are not historized, so
  GPS-based classes cannot be replay-scored. They are scored live after 2–4 weeks of logging, and
  stay detected-only until then.

**Result — 28 days, 06:00–23:00 every 15 min, both estimate bounds, 3,864 computes
[measured 2026-09-05, `analysis/out/23_advisor_precision.csv`]:**

Rail load first, because the prototype was wrong about it: **4.2 cards per glance (P50 3, P90 10,
max 21)**, of which 3.2 critical. Not 16. The prototype's four days at 3-hour ticks were not
representative, and the fear that the rail would drown a dispatcher is **not supported**.

Precision, live-estimates bound (the masked bound differs by <1 point everywhere, so the
estimate-historization blind spot turns out to be small):

| Card class | Cards/day | With plans | One-leg | Scored | >15 min | >10 min | Late at all | **>15 forecast-only** |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `late_cascade` critical — clock_only | 10.0 | 58% | 41% | 187 | 62.0% | 65.2% | 74.3% | **43.2%** |
| `overlap` critical — recorded_pickup | 9.2 | 96% | 0% | 238 | 32.4% | 37.4% | 59.7% | **32.4%** |
| `flight_change` critical — flight | 8.6 | 88% | 0% | 213 | 24.9% | 31.0% | 46.0% | **24.9%** |
| `overlap` warning — recorded_pickup | 4.8 | 64% | 0% | 129 | 24.0% | 25.6% | 45.0% | **24.0%** |
| `late_cascade` watch (hygiene) | 3.1 | 0% | 100% | 74 | 60.8% | 64.9% | 73.0% | — |
| `overrun` warning — recorded_pickup | 2.0 | 91% | 100% | 55 | 47.3% | 52.7% | 61.8% | — |
| `flight_change` warning | 2.0 | 70% | 100% | 51 | 23.5% | 37.3% | 54.9% | — |
| `unassigned` warning | 1.5 | 59% | 100% | 31 | 29.0% | 38.7% | 58.1% | — |
| `overlap` critical — clock_only | 1.2 | 100% | 0% | 33 | 39.4% | 45.5% | 54.5% | **39.4%** |
| `unassigned` critical | 1.0 | 100% | 100% | 17 | 47.1% | 47.1% | 64.7% | — |
| `overrun` critical — recorded_pickup | 0.5 | 93% | 0% | 14 | 50.0% | 57.1% | 78.6% | **50.0%** (n=14) |

**The last column is the one D5 must judge, and it is why the headline numbers flatter the tool.**
A card carrying a single leg has no downstream victim: it says "this leg is overdue" and gets
graded on "was this leg late". That is a description, not a forecast, and it is where the two
best-looking scores come from — `late_cascade` watch is **100%** one-leg cards, `late_cascade`
critical **41%**. Strip those out and the best genuine forecaster on the rail is `late_cascade`
critical at **43%**, then `overlap` at 32% and `flight_change` at 25%. `overrun` critical reaches
50% on fourteen scored cards, which is too few to bank.

**No class passes D5. Not one, on either bound, at >15 or >10 minutes.** On today's evidence the
Phase 2 visibility flip ships nothing at all.

**How far ahead can it see? [measured 2026-09-05, `analysis/out/23_lead_time.csv`]** The founder's
requirement is a tool that warns BEFORE, so the cards were re-cut by the gap between the warning
and the moment it is about — forecast-only cards, live bound:

| Warning fires… | Scored | >15 min | >10 min | Best class in the bucket |
|---|---:|---:|---:|---|
| inside 30 min | 94 | **55.3%** | 59.6% | `late_cascade` **68.2%** (n=22) |
| 30–60 min | 209 | 32.5% | 37.8% | `flight_change` 50% (n=8) |
| 1–2 h | 178 | 27.0% | 30.9% | `late_cascade` 50.0% (n=42) |
| 2–4 h | 88 | 21.6% | 25.0% | `flight_change` 24.4% |
| 4 h + | 177 | 24.9% | 30.5% | `overlap` 29.2% (n=24) |

**Read it carefully, because a 3-day probe of the same cut was misleading and said 4.8% in the
last row.** At 28 days the curve is not a cliff: it steps down once, at the 30-minute mark, and
then flattens at 22–33%. Near-term warnings really are about twice as good as everything else —
but "twice as good" tops out at **55%**, and the only class that approaches D5 does so on 22
cards. **Capping the horizon improves the rail; it does not by itself produce a 70% early-warning
tool.**

**And the warnings are weakest exactly where lateness costs money:**

| Impact trip | Scored | >15 min |
|---|---:|---:|
| return | 361 | **17.5%** |
| arrival | 304 | 47.7% |
| cruise | 60 | 35.0% |

An arrival's booked time is the landing slot, so ordinary deplaning reads as lateness and
inflates that 47.7% (19 hit the same artifact). Strip the flattery and the picture is: the
advisor is **right one time in six** about the fixed-time jobs — returns and departures, guest on
the kerb with a flight to catch — which are precisely the ones §1.2 identifies as where lateness
actually damages service. That is the gap to engineer against, and it is a stronger argument for
Phase 1.1 than anything else in this document: the fixed-time clock rule the live path is missing
is the same shape as the trips it is worst at.

**What the numbers do support.** Three things survive the test and are worth the build:

1. **The cards carry validated fixes.** 88–96% of the two highest-volume classes arrive with a
   move plan already checked against the whole remaining day. That is work a dispatcher does not
   have to do, independent of whether the warning was strictly necessary.
2. **The volume is sane.** 4.2 a glance against **70.9 scanner tasks a day** (§0.2) is an order-of-
   magnitude reduction in things to look at, and §0.2 says two thirds of those tasks buy nothing.
3. **It is fast and deterministic.** 61 ms P50, no budget breaches.

**So the honest framing changes, and §6 already anticipated it: the advisor's value is not early
warning — it is a fast, pre-validated response to something that has already happened.** A card
that says "George is 20 minutes overdue and here is a checked way to cover his 16:00 return" is
worth screen space even when George would have made it anyway; a card that *predicts* George
will be late is right one time in three and must not claim otherwise.

The founder decision Phase 0 hands over is therefore which of these, and it is a decision about
what the rail is *for*, not about a threshold:

- **(a) Cap the horizon and ship the ladder.** Cards may only speak about the next ~60 minutes,
  where precision is roughly double; beyond that the detector stays silent or files to the ops
  queue. Wording states what has happened and offers a checked fix rather than forecasting, and
  D5's 70% bar is retargeted at the *plans* (does the proposed move hold up?) rather than the
  warnings. **Recommended.** Note honestly what this does and does not buy: the rail gets much
  better, and it still is not a 70% predictor.
- **(b) Hold everything until the live log exists.** Ship nothing visible; build `AdvisorEvent`,
  log for a month with GPS history, and re-measure the classes replay cannot score.
- **(c) Re-cut the thresholds and re-measure.** Cheap to try (the script re-runs in 11 minutes)
  but the lead-time and trip-type cuts are the two most promising slices and neither produces a
  70% class, so a further threshold hunt is unlikely to find one.
- **(d) Fix the two things that could actually extend the horizon, then re-measure.** Phase 1.1
  (the clock — the advisor is currently blind to 5 breaks/day and optimistic on the rest, and its
  worst trip type is the one the missing rule governs) and Phase 1.3 (GPS history — 07 scores
  mid-trip GPS at 72% on "late at all", the strongest predictor in this project, and replay cannot
  score it at all because the sweep keeps no history). Both are invisible, both are already
  planned, and until they land every number above is a measurement of a tool doing its arithmetic
  wrong with its best sensor switched off. **Do this alongside (a), not instead of it.**

Caveats stated on the output and unchanged by the result: GPS (`dispatch_*`) is not historized —
`bulk_update` writes it — so `gps_fresh` classes are absent here entirely and can only be scored
live; flight `estimated_*` cannot be dated (`last_updated` is touched by every later refresh, a
median ~10 h after the arrival it describes), hence the two bounds; the truth clock is the
on-location tap, which only 57–67% of legs carry cleanly, and unscorable cards are counted, never
dropped; the roster, ops tasks and KEOI are not rewound.

Design consequences, folded into §3.1 and §4: hygiene cards leave the rail (they are one-leg
descriptions and carry no plan — ops-queue material); one-leg cards never claim a downstream
victim in their wording; a hard visible-card budget (≤5, ranked by severity then time-to-impact)
replaces the 6-plan truncation; **the two-fact rule is dropped as a ship criterion, having been
tested and failed.**

### 3.4 The milestone detector — the founder's spec, 2026-09-05

**This supersedes §3.2's two-fact bet and reframes what the day manager detects.** It arrived
after the replay and answers the exact gap the replay found, so it is recorded here in the
founder's own terms first, then in the engine's.

> "The system cannot judge each trip in isolation. It needs to understand the driver's entire
> upcoming chain. A five-minute delay for a driver who has nothing for three hours means almost
> nothing. A five-minute delay for a driver whose next pickup is in 57 minutes could be the first
> sign of a serious conflict. If the scheduler intentionally created a tight sequence, the Day
> Manager should know: *this next trip only works if the current trip stays approximately on
> schedule* — and watch that specific milestone."

**The idea in one line: stop asking "is this turn tight?" and start asking "what had to be true,
by when, for the next trip to work — and is it still true?"**

**Why this is the right answer to what §3.3 measured.** Every existing detector forecasts
lateness from arithmetic and is right 17.5% of the time about returns. A milestone detector does
not forecast at all: it names a deadline the chain depends on and then observes whether that
deadline passed. "It is 1:03 and the 1:00 pickup still has no tap" is a **fact**, and the claim
attached to it — "the assumption that made the 2:00 assignment possible has failed" — is an
inference about the *plan*, not a prediction about the driver. That is a categorically stronger
class of statement than anything measured in §3.3, and it fires EARLIER, which is what the
horizon cut says is the only region where anything works.

**The engine already computes this — backwards.** `check_feasibility` runs forward: given a
pickup at T, `chain_clear_dt` gives the clear time, `chain_repo_minutes` the reposition,
`feasibility_guards.required_turnaround` the pad, and the sum is compared with the next pickup.
Invert the same arithmetic and it yields, for each leg with a following job:

    latest_safe_clear  = next_pickup − repo − required_turnaround
    latest_safe_pickup = latest_safe_clear − (service + drive + unload for THIS leg)

That is a **backward pass over the existing formulas** — no new model, no new constants, no
network call, and it inherits the fixed-time rule of §3.1 for free. Every leg in a chain gets a
`latest_safe_pickup`; a leg with hours of slack gets one far in the future and is never watched.

**Escalation ladder, priced (the founder's progressive-accuracy rule).** The cost concern is
already settled precedent in this codebase: `2c36aada`'s sibling commit (2026-08-09) removed a
paid Distance Matrix call from `ops.tasks._reposition_minutes` for exactly this reason —
"COST … routing through `chain_repo_minutes` makes the wider scan CHEAPER than the narrow one
was." The ladder keeps that discipline:

| Slack state | What is read | Paid calls |
|---|---|---|
| Comfortable (milestone far off) | booked times + stored category/route table | **none** |
| Tightening | + trip status and the `dispatch_*` GPS the Samsara sweep already stores every 180 s | **none** — already fetched |
| **Milestone missed** | that fact alone triggers the card and the backup search | **none** |
| Conflict realistic, backup being priced | one fresh routing call from the car's actual position | one, per real conflict |

Only the last tier spends money, and only once a genuine conflict exists.

**The card says why, in the founder's words, not the engine's:**

> **2:00 PM pickup at risk.** Driver X is still On Location on his 1:00 PM trip at 1:03. This
> schedule needed that pickup by 1:00 to leave enough drive and unload time for the 2:00 departure.
> **Start identifying backup coverage now.**

**Observability — measured 2026-09-05, and it is good.** The detector depends on the picked-up
tap being real and timely. Over the 28-day window, 2,458 in-house legs: **93.7% carry a picked-up
tap; exactly 4 are bulk-entered** (picked-up and completed within 120 s, 19's rule); **98.1% of
taps land within two hours of the booked pickup**. Drivers tap, and they tap live. (The "57–67%
clean taps" caveat elsewhere in this document concerns the on-location tap used as a *truth
clock*; it does not apply to this signal.)

**The two halves of the problem (founder, 2026-09-05).** A live routing call returns an ETA that
already prices traffic *as it stands now*, so GPS does know a moving driver is stuck — it simply
cannot see traffic that has not happened yet. That splits day-of lateness cleanly:

| | What breaks | What sees it |
|---|---|---|
| **Half one** | He never got started — still on location, the plan is already broken | **the milestone rule** (§3.4) |
| **Half two** | He started fine and is now stuck in traffic | **GPS only** |

26 measures the milestone rule catching ~59% of late trips; the ~41% it misses are, by
construction, mostly half two — the driver left on time and the road did the rest. No plan-derived
rule can ever see those. 07 already scored the signal that can: mid-trip GPS saying the chained
next pickup is blowing is **72% right on "late at all"**, the strongest predictor measured
anywhere in this project — and unprovable further because the sweep keeps no history. **Phase 1.3
is therefore not support work for §3.4; it is the other half of the same detector.**

**The one real risk, and GPS's actual job here.** For the ~6% with no tap, a missed milestone is
ambiguous — he may have picked up and not tapped. That ambiguity is precisely what produced the
hygiene cards §3.3 found scoring 100% on one-leg tautologies. **So GPS is not needed for ETA
prediction; it is needed to disambiguate a missing tap** — has the car left the pickup point or
not. That is a far cheaper question than routing, and `dispatch_is_moving` /
`dispatch_stationary_minutes` already answer it from data the sweep stores anyway.

**Measured 2026-09-05, before a line of product code — `analysis/26_milestone_detector.py`,
28 days, real boards, `out/26_milestone_sweep.csv`.** The rule was implemented exactly as
specified above (shipped math read backwards, picked-up tap as the observation, next leg's
`pickup_deadline` as the outcome) and swept over how early it starts watching and how long it
waits before speaking. Pairs whose driver changed after the milestone are excluded, so no credit
is taken for trouble a dispatcher had already fixed.

Population: **72.3 chained pairs/day**, of which **11.2/day end with the next trip running >15 min
late** — that is the entire population any warning system has to catch. 6.0% of first legs carry
no pickup tap (the GPS-disambiguation case).

| Start watching | Grace | Fires/day | Precision | **Recall** | Warning P50 | Warning P25 |
|---:|---:|---:|---:|---:|---:|---:|
| at the milestone | 0 | 21.1 | 26.7% | **50.2%** | 87 min | 60 min |
| at the milestone | +5 | 18.3 | 28.7% | 46.7% | 83 min | 55 min |
| at the milestone | +10 | 15.4 | 30.2% | 41.3% | 80 min | 65 min |
| 10 min early | 0 | 27.1 | 24.5% | **59.0%** | 97 min | 70 min |
| 10 min early | +5 | 23.9 | 25.7% | 54.6% | 92 min | 65 min |

**Against the founder's own bar (2026-09-05: "45 minutes to an hour is good, 30 we can work with
but it's risky") — recall is reported AT the threshold, because a warning with less notice than
that is a countdown, not a warning:**

| Setting | Fires/day | Precision | Recall | …with ≥60 min | …with ≥45 min | …with ≥30 min |
|---|---:|---:|---:|---:|---:|---:|
| watch from the milestone, speak at once | 21.1 | 26.7% | 50.2% | 34.0% | 37.5% | 38.1% |
| watch from the milestone, +5 grace | 18.3 | 28.7% | 46.7% | 27.3% | 33.3% | 35.6% |
| **watch 10 min early, speak at once** | **27.1** | **24.5%** | **59.0%** | **39.4%** | **41.6%** | **58.4%** |

**The shape of that last row is the finding: what it catches, it catches in time.** Recall is
59.0% and recall-with-30-minutes-notice is 58.4% — **99% of everything it catches arrives with at
least half an hour to act, and 70% with the founder's preferred 45+.** Of the whole fire stream,
85.8% give ≥45 min and 71.5% give ≥60. Distribution of notice across all 513 fires: P10 15 min,
P25 55, P50 83, P75 100, P90 125.

In daily terms: of ~11.2 trips a day that end up >15 min late, this flags **~6.6**, of which
**~4.6 come with 45 minutes or more**, at a cost of ~27 flags a day. Against the status quo of
70.9 scanner tasks a day with the same ~24% hit rate, no measured recall, and nothing useful
outside the last half hour, that is a straight improvement on every axis except volume-per-catch.

**The warning time is the win, and it is decisive.** Typical notice is **80–97 minutes**, and even
the impatient quartile gets **55–70**. Every detector in §3.3 was useful only inside 30 minutes;
this one speaks well over an hour out, which is the founder's actual requirement — enough time to
find a backup rather than enough time to watch it happen.

**Recall is now measured for the first time in this project: ~47–59%.** The rule sees about half
of everything that goes wrong. That is a real answer to "does it watch my day", and it is honest:
half the late trips arrive with no missed milestone in front of them, because nothing in the plan
predicted them.

**Precision is the problem, and it is the same problem as everywhere else — by trip type
(grace +5):**

| Job at risk | Fires | Right | Precision | Recall | Warning P50 |
|---|---:|---:|---:|---:|---:|
| **return** | 227 | 37 | **16.3%** | 54.4% | 80 min |
| arrival | 237 | 95 | 40.1% | 43.8% | 90 min |
| cruise | 33 | 12 | 36.4% | 50.0% | 75 min |

On returns — the trips §1.2 says lateness actually costs — it fires eight times a day and is right
one time in six.

**An earlier draft of this section claimed the clock fix would cure that. It was asserted, then
tested, and it is wrong.** `26 --take-later` derives the milestone from the corrected clock —
exactly what Phase 1.1 makes the live path do — and the result moves the opposite way on
precision:

| Milestone derived from | Fires/day | Precision | Recall | Warning P50 | Recall ≥45 min |
|---|---:|---:|---:|---:|---:|
| the clock as shipped | 27.1 | 24.5% | 59.0% | 97 min | 41.6% |
| **the corrected clock (Phase 1.1)** | **29.5** | **23.6%** | **61.9%** | **105 min** | **44.8%** |

The corrected clock makes the previous job clear later, so the deadline lands earlier, so the rule
fires **more**: +2.4 flags/day and one point *worse* on precision. What it buys instead is **+3
points of recall and 8 more minutes of notice** — it sees more of the trouble, and sooner. That is
a good trade against the founder's bar (recall-with-45-minutes rises 41.6% → 44.8%), but it is not
a false-alarm cure and must not be sold as one.

**So the returns false-alarm rate is not yet explained.** The remaining candidates are the safety
pad inside `required_turnaround`, the modelled trip duration, or drivers genuinely making up time
on the road — and nothing here isolates which. That is a separate measurement (compare the
milestone against what the leg's *actual* recorded duration would have made it), not an assumption
to build on. Phase 1.1 is still worth doing for its own measured reasons (§0.1, 24: 5.1 turns/day
the board calls fine and the engine calls impossible) and it improves this detector's reach — but
it does not fix its precision.

**Verdict.** Ship-worthy as a *watch list*, not yet as an alarm: ~18 fires/day (against 70.9
scanner tasks, §0.2), half the trouble caught, an hour-plus of notice, and one in three right
overall. Re-measure after Phase 1.1 and after GPS history exists, and re-tune the grace against
whatever warning time the founder says he actually needs.

**What was measured, and why these four** (the house rule, and §3.3 is why it exists):

1. **Precision** — when a milestone is missed, does the next trip actually run late?
2. **Recall — never measured for anything in this project.** Of the trips that DID run late, how
   many had a missed milestone first? A warning system that is 90% precise and catches a third of
   the trouble is not what the founder asked for. This is the number that decides the design.
3. **Warning time** — minutes between the missed milestone and the next pickup. The founder's
   requirement is "enough time to find a backup", so the distribution of this, not its mean, is
   the acceptance test.
4. **Volume** — missed milestones per day, against the ≤5-a-glance budget and the 70.9 tasks/day
   the scanner files (§0.2).

Only classes clearing D5 on (1) with a usable (3) ship. If recall is low, the honest report is
that the milestone catches a *subset* of trouble well — which is still worth shipping, but must
be described that way rather than as "the system watches your day".

**Live instrument — `AdvisorEvent`. SHIPPED 2026-09-05 (Phase 1.2 below).** One row per card
lifecycle: shown, plan applied / refused / snoozed / task filed, and the realised outcome (filled
by a small job on the existing GHL loop once the service date has closed: impact leg's on-location
lateness). This makes the precision number stay true in production and is the D14 trust ledger.
Before this, snooze was cache-only and nothing was persisted at all.

**Three of that paragraph's four assumptions were measured before the table was designed, and two
were wrong** — `analysis/27_advisor_event_gate.py`, the same 28 days on a 3-minute grid
(`out/27_identity_stability.csv`, `27_cadence_coverage.csv`, `27_card_episodes.csv`). Details in
Phase 1.2; the short version is that "one row per card lifecycle" is not what the engine's card id
gives you (5.5% of ids carry more than one episode, `flight_change` 14.1%), and a card does not
stay the same card while it lives — under a stable id the **impact leg itself moves on 11.9% of
episodes**, which is the leg the outcome grades.

**Ship rule (D5, per class):** replay precision ≥70% on 28 days **and** ≥70% over the first two
live weeks from `AdvisorEvent`; otherwise the class renders as a passive row with no plans, or
not at all.

---

## 4. Repair design

### 4.1 The ladder (exists; ordering is the founder's SOP)

For each card, in this order, stopping at the first tier that yields a validated plan — farm
tiers run **only if no in-house plan survives**:

0. **Monitor** — warning-band cards lead with "no move warranted"; GPS on-time suppresses clock
   alarms.
1. **Match the flight** — retime to the controlling flight's best arrival (later only); combined
   with a cover move when the retime alone cannot re-seat the broken pickup.
2. **In-house**: (a) **reassign** to the top 3 receivers by resulting buffer; (b) **swap chain**
   via `find_swaps` (depth ≤3, ≤1.2 s, explicit budgets so `SchedulerSettings` cannot silently
   widen them) only when no clean direct taker exists; (c) **takeback** from an affiliate
   (call-first risk line).
3. **Call someone in** — *new tier, between 2 and 4*: a named bench driver from
   `standby_mints.standby_pool_ids` (active, zero legs, zero DVA, no approved time-off, 510-min
   rest both sides) on a **named free car** (no DVA holder, not out of service), evaluated by the
   same `check_feasibility` + share gate, priced with the premium it saves. Propose-only,
   dispatcher calls; nothing contacts the driver (D16 pattern).
4. **Farm** — direct farm of the first target that clears the hard gates (VIP, true departure,
   pending refund, far/unknown endpoint), priced by the waterfall ledger seeded with the day's
   committed farm-outs; or **evict-and-farm** (farm the receiver's cheapest arrival so he takes
   the broken job — arrivals are farm currency).

Ranking: farm never outranks in-house; then tier; then score = 1000 − depth·moves + buffer −
120·new-tight − farm base − 60·risk flags − 40·retimes.

### 4.2 Blast radius — how one fix cannot cascade

Already enforced, to keep: ≤2 target legs per card; swap depth ≤3; `validate_post_move_board`
runs on the **whole remaining day** on the planning clock and rejects any *new* negative turn,
any car-share conflict, any band worsening to critical (pre-existing problems elsewhere never
veto); frozen legs never move; affiliate-held legs never enter in-house tiers; one card per cause
(`claimed_prev_ids`); per-card time budget; apply re-validates against the current board inside a
row-locked transaction and 409s on staleness; snapshot Undo for ≥2 moves.

To add:

- **Touch budget in the title** — cap total legs touched per plan at 3 and say so.
- **No second-order cascade** — a plan may not move a leg that is the anchor or impact leg of
  another live card; if it must, the card is grouped into one episode with one plan (the original
  plan's "recovery episode" idea, dropped in the build).
- **Fixed-time protection weight** — a plan that leaves a return/departure/cruise with <15 min
  after the move is demoted below one that leaves an arrival tight (19: fixed-time turns are
  where lateness costs; arrivals have deplaning grace).
- **Held dates** — keep the advisor's existing rule: evaluate the live board, offer staging only
  to sandbox-granted users, never write live without the explicit override confirm; the tripwire
  stays strict in tests.

### 4.3 Explanation

Keep the two-layer contract that already exists: plain-language `display` (headline, story,
timeline percentages) over the verbatim engine `why`/`risks` under "Show the math". Every plan
names its basis, the resulting slack at the tightest turn after the move, the dollars if it
farms, and the "kept in-house because…" phrases for gated alternatives.

---

## 5. Phased build plan

House pattern throughout: the gate script is written and the baseline captured **before** the
code it judges; every dispatcher-visible change ships with a release note; invisible work carries
`Release-Note: none`.

### Phase 0 — instruments first (no product code)

| Deliverable | Gate |
|---|---|
| ~~Reconcile the two snapshots~~ **DONE 2026-09-05** — desktop copy in place, horizon 2026-08-21, provenance recorded in the header | ✔ |
| ~~`analysis/25_scanner_outcomes.py`~~ **DONE 2026-09-05** — volume, timing and closure taxonomy by month; three CSVs committed | ✔ Baseline: **70.9 tasks/day, 33.9% of legs, 65.9% of closes buy nothing**, rising month over month (§0.2) |
| **Re-run `25` on a pull taken after 2026-08-27** — the one measurement Phase 0 cannot make on any copy on disk; four of the five tuning commits land after this snapshot ends | If the scanner is genuinely quiet now, Phase 1.4 and Phase 2.2 shrink to "keep it that way" and the §1.3 rank-2 failure mode is downgraded. Does not block 23 or 24 |
| ~~`analysis/23_advisor_replay.py`~~ **DONE 2026-09-05** — 28 days, 15-min ticks, both estimate bounds, 3,864 computes; three CSVs committed | ✔ **4.2 cards/glance** (not 16); **no class passes D5** — best forecaster 43.2%, `overlap` 32.4%, `flight_change` 24.9%; compute P50 61 ms, zero budget breaches (§3.3) |
| ~~`analysis/24_live_clock_split.py`~~ **DONE 2026-09-05** — 28 real boards, every turn priced twice; two CSVs committed | ✔ **5.1/day** the board calls clean or tight and the corrected clock calls negative; 9.3 band flips/day; all 142 named with driver and times |
| **Founder decisions still open** — (1) §3.3's (a)/(b)/(c): what the rail is *for*, now that no class forecasts well enough to warn; (2) whether the scanner is retired or re-pointed, pending the post-08-27 pull; (3) call-in tier in v1? | Phase 1 can start on 1.1–1.3 regardless; only 1.4 and Phase 2 wait on these |

### Phase 1 — invisible fixes (`Release-Note: none`)

1. ~~**`take_later` on the live path**~~ **SHIPPED 2026-09-05.** `turn_slack_minutes` takes the
   later of (static clear, board estimate) when the next pickup is fixed-time — the same branch
   `check_feasibility` takes at `scheduler.py:1317` — on the planning branch only; the
   recorded-pickup re-anchor is a fact and is never raised to a model estimate. One formula, so
   the chips, `assign_warnings`, the advisor's math, `board_turn_bands` and
   `validate_post_move_board` all inherit it. Four regression tests in
   `tests_board_validation.TakesLaterParityTests`, including the sereen shape as a named case.
   **Dispatcher-visible, so it ships with a release note** (`2026-09-05-board-chips-tell-the-truth.md`)
   — the plan's assumption that Phase 1 was entirely invisible was wrong for this item.

   **Gate results, and one of them did not pass as written:**

   | Gate | Result |
   |---|---|
   | 24 re-run | ✔ Live formula now carries the rule; the 5.1/day it used to call clean are now banded red — same 142 cases, now visible |
   | `dispatching.tests_board_validation` | ✔ 22/22 |
   | 12 re-run ≥70% | **`turn_critical` 90.3% ✔, ALL 78.9% ✔, `turn_tight` 68.9% ✘** |

   **On the `turn_tight` miss.** Before/after, measured by reverting the change and re-running:

   | Class | Fired before → after | Real before → after | Precision |
   |---|---|---|---|
   | `turn_critical` | 156 → **298** | 146 → **269** | 93.6% → 90.3% |
   | `turn_tight` | 355 → 341 | 281 → 235 | 79.2% → **68.9%** |
   | ALL | 511 → 639 | **427 → 504** | 83.6% → 78.9% |

   The fix **catches 77 more genuine conflicts** and nearly doubles the critical band while losing
   3 points of precision on it. `turn_tight` falls because the real conflicts *migrated out of it*
   into critical, leaving the marginal ones behind — a compositional effect, not a degradation.
   Note also what 12's truth actually is: 09's flat occupancy model (`m09.OCC`), a **definition**
   computed from the same optimistic clock this change corrects, so it cannot arbitrate this
   particular change (§3.2 makes the same point about the 92.5% that let Build 1 ship). The
   evidence that the fix is right is 24's 142 named cases, not 12.

   **Open decision:** 12's own output says `turn_tight` should "demote to info" at 68.9%. That is a
   dispatcher-visible rendering change and is **not** included here — flagged, not taken.
2. ~~**`AdvisorEvent` model + nightly outcome fill**~~ **SHIPPED 2026-09-05.**
   `dispatching.models.AdvisorEvent` + `dispatching/advisor_events.py`; recorded from the rail's
   state endpoint, the ops task-detail card, and the apply / snooze / file-task writes; graded on
   the existing GHL loop once a service date has closed. No new daemon. Invisible, so
   `Release-Note: none`. Gate script `analysis/27_advisor_event_gate.py`, **written and baselined
   before the model existed** — the stated gate ("replay of one live week reproduces the card list
   the log holds") cannot run until a live week exists, so it was replaced by the two things that
   can be checked now: does the log's *shape* fit what cards actually do, and does its *grading*
   reproduce 23's.

   **The measurements changed three design decisions.** 28 days, 3-minute grid, 06:00–23:00,
   9,548 computes:

   | What was assumed | What was measured | What changed |
   |---|---|---|
   | "one row per card lifecycle" | 1,558 unique (date, id) pairs → **1,656 episodes** (59.1/day). **5.5% of ids** come back after leaving the rail — `flight_change` **14.1%** | The row is keyed `(service_date, card_id, episode)`, not `(date, id)`. Keying on the id alone would have merged 6% of lifecycles |
   | a card is one thing while it lives | Under a **stable id**: severity changes on **8.1%** of episodes, basis on 4.1%, and **the impact leg — `leg_ids[-1]`, the trip the outcome grades — on 11.9%** (`late_cascade` 23.6%) | The row keeps **both ends** (`severity`/`severity_last`, `impact_leg_first_id`/`impact_leg_id`) and grades the last claim. A single value would have reported whichever end the log happened to catch |
   | the log can ride the 30-minute GHL loop | Share of episodes a sampler sees **at all**, averaged over every phase offset: **3 min 100%**, 6 min 95.3%, 15 min 86.5%, **30 min 76.1%**, 60 min 60.8% | The unattended sweep rides the **180 s Samsara tick** (the `fleet_sync` precedent, same loop, same lock); only the nightly grading stayed on the GHL loop as planned |

   **On that last row, the reason is not the precision bias** — that stays under a point until an
   hour (30 min: −0.9). It is *which* cards a coarse sampler loses, and **D5 is applied class by
   class, so the pooled 76.1% is the wrong number to choose a cadence by**
   (`out/27_cadence_by_kind.csv`):

   | Seen at all, by class | 3 min | 15 min | 30 min | 60 min |
   |---|---:|---:|---:|---:|
   | `overrun` | 100% | 69.4% | **47.1%** | 24.7% |
   | `unassigned` | 100% | 66.0% | **50.4%** | 32.0% |
   | `late_cascade` | 100% | 80.1% | **63.6%** | 41.4% |
   | `flight_change` | 100% | 92.7% | 89.1% | 83.5% |
   | `overlap` | 100% | 96.0% | 89.9% | 75.8% |

   **44.7% of episodes live under 30 minutes** and they are not spread evenly — `overrun` 93.1% of
   its episodes under 30 min, `unassigned` 75.7%, `late_cascade` 68.5%, against `overlap`'s 25.3%.
   On the GHL loop `overrun` would be graded on fewer than half its cards while `overlap` kept nine
   in ten: a **40-point spread inside a bar applied class by class**. Cost of the finer cadence,
   measured on the same run: **P50 76 ms, max 925 ms, zero of 9,548 computes over the 4 s budget**
   — about 26 seconds of CPU across a day.

   **And one assumption the plan did not state, which is the reason the sweep exists at all.**
   Phase 1.2 as written feeds the log from the rail — and the rail is superuser-only
   (`advisor_views.advisor_visible_to`), **two active accounts** [measured: 3 superusers, one
   deactivated], on a panel that keeps polling while collapsed and stops while the tab is hidden.
   There is no record anywhere of how often it is actually open: `StaffActivityMiddleware` skips
   JSON responses, so **zero rows** in the entire snapshot record an advisor poll (the best proxy
   is 548 dashboard page-views by superusers over the 17 days since the rail shipped). A log fed
   only from there is an attendance record, and it cannot support either thing the log is *for*:
   §3.3(b) is explicitly "ship nothing visible, build `AdvisorEvent`, log for a month", and Phase
   2's gate is two live weeks of per-class precision **before** the rail opens. Hence the sweep,
   behind one constant (`advisor_events.ADVISOR_EVENT_SWEEP`) so it can be switched off without
   changing a pixel.

   **Gate results:**

   | Gate | Result |
   |---|---|
   | Grading reproduces 23's `build_truth` (`27 --verify-fill`) | ✔ **3,108 legs over 28 dates, 0 disagreements.** Last on-location tap, `pickup_deadline`, 19's batch rule, one decimal, strict `>15` — and no status filter, because build_truth scores cancelled legs too |
   | The fill's ceiling — how many rows can ever be resolved | **85.5%** of episodes have an impact leg with a usable tap. The other 14.5% can never be graded by any nightly job, and are recorded as such rather than dropped from the denominator |
   | `dispatching.tests_advisor_events` | ✔ 48/48 |
   | `dispatching` + `ops` suites | ✔ 2,485 tests, the same 7 pre-existing failures as a stashed baseline (samsara telemetry ×4, fleet ×2, staffing board) |

   **What this does not tell you.** GPS (`dispatch_*`) is blanked at every replayed tick because
   the sweep keeps no history, so every number above is measured with the advisor's best sensor
   switched off — `gps_fresh` classes have no measured episode shape at all. The live log is the
   first thing that will see them, which is the same argument Phase 1.3 rests on.

   **Six defects an adversarial pass then found, all of which made a number quietly wrong rather
   than raising anything** — the failure mode this instrument exists to avoid, so they are recorded
   rather than just fixed:

   | Defect | Why it mattered |
   |---|---|
   | The episode gap was set to **45 min on a guess** that real boundaries were wider | They are not. Measured from the gate's own output: 98 boundaries, min 6 min, **P50 28.5**, P75 72. At 45, **67% of them merged** — and unevenly, 60 of the 98 being `flight_change`. Now 10 min (23% merge, against a floor of 17% at 6) |
   | The outcome fill graded an impact leg **by id, never checking it was still on the card's date** | A guest confirming which night an overnight arrival takes off *moves the leg a day* (`overnight_arrival`), and the advisor raises cards on exactly that population — 114 such +1-day moves in the snapshot. The row would file a real signed lateness for a trip that never ran on that date, on the false-positive side of a class D5 gates. Now `unknown`, which is what 23 returns |
   | `OUTCOME_RETRY_DAYS = 7` **never bound** | The eight-attempt cap was being spent at the GHL loop's 30-minute cadence — all eight gone four hours after the day closed. A tap entered the next morning could never flip the row from unscorable to scored. Attempts are now spaced 20 h apart |
   | The **ops task page wrote whole-board columns from a leg-filtered compute** | `for_leg_id` narrows the card set *before* the six-card cap and the 4 s budget, so one surviving card always gets full plan generation — `had_plans` would have reported plan coverage the replay never measured. Leg-filtered sightings no longer write those two columns |
   | The concurrent-insert recovery **could not run** | Its read sat inside the atomic block the failed INSERT had just marked for rollback, so it raised instead of recovering and the applied/snoozed stamp was lost in exactly the race it existed to survive. Its own savepoint now |
   | This document said a 30-minute log would cost "a quarter fewer cards" **per class** | That was the pooled figure applied to a per-class claim, and the gate had not computed coverage by class at all. It does now, and the real per-class gap is roughly twice as large — the table above |
3. ~~**GPS sweep history**~~ **SHIPPED 2026-09-06.** `dispatching.models.DispatchEtaSample` +
   `dispatching/eta_samples.py`; `sweep_eta` builds a row per evaluated leg and bulk-inserts on
   the same 180 s tick, before `_apply_eta_fields` overwrites the leg. Invisible,
   `Release-Note: none`. Gate script `analysis/28_eta_history_gate.py`, written before the model.

   **The stated gate passes exactly:** 07's ETA-error table, rebuilt from sample-shaped rows by
   the shipped reader (`eta_samples.prediction_errors`) — **6,575 committed rows, 6,575 rebuilt,
   0 disagreements, 0 rows invented**. It matters that production code does the rebuilding: a
   live figure computed a different way could not be set against §3.4's 72%.

   **What the ticket did not say, and what it costs.** "A row per evaluated leg per tick" is a
   volume decision nobody had priced. Simulating the sweep's own target selection over the same
   28 days at its real cadence (`out/28_write_rules.csv`) — target selection depends only on leg
   status, pickup time and deadline, so the row *stream* is exactly replayable even though the
   ETA values are not:

   | Write rule | Rows/day | MiB/yr | Scorable kept | Ambiguous legs |
   |---|---:|---:|---:|---:|
   | everything (the ticket, read literally) | 6,868 | 557 | 100.0% | 21/25 · 84.0% |
   | under way only | 2,509 | 204 | 95.9% | **8/25 · 32.0%** |
   | within 60 min only | 1,650 | 134 | 38.6% | 20/25 · 80.0% |
   | **under way OR within 60 min** | **3,429** | **278** | **97.0%** | **21/25 · 84.0%** |

   "Scorable" is 07's own window — a sample between the two taps it would be graded against. Only
   **1,762 of the 6,868 daily rows are scorable at all**; the rest are a parked car hours from its
   next job, a row no analysis in this project can grade.

   **The last column is why the cheapest rule lost, and it is the measurement that mattered.**
   "Under way" halves the volume and keeps 96% of what 07 can score — then loses two thirds of the
   case §3.4 actually wants GPS for. An ambiguous leg is one whose milestone passed with no pickup
   tap, where GPS's job is to say whether the car ever left; **a leg with no tap is, by
   construction, not "under way" by status.** The union costs 900 more rows a day and loses
   neither. Stated honestly: n=25 over 28 days is thin, and even keeping every tick covers only 21
   of the 25 **inside the hour around the milestone**. The first version of this paragraph
   explained the other four as unmapped cars. **That was wrong, and an adversarial pass caught
   it**: all 25 drivers are in-house with a Samsara-mapped car, and all 25 legs *are* the sweep's
   target — for 19 to 191 ticks each. The four were sampled earlier in the day. Each carries a
   pickup deadline 83–171 minutes before the milestone and a `completed` tap before the coverage
   window opens, so by then the leg is closed and past the 45-minute grace and the sweep has moved
   on to the driver's next leg. It is a property of where the window is placed, not of fleet
   telemetry — widening it to ±2 h recovers 24 of 25, and the whole day recovers all 25. The
   mechanism behind the 32%-against-84% direction is structural rather than statistical and is
   unaffected.

   **Growth, so nobody is surprised.** ~1.25 M rows and ~278 MiB a year at today's fleet, which
   makes this the largest table in the database inside a year. Volume tracks **drivers, not trips**
   (~1.2 rows per driver-tick), so it grows with headcount. `eta_samples.RETENTION_DAYS` is the
   dial and ships at **0 — keep everything** — because deleting samples deletes the evidence behind
   a published number.

   **Three values the sweep computes and has always destroyed are now kept:** `minutes_to_target`
   and `slack_minutes` (they survive today only as English inside `dispatch_risk_reason`, where
   nothing can score them — and `slack` is the quantity §3.4's 72% is actually about), and
   `eta_carried` — this tick's ETA is the same number from the same anchor as the last, so it
   carries no new information about the road. That last one is deliberately *not* called "reused":
   whether a paid Google call happened is not knowable from the data, and for scoring it does not
   matter. It matters at all because 07's error formula treats the evaluation stamp as the instant
   the drive time was measured, and on a carried tick it is not.

   **Three defects worth recording, because every one of them would have been silent.**

   | Defect | Why it mattered |
   |---|---|
   | `Leg.dispatch_eta_origin_lat/lng` are `DecimalField`s, so last tick's anchor comes back as a `Decimal` against the sweep's `float`, and `28.42 == Decimal("28.42")` is False | The first cut compared them raw, which would have made `eta_carried` **permanently False** — a column that always says the same thing and tells you nothing. Caught by a test while writing it |
   | `eta_carried` compared the target's KIND, not the destination the drive time was priced to | `pickup` and `next_pickup` are the *same destination* — the same leg's pickup location — so `_can_reuse_eta` carries the stored minutes across that flip at **every ordinary trip handoff** while the kind changes. That row is the one where the evaluation stamp is furthest from when the drive time was measured, and the value carried across is a *chained* estimate relabelled as a direct one. It now compares `dispatch_eta_origin_target`, the string the reuse rule itself keys on, and the sample stores it so the flag is checkable rather than trusted |
   | This document, the module docstring and the commit message all said four ambiguous legs "are never the sweep's target at all, because the car is unmapped" | Not true, as the paragraph above now records. Published as a limitation of the fleet's telemetry when it was an artifact of where the analysis window sits — a reader would have gone hunting for unmapped cars that do not exist |
4. **One clock for the scanner** — `classify_turn` / `detect_driver_conflicts` call
   `turn_slack_minutes` + `_turn_severity`; the 10-min red line becomes `pickup_policy.turn_band`.
   Gate: 25 re-run shows the same or fewer tasks/day with a higher move-close share; no
   `estimate_job_end_time` left in any feasibility/verdict path (grep). **Scope depends on what
   Phase 0 finds the scanner is actually filing today (§0.2).**
5. **Runtime check** — 23's timing CSV at today's volume; if P90 > 4 s on a 186-leg day, move
   compute to the Samsara tick behind the existing cache key.

### Phase 2 — visible (release note)

1. Open the rail to `is_staff` (`advisor_views.advisor_visible_to`, the one-line gate) **for
   passing classes only**; failing classes render as detected-only rows or not at all.
2. Tasks and system KEOI flags are filed **from cards** (guard 9) and reconciled from cards, so
   the floor sees one verdict per turn.
3. A "not a problem" action on a card that logs the outcome (the KEOI "this is NOT a conflict"
   annotation becomes data).
4. Browser-tested on a real date by a dispatcher before it is called done.

Gate (two live weeks, from `AdvisorEvent`): per-class precision ≥70%; visible cards per glance
≤5 and open conflict tasks/day below the Phase-0 baseline; same-day transitions/day ≤ 53 and legs
touched/day ≤ 26 with fixed-time >15-min lateness not worse than 19's 8% / 19%; dispatcher
feedback recorded.

### Phase 3 — the call-in tier and D14 readiness

1. Tier 3 (bench driver on a free car) via `standby_pool_ids` + `check_feasibility` + share gate;
   propose-only.
2. Fixed-time protection weight and the no-second-order-cascade rule.
3. D14 prerequisites **listed, not built**: reconcile `views.auto_assign_drivers`'s inline write
   onto `set_leg_driver`; `apply_day_setup` DVA delete. No auto-apply in this project (D11 remains
   the founder's call).

Acceptance instrument for the whole project: the same CSVs from Phase 0, re-run monthly.

---

## 6. What NOT to build, and what to cut from the brief

- **Do not flip `advisor_visible_to` to `is_staff` while the cards still read as predictions.**
  The 28-day replay retires the volume objection — 4.2 cards a glance is fine — and replaces it
  with a sharper one: **no class forecasts lateness better than 43%**, so any card whose wording
  implies "this trip will be late" is wrong more often than right. The flip is safe only
  alongside the §3.3(a) reframing, and it stays the last step of Phase 2.
- **No second engine and no whole-day re-plan.** The pipeline is minutes, non-deterministic under
  budget, and optimises the wrong thing day-of.
- **No "smallest safe set of changes" optimiser.** Real day-of repairs are one move or a
  reciprocal swap (508 swap pairs / 4,448 driver→driver moves in the regime, 16× chance); a
  depth-3 chain with whole-day validation is the right ceiling. An optimiser here is precision
  theatre.
- **No new daemon, no websockets, no push, no driver contact.**
- **No alerts anchored on GPS `at_risk` alone** (4% at >15 min) and no "early" prediction from the
  plan alone (32–47%). Early is where precision dies; the honest value is a *fast, validated*
  response to a live fact.
- **No coverage-recapture tier day-of.** Churn is net-negative in every band; the takeback tier
  stays call-first and rare.
- **Cut from the framing:** "148 hand moves/day" as this tool's target — the day-of number is ~53
  on 26 legs; the other ~100 belong to the builder. "Flight retiming" as hand work — it is 36 bulk
  clicks. "Design for auto-apply later" as new architecture — the advisor's apply path is already
  the front door; the missing piece is a measured trust ledger, which is Phase 1.
- **Is this the right next investment?** Partly. The cheap half (Phase 0–2: measure, fix the
  clock, collapse the detectors, promote) is high-certainty and removes real daily work. The
  expensive half (a new live optimiser) is not justified by the data. The larger dollar line
  remains the builder's `ENGINE_FILLED_DIFF` loss (~5 trips/day, ~$9.9k/28 d, 18_cause_ranking)
  against the day-of leak (~2.8 legs/day, ~$5.6k/28 d). Do Phase 0–2 here, then return to the
  builder.

---

## Technical appendix

### Reuse (do not rewrite)

- `dispatching/conflict_advisor.py` — `build_board_state`, `detect_disruptions`,
  `advisor_clear_dt` (two-clock), `planning_clock_schedules`, `_downstream_breaks`,
  `_reach_dt`/`_unreachable` (guard 6b), `generate_plans`, `_finish_plan`, `_score_plan`,
  `compute_board_fingerprint`.
- `dispatching/conflict_advisor_actions.py` — `apply_advisor_plan` (locks → staleness → hard
  rules → whole-board revalidation → snapshot → front-door writes).
- `dispatching/advisor_views.py` — `advisor_visible_to` (rollout gate, `:68`), state/apply/snooze/
  file-task endpoints; `dispatching/templates/dispatching/includes/_recovery_advisor.html`
  (60 s poll).
- `dispatching/board_validation.py` — `turn_slack_minutes` (`:52`, the one slack formula; gains
  `take_later`), `board_turn_bands`, `validate_post_move_board`, `revalidate_moves_against_db`.
- `dispatching/scheduler.py` — `check_feasibility` (`:1127`), `_slot_chain_end` (`:1106`,
  `take_later`), `chain_clear_dt` (`:999`) / `chain_clear_dt_from_actual` (`:1029`),
  `CHAIN_CLEAR_TAKES_LATER` (`:236`).
- `dispatching/pickup_policy.py` — `pickup_deadline` (gate+10 in-terminal), `pickup_expected_dt`,
  `turn_band`, `pickup_risk`.
- `dispatching/swap_optimizer.py` — `find_swaps` (explicit budgets);
  `dispatching/farmout_optimizer.py` — `WaterfallLedger`, `cheapest_affiliate_for_leg`;
  `dispatching/standby_mints.py` — `standby_pool_ids` (`:104`), `FARMOUT_PREMIUM_PER_LEG` (`:38`).
- `dispatching/assignment.py` — `set_leg_driver`, `_active_draft_for_date`, `can_use_sandbox`;
  `dispatching/pickup_moves.py` — `apply_pickup_time_move`.
- Background: `reservations/utils._run_in_background`; loops `dispatching/samsara_scheduler.py`
  (180 s, lock `737_202`, `sweep_eta` at `:234`), `ghl_integration/scheduler.py` (1800 s,
  `737_201`, runs `generate_ops_tasks` at `:183`).
- Replay technique: `analysis/17_build3_gate.django_on_copy` (`:153`, throwaway migrated copy),
  `analysis/19_clock_calibration.on_location_minutes` (batch-tap rule), `analysis/_common.py`.

### Files to touch, by phase

- Phase 0: `docs/scheduling-redesign/analysis/25_scanner_outcomes.py`, `23_advisor_replay.py`,
  `24_live_clock_split.py` + `analysis/out/` CSVs; this document, updated with the decisions.
- Phase 1: `dispatching/board_validation.py` (take_later ✔), `dispatching/models.py` + migration
  (`AdvisorEvent` ✔ `0019`, `DispatchEtaSample`), `dispatching/advisor_events.py` (✔ the ledger,
  the outcome twin, the sweep), `dispatching/samsara_scheduler.py` (✔ ledger sweep; history insert
  still to come), `dispatching/advisor_views.py` + `conflict_advisor_actions.py` + `ops/views.py`
  (✔ the four record points), `ops/tasks.py` (`classify_turn`, `detect_driver_conflicts` → shared
  formula), `ghl_integration/scheduler.py` (✔ outcome fill hook), tests.
- Phase 2: `dispatching/advisor_views.py` (gate + outcome endpoint), `_recovery_advisor.html`
  (class filter, "not a problem"), `ops/tasks.py` (file-from-cards), release note.
- Phase 3: `dispatching/conflict_advisor.py` (`_callin_plans`, cascade rule, fixed-time weight),
  tests.

### Verification

- Phase 0 scripts run cold from the repo root against `content/db.sqlite3` **on the machine whose
  copy Phase 0 selected**, and reproduce this document's tables within tolerance, or state plainly
  where that snapshot cannot support them (§0.2); outputs committed under `analysis/out/` with the
  copy's provenance in the header.
- Every phase: full `dispatching` + `ops` suites green (`ENABLE_DEBUG_TOOLBAR=0`; 4 known env
  errors baseline), `analysis/12` and `14` byte-identical before/after any refactor they cover,
  the prime-directive test (a founder-built +0/+3 day yields zero cards) still passing.
- Phase 2: browser test on a real date by a dispatcher; two-week `AdvisorEvent` readout before any
  class is promoted; release note pasted to the group chat.
