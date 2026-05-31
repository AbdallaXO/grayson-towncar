# Phase 3 Prep — Per-Driver Availability / Window Model

**Status:** Investigation only. **STRICTLY READ-ONLY** (`readonly_local`, no writes). **No guards
built, no code changed.** Harness: `scratch/phase3_windows.py`. Live data as of 2026-05-30.

## TL;DR — is the window data trustworthy enough to build a guard on? **Not yet.**

The *model* is well-structured (3 layers: per-weekday schedule + date overrides + defaults). The
*data* is mostly **default and non-constraining**, so a feasibility guard built on it today would do
almost nothing for most drivers and could mis-fire on the few real ones:

- **0 of 28** active in-house drivers have **`max_hours`** set anywhere → the per-driver hours cap
  **never fires** in production.
- **Only 5 of 28** have a **real (non 6–23) time window**; ~14 are the default **6:00–23:00**
  (`end_hour=23` ⇒ a "last pickup ≤ 23:59" bound that essentially never bites), and **9 are marked
  OFF every day of the week** (placeholders / inactive-in-practice, e.g. a driver literally named
  `placeholder`).
- **5 of 28** are **`flexible`**, which makes the auto-assigner **skip the window entirely**.
- **0 of 28** carry a **night** shift type.
- The "latest" bound is modeled as a **last-pickup** time, **not** a "clear/finished-by" time (one
  partial exception in the single-driver builder only — see §1). There is **no dedicated clear-by field**.
- Phase 2.5's 44–56 "window violations" on real boards are consistent with **stale START bounds**
  (drivers doing pre-6 AM airport/cruise runs while marked `start_hour=6`) and the handful of
  real-window drivers — i.e., the fields don't match operational reality.

**Conclusion:** before any window-based feasibility guard, the windows + `max_hours` must be
**configured to match reality** (especially early-AM starts and any true finish-by expectations), the
9 OFF-all-week "active" drivers cleaned up, and a **product decision made on last-pickup vs clear-by**
semantics. A guard built on today's data would only constrain ~5 drivers and would inherit the stale
start bound.

---

## 1. The model & fields (Q1)

Three layers, resolved by `drivers/availability.py:resolve_effective_availability` (weekly row →
overlaid by an **approved** `DriverDateOverride` → fallback to `Driver.default_*`).

**`Driver` defaults** (`drivers/models.py:8`): `default_start_hour` (6), `default_end_hour` (23),
`default_flexible` (True), `default_shift_type` (`full_day`), `default_max_hours` (NULL), `night_bonus`.

**`DriverWeeklySchedule`** (`drivers/models.py:226`, unique per driver+day_of_week) — the real
per-driver day-shape:
| Field | Meaning |
|---|---|
| `is_available` | works that weekday or not |
| `start_hour` (int, default 6) | **earliest start** |
| `end_hour` (int, default 23) | **latest bound — see semantics below** |
| `flexible` (bool, default True) | "no hard time limits" — **bypasses the window in auto-assign** |
| `max_hours` (Decimal, NULL) | "Maximum hours to schedule this driver per day. NULL = no limit." |
| `shift_type` | morning/midday/evening/**night**/split/full_day/custom (classification) |
| `preferred_shift`, `preference` | soft prefs (trip-type, time-of-day) |

**`DriverDateOverride`** (`drivers/models.py:304`) — one-off exceptions (only `status='approved'`
count): `off`, `available_until`/`available_after`/`available_window`/`unavailable_window` (with
`start_time`/`end_time`), `flexible`, `note_only`.

### Latest-bound semantics — **last-pickup, not clear-by** (critical)

- **`end_hour` is enforced as a LAST-PICKUP bound** in both engines: a leg is allowed iff
  `pickup_time ≤ end_hour:59`. (`scheduler.py:1209` auto-assign; `scheduler.py:1659/1797` builder.)
- A **"must be finished/clear-by" check exists in ONLY ONE place** — the single-driver builder, as a
  soft `+1h` grace: `if est_end.hour > end_hour + 1: skip` (`scheduler.py:1801-1802`). The
  fleet-wide **auto-assigner has no finish-by check at all** — a leg picked up at `end_hour` that
  clears hours later is *not* blocked by the window (only the soft span penalty / `max_hours` apply).
- There is **no separate "clear-by" field**. So the two constraints you named are **not** both
  modeled: last-pickup = yes (both engines); clear-by = partial (builder only, derived, +1h grace);
  and the two engines disagree.
- The richer, exception-aware `is_pickup_within_window` (`drivers/availability.py:290`) *does* add a
  finish-past-cutoff check for `available_until`, but it is used **only for the drag-drop warning**
  in the UI (`check_driver_feasibility`, `views.py:2168`), **not** in auto-assign or `check_feasibility`.

## 2. Max-span / max-hours (Q2)

- **Per-driver `max_hours`** (weekly + `default_max_hours`), NULL = no limit. When set, the
  auto-assigner **hard-skips** a driver once their current **span** (first pickup → last estimated
  end) `≥ max_hours` (`scheduler.py:1213-1219`). Note it is **wall-clock span**, not actual duty/drive
  hours, and it's checked *before* adding the next leg. **In live data it is NULL for all 28 drivers,
  so it never fires.**
