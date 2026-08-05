# Real-Time Dispatch Recovery Advisor

**Status: SHIPPED 2026-08-04** (working tree; not yet committed). Full test suite green (1,335 tests, `dispatching` + `ops`). The "Original plan" section at the bottom predates the build — where it disagrees with this top section, this section is what exists.

## How it works (plain English)

1. **It watches the board the way a dispatcher would.** Every 60 seconds the dashboard asks the server "did anything change?" — a cheap fingerprint check (3 queries). Only when a driver taps a status, a flight moves, GPS updates, or an assignment changes does the advisor re-read the whole day.
2. **It finds five kinds of trouble**: overlapping jobs on one driver's chain, a live delay cascading into later pickups, flight changes nobody has handled, unassigned trips approaching pickup, and jobs running long. It judges each against the best truth available, in order: fresh Samsara GPS → the driver's recorded status taps → the controlling flight's gate time → the booked schedule. Fresh GPS saying "he makes it" silences clock alarms; a delayed flight is never called "late".
3. **For each problem it builds fixes in dispatcher order**: do-nothing/monitor → match pickup to flight → hand to a qualified in-house driver → multi-step swaps → farm out. When farming, the *smartest* trip to farm is often not the conflicted one — arrivals are farm currency; VIPs, true departures, and pending-refund trips never leave the house. Real affiliate prices attach to every farm option.
4. **Every option is tested against the whole remaining day** — the receiving driver's full chain, his shared-vehicle partner, his window, vehicle type/capacity. A fix that creates a new problem later is rejected outright (new critical) or demoted with the risk named (new tight turn). A fix that breaks something later is not a fix.
5. **Cards read like a colleague**: what's wrong, the arithmetic behind it, up to 3 ranked fixes with residual risks, and — when nothing internal works — exactly why each driver can't take it, before recommending the farm.
6. **The dispatcher decides.** Apply shows the exact moves, re-validates everything at click time, and refuses with a "board changed" message rather than double-booking if another dispatcher moved something meanwhile. Writes go through `set_leg_driver` (the sanctioned front door) with snapshot Undo for multi-move plans. Farm applies are never "resolved" — the card flips to "farmed — awaiting affiliate confirm" until you've actually called them.

## What shipped

| Piece | File(s) |
|---|---|
| Engine (read-only): detection, candidate plans, whole-board validation, ranking, explanations, fingerprint | `dispatching/conflict_advisor.py` |
| Whole-board post-move validator + turn-slack primitives (promoted from `views.py`; `views.py` delegates) | `dispatching/board_validation.py` |
| GPS-over-clock precedence (promoted `_pickup_risk`) | `dispatching/pickup_policy.py` (`pickup_risk`) |
| Apply write path: lock-first, staleness 409, farm hard rules, snapshot, `set_leg_driver` / `apply_pickup_time_move` | `dispatching/conflict_advisor_actions.py` |
| Endpoints: `GET /dispatching/recovery-advisor/` (state, fp short-circuit), `POST .../apply/`, `POST .../snooze/` | `dispatching/advisor_views.py`, `dispatching/urls.py` |
| Board rail (60s poll, anti-flap, confirm modal, Undo, held-day live-override confirm) | `includes/_recovery_advisor.html`, `_recovery_advisor_styles.html`, two includes in `legs_filter.html` |
| Task-page plans + collapse of the legacy no-feasibility-check assign buttons | `includes/_advisor_plans.html`, `conflict_task_detail.html`, `ops/views.py` |
| Ladder deep-links, navbar critical badge (cache-read-only) | `includes/_resolution_ladder.html`, `dispatcher_navbar.html`, `ops/context_processors.py`, `business/settings.py` |
| Board flags unified onto `pickup_policy` (no more advisor-vs-pill contradictions) | `dispatching/utils.py` (`detect_leg_flags`) |
| Snapshot trigger migration | `reservations/migrations/0124_…` |
| Tests (~166 focused; full suite 1,335 green) | `dispatching/tests_board_validation.py`, `tests_conflict_advisor.py`, `tests_conflict_advisor_apply.py`, `tests_recovery_advisor.py`, `ops/tests/test_advisor_task_links.py` |

**Owner-decided policies encoded**: applies go LIVE (sandbox unused; held days require an explicit live-override confirm, staging offered secondarily); pending-refund legs are never farm-recommended; snooze is shared/board-global (30 min default, 240 max); suggestion generation respects driver-hour caps (`enforce_cap=True`) while apply-time validation of a dispatcher's click is manual-sovereign (`enforce_cap=False`).

**Hard performance guarantees (pinned by tests)**: full board compute = exactly 15 queries, wall-clock budget 4 s with graceful truncation; fingerprint check ≤3 queries; zero external calls (Google/AeroAPI/Samsara) anywhere in the advisor path — GPS facts come only from the persisted 3-minute sweep. The prime-directive test: a founder-built tight day (+0/+3 min buffers) must produce zero cards.

