# Day Manager build — ready to paste into a fresh session

*(Copy everything below the line into a new chat. Phase 0 is finished and its findings
are committed; this is the build.)*

---

Continue the Grayson Towncar scheduling redesign — the **Day Manager (D13)**.
Branch: `scheduling-redesign/phase-1`.

**Read first, in this order:** `docs/scheduling-redesign/06_DAY_MANAGER.md` (the plan, with a
verification log in §0 and every measured number in place) and `06_SIMPLE_READ.md` (the same
thing in the founder's plain-English register — match that voice when you talk to him). Do not
re-derive anything in them; five analysis scripts already did, and their CSVs are committed under
`analysis/out/`.

## Where things stand

**Phase 0 is complete.** Five scripts, all re-runnable cold from the repo root:

| Script | What it established |
|---|---|
| `25_scanner_outcomes.py` | The floor's alarm load: **70.9 conflict tasks/day**, on 33.9% of the day's legs, **65.9% of closes buy nothing**. Rising: 13 → 37 → 37 → 71 over five months |
| `24_live_clock_split.py` | **5.1 turns/day** the board called clean or tight that the engine calls impossible; all 142 named in `out/24_clean_negative.csv` |
| `23_advisor_replay.py` | The shipped advisor scored over 28 days × 15-min ticks × 3,864 computes: **4.2 cards/glance, 60 ms P50, and no class passing D5** — best genuine forecaster 43%, and only 17.5% right about returns |
| `26_milestone_detector.py` | The founder's own rule, measured before being built: **59% recall, 80–97 min of warning, 24.5% precision**, ~27 fires/day |
| `12`, `14` | Pre-existing gates, re-run after the clock fix |

**Phase 1.1 is SHIPPED** (`board_validation.turn_slack_minutes` now applies
`CHAIN_CLEAR_TAKES_LATER` on the planning branch; four regression tests in
`tests_board_validation.TakesLaterParityTests`; release note
`2026-09-05-board-chips-tell-the-truth.md`). Note the plan's assumption that all of Phase 1 is
invisible was **wrong for this item** — it changes chip colours, so it shipped with a note. Check
that judgement again for each remaining item rather than trusting the phase label.

## What to build, in order

1. **`AdvisorEvent` + nightly outcome fill** (Phase 1.2) — one row per card lifecycle plus the
   realised outcome, filled on the existing GHL loop tick. **No new daemon** (04 §6). Invisible.
2. **`DispatchEtaSample`** (Phase 1.3) — the Samsara sweep already fetches every car's position
   every 180 s and discards it. Bulk-insert a compact row on the same tick. Invisible, and it is
   **the other half of the detector**, not support work: see §3.4's two-halves table — the
   milestone rule catches "he never got started", GPS catches "he started fine and is now stuck",
   and 07 scores that signal at 72% on "late at all".
3. **The milestone detector** (§3.4) — the founder's spec, already measured by 26. Visible.
   `latest_safe_pickup` derived by running the shipped chain math backwards; the escalation ladder
   spends no paid routing call until a real conflict is being priced (precedent: the 2026-08-09
   commit that pulled a paid Distance Matrix call out of `ops/tasks.py` for cost).
4. **Open the rail** — the one-line `advisor_visible_to` gate, last, and only alongside §3.3's
   horizon cap.

## Decisions the founder has made

- **45–60 minutes of warning is the bar**; 30 is workable but risky. Recall is reported *at* those
  thresholds in 26, not in the abstract.
- **He does not want a tool that reports what already happened** — it must warn before. §3.3's
  original "drop the prophecy" recommendation was rejected on those grounds and replaced by the
  milestone design.
- Build the clock fix first. Done.

## Decisions still open

- §3.3 (a)/(b)/(c)/(d): what the rail is *for*, now that no existing class forecasts well enough.
- Whether `turn_tight` demotes to info — it fell to 68.9% after the clock fix (composition, not
  decay; the real conflicts migrated into critical). Dispatcher-visible, so his call.
- Whether the ops scanner is retired or re-pointed — waits on a database pull after 2026-08-27.

## Things that will bite you

- **The snapshot ends 2026-08-21.** Every write stream stops there. Four of the five late-August
  tuning commits land after it, so their effect is unmeasurable on any copy on disk. A fresh pull
  is the one outstanding Phase 0 item.
- **Run analysis scripts with `venv/bin/python`**, not `python3` — Django is only in the venv.
- **`BUILD3_GATE_TMP=<dir>`** pins the throwaway migrated copy; without it every run copies 660 MB
  to a fresh temp dir.
- **Two replays running at once will fight over the copy** — one tick recorded 70 s that way and
  re-ran at 52 ms in isolation. Don't read a slow outlier as an engine problem.
- **Eight suite failures are pre-existing** (fleet, Samsara telemetry, load metrics, staffing
  board). Diff against a stashed baseline before blaming your change; `ChauffeurLoadViewTests`
  is order-sensitive and passes alone.
- **Scoring traps this project has already fallen into twice:** a card whose impact leg is its own
  leg grades itself (23's `pct_single_leg` column exists for that), and 12's "truth" is 09's flat
  occupancy model — a *definition* built on the same optimistic clock, so it cannot arbitrate a
  clock change. Check what a gate actually measures before trusting it.

## House rules

Release note for anything a dispatcher or chauffeur can see or do — write it before the commit,
never ask whether one is wanted, read `docs/release-notes/README.md` for the voice. Invisible work
carries `Release-Note: none`. Frontend work reads `docs/claude.md` first.

**And the one that matters most here:** measure before building, and when a measurement contradicts
an assumption, say so plainly and change the plan. This project has already refuted its own
two-fact bet (§3.2), its own rail-load fear (§3.3), and a claim that the clock fix would cure the
milestone's false alarms (§3.4). All three were caught because the script was written before the
code.
