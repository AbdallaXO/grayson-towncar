# Scheduler Automation — Roadmap

**Updated 2026-06-10 (evening).** The plain-language status of the schedule builder: what's
done, what's next, and why. Technical detail lives in `auto-assign-hour-balancing-design.md`
(same folder); this file is the map.

---

## ✅ Done and live in production

- **One-button day building**: Suggest Day Setup (roster + vehicle plan) → Apply →
  Auto-Assign. On the hardest benchmark Saturday (2026-05-16) it kept 110–112 jobs
  in-house vs 97 hand-built, same 15 drivers / 13 cars.
- **Shared cars (split shifts)**: when more drivers can work than there are cars, the
  morning driver hands the car off (3 PM default) to an evening driver. Double-booking a
  shared unit is physically impossible in every path, including manual swaps, with a
  60-minute warehouse buffer (return + wash/fuel + drive out) at every handoff.
- **Hour-balance guardrails**: hard 17-hour ceiling no rescue can exceed (a job that fits
  nobody under 17h farms, loudly, with a named reason); 13.5h-effective soft target with
  trim/relocation passes; midnight rule — a 00:00–02:59 pickup never lands on a day
  driver (explicit night windows still work; manual dispatcher moves always win).
- **Second-Shift Advisor**: after a build, proposes "add THIS driver with THIS unit" when
  residual jobs or untrimmable long days justify another body. One-click accept — now with
  a red coverage banner if the add actually made the day worse (a freed-unit share can
  constrain the car's holder; the backtest caught a card that silently cost 4 jobs).
- **Feasibility calibrated to reality**: deplaning grace, intra-resort drive times,
  pre-farm swap recovery, gap compaction (the "give David the job in his hole" move).
- **Demand-aware staffing** *(committed 2026-06-11 — local commit, NOT yet pushed/
  deployed; founder evaluation in progress)*:
  - **Solo-first Day Setup**: when drivers outnumber cars, the extras stay UNCHECKED
    ("available — add via Advisor if the day needs them") instead of an automatic AM/PM
    split. The 12-day backtest's biggest surprise: on the hardest Saturday (05-16),
    13 solo drivers built **113 in-house vs 110 with 15 drivers sharing** — splits
    fragment windows; coverage is car-bound. One flag restores the old auto-share.
  - **Fold-Out Advisor** (the mirror image): after a build, green cards propose releasing
    thin drivers — "X has only 3 jobs and they all fit on A/B/C — fold him out and free
    his car?" One click relocates the jobs (every move feasibility-, occupancy- and
    span-checked, all-or-nothing), takes him off the day, frees the unit, and shows a red
    banner + Undo if the rebuild came up short. Proposes only; never automatic.
  - **Rebalance Advisor** (your 06-01 round-2 feedback, blue cards): RELATIVE balance —
    "Aftab has 1 job vs the day's ~5 average — move these 3 to him" (fill) and
    "Raymond's 16:45 + 22:24 stretch a hollow day to 14.8h — move them; he ends at
    10:30" (compress). No absolute jobs-per-driver target; 3-each slow days stay silent;
    when an imbalance has a physical reason the card SAYS so ("his 9:06 job IS the
    peak"). One click moves the jobs; zero database writes; Undo built in.
  - **Peak-concurrency roster sizing**: Day Setup now counts legs IN FLIGHT per vehicle
    tier (the histogram you described — 06-01 peaks at 12-13 at 09:30) and checks
    peak+1 drivers. Rates still rank WHO; the peak decides HOW MANY. It also catches
    the opposite miss: days the old rate-gate under-checked (04-03 got 3 more bodies
    AND +1 coverage). Never naive legs-per-driver.
  - **"Yovanny in, someone out"**: tick anyone in Day Setup and hit "Re-suggest with my
    picks" — your pick joins at top priority (zero history needed), the lowest-priority
    suggestion steps aside with a hint, dedicated cars and already-applied rows are
    never touched, and the schedule's OFF gate still wins (the Advisor path handles
    OFF drivers, labeled).
  - **12-day acceptance backtest re-run with everything on, ALL gates passed again**:
    coverage >= the shipped engine on every day (net +16 this sweep); slow days run
    with up to 4 fewer drivers (05-19: 13 -> 9, same coverage); zero overlaps, zero
    days over 17h, midnight rule holds. Full tables in the design doc PARTS 6-7.
  - Also fixed along the way: a 06-10 regression where every Second-Shift freed-unit
    accept was rejected with "already assigned" (side effect of the decline-a-share
    fix), and a bug where one skipped-unpaid leg silently suppressed every Fold-Out
    card (found in your 06-01 drive).
  - **Still open in this arc (polish, not blockers)** — from the 06-11 evaluation:
    1. Balance cards only work BEFORE Apply. Once a day is applied, every leg is
       yours (manual-sovereign) and the advisors go quiet — correct, but the screen
       doesn't say so. A small "applied days are read-only to advisors; unassign +
       rebuild to reshape" notice would prevent the "why no card?" confusion.
    2. Busy days never see balance cards (any uncovered paid job suppresses them).
       One-flag experiment when busy-day balance becomes the pain.
    3. Compress fired rarely in the backtest (hollow days on 05-22/06-01 survived) —
       worth one investigation pass into WHY (receivers? thresholds?).
    4. Deploy decision: the new defaults (solo-first, peak sizing) change prod
       behavior the moment this is pushed — push only after you're satisfied.