## Where the build diverged from the original plan below

- No `RecoveryAdvisorSnapshot` model and no background 60-second evaluation cycle — state is computed on demand per request, cached per `(date, board-fingerprint)`, which achieves the same freshness without a new table or daemon.
- Poll cadence is 60 s (not 20 s); URLs are `/dispatching/recovery-advisor/[apply|snooze]` (no `/state/` suffix).
- Apply payload is the full action list + expected-state staleness maps (farmout_actions pattern), not a stored `plan_id`.
- Search budget is 4 s (`ADVISOR_BUDGET_MS`), config lives as module constants (house advisor style), not `SchedulerSettings` yet (P2 item).
- The rail lives on the legs dashboard (`legs_filter.html`), the board dispatchers actually work, not the schedule board.

---

## Original plan (kept for reference — predates the build)

### Summary

Build a deterministic, continuously refreshed recovery advisor for the live dispatch board. It will project the entire remaining operating day, identify urgent conflicts and fragile turns, generate and simulate alternative recovery plans, and present up to three ranked options without changing anything automatically.

The advisor will reuse the existing scheduling, swap, farmout, flight, routing, GPS, vehicle, and assignment safeguards. Recommendations will be explainable and auditable rather than generated by an opaque model.

## Implementation Changes

### Live operating-state projection

- Build an immutable snapshot of today’s live board plus its existing overnight tail. While unfinished overnight work remains, also maintain the previous service date’s snapshot.
- Project each driver’s earliest realistic availability using actual status timestamps, Samsara location/ETA, current-trip completion and service time, cached travel time, flight changes, and scheduled pickup time.
- Include driver availability, qualifications, duty limits, vehicle type/capacity, physical unit sharing, affiliate capacity/rates, special service requirements, and current assignment state.
- Treat GPS older than 10 minutes as stale: fall back to scheduled/cached routing, lower recommendation confidence, and clearly disclose the fallback.
- Group connected conflicts into recovery episodes so one disruption produces one coordinated board-wide recommendation rather than several contradictory pairwise alerts.

### Recovery search and ranking

- Detect:
  - projected late or impossible pickups;
  - overlapping driver chains and excessive overruns;
  - thin turns with 15 minutes or less projected slack;
  - early, delayed, cancelled, or diverted flights;
  - shared-vehicle conflicts;
  - incompatible or unavailable vehicles;
  - urgent unassigned trips.
- Generate candidates from direct reassignment, pair swaps, cascading swaps, reallocation of later work, valid flight-time matching, and farmout of an eligible trip elsewhere on the affected board.
- Extend the existing swap search to use projected live clear times and validate every candidate against the complete remaining board.
- Consider every movable farmout-eligible trip in the recovery episode—not merely the two visibly conflicting trips—and reuse current affiliate capability, permit, capacity, pricing, and opportunity-cost checks.
- Reject plans that introduce a new critical conflict, violate a hard commitment, exceed vehicle or driver constraints, or depend on unavailable resources.
- Rank surviving plans lexicographically:
  1. Avoid missed or physically impossible service.
  2. Minimize high-priority and total projected lateness.
  3. Protect airport, cruise, VIP, and service-critical work.
  4. Minimize farmouts and preserve valuable Grayson work.
  5. Minimize trip moves and disruption to committed drivers.
  6. Maximize the smallest remaining buffer and minimize deadhead, duty-span damage, and farmout cost.
- Return at most three meaningfully different plans. If no fully safe plan exists, show the best mitigation with explicit residual risks; never manufacture a policy-violating farmout option.
- Each plan will contain ordered actions, before/after timing, affected trips and drivers, why it ranks where it does, downstream trips checked, remaining risks, recommended affiliate choices where relevant, and input freshness/confidence.

### Continuous evaluation and persistence

- Add a `RecoveryAdvisorSnapshot` record keyed by service date, containing generation, board fingerprint, evaluation state, detected issues, ranked plans, watch items, data-freshness metadata, timestamps, run duration, dirty state, and last error.
- Store plan actions and their expected assignments/statuses/times in snapshot JSON; derive stable plan IDs from the generation and action signature.
- Add a leader-locked, 60-second evaluation cycle to the existing scheduler infrastructure. It will use persisted data only and make no Samsara, Google, or flight API calls.
- Preserve current external-call cadences: Samsara GPS every three minutes, paid ETA refresh every six minutes, and flight refresh every thirty minutes.
- Include the current minute and all scheduling-relevant fields in the board fingerprint so elapsed time alone can trigger a new projection.
- Mark snapshots dirty after assignment, status, pickup-time, flight, driver-availability, or vehicle-allocation changes. Failed runs retain the last good snapshot with a stale/error warning.
- Upsert the existing `driver_conflict` and `tight_turn` tasks from recovery episodes after each evaluation, and close generated tasks once the episode is genuinely resolved. The existing 30-minute task scan remains a backstop.

### Safe review and application

