# Build 4 kickoff — finish, ship, and improve the Day-Builder

*(Copy everything below the line into a new chat. Written 2026-09-02 after
verifying branch state, test baseline, and merge cleanliness against main.)*

---

Continue the Grayson Towncar scheduling redesign — **Build 4: finish it, ship
it, then make it build better schedules.**
Branch: `scheduling-redesign/phase-1`, tip `c6c35787` (2026-08-25 session
wrap). Start there. Read `docs/scheduling-redesign/HANDOFF_2026-08-25.md`
first — it is the whole picture in two pages.

**Where things stand (verified 2026-09-02):**

- Build 3b is COMPLETE. The Day-Builder ("Build a plan" in Day Setup on the
  capacity planner: `dispatching/day_planner.py`, `receiver_gate.py`, the
  `DayPlan` model, migration dispatching 0018) passed Gate 4 on all 10
  criteria × 10 dates (`docs/scheduling-redesign/analysis/17_build3_gate.py`)
  and ships with `SchedulerSettings.opt_enabled = False`. Propose-only: it
  names swaps and "catch the rest" additions in plain words; the dispatcher
  applies.
- The full `dispatching` suite is green on the branch exactly as pushed:
  1,897 tests, 0 failures, ~3.5 min via
  `ENABLE_DEBUG_TOOLBAR=0 python manage.py test dispatching` (no `--parallel`
  — it dies on a pickle error). "database table is locked" log lines are
  known background-email noise, not failures; `tests_overnight_arrival` is a
  known intermittent flake — re-run before believing it.
- `main` is 28 commits past the merge-base (booking-wizard rebuild, guest
  texts, driver documents, office timeclock). `git merge-tree` main→branch
  shows ZERO conflicts and the migrations don't overlap (branch adds
  dispatching 0015–0018; main added drivers 0050–0054, ops 0016–0018,
  rates 0025–0027, reservations 0127–0129).
- **BLOCKER on main, not on the branch:** commit `315714cb` imports
  `tidy_address` from `reservations.place_names` in `dispatching/forms.py`,
  and that module was never committed anywhere. Main cannot boot and
  Railway's pre-deploy migrate will fail. I am committing that file from my
  other computer. Before any merge, check
  `git ls-tree origin/main --name-only reservations/place_names.py`; if it is
  still missing, do everything else and stop before the merge-to-main step.
- Local env: `.venv` (Python 3.13); local DB is the scrubbed prod copy at
  `content/db.sqlite3`, migrated through dispatching 0018 plus all of main's
  migrations. Runtime fact that bounds any search: one cold pipeline run is
  median 6 s / P90 12 s / max 15 s, superlinear in legs.

**The goal, in my words, binding:** get this finished and into dispatchers'
hands, then make it build better schedules than it does today. Better means:
more trips in-house at the same crew, smarter day splits (which two drivers
share which car and where the handoff falls), fewer long days, zero hard
conflicts, and plans that survive flight retimes. Coverage is the number that
goes up; conflicts and hours are walls, never trade goods. Decisions D1–D16 in
`01_REVISED_SCOPE_AND_PLAN.md` §1.1 all still bind — especially D4 (13.5 h
default, 15 h only as a priced visible exception), D11 (additive; promotion is
my call), D12 (no rushed build — this is the heart of the operation), D13 (no
live day-of logic; that is the later Day Manager), D14 (propose-only now, but
never architect against auto-apply later), D15 (rest findings accepted for v1,
diagnose later) and D16 (coverage beats idle capacity: give an available
driver a few jobs before farming one).

Read in full, in this order, before writing any code:

1. `docs/scheduling-redesign/HANDOFF_2026-08-25.md`
2. `docs/scheduling-redesign/05_BUILD3B_TICKETS.md` — §11 (build-time
   addendum: what actually shipped, D15/D16) and §9 (open items) especially
