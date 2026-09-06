# The build-session kickoff prompt

**Paste the block below into the new chat — but only once Gate 3 is actually a yes.** It puts
your approval on record (the specs forbid building without it), forces the docs to be read in
the right order, scopes the first task narrowly, and restates the standing rules as live
instructions instead of history.

---

Continue the Grayson Towncar scheduling redesign — we are now in the BUILD phase.
Branch: `scheduling-redesign/phase-1`.

**GATE 3 IS APPROVED.** I have reviewed and approve `02_BENCHMARK_AND_EVIDENCE.md` and
`04_PLANNER_AND_BUILD_PLAN.md`. You may build.

Before writing any code, read in full, in this order:
1. `docs/scheduling-redesign/04_PLANNER_AND_BUILD_PLAN.md` — the approved spec. It governs;
   follow it exactly. Deviations come back to me for review, never judgment calls mid-build.
2. `docs/scheduling-redesign/03_STANDBY_AND_HANDOFF_MODEL.md` — the operational model and config
   values the builds implement.
3. `docs/scheduling-redesign/01_REVISED_SCOPE_AND_PLAN.md` — scope and my decision ledger D1–D12.
4. `docs/scheduling-redesign/02_BENCHMARK_AND_EVIDENCE.md` — the verified numbers; and skim
   `00_DATA_AUDIT_AND_INVENTORY.md` §A6/§A4.6 for the data rules (read-only snapshot, the audit
   log phantom trap, never filter history on current-state flags).

**Start with BUILD 1 ONLY (04 §2)** — the four quick fixes: (1a) warn-only validation on the
manual assign path, gated on ≥70% precision against the replay per the spec; (1b) the
signals.py:751 phantom fix; (1c) the share pad 60→120 as a SchedulerSettings field; (1d) the
one-L 'canceled' spelling. Ship each per its spec'd gate and tests.
**STOP when Build 1 is done and verified, show me what shipped, and wait for my review before
touching Build 2.**

Standing rules, non-negotiable:
- Everything is additive — current dispatcher workflows unchanged except where 04 names the
  change. No auto-apply anywhere. Nothing driver-facing. Respect every non-goal in 04 §6.
- Dispatcher-visible changes ship with a release note per CLAUDE.md; invisible ones commit with
  `Release-Note: none`.
- Any frontend/template work: read `docs/claude.md` first, and test in a real browser on a real
  date before calling it done.
- `content/db.sqlite3` is opened read-only for analysis, never written; production data rules
  per 00 §A6.
- Keep me in the loop: at each build boundary you propose, I approve, then you execute. Report
  outcomes plainly — if a test fails or a gate doesn't pass, say so with the output.

---

*After Build 1 review: Build 2 (split/handoff in Day Setup), then Build 3 (the day-builder),
each behind its own review, per 04. This file can be deleted once the build is underway.*