- Add a staff-only transactional apply service that accepts a stored plan ID, never arbitrary client-provided moves or prices.
- Before writing, lock affected records and revalidate:
  - snapshot generation and board fingerprint;
  - trip statuses and commitment rules;
  - current assignments and pickup times;
  - the latest valid flight arrival;
  - driver qualifications and availability;
  - vehicle type, capacity, and shared-unit conflicts;
  - affiliate policy, capacity, permits, and current rate;
  - feasibility of the complete resulting board.
- Apply valid flight-time matching first, then assignments/farmout actions through the existing shared write paths, followed by one conflict/task refresh.
- Roll back the entire plan if any action or final-board validation fails. Return HTTP 409 with the changed facts so the interface can load fresh recommendations.
- Keep all existing assignment and pickup-time audit behavior. Driver notifications continue through their existing policy gate.
- If a held scheduling draft exists, the advisor still evaluates the actual live board. Applying requires an explicit “Apply to live board” confirmation and uses the current live-override behavior while keeping the draft overlay coherent.

### Dispatcher experience

- Add a collapsible command-center advisor rail to the schedule board, following the existing industrial dispatch visual language:
  - “Act now” and “Watch” counts;
  - last evaluation and GPS/flight freshness;
  - ranked recommendation cards;
  - internal, farmout, and flight-match badges;
  - projected minimum buffer and affected-driver summaries;
  - expandable action sequence and downstream impact.
- Poll persisted advisor state every 20 seconds without reloading the board or invoking external services.
- Require “Review plan” before enabling “Apply plan.” Farmout plans allow selection only among currently recommended eligible affiliates.
- Disable application when the snapshot is dirty, errored, or more than three minutes old.
- Show the same recovery episode and ranked plans on conflict/tight-turn task pages, replacing the current unvalidated raw affiliate list. Existing manual dispatch controls remain available as a secondary escape hatch.
- After successful application, reload the live board and show the resulting assignments and resolved or remaining risks.

## Public Interfaces and Configuration

- `GET /dispatching/recovery-advisor/state/?date=YYYY-MM-DD`
  - Returns snapshot status, generation, fingerprint, freshness, act-now episodes, watch items, and ranked recommendations.
  - Historical/future boards return an unavailable state rather than simulated “live” advice.
- `POST /dispatching/recovery-advisor/apply/`
  - Request: `generation`, `plan_id`, optional recommended `affiliate_id`, and `live_override`.
  - Response: affected trips, resulting live/draft state, task updates, and new board fingerprint; stale or invalid plans return 409 without writes.
- Add admin-tunable scheduler settings with defaults:
  - on-the-way movable horizon: 60 minutes;
  - watch-buffer threshold: 15 minutes;
  - maximum displayed plans: 3;
  - recovery search budget: 5 seconds.
- Recommendation explanations use structured facts and templates so displayed reasons always correspond to validated calculations.

## Test Plan

- Projection tests for active-trip GPS completion, stale GPS fallback, overruns, early/delayed/cancelled flights, overnight work, and elapsed-time deterioration.
- Constraint tests for driver hours, availability, certifications, vehicle type/capacity, shared vehicles, service requirements, and route repositioning.
- Commitment tests proving picked-up/on-location work is frozen and on-the-way work is movable only at least 60 minutes before pickup.
- Search tests covering direct moves, swaps, cascading recovery, rejection of downstream conflicts, grouped episodes, deterministic ranking, distinct top-three plans, and bounded-search fallback.
- Farmout tests proving the best trip may be elsewhere on the board, while VIP and true airport departures are never recommended for farming; validate affiliate capacity, rates, permits, and unavailable-quote behavior.
- Apply tests for stale fingerprints, changed statuses, changed flights, transaction rollback, full-board revalidation, audit records, held-draft live override, and no automatic writes.
- UI tests for polling, loading/error/stale states, review confirmation, affiliate selection, 409 refresh, task-page reuse, keyboard/focus behavior, responsive layouts, and reduced motion.
- Scheduler tests proving only one leader evaluates, failures preserve the last good snapshot, and the new cycle does not increase external API calls.
- Performance acceptance: complete a production-sized evaluation within the five-second budget and serve persisted state within 200 ms. Preserve the currently passing focused baseline of 198 scheduler, swap, farmout, flight-safety, timeline, and Samsara tests.

## Assumptions and Defaults

- Picked-up, on-location, completed, and cancelled trips are immutable.
- An on-the-way assignment may move only when its effective pickup is at least 60 minutes away; such moves receive a disruption penalty and an explicit warning.
- VIP trips and true airport departures retain their existing hard no-farmout policy.
- Only valid live-flight matching may change pickup time; the advisor will not invent arbitrary customer time changes.
- Physical vehicle allocations are evaluated as constraints but are not automatically rewritten in the first release. If a vehicle-only intervention is required, the advisor identifies it as a manual fleet action and does not present the plan as one-click applicable.
- The dispatcher always makes the final decision; evaluation, task creation, and polling never alter assignments.