3. `docs/scheduling-redesign/01_REVISED_SCOPE_AND_PLAN.md` §1.1 — the ledger
4. `docs/scheduling-redesign/04_PLANNER_AND_BUILD_PLAN.md` §1, §4, §6 —
   the non-goals are walls
5. `docs/scheduling-redesign/03_STANDBY_AND_HANDOFF_MODEL.md` and
   `02_BENCHMARK_AND_EVIDENCE.md` §1 (state B is the scorecard); skim
   00 §A6 and §B3

**Session plan — report at each numbered stop and wait for me:**

1. **Ship it.** Merge main into the branch (expect clean). Run the
   dispatching suite and Gate 4 (`analysis/17`, on a FROZEN migrated copy of
   the snapshot, ~10–15 min). Report both results with the actual output.
   Then, only on my explicit "go" in that message: merge to main and push.
   After deploy I flip Planner → Tuning → Day-Builder to 1 and paste
   `docs/release-notes/2026-08-25-build-a-plan.md` to the team. Ask me
   whether the branch's unrelated hitchhikers (the SOPS reservation
   walkthrough files and the `OPS_TASK_LAYER_PLAN.md` deletion) ride along
   or get split out.

2. **Find where it loses to the hand board.** Run the Day-Builder cold on
   the 10 most recent real built dates, plus 2026-08-03 — the parked "Aug 3
   question", where the hand board hit 83.7 % in-house with 16 drivers and
   the engine gets 81.7 % with 14. For every trip the engine farmed that the
   hand board kept in-house, say in plain English what the humans did
   differently: a split the engine didn't propose, a handoff it banded red,
   a rest rule it applied harder than they did, a vehicle or certification
   choice, a retime it could not see. Bring me a ranked list —
   cause → trips/day lost → dollars per 28 days → what a fix would touch.
   That list picks the improvement targets. Do not pre-empt it.

3. **Improve, one target at a time.** Candidates I expect on the list:
   (a) day splits — smarter shared-car pairing and handoff placement
   (`standby_mints.py`, `handoff_chain.py`, the Afternoon Exchange window
   from the D4 shift architecture); (b) the overnight-rest floor as a real
   placement gate instead of a soft penalty (the D15 diagnosis); (c) the
   5-of-290 co-driver convention discrepancy (05 §9.1); (d) the small
   inconsistencies in 05 §9.2–9.3; (e) the two door gaps that block one-click
   apply (D14) — only if I say so. For each: gate before, change, gate
   after; byte-identical replay wherever the change is meant to be
   verdict-neutral. **If a change moves any Gate-4 criterion the wrong way,
   or flips a verdict you did not intend, stop and bring me the numbers —
   do not pick a side.**

4. **Promotion** (D11) and the **Day Manager** (D13) are my calls, made
   later. Do not start either.

**Standing rules, non-negotiable:**

- Never commit or push without my explicit ask in that same message.
  Pushing main deploys production.
- Everything additive. No new write doors; no third copy of
  `set_leg_driver` semantics; nothing driver-facing; propose-only stays.
- Freeze a snapshot copy before any replay arithmetic and point
  `GRAYSON_SNAPSHOT_DB` at it — my dev server writes to the live local DB
  while you work. `content/db.sqlite3` is read-only for analysis. Use
  `route_distance.probe_mode()` for planning probes; analysis must never
  enqueue a billable distance lookup.
- Gate technique that works: `analysis/14` parity (RequestFactory, 10 dates
  × 4 scenarios, the cold scenario wipes assignments first) and
  `analysis/17` for acceptance. Prove determinism first (two captures diff
  to 0), then before/after.
- Dispatcher-visible changes ship with a release note per CLAUDE.md
  (`docs/release-notes/README.md` for voice). Frontend work reads
  `docs/claude.md` first and is browser-tested on a real date.
- Report outcomes plainly at each stop. If a gate fails, show the output.

STOP after step 1's report and wait for my go before the merge to main.
