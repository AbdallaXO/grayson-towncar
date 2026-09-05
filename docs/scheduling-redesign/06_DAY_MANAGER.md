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
| Analysis script numbering 23–25 is free | `docs/scheduling-redesign/analysis/` runs 00–20, 22 (21 is unused) |

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

- **(a) Ship the ladder, drop the prophecy.** Cards render as "here is what is happening and a
  checked fix", with no implied prediction, ranked by severity and time-to-impact, capped at 5.
  D5's 70% bar is retargeted at the *plans* (does the proposed move hold up?), not the warnings.
  **Recommended** — it is the only reading the measurement supports.
- **(b) Hold everything until the live log exists.** Ship nothing visible; build `AdvisorEvent`,
  log for a month with GPS history, and re-measure the classes replay cannot score.
- **(c) Re-cut the thresholds and re-measure.** Cheap to try (the script re-runs in 11 minutes)
  but nothing in the distribution suggests a threshold exists that lifts a class over 70%.

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

**Live instrument — `AdvisorEvent`.** One row per card lifecycle: shown (first fingerprint), plan
applied / snoozed / task filed / expired / superseded, and the realised outcome (filled nightly
by a small job on the existing GHL loop's `generate_ops_tasks` tick: impact leg's on-location
lateness). This makes the precision number stay true in production and is the D14 trust ledger.
Today snooze is cache-only and nothing is persisted.

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

1. **`take_later` on the live path** — `board_validation.turn_slack_minutes` (and `_slot_leg_shim`
   callers) apply the fixed-time rule; `board_turn_bands` inherits. Gate: 24 re-run shows 0
   clean-but-negative flips; 12 re-run stays ≥70%; full `dispatching` suite green.
2. **`AdvisorEvent` model + nightly outcome fill** on the existing GHL loop tick (no new daemon).
   Gate: replay of one live week reproduces the card list the log holds.
3. **GPS sweep history** — `sweep_eta` writes a compact `DispatchEtaSample` row per evaluated leg
   (bulk insert on the same 180 s tick it already runs). Gate: 07's ETA-error table reproducible
   from the new table.
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
- Phase 1: `dispatching/board_validation.py` (take_later), `dispatching/models.py` + migration
  (`AdvisorEvent`, `DispatchEtaSample`), `dispatching/samsara_scheduler.py` (history insert),
  `ops/tasks.py` (`classify_turn`, `detect_driver_conflicts` → shared formula),
  `ghl_integration/scheduler.py` (nightly outcome fill hook), tests.
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