- **Global span penalty** (not per-driver): `span_threshold_hours=13`, `span_penalty_per_hour=30`
  (live `SchedulerSettings`) — a **soft score penalty** for days over 13h, no hard cap
  (`scheduler.py:1380-1393`). This is why Phase 2 saw 18–24h auto-built spans.

## 3. How the feasibility engine uses these today (Q4)

| Component | Uses the window? |
|---|---|
| **`check_feasibility`** (`scheduler.py:614`, the chain engine) | **No.** Only checks inter-leg turnaround (drive + buffer between adjacent slots). Window/hours/max are **not** considered here at all. |
| **`suggest_assignments`** (auto-assign, the path `auto_assign_drivers` runs) | Last-pickup window `pickup ≤ end_hour:59` **(skipped entirely for `flexible` drivers)** `:1206-1210`; `max_hours` hard span-cap `:1213-1219`; soft span penalty `:1380-1393`. **No finish-by.** |
| **`build_smart_schedule`** (single-driver builder UI) | Last-pickup `pickup ≤ end_hour:59` `:1659/1797` **and** finish-by `est_end ≤ end_hour+1h` `:1801-1802`. |
| **`assign_drivers_to_clusters`** (cluster pre-assign) | Hour-level overlap only (`dh_start>cluster_end or dh_end<cluster_start`) `:887`. |
| **`check_driver_feasibility`** view (drag-drop warning) | Uses the rich `is_pickup_within_window` (exception-aware, incl. finish-past-cutoff). **Advisory warning, not enforced.** |

So the window is a **pre-filter in the assignment loop**, applied **inconsistently** (auto-assign vs
builder differ; flexible bypasses it), and **invisible to the core feasibility/chain engine**.

## 4. Live data — active in-house drivers (Q3)

**28 active in-house drivers.** Grouped by what their window actually encodes:

### (a) Real, constrained windows — the only 5 a guard would meaningfully bind
| Driver | id | Pattern |
|---|---|---|
| Junaid Baidr | 52 | Mon/Tue **4–18**, Wed/Thu **4–17**, **Fri 4–12**, Sat 6–19, Sun OFF — genuine early shift |
| Michael Olmo | 48 | Thu/Fri/Sat **0–17**, else OFF |
| Yovanny Suarez | 46 | Mon/Tue/Thu/Sun **6–17**, Wed **6–18**, Fri/Sat OFF |
| Angel Almanzar | 38 | Tue **0–14**, Thu **6–14**, Wed/Fri 6–23, else OFF |
| runer | 33 | **Mon 0–15**, rest 6–23, Wed OFF |

### (b) All-day 6–23 but **flexible** → auto-assign **ignores the window** (5)
Aftab (59), HassanA (65), Raymond (64), mesfin (63), shelley (62, Fri fixed).

### (c) All-day 6–23 **fixed** → window present but `end_hour=23` ≈ no real bound (~9)
David Encarancion (51), Seline (57), Steven Kleisath (54), george (56), ken (58), rizwan (55),
roberto (32), sereen (53) (+ runer's non-Mon days).

### (d) Marked **OFF every weekday** despite `is_active=True` — data hygiene (9)
Abdalla (9), Carlos Medina (20), Hasan (35), Julio Bonilla (31), Rayyan Vorajee (1), alex (49),
neuma (26), **placeholder (6)**, shipo (34). These never get scheduled; they inflate the "active" roster.

### Summary counts
| Metric | Value |
|---|---|
| Active in-house drivers | **28** |
| With **NO** weekly rows (pure defaults) | 0 |
| **`max_hours` set** (any day) | **0** |
| `night` shift type (any day) | **0** |
| **Non-default window** (not 6–23) | **5** |
| Flexible on all available days (window bypassed) | **5** |
| OFF all 7 weekdays (placeholder/inactive) | **9** |

> Every driver's **defaults** are the same factory values (`start=6, end=23, flexible=True,
> max_hours=NULL`), so wherever a weekly row is missing or default, the "window" is just the factory
> default — not a real operational constraint. `night_bonus` is $10 for almost everyone ($20 for
> `alex`) but that's a **pay** field, not an availability/shift flag.

## 5. What this means for a future guard (noted, not built)

- A window/clear-by guard would **only bind ~5 drivers** on today's data; for the ~14 all-day-6–23
  drivers it is a no-op, and for the 5 flexible drivers auto-assign ignores it anyway.
- The **start bound is the likelier real constraint** that's currently stale: pre-6 AM airport/cruise
  pickups assigned to `start_hour=6` drivers are probably the bulk of Phase 2.5's window violations —
  worth confirming, and worth fixing the data (real early starts) before trusting a guard.
- Decisions needed **before** building: (1) make `end_hour` a **clear-by** (finish) constraint, not
  just last-pickup, and apply it consistently in *both* engines and in `check_feasibility`;
  (2) populate **`max_hours`** for drivers who actually have daily caps; (3) clean up the 9
  OFF-all-week "active" drivers; (4) decide how `flexible` should interact with a hard clear-by.

*Read-only investigation only. No guards, no code, no data changes. Stop.*
