# Performance Fix Changelog

Append-only log of performance changes. One row per fix.

> **Status: no code changes applied yet.** This audit was delivered as documentation only
> (see `PERFORMANCE_AUDIT_AND_ACTION_PLAN.md` and `PERFORMANCE_FIX_TASKS.md`).
> Implementation will follow later. Add a row here as each fix lands.

## How to use this log
For every fix, record:
- **Date** — ISO date the change merged.
- **File(s)** — paths touched.
- **Finding ID** — the audit finding(s) addressed (e.g. `DISP-03`).
- **Issue fixed** — one line.
- **Risk level** — Low / Medium / High (the fix's risk, not the bug's severity).
- **Test performed** — what you ran/checked (query-count before→after, manual steps, unit tests).
- **Notes** — surprises, follow-ups, rollback notes.

## Log

| Date | File(s) | Finding ID | Issue fixed | Risk | Test performed | Notes |
|------|---------|------------|-------------|------|----------------|-------|
| _(none yet)_ | | | | | | |

<!--
Template row:
| 2026-06-04 | dispatching/views.py | DISP-03 | auto_assign per-leg save() -> bulk_update | Med | apply on 50-leg day, assignments identical, 1 UPDATE vs 50 | rollback = revert commit |
-->