**Settled by experiment (don't revisit without new data):**
- The handoff hour (12/1/2/3 PM) does NOT change coverage — busy-day coverage is bound by
  CARS, not driver-hours. The 3 PM default stands.
- An extra vehicle pays only on days when available drivers outnumber cars (+5 in-house,
  ~$500–750/day on 05-16; +0 on driver-bound days). **Every SHARED badge in Day Setup
  marks a day a spare unit would have earned its keep — count them to size the fleet.**

---

## ▶ Next (recommended order)

**Recommendation for the next session: start the chain-aware builder (#1).** The
staffing layer is now measurably at-or-above your hand on 10 of 12 benchmark days; the
two days you still beat the engine (03-28: 95 vs 82, 04-03: 99 vs 95) lose on leg
ORDERING with staffing already optimal — that's #1's territory and the biggest coverage
money left. The four "still open" polish items above are good warm-up tasks for the
same session.

### 1. Chain-aware builder
The greedy builder sometimes takes a single job over a better two-job round-trip chain
(the confirmed Aftab case: a 10:42 Publix run over an 11:00→12:15 airport chain). Needs
lookahead scoring; A/B on 05-09 / 05-16 / 06-01 / 06-02. Worth a few extra in-house jobs
and less deadhead on busy days. The staffing backtest sharpened the target: the engine
trails your hand boards on 03-28 (82 vs 95) and 04-03 (97 vs 99) with staffing already
optimal — that remaining gap is leg-ORDERING quality, exactly this arc.

### 2. Flexible-start staggering
Start late finishers later the next day (rest-aware). Designed; review found the
early-coverage guard must become vehicle-tier-aware before building (could strand an
early van pickup). Needs real prod hours data.

### 3. Real driver windows
Flip `USE_STUB_WINDOWS=False` once driver schedule data is cleaned (deactivate
placeholder id 6, fix OFF-marked-but-working drivers, pre-6AM starts), then re-benchmark.
Until then start/end windows come from observed history.

### 4. Live road-distance matrix
Re-introduce live drive times WITHOUT a network call during page loads (precomputed
matrix or Redis). Restores exact distances for far/unknown + intra-resort routes; today
those use the coarse category table (accuracy traded for the 2026-05-31 timeout fix).

### 5. Warehouse/base-location concept
Make the shared-car handoff buffer geography-aware: car-ready time = last clear + drive
to warehouse + wash/fuel; next pickup ≥ car-ready + drive out. Today it's a flat 60 min.
Low urgency — the flat hour matches founder practice.

### Business decision (no code): the 14th vehicle
The signal moved with solo-first: SHARED badges no longer appear by default. Now count
days where Day Setup says "MORE DRIVERS THAN CARS" and the Second-Shift Advisor proposes
the extras after the build. If that happens on most busy Saturdays, an extra unit pays
for itself (~$500–750/busy day at current farm-out spreads).

---

## Founder to-dos (10 minutes, admin screens)
- Set preferred vehicles: David → #008, roberto → #004, sereen → #003 (george done).
- Fix rizwan/ken weekly schedules (marked OFF on days they actually work).
- Keep retiring unused units (e.g. #015 done) so the car count the engine plans around
  stays honest.

## Known small gaps (accepted for now)
- The UI's "Skip unpaid" toggle (on by default) makes UI coverage numbers read ~3 lower
  than offline benchmarks on the same board — same engine, deliberate rule.
- Fold-Out v1 never proposes folding a share partner (it would orphan the partner's
  planned handoff window) and only counts jobs (≤3), not revenue or hours.
- Fold-Out and Rebalance stay silent while ANY paid job is uncovered (one question on
  screen at a time) — so busy days with farm-outs never see balance cards yet. If
  busy-day balance becomes a pain, it's a one-flag experiment.
- Force-include can't displace an already-APPLIED row (real vehicle assignments are
  yours) — the warning says which row to clear in the panel first.
